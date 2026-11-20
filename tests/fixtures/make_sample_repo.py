"""Build a tiny Python project as a git repo for revali trials and tests.

    python tests/fixtures/make_sample_repo.py <dir> [--no-remote] [--no-branch] [--local]

Creates: src/calc.py, tests/test_calc.py, revali.toml, CONVENTIONS.md,
.gitignore, requirements.txt, one commit on main, a bare `origin` remote the
main branch is pushed to, and a feature branch `feature/mul` with a change and
a filled-in .revali/feature__mul/change.md.

The command line and build() run git each time; create(), which the tests use, builds
each kind once per process and copies it.
"""

import atexit
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))

CALC = '''"""Tiny calculator used by the revali fixture."""


def add(a, b):
    return a + b


def sub(a, b):
    return a - b
'''

CALC_WITH_MUL = CALC + """

def mul(a, b):
    return a * b
"""

TEST_CALC = """import unittest

from src.calc import add, sub


class CalcTests(unittest.TestCase):
    def test_add(self):
        self.assertEqual(add(2, 3), 5)

    def test_sub(self):
        self.assertEqual(sub(5, 3), 2)
"""

CONFIG = """[project]
config_version = 1
base_branch = "main"
platforms = ["linux"]
lint = ""
test_dir = "tests"

[review]
max_fixes = 2
max_diff_lines = 800
budget_usd = 1.0
exclude = ["*.lock"]

[validate]
budget_usd = 0.5

[validate.linux]
%(platform)s

[merge]
method = "squash"
"""

PLATFORM_WSL = '''runner = "wsl"
distro = "Ubuntu"
setup = "python3 -m venv .venv && .venv/bin/pip install -q -r requirements.txt"
test = ".venv/bin/python -m pytest -q"
new_test = ".venv/bin/python -m pytest -q tests"'''

# The interpreter name differs per platform: Windows and most venvs have `python`,
# a stock Ubuntu (the WSL sandbox) only `python3`.
PY = "python" if shutil.which("python") else "python3"
LOCAL_TEST = PY + ' -m unittest discover -s tests -t . -p "test_calc*.py"'
LOCAL_NEW_TEST = PY + ' -m unittest discover -s tests -t . -p "test_review_*.py"'


def toml_str(value):
    """A TOML basic string; JSON escaping is a subset of TOML's."""
    return json.dumps(value)


PLATFORM_LOCAL = """runner = "local"
setup = ""
test = %s
new_test = %s""" % (toml_str(LOCAL_TEST), toml_str(LOCAL_NEW_TEST))

CHANGE_MD = """---
title: Add mul to calc
kind: feature
author_model: fixture
---

## Request
add a mul(a, b) function to calc that multiplies two numbers

## What
Added `mul(a, b)` to `src/calc.py`.

## Why
The calculator only had add and sub.

## Goal
`mul` multiplies two integers.

## Acceptance criteria
- AC-1: mul(a, b) returns the product of a and b for integers
- AC-2: mul with zero returns zero

## Out of scope
Division.

## Dependencies
none
"""


# Kept out of the index, where they would record the directory the fixture was built in
# and a copy would carry it.
NO_INDEX_CACHE = ["-c", "core.untrackedCache=false", "-c", "core.fsmonitor=false"]
# No git process left behind: after a commit, git may start maintenance or gc in the
# background (git 2.56 on macOS does), which writes into the build, for instance
# .git/objects/maintenance.lock, while create() copies it. The receiving end of a push
# does the same in the remote; a local push clears -c settings before starting it, so the
# remote turns it off in its own config (build()).
NO_BACKGROUND = ["-c", "maintenance.auto=false", "-c", "gc.auto=0"]
GIT_OPTIONS = NO_INDEX_CACHE + NO_BACKGROUND


def git(args, cwd):
    subprocess.run(
        ["git"] + GIT_OPTIONS + args, cwd=cwd, check=True, capture_output=True, text=True
    )


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


# (with_remote, with_branch, runner) -> what build() returned under _build_root
_built = {}
_build_root = None
# The temporary directory as the process starts. A test may point tempfile.tempdir at a
# directory of its own and delete it; the builds must outlive that test.
_TEMP = tempfile.gettempdir()


def build_dir():
    """The directory this process builds its fixtures in, or None before the first build."""
    return _build_root


def create(target, with_remote=True, with_branch=True, runner="wsl"):
    """A copy of the fixture build() makes with these options, built once per process.

    A git call costs 70-85 ms on a busy Windows host, and a build makes about 15, so every
    test but the first of each kind copies files instead. The copies of a kind share their
    commit ids, and every copy carries the environment of the first build (git config,
    `GIT_*` variables). The builds live in a temporary directory removed at exit; a build
    that fails is removed and raises, so the next call builds again. A build that is gone
    from disk is made again. `target` is expected to be absent or empty: files already
    there stay, unknown to the copy's index.
    """
    global _build_root
    key = (bool(with_remote), bool(with_branch), runner)
    if _build_root is not None and not os.path.isdir(_build_root):
        _build_root = None
        _built.clear()
    if key in _built and not _on_disk(_built[key]):
        del _built[key]
    if key not in _built:
        if _build_root is None:
            _build_root = tempfile.mkdtemp(prefix="revali fixtures ", dir=_TEMP)
            atexit.register(_remove, _build_root)
        place = tempfile.mkdtemp(dir=_build_root)
        try:
            _built[key] = build(os.path.join(place, "sample"), *key)
        except BaseException:
            _remove(place)
            raise
    return _copy(_built[key], target)


