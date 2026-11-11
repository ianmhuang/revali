"""fix/ci-followups, AC-1 and AC-2: nothing under `.revali/` is tracked, and the suite says
so when something is.

AC-1 is asked of the checkout the suite runs from. AC-2 is asked of the author's test
module, run as a subprocess in copies of this tree: one that is not a git checkout, one that
is a repository whose `.revali/` files are only on disk, and the same repository after a
forced add. Nothing is patched: each copy is "the repository the suite runs from".
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

from tests.helpers import ROOT, rmtree_force

MODULE = "tests.test_ci_followups"
STRAY = ".revali/ci__windows-job/change.md"
ON_DISK = (".revali/feature__x/change.md", ".revali/feature__x/state.json")
LEFT_OUT = shutil.ignore_patterns(
    ".git",
    ".revali",
    ".venv",
    "venv",
    "__pycache__",
    "*.pyc",
    ".ruff_cache",
    ".mypy_cache",
    ".pytest_cache",
    "node_modules",
)


def run_git(args, cwd, env=None):
    return subprocess.run(
        ["git"] + args,
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def copy_tree(dst):
    """This tree without its git data, its `.revali/` and the caches. A file another test
    creates and removes while the copy runs is not one the module needs."""
    try:
        shutil.copytree(ROOT, dst, ignore=LEFT_OUT)
    except shutil.Error:
        pass


class ThisCheckout(unittest.TestCase):
    def test_nothing_under_revali_is_tracked(self):
        # AC-1: `git ls-files -- .revali` is empty here, so the stray change.md is gone too
        try:
            inside = run_git(["rev-parse", "--is-inside-work-tree"], ROOT)
        except OSError:
            self.skipTest("git is not available")
        if inside.returncode != 0 or inside.stdout.strip() != "true":
            self.skipTest("the suite does not run from a git checkout")
        listed = run_git(["ls-files", "-z", "--", ".revali"], ROOT)
        self.assertEqual(listed.returncode, 0, listed.stderr)
        tracked = [name for name in listed.stdout.split("\0") if name]
        self.assertNotIn(STRAY, tracked)
        self.assertEqual(tracked, [], "tracked under .revali/")


class TheSuiteSaysSo(unittest.TestCase):
    """AC-2"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="revali review ")
        cls.addClassCleanup(rmtree_force, cls.tmp)
        cls.plain = os.path.join(cls.tmp, "plain")
        cls.repo = os.path.join(cls.tmp, "repo")
        cls.env = dict(os.environ)
        for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
            cls.env.pop(name, None)
        # the plain copy must not be taken for part of a repository above the temp directory
        ceilings = {cls.tmp, os.path.realpath(cls.tmp)}
        cls.env["GIT_CEILING_DIRECTORIES"] = os.pathsep.join(sorted(ceilings))
        cls.env["PYTHONDONTWRITEBYTECODE"] = "1"
        for dst in (cls.plain, cls.repo):
            copy_tree(dst)
            for rel in ON_DISK:
                path = os.path.join(dst, *rel.split("/"))
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "w", encoding="utf-8", newline="") as fh:
                    fh.write("x\n")
        init = run_git(["init", "--quiet"], cls.repo, cls.env)
        if init.returncode != 0:
            raise AssertionError("git init failed: %s" % init.stderr)
        # author's change after round 1: test_review_ciidentity wants an identity with every
        # `git init`; this repository never commits
        for key, value in (("user.email", "test@example.invalid"), ("user.name", "revali test")):
            run_git(["config", key, value], cls.repo, cls.env)
        cls.clean = None

    def run_module(self, cwd):
        res = subprocess.run(
            [sys.executable, "-m", "unittest", MODULE],
            cwd=cwd,
            env=self.env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=600,
        )
        ran = re.search(r"^Ran (\d+) tests?", res.stderr, re.M)
        last = [ln for ln in res.stderr.splitlines() if ln.startswith(("OK", "FAILED"))]
        counts = {"ran": int(ran.group(1)) if ran else 0}
        if last:
            counts.update((k, int(v)) for k, v in re.findall(r"(\w+)=(\d+)", last[-1]))
        return res, counts

    def clean_run(self):
        cls = type(self)
        if cls.clean is None:
            cls.clean = self.run_module(self.repo)
        return cls.clean

    def test_files_that_are_only_on_disk_pass(self):
        # a repository whose .revali/ files are untracked: the module passes
        res, counts = self.clean_run()
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertGreater(counts["ran"], 0)
        tracked = run_git(["ls-files", "--", ".revali"], self.repo, self.env)
        self.assertEqual(tracked.stdout.strip(), "")

    def test_a_tracked_file_fails_the_suite_and_is_named(self):
        # `git add -f` puts two files under .revali/ into the index: a test fails (a
        # failure, not an error) and its message names both
        _, before = self.clean_run()
        added = run_git(["add", "-f", "--"] + list(ON_DISK), self.repo, self.env)
        self.assertEqual(added.returncode, 0, added.stderr)
        self.addCleanup(
            run_git, ["rm", "--cached", "--quiet", "--"] + list(ON_DISK), self.repo, self.env
        )
        res, counts = self.run_module(self.repo)
        self.assertNotEqual(res.returncode, 0, "the suite accepts tracked .revali/ files")
        self.assertEqual(counts["ran"], before["ran"])
        self.assertGreaterEqual(counts.get("failures", 0), 1, res.stderr)
        self.assertEqual(counts.get("errors", 0), 0, res.stderr)
        for rel in ON_DISK:
            with self.subTest(file=rel):
                self.assertIn(rel, res.stderr)

    def test_outside_a_git_checkout_the_check_skips(self):
        # the same tree with no git data: nothing fails, and one more test is skipped than
        # in the repository
        _, in_repo = self.clean_run()
        res, counts = self.run_module(self.plain)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(counts["ran"], in_repo["ran"])
        self.assertGreater(counts.get("skipped", 0), in_repo.get("skipped", 0), res.stderr)


if __name__ == "__main__":
    unittest.main()
