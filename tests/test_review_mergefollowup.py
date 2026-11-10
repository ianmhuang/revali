"""Review of fix/cleanup-failures, AC-5 to AC-7: after the PR merged, `revali merge` archives
the state directory with the root it found at its start (AC-5), puts a failed `git branch -D`
on one line of at most 200 characters (AC-6), and in a linked worktree reports git that times
out, cannot start or fails instead of raising it (AC-7).

Black-box through the CLI with the fake gh and real git. The one git command a test is about
is answered where revali starts it (`subprocess.run`): a timeout, a spawn error or a failing
exit; every other command runs for real."""

import os
import subprocess
import unittest
from unittest import mock

from revali import EXIT_OK, gitops
from revali.config import history_path
from revali.state import State, read_history
from tests.helpers import RepoCase, git, run_cli


def git_cmd(argv, *words):
    """True for a git command line that carries every one of `words`."""
    return os.path.basename(argv[0]).lower().startswith("git") and all(w in argv[1:] for w in words)


def gh_merge(argv):
    return any(os.path.basename(a) == "gh_stub.py" for a in argv) and "merge" in argv


class Commands:
    """While active, `decide(argv, self)` sees every command started through subprocess.run
    and settles its outcome: None runs it, an exception is raised in its place, and
    (exit code, stderr) is returned as its result without running it."""

    def __init__(self, decide):
        self.decide = decide
        self.seen = []
        self._patch = None

    def __enter__(self):
        real = subprocess.run

        def run(args, *a, **kw):
            argv = [str(x) for x in args] if isinstance(args, (list, tuple)) else [str(args)]
            self.seen.append(argv)
            answer = self.decide(argv, self)
            if isinstance(answer, BaseException):
                raise answer
            if answer is not None:
                return subprocess.CompletedProcess(args, answer[0], stdout="", stderr=answer[1])
            return real(args, *a, **kw)

        self._patch = mock.patch.object(subprocess, "run", run)
        self._patch.start()
        return self

    def __exit__(self, *exc):
        self._patch.stop()
        return False

    def merged(self):
        """`gh pr merge` was started before the command being decided."""
        return any(gh_merge(c) for c in self.seen[:-1])

    def ran(self, *words):
        """A git command with `words` was started before the command being decided."""
        return any(git_cmd(c, *words) for c in self.seen[:-1])


def always(answer, *words):
    """Every git command with `words` gets `answer`."""

    def decide(argv, _cmds):
        return answer if git_cmd(argv, *words) else None

    return decide