def _on_disk(proto):
    if not os.path.isdir(os.path.join(proto["repo"], ".git")):
        return False
    return not proto["remote"] or os.path.isdir(proto["remote"])


def _copy(proto, target):
    target = os.path.abspath(target)
    shutil.copytree(proto["repo"], target, symlinks=True, dirs_exist_ok=True)
    remote = None
    if proto["remote"]:
        remote = target + ".git"
        shutil.copytree(proto["remote"], remote, symlinks=True, dirs_exist_ok=True)
        _set_origin_url(target, remote)
    return {"repo": target, "remote": remote, "branch": proto["branch"]}


def _set_origin_url(repo, url):
    """Point origin at `url` by rewriting .git/config, which `git config` would do in a call."""
    path = os.path.join(repo, ".git", "config")
    with open(path, "r", encoding="utf-8", newline="") as fh:
        lines = fh.read().split("\n")
    found = [i for i, line in enumerate(lines) if line.strip().startswith("url = ")]
    if len(found) != 1:
        raise RuntimeError("%s: expected one url line, found %d" % (path, len(found)))
    quoted = url.replace("\\", "\\\\").replace('"', '\\"')
    lines[found[0]] = '\turl = "%s"' % quoted
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write("\n".join(lines))


def _remove(path):
    def retry(func, p, _exc):
        try:
            os.chmod(p, stat.S_IWRITE)  # git objects are read-only on Windows
            func(p)
        except OSError:
            pass

    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=retry)
    else:
        shutil.rmtree(path, onerror=lambda f, p, e: retry(f, p, e[1]))
    if os.path.exists(path):
        sys.stderr.write("make_sample_repo: could not remove %s\n" % path)


def build(target, with_remote=True, with_branch=True, runner="wsl"):
    """Build the fixture at `target` with git (what the command line does)."""
    target = os.path.abspath(target)
    os.makedirs(target, exist_ok=True)
    git(["init", "-q", "-b", "main"], target)
    git(["config", "user.email", "fixture@example.invalid"], target)
    git(["config", "user.name", "Fixture"], target)
    git(["config", "commit.gpgsign", "false"], target)
    git(["config", "core.autocrlf", "false"], target)
    write(os.path.join(target, "src", "__init__.py"), "")
    write(os.path.join(target, "src", "calc.py"), CALC)
    write(os.path.join(target, "tests", "__init__.py"), "")
    write(os.path.join(target, "tests", "test_calc.py"), TEST_CALC)
    write(os.path.join(target, "requirements.txt"), "pytest\n")
    platform = PLATFORM_LOCAL if runner == "local" else PLATFORM_WSL
    write(os.path.join(target, "revali.toml"), CONFIG % {"platform": platform})
    write(os.path.join(target, ".gitignore"), ".revali/\n.venv/\n__pycache__/\n")
    with open(os.path.join(ROOT, "templates", "CONVENTIONS.md"), "r", encoding="utf-8") as fh:
        write(os.path.join(target, "CONVENTIONS.md"), fh.read())
    write(os.path.join(target, "README.md"), "# sample\n\nFixture project for revali.\n")
    git(["add", "-A"], target)
    git(["commit", "-q", "-m", "Initial sample project"], target)

    remote = None
    if with_remote:
        remote = target + ".git"
        git(["init", "-q", "--bare", remote], target)
        git(["config", "receive.autogc", "false"], remote)
        git(["remote", "add", "origin", remote], target)
        git(["push", "-q", "-u", "origin", "main"], target)

    if with_branch:
        git(["checkout", "-q", "-b", "feature/mul"], target)
        write(os.path.join(target, "src", "calc.py"), CALC_WITH_MUL)
        git(["add", "-A"], target)
        git(["commit", "-q", "-m", "Add mul"], target)
        write(os.path.join(target, ".revali", "feature__mul", "change.md"), CHANGE_MD)
    return {"repo": target, "remote": remote, "branch": "feature/mul" if with_branch else "main"}


def main(argv):
    if not argv or argv[0].startswith("-"):
        print(__doc__)
        return 2
    info = build(
        argv[0],
        with_remote="--no-remote" not in argv,
        with_branch="--no-branch" not in argv,
        runner="local" if "--local" in argv else "wsl",
    )
    print("repo: %s" % info["repo"])
    if info["remote"]:
        print("remote: %s" % info["remote"])
    print("branch: %s" % info["branch"])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
