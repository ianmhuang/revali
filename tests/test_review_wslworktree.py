"""Review tests for fix/sandbox-key-collision, AC-9: the wsl runner works from a linked
worktree whose `.git` file names its git directory by a path git inside the sandbox cannot
resolve (a worktree added on Windows, read from WSL), and a symbolic ref such as `HEAD` means
the worktree's commit, not the primary tree's.

The real WslRunner runs against the repository's stand-in for wsl.exe, wrapped by a stub
written here. The stand-in's bash reads host paths, so by itself it cannot show the failure;
the wrapper therefore gives the sandbox the view WSL has: while the sandbox script runs, the
worktree's `.git` file names a git directory that does not resolve, and it is put back when
the script ends. Everything the runner does on the host before and after sees the real file.
"""

import os
import shutil
import sys
import tempfile
import unittest

from revali.config import PlatformCfg
from revali.runners import WslRunner
from tests.helpers import FAKE_BIN, RepoCase, _quote, git
from tests.test_ssh_runner import HAVE_BASH

LABEL = "validate-r1"

WSL_WRAP = r"""import os
import sys

sys.path.insert(0, os.environ["WTSIM_FAKE_BIN"])

import wsl_stub as inner

UNRESOLVABLE = "gitdir: Q:/no/such/host/path/.git/worktrees/wt\n"


def rewrite(path, text):
    # r+: git on Windows marks the file hidden, and a hidden file cannot be opened with "w"
    with open(path, "r+", encoding="utf-8", newline="") as fh:
        old = fh.read()
        fh.seek(0)
        fh.truncate()
        fh.write(text)
    return old


def main(argv):
    cmd = argv[3:] if argv[:1] == ["-d"] else argv
    dotgit = os.environ.get("WTSIM_DOTGIT", "")
    if not dotgit or cmd[:1] != ["bash"]:
        return inner.main(argv)
    saved = rewrite(dotgit, UNRESOLVABLE)
    try:
        return inner.main(argv)
    finally:
        rewrite(dotgit, saved)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
"""


def bash_form(path):
    """A host path bash accepts on both platforms (Git Bash takes D:/x/y)."""
    return path.replace("\\", "/")


class FromALinkedWorktree(RepoCase):
    runner = "wsl"

    def setUp(self):
        super().setUp()
        self.use_real_local_runner()
        stub = os.path.join(self.tmp, "wtsim_wsl.py")
        with open(stub, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(WSL_WRAP)
        os.environ["REVALI_WSL_CMD"] = "%s %s" % (_quote(sys.executable), _quote(stub))
        os.environ["WTSIM_FAKE_BIN"] = FAKE_BIN
        os.environ.pop("WTSIM_DOTGIT", None)
        self.sand = tempfile.mkdtemp(prefix="revali-sand-")
        self.addCleanup(shutil.rmtree, self.sand, True)
        # a linked worktree on its own branch, one commit ahead of the primary tree
        self.wt = os.path.join(self.tmp, "wt")
        git(["worktree", "add", "-q", "-b", "feature/other", self.wt], self.repo)
        with open(os.path.join(self.wt, "other.txt"), "w", encoding="utf-8", newline="") as fh:
            fh.write("only in the worktree\n")
        git(["add", "other.txt"], self.wt)
        git(["commit", "-q", "-m", "worktree commit"], self.wt)
        self.wt_head = git(["rev-parse", "HEAD"], self.wt).strip()
        self.primary_head = git(["rev-parse", "HEAD"], self.repo).strip()
        self.assertNotEqual(self.wt_head, self.primary_head)
        self.count = 0

    def sandbox_cannot_resolve_the_worktree(self):
        os.environ["WTSIM_DOTGIT"] = os.path.join(self.wt, ".git")

    def head_seen(self, repo_root, ref, scope):
        """Run one session of `ref`; the commit its step found checked out."""
        self.count += 1
        logs = os.path.join(self.tmp, "wt-logs-%d" % self.count)
        marker = os.path.join(logs, "head.txt")
        runner = WslRunner(
            PlatformCfg(
                runner="wsl",
                distro="Ubuntu",
                command_timeout_min=1,
                sandbox_dir=bash_form(self.sand),
            )
        )
        report = runner.run(
            repo_root,
            ref,
            [("test", 'git rev-parse HEAD > "%s"' % bash_form(marker))],
            {},
            logs,
            LABEL,
            scope=scope,
        )
        self.assertTrue(report.ok, [(s.name, s.returncode, s.stdout[-300:]) for s in report.steps])
        with open(marker, encoding="utf-8") as fh:
            return fh.read().strip()

    @unittest.skipUnless(HAVE_BASH, "needs bash")
    def test_head_from_a_worktree_the_sandbox_cannot_resolve(self):
        # what validation asks for: `HEAD` of the checkout the run is in
        self.sandbox_cannot_resolve_the_worktree()
        self.assertEqual(self.head_seen(self.wt, "HEAD", "feature__other"), self.wt_head)
        self.assertEqual(os.listdir(self.sand), [])  # and the session cleaned up after itself
        # the stand-in put the worktree's `.git` file back
        self.assertEqual(git(["rev-parse", "HEAD"], self.wt).strip(), self.wt_head)

    @unittest.skipUnless(HAVE_BASH, "needs bash")
    def test_a_commit_from_a_worktree_the_sandbox_cannot_resolve(self):
        # what the baseline asks for: the head commit by its sha
        self.sandbox_cannot_resolve_the_worktree()
        self.assertEqual(self.head_seen(self.wt, self.wt_head, "feature__other"), self.wt_head)

    @unittest.skipUnless(HAVE_BASH, "needs bash")
    def test_a_branch_name_is_the_commit_it_names_in_the_worktree(self):
        self.sandbox_cannot_resolve_the_worktree()
        self.assertEqual(self.head_seen(self.wt, "feature/other", "feature__other"), self.wt_head)

    @unittest.skipUnless(HAVE_BASH, "needs bash")
    def test_head_from_the_primary_tree_stays_the_primary_trees_commit(self):
        # the worktree is ahead; a session of the primary tree must not pick that commit up
        self.assertEqual(self.head_seen(self.repo, "HEAD", "feature__mul"), self.primary_head)


if __name__ == "__main__":
    unittest.main()