class MergeCase(RepoCase):
    """self.repo is the primary tree on feature/mul, main pushed to a bare origin."""

    def setUp(self):
        super().setUp()
        os.environ["REVALI_POLL_SECONDS"] = "0.01"

    def ready(self, root):
        rdir = os.path.join(root, ".revali", "feature__mul")
        State(
            repo="owner/repo",
            branch="feature/mul",
            base="main",
            stage="ready_to_merge",
            message="validation 1 passed",
            last_exit=EXIT_OK,
            pr_number=7,
            head_sha=gitops.rev_parse("HEAD", root),
            test_files=["tests/test_review_mul.py"],
        ).save(rdir)
        git(["push", "-q", "-u", "origin", "feature/mul"], root)
        return rdir

    def linked_worktree(self):
        """The primary tree goes to main, feature/mul gets a linked worktree, and the test
        works in it: the layout in which `revali merge` runs its worktree follow-up."""
        git(["checkout", "-q", "main"], self.repo)
        path = os.path.join(self.tmp, "wt-feature")
        git(["worktree", "add", "--quiet", path, "feature/mul"], self.repo)

        def drop():
            os.chdir(self.repo)
            subprocess.run(
                ["git", "worktree", "remove", "--force", path], cwd=self.repo, capture_output=True
            )

        self.addCleanup(drop)
        os.chdir(path)
        return path

    def merge(self, decide):
        with Commands(decide):
            code, out = run_cli(["merge"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertNotIn("Traceback", out)
        self.assertIn("MERGED: PR #7 into main", out)
        rows = read_history(history_path())
        self.assertTrue(any(r.get("stage") == "merged" for r in rows), rows)
        return out

    def line_with(self, out, needle):
        lines = [line for line in out.splitlines() if needle in line]
        self.assertEqual(len(lines), 1, "expected one line with %r in:\n%s" % (needle, out))
        return lines[0]


def root_lost_after_the_merge(argv, cmds):
    """git no longer finds the repository once the PR is merged."""
    if git_cmd(argv, "rev-parse", "--show-toplevel") and cmds.merged():
        return (128, "fatal: not a git repository (or any of the parent directories): .git\n")
    return None


class ArchiveWithTheRootFoundAtTheStart(MergeCase):
    """AC-5"""

    def test_primary_tree(self):
        rdir = self.ready(self.repo)
        out = self.merge(root_lost_after_the_merge)
        self.assertFalse(os.path.isdir(rdir), "state directory not archived:\n" + out)
        self.line_with(out, "archived to")

    def test_linked_worktree(self):
        rdir = self.ready(self.linked_worktree())
        out = self.merge(root_lost_after_the_merge)
        self.assertFalse(os.path.isdir(rdir), "state directory not archived:\n" + out)
        self.line_with(out, "archived to")

    def test_an_archive_that_fails_still_says_so(self):
        rdir = self.ready(self.repo)
        # a file where the archive root would be: the move cannot happen
        with open(os.path.join(self.home, "archive"), "w", encoding="utf-8") as fh:
            fh.write("in the way\n")
        out = self.merge(root_lost_after_the_merge)
        self.line_with(out, "could not archive")
        self.assertTrue(os.path.isdir(rdir), "the state directory must be left in place")


class BranchDeleteErrorOnOneLine(MergeCase):
    """AC-6"""

    ERROR = "error: cannot delete branch 'feature/mul'\nhint: it is used by another worktree\n"
    FOLDED = (
        "git branch -D failed: error: cannot delete branch 'feature/mul' "
        "hint: it is used by another worktree"
    )

    def check(self, out, prefix):
        line = self.line_with(out, prefix)
        self.assertIn(self.FOLDED, line)
        self.assertIn("feature/mul kept", line)
        # nothing of git's error on a line of its own
        self.assertEqual([ln for ln in out.splitlines() if "hint:" in ln], [line], out)
        return line

    def test_primary_tree(self):
        self.ready(self.repo)
        out = self.merge(always((1, self.ERROR), "branch", "-D"))
        self.check(out, "local:")

    def test_linked_worktree(self):
        self.ready(self.linked_worktree())
        out = self.merge(always((1, self.ERROR), "branch", "-D"))
        line = self.check(out, "worktree:")
        self.assertIn("git worktree remove", line)  # the rest of the line follows the error

    def test_primary_tree_cut_to_200_characters(self):
        self.ready(self.repo)
        text = "a" * 150 + "\n" + "b" * 150 + "\n"
        out = self.merge(always((1, text), "branch", "-D"))
        line = self.line_with(out, "local:")
        self.assertIn("a" * 150 + " " + "b" * 49, line)
        self.assertNotIn("b" * 50, out)

    def test_linked_worktree_cut_to_200_characters(self):
        self.ready(self.linked_worktree())
        text = "a" * 150 + "\n" + "b" * 150 + "\n"
        out = self.merge(always((1, text), "branch", "-D"))
        line = self.line_with(out, "worktree:")
        self.assertIn("a" * 150 + " " + "b" * 49, line)
        self.assertNotIn("b" * 50, out)


class WorktreeFollowUpReportsGit(MergeCase):
    """AC-7: every case exits 0 with MERGED: and a history row (MergeCase.merge), and the
    state directory is archived."""

    def setUp(self):
        super().setUp()
        self.wt = self.linked_worktree()
        self.rdir_wt = self.ready(self.wt)

    def follow_up(self, decide):
        out = self.merge(decide)
        self.assertFalse(os.path.isdir(self.rdir_wt), "state directory not archived:\n" + out)
        return out

    def test_push_delete_times_out(self):
        out = self.follow_up(always(subprocess.TimeoutExpired("git", 300), "push", "--delete"))
        note = self.line_with(out, "note: could not delete origin/feature/mul")
        self.assertIn("timed out", note)
        # the rest of the follow-up went on
        line = self.line_with(out, "worktree:")
        self.assertIn("detached at the merged main", line)
        self.assertIn("local branch feature/mul removed", line)
        self.assertEqual(gitops.current_branch(self.wt), "HEAD")

    def test_fetch_cannot_be_found(self):
        out = self.follow_up(always(FileNotFoundError(2, "No such file or directory"), "fetch"))
        note = self.line_with(out, "note: git fetch failed")
        self.assertIn("No such file or directory", note)
        # no checkout without a fetched base: the worktree stays on the branch, which is kept
        line = self.line_with(out, "worktree:")
        self.assertIn("still on feature/mul", line)
        self.assertIn("local branch feature/mul kept", line)
        self.assertEqual(gitops.current_branch(self.wt), "feature/mul")

    def test_fetch_times_out(self):
        out = self.follow_up(always(subprocess.TimeoutExpired("git", 300), "fetch"))
        self.assertIn("timed out", self.line_with(out, "note: git fetch failed"))
        self.assertIn("still on feature/mul", self.line_with(out, "worktree:"))

    def test_checkout_detach_cannot_be_started(self):
        out = self.follow_up(
            always(PermissionError(13, "Access is denied"), "checkout", "--detach")
        )
        note = self.line_with(out, "note: git checkout --detach failed")
        self.assertIn("Access is denied", note)
        line = self.line_with(out, "worktree:")
        self.assertIn("still on feature/mul", line)
        self.assertIn("local branch feature/mul kept", line)

    def test_checkout_detach_times_out(self):
        out = self.follow_up(always(subprocess.TimeoutExpired("git", 120), "checkout", "--detach"))
        self.assertIn("timed out", self.line_with(out, "note: git checkout --detach failed"))

    def test_branch_check_fails_in_rev_parse(self):
        def decide(argv, cmds):
            if git_cmd(argv, "rev-parse", "--abbrev-ref") and cmds.merged():
                return (128, "fatal: index file corrupt\nhint: run git fsck\n")
            return None

        out = self.follow_up(decide)
        line = self.line_with(out, "worktree:")
        self.assertIn("could not tell", line)
        self.assertIn("feature/mul", line)
        self.assertIn("index file corrupt", line)
        # still one line: what the user is told to do follows the reason
        self.assertIn("git worktree remove", line)

    def test_branch_check_fails_after_the_deletion(self):
        def decide(argv, cmds):
            if git_cmd(argv, "rev-parse", "--abbrev-ref") and cmds.ran("branch", "-D"):
                return (128, "fatal: index file corrupt\n")
            return None

        out = self.follow_up(decide)
        line = self.line_with(out, "worktree:")
        self.assertIn("index file corrupt", line)
        self.assertIn("local branch feature/mul removed", line)  # the deletion did happen
        self.assertIsNone(gitops.rev_parse("feature/mul", self.wt))

    def test_branch_deletion_times_out(self):
        out = self.follow_up(always(subprocess.TimeoutExpired("git", 120), "branch", "-D"))
        line = self.line_with(out, "worktree:")
        self.assertIn("detached at the merged main", line)
        self.assertIn("timed out", line)
        self.assertIn("feature/mul", line)
        self.assertNotIn("feature/mul removed", line)

    def test_branch_deletion_cannot_be_started(self):
        out = self.follow_up(always(PermissionError(13, "Access is denied"), "branch", "-D"))
        line = self.line_with(out, "worktree:")
        self.assertIn("Access is denied", line)
        self.assertNotIn("feature/mul removed", line)


if __name__ == "__main__":
    unittest.main()
