"""Review of fix/short-path-temp.

AC-1: `tests.helpers.RepoCase` works in the real form of the directory `tempfile` gives it, so
the paths a test builds from `self.tmp` / `self.repo` match the ones git and revali print.
AC-3: `gitops.is_linked_worktree` answers the same for a checkout reached through its real
path and through another name of it.

"Another name" comes in two kinds, one class each:
- the 8.3 short path the AC names (Windows volumes that generate short names; skipped
  elsewhere);
- a symlink to the directory. It is the same mismatch on every platform: git answers with the
  physical path, the caller holds the other name. The validation sandbox is Linux, where no
  8.3 name exists, so this is the kind that runs there. Skipped where a symlink cannot be
  created (Windows without the privilege).

Black-box: RepoCase's public attributes, `gitops`, and the CLI through `run_cli`."""

import os
import subprocess
import tempfile
import unittest

from revali import EXIT_ERROR, EXIT_OK, gitops
from revali.state import State
from tests.helpers import RepoCase, git, rmtree_force, run_cli


def norm(path):
    return os.path.normcase(os.path.normpath(path))


def text_has_path(text, path):
    return norm(path).replace("\\", "/") in os.path.normcase(text).replace("\\", "/")


def short_form(path):
    """The 8.3 form of an existing path; "" when there is none (not Windows, short names off
    on the volume, or nothing in the path needs shortening)."""
    if os.name != "nt":
        return ""
    import ctypes
    from ctypes import wintypes

    get = ctypes.windll.kernel32.GetShortPathNameW
    get.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
    get.restype = wintypes.DWORD
    size = get(path, None, 0)
    if not size:
        return ""
    buf = ctypes.create_unicode_buffer(size)
    if not get(path, buf, size):
        return ""
    return "" if norm(buf.value) == norm(path) else buf.value


class OtherNameTemp(RepoCase):
    """RepoCase set up while `tempfile` hands out another name of a real directory.
    `self.real_temp` is that directory, `self.other_temp` the name tempfile was given."""

    def other_name_of_the_temp_dir(self):
        raise NotImplementedError

    def other_name(self, path):
        """`path` (below self.real_temp, real form) spelled through the other name."""
        raise NotImplementedError

    def setUp(self):
        self.real_temp = os.path.realpath(tempfile.mkdtemp(prefix="revali review long name "))
        self.addCleanup(rmtree_force, self.real_temp)
        self.other_temp = self.other_name_of_the_temp_dir()
        saved = tempfile.tempdir
        tempfile.tempdir = self.other_temp
        try:
            self.handed_out = tempfile.gettempdir()
            super().setUp()
        finally:
            tempfile.tempdir = saved

    def assert_other_name(self, other, real):
        """The premise of every check: two spellings, one directory."""
        self.assertNotEqual(norm(other), norm(real))
        self.assertTrue(os.path.samefile(other, real), (other, real))

    def add_worktree(self, path, branch=""):
        """A linked worktree at `path`: on `branch`, or detached at HEAD without one."""
        what = [path, branch] if branch else ["--detach", path]
        git(["worktree", "add", "--quiet"] + what, self.repo)

        def drop():
            os.chdir(self.repo)
            subprocess.run(
                ["git", "worktree", "remove", "--force", path], cwd=self.repo, capture_output=True
            )

        self.addCleanup(drop)
        return path


class ShortNameTemp(OtherNameTemp):
    def other_name_of_the_temp_dir(self):
        short = short_form(self.real_temp)
        if not short:
            self.skipTest("no 8.3 name for %s" % self.real_temp)
        return short

    def other_name(self, path):
        short = short_form(path)
        if not short:
            self.skipTest("no 8.3 name for %s" % path)
        return short


