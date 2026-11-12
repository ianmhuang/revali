"""The link graph of a repository's documents, and checks that names are documented.

From a few root files, every file reachable through relative Markdown links, at any
depth: Markdown files are read and followed, any other file (code, TOML, LICENSE) is a
leaf. Links inside fenced code blocks or code spans, URLs, `mailto:` and bare anchors are
not followed; a link to a file that does not exist is recorded as broken, and one that
leaves the repository root as outside, neither followed. A target starting with `/` is
relative to the repository root, as GitHub reads it.
"""

import os
import re
from collections import deque
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import unquote

INLINE_LINK = re.compile(r"\[[^\]]*\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)")
# a footnote definition (`[^1]: text`) is not a link
REFERENCE_DEF = re.compile(r"^\s{0,3}\[(?!\^)[^\]]+\]:\s*<?(\S+?)>?(?:\s+.*)?$")
CODE_SPAN = re.compile(r"`[^`\n]*`")
SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
TOML_HEADER = re.compile(r"^\s*#?\s*\[\s*([A-Za-z0-9_.-]+)\s*\]\s*(?:#.*)?$")
TOML_KEY = re.compile(r"^\s*#?\s*([A-Za-z0-9_-]+)\s*=")


@dataclass
class Node:
    path: str  # relative to the repository root, "/" separated
    depth: int  # links from the nearest root
    linked_from: List[Tuple[str, int]] = field(default_factory=list)  # (page, line)


@dataclass
class Graph:
    nodes: Dict[str, Node] = field(default_factory=dict)  # in the order they were reached
    broken: List[Tuple[str, int, str]] = field(default_factory=list)  # (page, line, target)
    outside: List[Tuple[str, int, str]] = field(default_factory=list)  # leaves the root


def _rel(root: str, path: str) -> str:
    return os.path.relpath(path, root).replace(os.sep, "/")


def _lines(text: str) -> List[str]:
    """Split on "\\n" only, so a line number is the editor's (splitlines also splits on a
    form feed or U+2028)."""
    return [line.rstrip("\r") for line in text.split("\n")]


def prose_lines(text: str) -> Iterable[Tuple[int, str]]:
    """(line number, line) outside fenced code blocks, code spans blanked."""
    fence = ""
    for no, line in enumerate(_lines(text), 1):
        mark = line.strip()[:3]
        if mark in ("```", "~~~"):
            if not fence:
                fence = mark
                continue
            if mark == fence:
                fence = ""
                continue
        if not fence:
            yield no, CODE_SPAN.sub(lambda m: " " * len(m.group(0)), line)


def page_links(text: str) -> List[Tuple[int, str]]:
    """(line, target without its anchor) of every relative link a page holds."""
    found = []
    for no, line in prose_lines(text):
        targets = INLINE_LINK.findall(line)
        ref = REFERENCE_DEF.match(line)
        if ref:
            targets.append(ref.group(1))
        for target in targets:
            if target.startswith("#") or SCHEME.match(target) or target.startswith("//"):
                continue
            target = unquote(target.split("#", 1)[0])
            if target:
                found.append((no, target))
    return found


def build(root: str, roots: Sequence[str]) -> Graph:
    """The graph from `roots` (paths relative to `root`); a root that does not exist is
    skipped."""
    root = os.path.abspath(root)
    graph = Graph()
    queue = deque()
    queued = set()
    incoming: Dict[str, List[Tuple[str, int]]] = {}
    for r in roots:
        path = os.path.join(root, r)
        rel = _rel(root, path)
        if os.path.exists(path) and rel not in queued:
            queued.add(rel)
            queue.append((rel, 0))
    while queue:  # breadth first, so a node's depth is its distance from the nearest root
        rel, depth = queue.popleft()
        graph.nodes[rel] = Node(rel, depth)
        path = os.path.join(root, rel)
        if not (rel.lower().endswith(".md") and os.path.isfile(path)):
            continue  # a leaf
        with open(path, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
        for no, target in page_links(text):
            if target.startswith("/"):
                full = os.path.normpath(os.path.join(root, target.lstrip("/")))
            else:
                full = os.path.normpath(os.path.join(os.path.dirname(path), target))
            if os.path.commonpath([root, full]) != root:
                graph.outside.append((rel, no, target))
                continue
            if not os.path.exists(full):
                graph.broken.append((rel, no, target))
                continue
            dest = _rel(root, full)
            incoming.setdefault(dest, []).append((rel, no))
            if dest not in queued:
                queued.add(dest)
                queue.append((dest, depth + 1))
    for dest, sources in incoming.items():
        graph.nodes[dest].linked_from = sources
    return graph


def broken_links(graph: Graph) -> List[str]:
    """Links to a missing file, and links that leave the repository (a reader of the
    repository on its own, or on GitHub, cannot follow them)."""
    return ["%s:%d missing: %s" % b for b in graph.broken] + [
        "%s:%d outside the repository: %s" % o for o in graph.outside
    ]


def orphans(
    root: str,
    graph: Graph,
    exclude: Sequence[str] = (),
    files: Optional[Sequence[str]] = None,
) -> List[str]:
    """Markdown files that are not in the graph. `files` (paths relative to `root`, "/"
    separated; say, what git tracks) are the candidates; without it every file under `root`
    is, untracked and ignored ones included. Directories whose name starts with a dot, and
    the `exclude` paths, are never candidates."""
    root = os.path.abspath(root)
    skip = {e.strip("/") for e in exclude}
    if files is not None:

        def excluded(rel):
            parts = rel.split("/")[:-1]
            prefixes = ["/".join(parts[: i + 1]) for i in range(len(parts))]
            return any(p.startswith(".") for p in parts) or any(p in skip for p in prefixes)

        return sorted(
            f
            for f in files
            if f.lower().endswith(".md") and not excluded(f) and f not in graph.nodes
        )
    found = []
    for directory, dirs, files_here in os.walk(root):
        rel_dir = _rel(root, directory)
        dirs[:] = sorted(
            d
            for d in dirs
            if not d.startswith(".") and (d if rel_dir == "." else rel_dir + "/" + d) not in skip
        )
        for name in files_here:
            if name.lower().endswith(".md"):
                rel = name if rel_dir == "." else rel_dir + "/" + name
                if rel not in graph.nodes:
                    found.append(rel)
    return sorted(found)


def code_spans(text: str) -> List[str]:
    return [m.group(0)[1:-1] for m in CODE_SPAN.finditer(text)]


def missing_names(names: Sequence[str], texts: Sequence[str], exact: bool = False) -> List[str]:
    """The names that no text holds in a code span: as the whole span with `exact`, else as
    a word inside one (`name`, `[section] name`, `name = 1`)."""
    spans = [s for t in texts for s in code_spans(t)]
    if exact:
        held = {s.strip() for s in spans}
        return [name for name in names if name not in held]
    missing = []
    for name in names:
        word = re.compile(r"(?<![A-Za-z0-9_])%s(?![A-Za-z0-9_])" % re.escape(name))
        if not any(word.search(s) for s in spans):
            missing.append(name)
    return missing


def toml_keys(text: str) -> List[Tuple[str, str]]:
    """(section, key) of every `key =` line in a TOML file, commented-out ones included (a
    template documents optional keys as comments); "" is the top level."""
    section, keys = "", []
    for line in _lines(text):
        header = TOML_HEADER.match(line)
        if header:
            section = header.group(1)
            continue
        key = TOML_KEY.match(line)
        if key:
            keys.append((section, key.group(1)))
    return keys
