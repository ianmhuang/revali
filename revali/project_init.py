"""`revali init`: set a project up from revali's templates.

Writes, at the repository root, each setup file the project does not have yet: revali.toml,
CONVENTIONS.md, .github/release.yml and CLAUDE.md (the block of templates/CLAUDE-snippet.md,
appended to an existing CLAUDE.md that lacks it), and makes the state directory ignored.
Never overwrites a file, never commits, never calls GitHub.
"""

import os
from typing import Callable, List, Tuple

from revali import EXIT_ERROR, EXIT_OK, gitops
from revali.config import PROJECT_FILE, TOOL_ROOT, paths_for, user_home

TEMPLATES_DIR = os.path.join(TOOL_ROOT, "templates")

# (path in the project, template name), written in this order
COPIES = (
    ("revali.toml", "revali.toml"),
    ("CONVENTIONS.md", "CONVENTIONS.md"),
    (".github/release.yml", "release.yml"),
)
CLAUDE_FILE = "CLAUDE.md"
SNIPPET_TEMPLATE = "CLAUDE-snippet.md"

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
    except (OSError, UnicodeDecodeError) as exc:
        raise InitError("cannot read the template %s (%s)" % (path, exc)) from exc


def snippet_block(text: str) -> str:
    """The part of CLAUDE-snippet.md meant for CLAUDE.md: what follows its first HTML comment
    (the paste instruction)."""
    end = text.find("-->")
    if text.lstrip().startswith("<!--") and end >= 0:
        text = text[end + 3 :]
    return text.lstrip("\n")


def snippet_heading(block: str) -> str:
    """The block's `## ` heading (its first non-empty line when it has none): a CLAUDE.md
    holding that line already has the block, in the user's own wording maybe. Taken from the
    template, so rewording the template's heading cannot leave init looking for the old one."""
    lines = [ln.strip() for ln in block.splitlines() if ln.strip()]
    for line in lines:
        if line.startswith("## "):
            return line
    return lines[0] if lines else ""


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
    heading = snippet_heading(block)
    # an editor may have saved the file with a BOM, which strip() keeps on the first line
    lines = text.lstrip("\ufeff").splitlines()
    if heading and any(line.strip() == heading for line in lines):
        return "kept"
    # the file's own line ending (CRLF in a Windows checkout with core.autocrlf), so the
    # appended block does not leave it with mixed endings
    prefix = ""
    if text:
        prefix = "\n" if text.endswith("\n") else "\n\n"
    data = prefix + block  # the block is LF (templates are read that way)
    if "\r\n" in text:
        data = data.replace("\n", "\r\n")
    try:
        with open(path, "a", encoding="utf-8", newline="") as fh:
            fh.write(data)
    except OSError as exc:
        raise InitError("cannot write %s (%s)" % (path, exc)) from exc
    return "appended"


def _ignore(root: str) -> Tuple[str, str]:
    """(action, what it is about): `.gitignore` when that file was written, appended or
    already holds the rule, else the state directory, which git ignores by another rule
    (`.git/info/exclude`, a global excludes file)."""
    # [paths] state_dir comes from the user's config.toml and the project's revali.toml; a
    # decode error carries no file name, so each file is read here first to name the one
    config_files = (os.path.join(user_home(), "config.toml"), os.path.join(root, PROJECT_FILE))
    for path in config_files:
        if not os.path.isfile(path):
            continue
        try:
            with open(path, "r", encoding="utf-8", newline="") as fh:
                fh.read()
        except (OSError, UnicodeDecodeError) as exc:
            raise InitError("cannot read %s (%s)" % (path, exc)) from exc
    try:
        entry = paths_for(root).state_dir.rstrip("/") + "/"
    except (OSError, UnicodeDecodeError) as exc:  # a file changed after the check above
        raise InitError("cannot read %s or %s (%s)" % (config_files + (exc,))) from exc
    gitignore = os.path.join(root, ".gitignore")
    existed = os.path.isfile(gitignore)
    try:
        changed = gitops.ensure_gitignore(root, entry)
    except (OSError, UnicodeDecodeError) as exc:
        raise InitError("cannot update %s (%s)" % (gitignore, exc)) from exc
    if changed:
        return ("appended" if existed else "wrote"), ".gitignore"
    if existed:
        return "kept", ".gitignore"
    return "kept", "%s (already ignored by git)" % entry


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
    record(*_ignore(root))
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
