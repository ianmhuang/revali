"""fix/exclude-pathspec (M3.8): one rule (`gitops.left_out`) decides which changed files the
review leaves out, for preflight's omitted list and diff size, the reviewer's diff and the
related-documents section."""

import os
import re
import unittest

from revali import gitops
from revali.preflight import preflight
from revali.review import build_prompt
from revali.state import State
from tests.helpers import RepoCase, git

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def diff_block(prompt):
    return re.search(r"```diff\n(.*?)\n```", prompt, re.S).group(1)


def omitted_line(prompt):
    return re.search(r"^Files changed but omitted from the diff above: (.*)$", prompt, re.M).group(
        1
    )


class Exclusion(RepoCase):
    def prompt(self):
        ctx = preflight(self.repo)
        ctx.log = None
        return ctx, build_prompt(ctx, State(), self.rdir(), 1)

    def test_nested_test_and_lock_files_are_left_out(self):
        """AC-1: the fixture's test pattern is test_review_{topic}.py, exclude ["*.lock"]."""
        self.write("tests/test_review_x.py", "def test_x():\n    assert True\n")
        self.write("pkg/deps.lock", "v2\n")
        self.commit_all("nested")
        ctx, p = self.prompt()
        diff = diff_block(p)
        self.assertNotIn("test_review_x.py", diff)
        self.assertNotIn("deps.lock", diff)
        self.assertIn("src/calc.py", diff)
        omitted = omitted_line(p)
        self.assertIn("tests/test_review_x.py", omitted)
        self.assertIn("pkg/deps.lock", omitted)

    def test_neighbours_and_odd_names_stay(self):
        """AC-2: names are given to git literally."""
        self.write("tests/helper_review.py", "HELPER = 1\n")
        self.write("docs/a [b].md", "brackets\n")
        self.write("pkg/x [1].lock", "excluded, brackets and all\n")
        self.write("pkg/x 1.md", "kept\n")
        if os.name != "nt":  # `*` cannot be in a Windows file name
            self.write("notes*.md", "star\n")
        self.commit_all("odd names")
        ctx, p = self.prompt()
        diff = diff_block(p)
        self.assertIn("tests/helper_review.py", diff)
        self.assertIn("docs/a [b].md", diff)
        self.assertIn("pkg/x 1.md", diff)
        self.assertNotIn("x [1].lock", diff)
        self.assertIn("pkg/x [1].lock", ctx.excluded_files)
        if os.name != "nt":
            self.assertIn("notes*.md", diff)

    def test_a_rename_into_the_test_pattern_is_left_out_whole(self):
        """AC-3: no half-rename shown as a deletion of the old name."""
        self.write("tests/old_helper.py", "".join("line %d\n" % i for i in range(20)))
        self.commit_all("old helper")
        git(["push", "-q", "origin", "HEAD:main"], self.repo)
        git(["fetch", "-q", "origin"], self.repo)
        git(["mv", "tests/old_helper.py", "tests/test_review_moved.py"], self.repo)
        self.commit_all("rename into the pattern")
        ctx, p = self.prompt()
        diff = diff_block(p)
        self.assertNotIn("old_helper.py", diff)
        self.assertNotIn("test_review_moved.py", diff)
        self.assertIn("tests/test_review_moved.py", ctx.excluded_files)
        self.assertNotIn("old_helper", " ".join(ctx.changed_files))
        self.assertNotIn(
            "tests/old_helper.py",
            gitops.diff_paths(ctx.base_ref, "HEAD", self.repo, exclude=["test_review_*.py"]),
        )

    def test_a_plain_rename_is_counted_once_under_its_new_name(self):
        """AC-3: preflight no longer reads git's `dir/{old => new}` as a path."""
        git(["mv", "src/calc.py", "src/arith.py"], self.repo)
        self.commit_all("rename")
        ctx, p = self.prompt()
        self.assertIn("src/arith.py", ctx.changed_files)
        self.assertFalse([f for f in ctx.changed_files if "=>" in f or "{" in f])
        self.assertIn("rename to src/arith.py", diff_block(p))

    def test_nothing_excluded_leaves_the_diff_as_it_was(self):
        """AC-5"""
        base = gitops.rev_parse("main", self.repo)
        plain = git(["diff", "--src-prefix=a/", "--dst-prefix=b/", "%s...HEAD" % base], self.repo)
        self.assertEqual(gitops.diff_text(base, "HEAD", self.repo), plain)
        self.assertEqual(
            gitops.diff_text(base, "HEAD", self.repo, exclude=["*.lock", "test_review_*.py"]),
            plain,
        )


class Numstat(RepoCase):
    def test_a_rename_gives_both_names(self):
        git(["mv", "src/calc.py", "src/arith.py"], self.repo)
        self.write("README.md", self.read("README.md") + "more\n")
        self.commit_all("rename and edit")
        rows = gitops.diff_numstat("main", "HEAD", self.repo)
        names = [r[2] for r in rows]
        self.assertIn(("src/calc.py", "src/arith.py"), names)
        self.assertIn(("README.md",), names)

    def test_left_out_matches_either_name(self):
        self.assertTrue(
            gitops.left_out(("tests/x.py", "tests/test_review_x.py"), ["test_review_*.py"])
        )
        self.assertTrue(gitops.left_out(("a.lock", "b.txt"), ["*.lock"]))
        self.assertFalse(gitops.left_out(("a.txt", "b.txt"), ["*.lock"]))
        self.assertFalse(gitops.left_out(("a.lock",), []))


class Documented(unittest.TestCase):
    """AC-6"""

    def test_defaults_and_template_say_how_exclude_matches(self):
        for name in ("defaults.toml", os.path.join("templates", "revali.toml")):
            with open(os.path.join(HERE, name), encoding="utf-8") as fh:
                line = [ln for ln in fh if ln.startswith("exclude =")][0]
            self.assertIn("the file name", line, name)
            self.assertIn("dir/**", line, name)
