"""fix/exclude-pathspec (M3.8): one rule (`gitops.left_out`) decides which changed files the
review leaves out, for preflight's omitted list and diff size, the reviewer's diff and the
related-documents section."""

import os
import re
import unittest
from unittest import mock

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

    def test_a_rename_into_the_test_pattern_is_reviewed_whole(self):
        """AC-3: no half-rename shown as a deletion of the old name. Since
        fix/review-modified-tests the test pattern leaves out only files the branch adds, so
        a file on the base renamed into it is shown whole, as a rename."""
        self.write("tests/old_helper.py", "".join("line %d\n" % i for i in range(20)))
        self.commit_all("old helper")
        git(["push", "-q", "origin", "HEAD:main"], self.repo)
        git(["fetch", "-q", "origin"], self.repo)
        git(["mv", "tests/old_helper.py", "tests/test_review_moved.py"], self.repo)
        self.commit_all("rename into the pattern")
        ctx, p = self.prompt()
        diff = diff_block(p)
        self.assertIn("rename from tests/old_helper.py", diff)
        self.assertIn("rename to tests/test_review_moved.py", diff)
        self.assertNotIn("deleted file mode", diff)
        self.assertNotIn("tests/test_review_moved.py", ctx.excluded_files)
        self.assertIn("tests/test_review_moved.py", ctx.changed_files)
        # given the pattern as an `exclude`, the rename is left out under both names
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

    def test_left_out_decides_by_the_new_name(self):
        """PR #82 round 1 F3: a rename out of an excluded name is reviewed."""
        self.assertTrue(
            gitops.left_out(("tests/x.py", "tests/test_review_x.py"), ["test_review_*.py"])
        )
        self.assertFalse(gitops.left_out(("a.lock", "b.txt"), ["*.lock"]))
        self.assertFalse(gitops.left_out(("a.txt", "b.txt"), ["*.lock"]))
        self.assertFalse(gitops.left_out(("a.lock",), []))


BODY = "".join("line_%d = %d\n" % (i, i) for i in range(20))


