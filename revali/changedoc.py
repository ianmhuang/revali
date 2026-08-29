"""Parse and validate .revali/<branch>/change.md (the author's description, "file 1").

Format:
    ---
    title: Short title
    kind: feature
    author_model: claude-fable-5
    status: draft          (optional; a draft is refused)
    ---
    ## Request
    <the user's instruction, verbatim>
    ## What / ## Why / ## Goal / ## Acceptance criteria / ## Out of scope / ## Dependencies
    Acceptance criteria are lines "- AC-1: ..." (numbering must be unique).
"""
import re
from dataclasses import dataclass, field
from typing import Dict, List, Tuple

from revali import ALL_KINDS, V1_KINDS

FILENAME = "change.md"
SECTION_KEYS = {
    "request": "request",
    "what": "what",
    "why": "why",
    "goal": "goal",
    "acceptance criteria": "acceptance",
    "out of scope": "out_of_scope",
    "dependencies": "dependencies",
}
AC_RE = re.compile(r"^\s*[-*]\s*(AC-\d+)\s*[:：]\s*(.+?)\s*$")
FRONT_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.*?)\s*$")


@dataclass
class ChangeDoc:
    title: str = ""
    kind: str = ""
    author_model: str = ""
    status: str = ""
    sections: Dict[str, str] = field(default_factory=dict)
    acs: List[Tuple[str, str]] = field(default_factory=list)
    raw: str = ""

    def section(self, key: str) -> str:
        return self.sections.get(key, "").strip()

    @property
    def ac_ids(self) -> List[str]:
        return [ac_id for ac_id, _ in self.acs]


def parse(text: str) -> ChangeDoc:
    doc = ChangeDoc(raw=text)
    lines = text.splitlines()
    idx = 0
    if lines and lines[0].strip() == "---":
        idx = 1
        while idx < len(lines) and lines[idx].strip() != "---":
            m = FRONT_RE.match(lines[idx])
            if m:
                key, value = m.group(1).lower(), m.group(2)
                if key == "title":
                    doc.title = value
                elif key == "kind":
                    doc.kind = value.lower()
                elif key == "author_model":
                    doc.author_model = value
                elif key == "status":
                    doc.status = value.lower()
            idx += 1
        idx += 1  # closing ---
    current = None
    buf: Dict[str, List[str]] = {}
    for line in lines[idx:]:
        m = re.match(r"^##\s+(.+?)\s*$", line)
        if m:
            name = m.group(1).strip().lower()
            current = SECTION_KEYS.get(name, name.replace(" ", "_"))
            buf.setdefault(current, [])
            continue
        if current is None:
            if not doc.title and line.startswith("# "):
                doc.title = line[2:].strip()
            continue
        buf[current].append(line)
    doc.sections = {k: "\n".join(v).strip() for k, v in buf.items()}
    for line in buf.get("acceptance", []):
        m = AC_RE.match(line)
        if m:
            doc.acs.append((m.group(1), m.group(2)))
    return doc


def validate(doc: ChangeDoc, allowed_kinds=V1_KINDS) -> List[str]:
    problems = []
    if not doc.title:
        problems.append("change.md: missing title (front matter 'title:' or a '# ' heading)")
    if not doc.kind:
        problems.append("change.md: missing 'kind:' (one of %s)" % ", ".join(ALL_KINDS))
    elif doc.kind not in ALL_KINDS:
        problems.append("change.md: unknown kind '%s' (one of %s)" % (doc.kind, ", ".join(ALL_KINDS)))
    elif doc.kind not in allowed_kinds:
        problems.append("change.md: kind '%s' is not available in this version (v1.0: %s)"
                        % (doc.kind, ", ".join(allowed_kinds)))
    if doc.status == "draft":
        problems.append("change.md: status is 'draft'; review it and remove the status line")
    if not doc.section("request"):
        problems.append("change.md: '## Request' must contain the user's instruction verbatim")
    if not doc.section("goal"):
        problems.append("change.md: '## Goal' is empty")
    if not doc.acs:
        problems.append("change.md: '## Acceptance criteria' needs at least one '- AC-n: ...' line")
    seen = set()
    for ac_id, text in doc.acs:
        if ac_id in seen:
            problems.append("change.md: duplicate %s" % ac_id)
        seen.add(ac_id)
        if len(text.strip()) < 8:
            problems.append("change.md: %s is too short to be testable" % ac_id)
    return problems


def load(path: str) -> ChangeDoc:
    with open(path, "r", encoding="utf-8", newline="") as fh:
        return parse(fh.read())
