"""`revali init`: set a project up from revali's templates.

Writes, at the repository root, each setup file the project does not have yet: revali.toml,
CONVENTIONS.md, .github/release.yml and CLAUDE.md (the block of templates/CLAUDE-snippet.md,
appended to an existing CLAUDE.md that lacks it), and makes the state directory ignored.
Never overwrites a file, never commits, never calls GitHub.
"""

import os
from typing import Callable, List, Tuple

from revali import EXIT_ERROR, EXIT_OK, gitops
from revali.config import TOOL_ROOT, paths_for

TEMPLATES_DIR = os.path.join(TOOL_ROOT, "templates")

# (path in the project, template name), written in this order
COPIES = (
    ("revali.toml", "revali.toml"),
    ("CONVENTIONS.md", "CONVENTIONS.md"),
    (".github/release.yml", "release.yml"),
)
CLAUDE_FILE = "CLAUDE.md"
SNIPPET_TEMPLATE = "CLAUDE-snippet.md"
# a CLAUDE.md holding this line already has the block (in the user's own wording, maybe)
SNIPPET_HEADING = "## Changes go through revali"

NEXT_STEPS = """next:
  - edit revali.toml: the [validate.<platform>] commands (setup, test, new_test) are
    examples for a Python project; set [project] lint and format if the project has them
  - CLAUDE.md names revali's checkout as <revali dir>: replace it with the path, or name it
    once in your own global Claude instructions
  - review and commit these files; then each change starts with a change.md (docs/workflow.md)"""


class InitError(Exception):
    """One line for the user: what failed and on which path."""


def _read_template(name: str) -> str:
    path = os.path.join(TEMPLATES_DIR, name)
    try:
        with open(path, "r", encoding="utf-8", newline="") as fh:
            return fh.read().replace("\r\n", "\n")
    except OSError as exc:
        raise InitError("cannot read the template %s (%s)" % (path, exc)) from exc


def snippet_block(text: str) -> str:
    """The part of CLAUDE-snippet.md meant for CLAUDE.md: what follows its first HTML comment
    (the paste instruction)."""
    end = text.find("-->")
    if text.lstrip().startswith("<!--") and end >= 0:
        text = text[end + 3 :]
    return text.lstrip("\n")


def _create(path: str, text: str) -> None:
    """Write a new file; "x" mode, so a file that appeared meanwhile is never replaced."""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "x", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
    except OSError as exc:
        raise InitError("cannot write %s (%s)" % (path, exc)) from exc


def _claude_md(path: str, block: str) -> str:
    if not os.path.exists(path):
        _create(path, block)
        return "wrote"
    try:
        with open(path, "r", encoding="utf-8", newline="") as fh:
            text = fh.read()
    except (OSError, UnicodeDecodeError) as exc:
        raise InitError("cannot read %s (%s)" % (path, exc)) from exc
    if any(line.strip() == SNIPPET_HEADING for line in text.splitlines()):
        return "kept"
    prefix = ""
    if text:
        prefix = "\n" if text.endswith("\n") else "\n\n"
    try:
        with open(path, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(prefix + block)
    except OSError as exc:
        raise InitError("cannot write %s (%s)" % (path, exc)) from exc
    return "appended"


def _ignore(root: str) -> str:
    entry = paths_for(root).state_dir.rstrip("/") + "/"
    gitignore = os.path.join(root, ".gitignore")
    existed = os.path.isfile(gitignore)
    try:
        changed = gitops.ensure_gitignore(root, entry)
    except OSError as exc:
        raise InitError("cannot write %s (%s)" % (gitignore, exc)) from exc
    if not changed:
        return "kept"
    return "appended" if existed else "wrote"


def init_project(root: str, report: Callable[[str, str], None]) -> List[Tuple[str, str]]:
    """Set up the repository at `root`, calling `report(action, path relative to root)` as
    each file is done (so what was written before a failure is still reported); returns the
    same pairs. Every template is read before anything is written, so a missing one writes
    nothing. Raises InitError."""
    texts = {name: _read_template(name) for _, name in COPIES}
    block = snippet_block(_read_template(SNIPPET_TEMPLATE))
    done: List[Tuple[str, str]] = []

    def record(action: str, rel: str) -> None:
        done.append((action, rel))
        report(action, rel)

    for rel, name in COPIES:
        path = os.path.join(root, *rel.split("/"))
        if os.path.exists(path):
            record("kept", rel)
            continue
        _create(path, texts[name])
        record("wrote", rel)
    record(_claude_md(os.path.join(root, CLAUDE_FILE), block), CLAUDE_FILE)
    record(_ignore(root), ".gitignore")
    return done


def cmd_init(args) -> int:
    root = gitops.repo_root(os.getcwd())
    if not root:
        print("ERROR: not inside a git working tree; run `revali init` in the project's repository")
        return EXIT_ERROR
    print("repo: %s" % root)
    try:
        init_project(root, lambda action, rel: print("%s %s" % (action, rel)))
    except InitError as exc:
        print("ERROR: %s" % exc)
        return EXIT_ERROR
    print(NEXT_STEPS)
    return EXIT_OK
