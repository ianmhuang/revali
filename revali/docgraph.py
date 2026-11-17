"""The link graph of a repository's documents, and checks that names are documented.

From a few root files, every file reachable through relative Markdown links, at any
depth: Markdown files are read and followed, any other file (code, TOML, LICENSE) is a
leaf. Links inside fenced code blocks or code spans, URLs, `mailto:` and bare anchors are
not followed; a link to a file that does not exist is recorded as broken, and one that
leaves the repository root as outside, neither followed. A target starting with `/` is
relative to the repository root, as GitHub reads it. A target whose letter case differs
from the file's is broken on every platform: Windows and macOS find the file, GitHub does
not.

`related` answers which documents of a graph a change concerns: those linking to a changed
file, and those naming, in a code span, a name the change adds or removes.
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
FENCE = re.compile(r"^\s*(`{3,}|~{3,})(.*)$")
# a name has a letter and a mark a plain word lacks: `_`, `-`, `.`, `/`, or a camelCase hump
NAME_MARK = re.compile(r"[_\-./]|[a-z][A-Z]")
NAME_EDGE = "\"'()[]{}<>,;:"
NAME_MIN = 3


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
    # (page, line, path relative to the root) of every link inside the root, broken or not
    links: List[Tuple[str, int, str]] = field(default_factory=list)
    texts: Dict[str, str] = field(default_factory=dict)  # Markdown page -> its text


def _rel(root: str, path: str) -> str:
    return os.path.relpath(path, root).replace(os.sep, "/")


def _inside(root: str, path: str) -> bool:
    try:
        return os.path.commonpath([root, path]) == root
    except ValueError:  # Windows: another drive, or a UNC path (`\\server\x.md`)
        return False


def _lines(text: str) -> List[str]:
    """Split on "\\n" only, so a line number is the editor's (splitlines also splits on a
    form feed or U+2028)."""
    return [line.rstrip("\r") for line in text.split("\n")]


def _unfenced_lines(text: str) -> Iterable[Tuple[int, str]]:
    """(line number, line) outside fenced code blocks. As in CommonMark, a fence is three or
    more backticks or tildes, and only a fence of the same character, at least as long and
    with nothing after it, closes the block; a backtick fence's info string has no backtick."""
    fence = ""
    for no, line in enumerate(_lines(text), 1):
        m = FENCE.match(line)
        if not fence:
            if m and not (m.group(1)[0] == "`" and "`" in m.group(2)):
                fence = m.group(1)
                continue
            yield no, line
        elif (
            m
            and m.group(1)[0] == fence[0]
            and len(m.group(1)) >= len(fence)
            and not m.group(2).strip()
        ):
            fence = ""


def prose_lines(text: str) -> Iterable[Tuple[int, str]]:
    """(line number, line) outside fenced code blocks, code spans blanked."""
    for no, line in _unfenced_lines(text):
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


class _ExactNames:
    """Whether a path exists with exactly the letter case given, on any file system."""

    def __init__(self, root: str):
        self.root = root
        self.listed: Dict[str, set] = {}

    def __call__(self, rel: str) -> bool:
        if not os.path.exists(os.path.join(self.root, rel)):
            return False
        if rel == ".":  # the root itself
            return True
        parent = self.root
        for part in rel.split("/"):
            if parent not in self.listed:
                try:
                    self.listed[parent] = set(os.listdir(parent))
                except NotADirectoryError:
                    return False
            if part not in self.listed[parent]:
                return False
            parent = os.path.join(parent, part)
        return True


def build(root: str, roots: Sequence[str]) -> Graph:
    """The graph from `roots` (paths relative to `root`); a root that does not exist, with
    that letter case, is skipped."""
    root = os.path.abspath(root)
    graph = Graph()
    exists = _ExactNames(root)
    queue = deque()
    queued = set()
    incoming: Dict[str, List[Tuple[str, int]]] = {}
    for r in roots:
        path = os.path.join(root, r)
        rel = _rel(root, path)
        if _inside(root, os.path.normpath(path)) and exists(rel) and rel not in queued:
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
        graph.texts[rel] = text
        for no, target in page_links(text):
            if target.startswith("/"):
                full = os.path.normpath(os.path.join(root, target.lstrip("/")))
            else:
                full = os.path.normpath(os.path.join(os.path.dirname(path), target))
            if not _inside(root, full):
                graph.outside.append((rel, no, target))
                continue
            dest = _rel(root, full)
            graph.links.append((rel, no, dest))
            if not exists(dest):
                graph.broken.append((rel, no, target))
                continue
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


def name_like(word: str) -> bool:
    """A word that reads as a name rather than an English word: at least NAME_MIN
    characters, a letter, and a `_`, `-`, `.`, `/` or camelCase hump (`max_procs`,
    `--branch`, `revali/config.py`, `buildPrompt`; not `run`, `true`, `0.3.0`)."""
    return (
        len(word) >= NAME_MIN
        and any(c.isalpha() for c in word)
        and bool(NAME_MARK.search(word))
        and len(word.strip("_-./")) >= 2
    )


def span_names(text: str) -> List[Tuple[int, str]]:
    """(line, name) of every name-shaped word in a code span outside fenced blocks, in
    order; a span is split on whitespace and `=`, and quotes and brackets are stripped
    (`[review] doc_roots` gives `doc_roots`, `max_procs = 4096` gives `max_procs`)."""
    found = []
    for no, line in _unfenced_lines(text):
        for span in code_spans(line):
            for word in re.split(r"[\s=]+", span):
                word = word.strip(NAME_EDGE)
                if name_like(word):
                    found.append((no, word))
    return found


def _whole_word(name: str, text: str) -> bool:
    if name not in text:  # the cheap test first: most names are on no changed line
        return False
    return bool(re.search(r"(?<![A-Za-z0-9_])%s(?![A-Za-z0-9_])" % re.escape(name), text))


@dataclass
class Related:
    page: str
    changed: bool  # the diff changes this document itself
    links: List[Tuple[str, int]] = field(default_factory=list)  # (changed file, first line)
    names: List[Tuple[str, int]] = field(default_factory=list)  # (name, first line)


def related(
    graph: Graph, paths: Sequence[str], changed_lines: Dict[str, List[str]]
) -> List[Related]:
    """The Markdown documents of `graph` that concern a change, in graph order: those linking
    to one of `paths` (the files the change adds, modifies, deletes or renames, "/"
    separated), and those whose code spans name a name that is one of `paths` or a whole
    word on a changed line (`changed_lines`: file -> its added and removed lines) of a file
    other than the document itself. A path's last part alone is not matched: a common file
    name would list every document that names any file of that name."""
    changed = set(paths)
    all_text = "\n".join(line for lines in changed_lines.values() for line in lines)

    found = []
    for page, text in graph.texts.items():
        entry = Related(page, page in changed)
        for src, no, dest in graph.links:
            if src == page and dest in changed and dest not in {d for d, _ in entry.links}:
                entry.links.append((dest, no))
        others = all_text
        if page in changed_lines:
            others = "\n".join(
                line for f, lines in changed_lines.items() if f != page for line in lines
            )
        seen = set()
        for no, name in span_names(text):
            if name in seen:
                continue
            seen.add(name)
            if name in changed or _whole_word(name, others):
                entry.names.append((name, no))
        if entry.links or entry.names:
            found.append(entry)
    return found


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
