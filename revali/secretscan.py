"""Credential scan over added diff lines. A hit blocks the push (RULES: revoke first)."""
import re
from dataclasses import dataclass
from typing import List

ALLOW_MARKER = "revali:allow-secret"

PATTERNS = [
    ("aws-access-key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("github-token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("github-pat", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{22,}\b")),
    ("anthropic-key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}\b")),
    ("openai-key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9]{32,}\b")),
    ("google-api-key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("slack-token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("private-key", re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY")),
    ("generic-assignment", re.compile(
        r"(?i)\b(?:api[_-]?key|secret[_-]?key|access[_-]?token|auth[_-]?token|password|passwd)\b"
        r"\s*[:=]\s*['\"][^'\"\s]{8,}['\"]")),
]

PLACEHOLDER_RE = re.compile(r"(?i)(example|placeholder|dummy|changeme|xxxx|<[^>]+>|\$\{[^}]+\}|os\.environ|getenv)")


@dataclass
class Hit:
    file: str
    line: int
    pattern: str
    excerpt: str


def _mask(text: str) -> str:
    text = text.strip()
    if len(text) <= 12:
        return "***"
    return text[:6] + "***" + text[-3:]


def scan_diff(diff_text: str) -> List[Hit]:
    """Scan unified diff text; only '+' lines count, headers are skipped."""
    hits = []
    current_file = "?"
    new_line = 0
    for raw in diff_text.splitlines():
        if raw.startswith("+++ "):
            current_file = raw[4:].strip()
            if current_file.startswith("b/"):
                current_file = current_file[2:]
            continue
        if raw.startswith("--- ") or raw.startswith("diff --git") or raw.startswith("index "):
            continue
        m = re.match(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@", raw)
        if m:
            new_line = int(m.group(1)) - 1
            continue
        if raw.startswith("-"):
            continue
        new_line += 1
        if not raw.startswith("+"):
            continue
        line = raw[1:]
        if ALLOW_MARKER in line:
            continue
        for name, pattern in PATTERNS:
            m = pattern.search(line)
            if not m:
                continue
            if name == "generic-assignment" and PLACEHOLDER_RE.search(line):
                continue
            hits.append(Hit(file=current_file, line=new_line, pattern=name, excerpt=_mask(m.group(0))))
            break
    return hits


def scan_text(text: str, label: str = "text") -> List[Hit]:
    """Scan arbitrary text (e.g. a review body before it is posted)."""
    hits = []
    for idx, line in enumerate(text.splitlines(), 1):
        if ALLOW_MARKER in line:
            continue
        for name, pattern in PATTERNS:
            m = pattern.search(line)
            if m and not (name == "generic-assignment" and PLACEHOLDER_RE.search(line)):
                hits.append(Hit(file=label, line=idx, pattern=name, excerpt=_mask(m.group(0))))
                break
    return hits


def format_hits(hits: List[Hit]) -> str:
    lines = ["possible credentials in the diff (push blocked; revoke and rotate before anything else):"]
    for h in hits:
        lines.append("  %s:%d  %s  %s" % (h.file, h.line, h.pattern, h.excerpt))
    lines.append("  false positive? add '%s' to that line" % ALLOW_MARKER)
    return "\n".join(lines)