class SymlinkTemp(OtherNameTemp):
    def other_name_of_the_temp_dir(self):
        holder = os.path.realpath(tempfile.mkdtemp(prefix="revali review link "))
        self.addCleanup(rmtree_force, holder)
        link = os.path.join(holder, "alias")
        try:
            os.symlink(self.real_temp, link, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            self.skipTest("cannot create a symlink here: %s" % exc)
        return link

    def other_name(self, path):
        return os.path.join(self.other_temp, os.path.relpath(path, self.real_temp))


class RealFormChecks:
    """AC-1"""

    def test_the_case_lives_in_the_real_form_of_the_directory(self):
        self.assert_other_name(self.other_temp, self.real_temp)
        # tempfile handed out the other name, and the case did not stay in it
        self.assertEqual(norm(self.handed_out), norm(self.other_temp))
        self.assertEqual(norm(os.path.dirname(self.tmp)), norm(self.real_temp))
        self.assertEqual(norm(self.tmp), norm(os.path.realpath(self.tmp)))
        for path in (self.repo, self.home, self.rdir(), self.change_md()):
            with self.subTest(path=path):
                self.assertTrue(norm(path).startswith(norm(self.tmp) + os.sep), path)

    def test_paths_match_what_git_prints(self):
        self.assertEqual(norm(gitops.repo_root(self.repo)), norm(self.repo))
        self.assertEqual(norm(os.getcwd()), norm(self.repo))  # setUp's chdir
        linked = self.add_worktree(os.path.join(self.tmp, "wt main"), "main")
        self.assertEqual(norm(gitops.repo_root(linked)), norm(linked))
        # `git worktree list`, which the merge refusal quotes
        self.assertEqual(norm(gitops.worktree_holding("main", self.repo)), norm(linked))

    def test_status_names_the_repo_as_the_test_spells_it(self):
        code, out = run_cli(["status"])
        self.assertTrue(out.strip(), code)
        self.assertEqual(
            os.path.normcase(out.splitlines()[0]),
            os.path.normcase("repo: %s  branch: feature/mul" % self.repo),
        )

    def test_merge_refusal_names_the_worktree_as_the_test_spells_it(self):
        # one of the failures the change is about: the primary tree refuses to merge while a
        # linked worktree holds the base, and the ERROR line carries that worktree's path
        linked = self.add_worktree(os.path.join(self.tmp, "wt main"), "main")
        State(
            repo="owner/repo",
            branch="feature/mul",
            base="main",
            stage="ready_to_merge",
            message="validation 1 passed",
            last_exit=EXIT_OK,
            pr_number=7,
            head_sha=gitops.rev_parse("HEAD", self.repo),
            test_files=["tests/test_review_mul.py"],
        ).save(self.rdir())
        git(["push", "-q", "-u", "origin", "feature/mul"], self.repo)
        code, out = run_cli(["merge"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertIn("ERROR: main is checked out in ", out)
        self.assertTrue(text_has_path(out, linked), out)


class LinkedWorktreeChecks:
    """AC-3. The real paths are taken with realpath here, so these checks depend on
    `gitops.is_linked_worktree` alone, not on which form RepoCase works in."""

    def both_names(self, path):
        other = self.other_name(path)
        self.assert_other_name(other, path)
        return path, other

    def test_the_primary_tree_and_a_subdirectory_of_it(self):
        repo = os.path.realpath(self.repo)
        for real in (repo, os.path.join(repo, "src")):
            for path in self.both_names(real):
                with self.subTest(path=path):
                    self.assertIs(gitops.is_linked_worktree(path), False)

    def test_the_primary_tree_while_a_linked_worktree_exists(self):
        repo = os.path.realpath(self.repo)
        self.add_worktree(os.path.join(os.path.realpath(self.tmp), "linked worktree"))
        for real in (repo, os.path.join(repo, "src")):
            for path in self.both_names(real):
                with self.subTest(path=path):
                    self.assertIs(gitops.is_linked_worktree(path), False)

    def test_a_linked_worktree_and_a_subdirectory_of_it(self):
        linked = self.add_worktree(os.path.join(os.path.realpath(self.tmp), "linked worktree"))
        for real in (linked, os.path.join(linked, "src")):
            for path in self.both_names(real):
                with self.subTest(path=path):
                    self.assertIs(gitops.is_linked_worktree(path), True)


class RealFormWithAShortTemp(RealFormChecks, ShortNameTemp):
    pass


class RealFormWithASymlinkedTemp(RealFormChecks, SymlinkTemp):
    pass


class LinkedWorktreeThroughAShortPath(LinkedWorktreeChecks, ShortNameTemp):
    pass


class LinkedWorktreeThroughASymlink(LinkedWorktreeChecks, SymlinkTemp):
    pass


if __name__ == "__main__":
    unittest.main()
