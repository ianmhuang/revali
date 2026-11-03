"""Parse and validate <state_dir>/<branch>/change.md (the Developer's description, "file 1").

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
    Acceptance criteria are lines "- AC-1: ..." (numbering must be unique). A note in
    parentheses may stand before the colon ("- AC-2 (PR #30 F1): ...") and goes into the
    text, and may wrap onto the following lines until ")" and the colon. The lines after
    an AC line join its text until a blank line or a list item indented no deeper than the
    AC line; an AC line at any indent starts a new AC. Any other line in that section
    that starts like an AC ("1. AC-3", "- **AC-4**", "- AC-5 text") is a problem, never
    silently skipped: a skipped AC would also escape the reviewer's coverage check. The
    exception is a bare "AC-n ..." with no list marker and no colon after the id (or its
    note): inside an open AC it is a wrapped sentence that joins the text, outside one it
    is a prose paragraph ("AC-9 was added during implementation.") and is ignored. A bare
    "AC-n: ..." is a forgotten bullet and a problem wherever it stands. The cost: a real AC
    written without a bullet and without a colon ("AC-3 mul returns ...") reads as prose.
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
# A note in parentheses, with at most one level of nested parentheses: "(probe, as for (m))".
_NOTE = r"\(((?:[^()]|\([^()]*\))*)\)"
AC_RE = re.compile(r"^\s*[-*]\s*(AC-\d+)\s*(?:" + _NOTE + r")?\s*[:：]\s*(.*?)\s*$")
# Starts like an AC: optional list marker, optional emphasis, then the id.
AC_LIKE_RE = re.compile(r"^\s*(?:(?:[-*+]|\d+[.)])\s*)?[*_`]*AC-\d+")
LIST_ITEM_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])(?:\s|$)")
# Starts like an AC with no list marker at all; prose unless BARE_DEF_RE matches.
BARE_RE = re.compile(r"^\s*[*_`]*AC-\d+")
# A bare AC definition (id, optional note, colon) without its bullet: never a wrapped sentence.
BARE_DEF_RE = re.compile(r"^\s*[*_`]*AC-\d+[*_`]*\s*(?:" + _NOTE + r")?\s*[:：]")
# An AC line whose note opens; it is still open when NOTE_CLOSED_RE does not match.
NOTE_START_RE = re.compile(r"^\s*[-*]\s*AC-\d+\s*\(")
NOTE_CLOSED_RE = re.compile(r"^\s*[-*]\s*AC-\d+\s*" + _NOTE)
AC_FORM = '"- AC-n: text"'
HEADING_RE = re.compile(r"^##\s+(.+?)\s*$")  # a section heading, on a line without its newline


def section_key(line: str) -> str:
    """The section key a heading line opens ("request", "goal", ...), "" for any other line.
    The one rule for what counts as a heading, shared with pr.strip_request."""
    m = HEADING_RE.match(line.rstrip("\r\n"))
    if not m:
        return ""
    name = m.group(1).strip().lower()
    return SECTION_KEYS.get(name, name.replace(" ", "_"))


FRONT_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.*?)\s*$")


@dataclass
class ChangeDoc:
    title: str = ""
    kind: str = ""
    author_model: str = ""
    status: str = ""
    sections: Dict[str, str] = field(default_factory=dict)
    acs: List[Tuple[str, str]] = field(default_factory=list)
    # per AC, the text without its note (what the length check measures)
    ac_bodies: List[str] = field(default_factory=list)
    # (1-based line number, line) of each line that starts like an AC but is not one
    malformed_acs: List[Tuple[int, str]] = field(default_factory=list)
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
    acceptance: List[Tuple[int, str]] = []
    for number, line in enumerate(lines[idx:], start=idx + 1):
        key = section_key(line)
        if key:
            current = key
            buf.setdefault(current, [])
            continue
        if current is None:
            if not doc.title and line.startswith("# "):
                doc.title = line[2:].strip()
            continue
        buf[current].append(line)
        if current == "acceptance":
            acceptance.append((number, line))
    doc.sections = {k: "\n".join(v).strip() for k, v in buf.items()}
    _parse_acs(doc, acceptance)
    return doc


def _indent(line: str) -> int:
    expanded = line.expandtabs(4)
    return len(expanded) - len(expanded.lstrip())


# What a line does in the acceptance section; see _classify.
START, NOTE, REPORT, END, JOIN = "start", "note", "report", "end", "join"


def _classify(line: str, indent) -> str:
    """What `line` does, `indent` being the indent of the AC line whose text or note is still
    open (None when neither is). The one rule for an open text and an open note, so both end
    on the same lines and report the same ones.

    START: a well-formed AC line. NOTE: an AC line whose note does not close on it.
    REPORT: starts like an AC but is not one; with nothing open only bare prose
    ("AC-9 was added ...") is spared, inside an open text or note only a list item or a
    bare definition is reported and a wrapped "AC-8 and AC-9 calls" joins.
    END: a blank line or a list item no deeper than `indent`, and with nothing open every
    other line (it is skipped). JOIN: the rest, which continues the open text or note."""
    if AC_RE.match(line):
        return START
    if NOTE_START_RE.match(line) and not NOTE_CLOSED_RE.match(line):
        return NOTE
    if AC_LIKE_RE.match(line) and (
        LIST_ITEM_RE.match(line)
        or BARE_DEF_RE.match(line)
        or (indent is None and not BARE_RE.match(line))
    ):
        return REPORT
    if indent is None or not line.strip():
        return END
    if LIST_ITEM_RE.match(line) and _indent(line) <= indent:
        return END
    return JOIN


def _parse_acs(doc: ChangeDoc, lines: List[Tuple[int, str]]) -> None:
    """Fill doc.acs, doc.ac_bodies and doc.malformed_acs from the acceptance section."""
    entries: List[Tuple[str, str, List[str]]] = []  # (id, note, text parts)
    open_indent = None  # indent of the AC line whose text is still open, None when closed
    # an AC line whose note has not closed yet: [line number, first line, joined head, indent]
    pending = None

    def start(m, indent: int) -> None:
        nonlocal open_indent
        entries.append((m.group(1), " ".join((m.group(2) or "").split()), [m.group(3)]))
        open_indent = indent

    for number, line in lines:
        if pending is not None:
            if _classify(line, pending[3]) == JOIN:
                pending[2] += " " + line.strip()
                m = AC_RE.match(pending[2])
                if m:
                    start(m, pending[3])
                    pending = None
                elif NOTE_CLOSED_RE.match(pending[2]):
                    doc.malformed_acs.append((pending[0], pending[1].strip()))
                    pending = None
                continue
            # the note never closed: report its AC line, then take this line afresh
            doc.malformed_acs.append((pending[0], pending[1].strip()))
            pending = None
        kind = _classify(line, open_indent)
        if kind == START:
            start(AC_RE.match(line), _indent(line))
        elif kind == NOTE:
            pending = [number, line, line.rstrip(), _indent(line)]
            open_indent = None
        elif kind == REPORT:
            doc.malformed_acs.append((number, line.strip()))
            open_indent = None
        elif kind == END:
            open_indent = None
        else:
            entries[-1][2].append(line.strip())
    if pending is not None:
        doc.malformed_acs.append((pending[0], pending[1].strip()))
    doc.malformed_acs.sort()
    for ac_id, note, parts in entries:
        body = " ".join(p for p in parts if p)
        doc.ac_bodies.append(body)
        doc.acs.append((ac_id, ("(%s) %s" % (note, body)).strip() if note else body))


def validate(doc: ChangeDoc, allowed_kinds=V1_KINDS) -> List[str]:
    problems = []
    if not doc.title:
        problems.append("change.md: missing title (front matter 'title:' or a '# ' heading)")
    if not doc.kind:
        problems.append("change.md: missing 'kind:' (one of %s)" % ", ".join(ALL_KINDS))
    elif doc.kind not in ALL_KINDS:
        problems.append(
            "change.md: unknown kind '%s' (one of %s)" % (doc.kind, ", ".join(ALL_KINDS))
        )
    elif doc.kind not in allowed_kinds:
        problems.append(
            "change.md: kind '%s' is not available in this version (v1.0: %s)"
            % (doc.kind, ", ".join(allowed_kinds))
        )
    if doc.status == "draft":
        problems.append("change.md: status is 'draft'; review it and remove the status line")
    if not doc.section("request"):
        problems.append("change.md: '## Request' must contain the user's instruction verbatim")
    if not doc.section("goal"):
        problems.append("change.md: '## Goal' is empty")
    if not doc.acs:
        problems.append("change.md: '## Acceptance criteria' needs at least one '- AC-n: ...' line")
    for number, line in doc.malformed_acs:
        problems.append(
            "change.md: line %d looks like an AC but is not %s: %s" % (number, AC_FORM, line)
        )
    seen = set()
    for (ac_id, _), body in zip(doc.acs, doc.ac_bodies, strict=True):
        if ac_id in seen:
            problems.append("change.md: duplicate %s" % ac_id)
        seen.add(ac_id)
        if len(body.strip()) < 8:
            problems.append("change.md: %s is too short to be testable" % ac_id)
    return problems


def load(path: str) -> ChangeDoc:
    with open(path, "r", encoding="utf-8", newline="") as fh:
        return parse(fh.read())