class Sections(RepoCase):
    """PR #82 round 1: the left-out files' sections are cut from the whole diff (F1), renames
    are found with -M whatever `diff.renames` says (F2), a name may hold a tab (F4)."""

    def on_base(self, files):
        """Commit `files` on main and merge main into feature/mul."""
        git(["checkout", "-q", "main"], self.repo)
        for path, text in files.items():
            self.write(path, text)
        self.commit_all("on the base")
        git(["checkout", "-q", "feature/mul"], self.repo)
        git(["merge", "-q", "--no-edit", "main"], self.repo)

    def diff(self, exclude):
        """The diff with sections cut, after checking the pathspec form gives the same text."""
        base = gitops.rev_parse("main", self.repo)
        entries = gitops._entries(base, "HEAD", self.repo)
        whole = gitops.git_ok(
            ["diff", "-M", "--src-prefix=a/", "--dst-prefix=b/", "%s...HEAD" % base], self.repo
        ).stdout
        self.assertIsNotNone(gitops._without_sections(whole, entries, exclude))
        cut = gitops.diff_text(base, "HEAD", self.repo, exclude=exclude)
        with mock.patch.object(gitops, "_without_sections", return_value=None):
            self.assertEqual(gitops.diff_text(base, "HEAD", self.repo, exclude=exclude), cut)
        return cut

    def test_the_command_line_does_not_grow_with_the_left_out_files(self):
        """F1: 600 names of 60 characters are more than Windows takes on one command line."""
        for i in range(600):
            self.write("vendor/some_vendored_package/module_with_a_long_name_%04d.py" % i, "v\n")
        self.commit_all("vendored")
        base = gitops.rev_parse("main", self.repo)
        calls = []
        real = gitops._git

        def record(args, cwd, log=None, timeout=120):
            calls.append(list(args))
            return real(args, cwd, log, timeout)

        with mock.patch.object(gitops, "_git", record):
            text = gitops.diff_text(base, "HEAD", self.repo, exclude=["vendor/**"])
        self.assertIn("src/calc.py", text)
        self.assertNotIn("vendor/", text)
        self.assertLess(max(len(" ".join(a)) for a in calls), 200, calls)

    def test_mixed_entries_pair_with_their_sections(self):
        self.on_base({"docs/gone.md": "gone\n", "pkg/a.lock": "a\n", "pkg/b.lock": "b\n"})
        git(["rm", "-q", "docs/gone.md", "pkg/a.lock"], self.repo)
        self.write("pkg/b.lock", "b2\n")
        self.write("docs/a [b].md", "kept\n")
        self.write("pkg/x [1].lock", "x\n")
        self.commit_all("mixed")
        diff = self.diff(["*.lock"])
        self.assertIn("deleted file mode", diff)
        self.assertIn("docs/gone.md", diff)
        self.assertIn("docs/a [b].md", diff)
        self.assertIn("def mul", diff)
        self.assertNotIn(".lock", diff)

    @unittest.skipIf(os.name == "nt", "symbolic links")
    def test_a_type_change_has_two_sections(self):
        self.on_base({"docs/t.md": "file\n", "pkg/t.lock": "file\n"})
        for name in ("docs/t.md", "pkg/t.lock"):
            os.remove(os.path.join(self.repo, name))
            os.symlink("elsewhere", os.path.join(self.repo, name))
        self.commit_all("type changes")
        diff = self.diff(["*.lock"])
        self.assertEqual(diff.count("diff --git a/docs/t.md b/docs/t.md\n"), 2)
        self.assertNotIn("t.lock", diff)
        self.assertIn("def mul", diff)

    def test_copy_detection_in_the_users_config_changes_nothing(self):
        """F2: with `diff.renames = copies` git would report the reviewer's test as a copy of
        the author's edited file."""
        self.on_base({"src/big.py": BODY})
        self.write("src/big.py", BODY.replace("line_3 = 3", "author_edit = 3"))
        self.write("tests/test_review_big.py", BODY.replace("line_5 = 5", "reviewer = 5"))
        self.commit_all("edit and copy")
        git(["config", "diff.renames", "copies"], self.repo)
        base = gitops.rev_parse("main", self.repo)
        self.assertIn("C", git(["diff", "--name-status", "%s...HEAD" % base], self.repo))
        diff = self.diff(["test_review_*.py"])
        self.assertIn("author_edit", diff)
        self.assertNotIn("test_review_big.py", diff)
        paths = gitops.diff_paths(base, "HEAD", self.repo, exclude=["test_review_*.py"])
        self.assertIn("src/big.py", paths)
        self.assertNotIn("tests/test_review_big.py", paths)

    def cut_under(self, key, value):
        """PR #82 round 2 F5, round 3 F6: with `key = value` in the repository's config the
        diff still has the shape the cut needs, so git is not given the left-out names."""
        self.write("pkg/deps.lock", "lock\n")
        self.commit_all("lock")
        git(["config", key, value], self.repo)
        base = gitops.rev_parse("main", self.repo)
        calls = []
        real = gitops._git

        def record(args, cwd, log=None, timeout=120):
            calls.append(list(args))
            return real(args, cwd, log, timeout)

        with mock.patch.object(gitops, "_git", record):
            text = gitops.diff_text(base, "HEAD", self.repo, exclude=["*.lock"])
        self.assertNotIn("deps.lock", text)
        self.assertIn("def mul", text)
        self.assertFalse([a for a in calls if "--" in a], calls)
        return text

    def test_colour_is_off(self):
        self.assertNotIn("\x1b[", self.cut_under("color.ui", "always"))

    def test_an_external_diff_driver_is_not_used(self):
        self.assertNotIn("EXTERNAL_DRIVER", self.cut_under("diff.external", "echo EXTERNAL_DRIVER"))

    def test_a_submodule_change_is_shown_as_a_commit_line(self):
        """A gitlink moved on the branch; with `diff.submodule = log` git would write it as a
        `Submodule sub ...` line without a `diff --git` header."""

        def gitlink(sha, message):
            git(["update-index", "--add", "--cacheinfo", "160000,%s,sub" % sha], self.repo)
            git(["commit", "-q", "-m", message], self.repo)

        git(["checkout", "-q", "main"], self.repo)
        gitlink("1" * 40, "submodule on the base")
        git(["checkout", "-q", "feature/mul"], self.repo)
        git(["merge", "-q", "--no-edit", "main"], self.repo)
        gitlink("2" * 40, "submodule moved")
        text = self.cut_under("diff.submodule", "log")
        self.assertIn("+Subproject commit " + "2" * 40, text)

    @unittest.skipIf(os.name == "nt", "a tab cannot be in a Windows file name")
    def test_a_name_with_a_tab_is_counted(self):
        """F4"""
        self.write("docs/a\tb.md", "tab\n")
        self.commit_all("tab")
        rows = gitops.diff_numstat("main", "HEAD", self.repo)
        self.assertIn((1, 0, ("docs/a\tb.md",)), rows)


class Documented(unittest.TestCase):
    """AC-6"""

    def test_defaults_and_template_say_how_exclude_matches(self):
        for name in ("defaults.toml", os.path.join("templates", "revali.toml")):
            with open(os.path.join(HERE, name), encoding="utf-8") as fh:
                line = [ln for ln in fh if ln.startswith("exclude =")][0]
            self.assertIn("the file name", line, name)
            self.assertIn("dir/**", line, name)
