"""fix/short-path-temp (M3.2.2): with the temp directory reached through an 8.3 short path, as
on GitHub's Windows runners (`C:\\Users\\RUNNER~1\\...`), RepoCase works in the long form of
its directory (AC-1), and `gitops.is_linked_worktree` answers the same for a short and a long
path to one checkout (AC-3).

8.3 names exist only on Windows volumes that generate them; every test here skips
elsewhere."""

import os
import shutil
import tempfile
import unittest

from revali import gitops
from tests.helpers import RepoCase, git


def short_path(path: str) -> str:
    """The 8.3 form of an existing path, or "" when there is none (not Windows, 8.3 names
    disabled on the volume, or a path whose parts are all short already)."""
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
    short = buf.value
    return "" if os.path.normcase(short) == os.path.normcase(path) else short


class ShortTempCase(RepoCase):
    """RepoCase with `tempfile` pointed at the 8.3 form of a long-named directory."""

    def setUp(self):
        long_dir = tempfile.mkdtemp(prefix="revali-long-name-for-8dot3 ")
        self.addCleanup(shutil.rmtree, long_dir, True)
        short = short_path(long_dir)
        if not short:
            self.skipTest("no 8.3 name for %s" % long_dir)
        self.short_temp = short
        saved = tempfile.tempdir
        tempfile.tempdir = short
        try:
            super().setUp()
        finally:
            tempfile.tempdir = saved
        self.long_temp = os.path.realpath(long_dir)


class LongFormOfTheTempDir(ShortTempCase):
    """AC-1"""

    def test_the_case_works_in_the_long_form(self):
        # the premise: tempfile handed out the other spelling of the directory
        self.assertNotEqual(os.path.normcase(self.short_temp), os.path.normcase(self.long_temp))
        self.assertEqual(self.tmp, os.path.realpath(self.tmp))
        self.assertEqual(
            os.path.normcase(os.path.dirname(self.tmp)), os.path.normcase(self.long_temp)
        )
        self.assertTrue(self.repo.startswith(self.tmp))

    def test_paths_match_what_git_prints(self):
        self.assertEqual(os.path.normcase(gitops.repo_root(self.repo)), os.path.normcase(self.repo))
        self.assertEqual(os.path.normcase(os.getcwd()), os.path.normcase(self.repo))


class LinkedWorktreeThroughAShortPath(ShortTempCase):
    """AC-3"""

    def short(self, path):
        s = short_path(path)
        if not s:
            self.skipTest("no 8.3 name for %s" % path)
        return s

    def test_the_primary_tree(self):
        repo = self.short(self.repo)
        self.assertFalse(gitops.is_linked_worktree(self.repo))
        self.assertFalse(gitops.is_linked_worktree(repo))
        self.assertFalse(gitops.is_linked_worktree(os.path.join(repo, "src")))

    def test_a_linked_worktree(self):
        linked = os.path.join(self.tmp, "linked worktree")
        git(["worktree", "add", "--quiet", "--detach", linked], self.repo)
        short = self.short(linked)
        self.assertTrue(gitops.is_linked_worktree(linked))
        self.assertTrue(gitops.is_linked_worktree(short))
        self.assertTrue(gitops.is_linked_worktree(os.path.join(short, "src")))


if __name__ == "__main__":
    unittest.main()
