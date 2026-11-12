"""The link graph of a repository's documents, and checks that names are documented.

From a few root files, every file reachable through relative Markdown links, at any
depth: Markdown files are read and followed, any other file (code, TOML, LICENSE) is a
leaf. Links inside fenced code blocks or code spans, URLs, `mailto:` and bare anchors are
not followed; a link to a file that does not exist is recorded as broken.
"""

import os
import re
from collections import deque
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Sequence, Tuple
from urllib.parse import unquote

INLINE_LINK = re.compile(r"\[[^\]]*\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)")
REFERENCE_DEF = re.compile(r"^\s{0,3}\[[^\]]+\]:\s*<?(\S+?)>?(?:\s+.*)?$")
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


def _rel(root: str, path: str) -> str:
    return os.path.relpath(path, root).replace(os.sep, "/")


def prose_lines(text: str) -> Iterable[Tuple[int, str]]:
    """(line number, line) outside fenced code blocks, code spans blanked."""
    fence = ""
    for no, line in enumerate(text.splitlines(), 1):
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
            full = os.path.normpath(os.path.join(os.path.dirname(path), target))
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
    return ["%s:%d missing: %s" % b for b in graph.broken]


def orphans(root: str, graph: Graph, exclude: Sequence[str] = ()) -> List[str]:
    """Markdown files under `root` that are not in the graph. Directories whose name starts
    with a dot, and the `exclude` paths (relative, "/" separated), are not searched."""
    root = os.path.abspath(root)
    skip = {e.strip("/") for e in exclude}
    found = []
    for directory, dirs, files in os.walk(root):
        rel_dir = _rel(root, directory)
        dirs[:] = sorted(
            d
            for d in dirs
            if not d.startswith(".") and (d if rel_dir == "." else rel_dir + "/" + d) not in skip
        )
        for name in files:
            if name.lower().endswith(".md"):
                rel = name if rel_dir == "." else rel_dir + "/" + name
                if rel not in graph.nodes:
                    found.append(rel)
    return sorted(found)


def code_spans(text: str) -> List[str]:
    return [m.group(0)[1:-1] for m in CODE_SPAN.finditer(text)]


def missing_names(names: Sequence[str], texts: Sequence[str]) -> List[str]:
    """The names that no text holds as a word inside a code span (`name`, `[section] name`,
    `name = 1`)."""
    spans = [s for t in texts for s in code_spans(t)]
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
    for line in text.splitlines():
        header = TOML_HEADER.match(line)
        if header:
            section = header.group(1)
            continue
        key = TOML_KEY.match(line)
        if key:
            keys.append((section, key.group(1)))
    return keys
