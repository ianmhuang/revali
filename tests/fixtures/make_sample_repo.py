"""Build a tiny Python project as a git repo for revali trials and tests.

    python tests/fixtures/make_sample_repo.py <dir> [--no-remote] [--no-branch] [--local]

Creates: src/calc.py, tests/test_calc.py, revali.toml, CONVENTIONS.md,
.gitignore, requirements.txt, one commit on main, a bare `origin` remote the
main branch is pushed to, and a feature branch `feature/mul` with a change and
a filled-in .revali/feature__mul/change.md.
"""
import json
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))

CALC = '''"""Tiny calculator used by the revali fixture."""


def add(a, b):
    return a + b


def sub(a, b):
    return a - b
'''

CALC_WITH_MUL = CALC + '''

def mul(a, b):
    return a * b
'''

TEST_CALC = '''import unittest

from src.calc import add, sub


class CalcTests(unittest.TestCase):
    def test_add(self):
        self.assertEqual(add(2, 3), 5)

    def test_sub(self):
        self.assertEqual(sub(5, 3), 2)
'''

CONFIG = '''[project]
config_version = 1
base_branch = "main"
platforms = ["linux"]
lint = ""
test_dir = "tests"

[review]
model = "fable"
max_fixes = 2
max_diff_lines = 800
budget_usd = 1.0
exclude = ["*.lock"]

[validate]
model = "opus"
budget_usd = 0.5

[validate.linux]
%(platform)s

[merge]
method = "squash"
'''

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


PLATFORM_LOCAL = '''runner = "local"
setup = ""
test = %s
new_test = %s''' % (toml_str(LOCAL_TEST), toml_str(LOCAL_NEW_TEST))

CHANGE_MD = '''---
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
'''


def git(args, cwd):
    subprocess.run(["git"] + args, cwd=cwd, check=True, capture_output=True, text=True)


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def create(target, with_remote=True, with_branch=True, runner="wsl"):
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
        subprocess.run(["git", "init", "-q", "--bare", remote], check=True, capture_output=True)
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
    info = create(argv[0], with_remote="--no-remote" not in argv, with_branch="--no-branch" not in argv,
                  runner="local" if "--local" in argv else "wsl")
    print("repo: %s" % info["repo"])
    if info["remote"]:
        print("remote: %s" % info["remote"])
    print("branch: %s" % info["branch"])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
