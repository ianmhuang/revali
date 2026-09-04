"""Review of feature/worktree-docs: AC-3 (the worktree merge follow-up says what actually
happened, and the primary tree refuses to merge while a linked worktree holds the base) and
AC-4 (`merge` takes the tree lock inside a try, so a TreeLockHeld is one ERROR line).

Black-box through `revali merge` with the fake gh and a real git remote; the one white-box
case (a failing `git checkout --detach`) is what AC-3 asks for by name."""
import os
import subprocess
import unittest
from unittest import mock

from tests.helpers import RepoCase, git, run_cli
from revali import EXIT_ERROR, EXIT_OK
from revali import gitops, merge, pipeline
from revali.procs import Result
from revali.state import State, TreeLockHeld, lock_path, tree_lock_path


def same_path(a, b):
    """Path equality that survives git's forward slashes and Windows drive-letter case."""
    return os.path.normcase(os.path.normpath(a)) == os.path.normcase(os.path.normpath(b))


def mentions_path(text, path):
    return os.path.normcase(os.path.normpath(path)).replace("\\", "/") in \
        os.path.normcase(text).replace("\\", "/")


class WorktreeCase(RepoCase):
    """The fixture: self.repo is the primary tree (owns .git) on feature/mul with main pushed
    to a bare origin. Helpers add linked worktrees and a ready_to_merge state."""

    def add_worktree(self, branch):
        path = os.path.join(self.tmp, "wt-" + branch.replace("/", "__"))
        git(["worktree", "add", "--quiet", path, branch], self.repo)

        def drop():
            os.chdir(self.repo)
            subprocess.run(["git", "worktree", "remove", "--force", path], cwd=self.repo, capture_output=True)

        self.addCleanup(drop)
        return path

    def ready(self, root):
        rdir = os.path.join(root, ".revali", "feature__mul")
        State(repo="owner/repo", branch="feature/mul", base="main", stage="ready_to_merge",
              message="validation 1 passed", last_exit=EXIT_OK, pr_number=7,
              head_sha=gitops.rev_parse("HEAD", root), test_files=["tests/test_review_mul.py"]).save(rdir)
        git(["push", "-q", "-u", "origin", "feature/mul"], root)
        return rdir

    def remote_heads(self, root):
        return sorted(l.split("refs/heads/")[1]
                      for l in git(["ls-remote", "--heads", "origin"], root).splitlines())

    def gh_merges(self):
        return [c["argv"] for c in self.fake_calls("gh") if c["argv"][:2] == ["pr", "merge"]]


class PrimaryTreeRefusal(WorktreeCase):
    """AC-3: `merge` in the primary tree while a linked worktree holds the base."""

    def test_refused_with_exit_1_and_the_worktree_path(self):
        linked = self.add_worktree("main")
        rdir = self.ready(self.repo)
        code, out = run_cli(["merge"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertIn("ERROR: main is checked out in ", out)
        self.assertTrue(mentions_path(out, linked), out)
        self.assertIn("remove or switch that worktree, then merge again", out)
        self.assertNotIn("Traceback", out)
        # nothing was merged, detached, or deleted
        self.assertEqual(self.gh_merges(), [])
        self.assertEqual(gitops.current_branch(self.repo), "feature/mul")
        self.assertEqual(gitops.current_branch(linked), "main")
        self.assertIsNotNone(gitops.rev_parse("feature/mul", self.repo))
        self.assertIn("feature/mul", self.remote_heads(self.repo))
        self.assertEqual(State.load(rdir).stage, "ready_to_merge")
        self.assertTrue(os.path.isdir(rdir))                     # the state directory stays
        # both locks are released
        self.assertFalse(os.path.isfile(lock_path(rdir)))
        self.assertFalse(os.path.isfile(tree_lock_path(self.repo, ".revali")))

    def test_is_linked_worktree_tells_the_two_apart(self):
        self.assertFalse(gitops.is_linked_worktree(self.repo))
        linked = self.add_worktree("main")
        self.assertFalse(gitops.is_linked_worktree(self.repo))
        self.assertTrue(gitops.is_linked_worktree(linked))
        # from a subdirectory of each tree as well
        self.assertFalse(gitops.is_linked_worktree(os.path.join(self.repo, "src")))
        self.assertTrue(gitops.is_linked_worktree(os.path.join(linked, "src")))


class LinkedWorktreeFollowUp(WorktreeCase):
    """AC-3: the follow-up after `gh pr merge` from a linked worktree (primary on main)."""

    def setUp(self):
        super().setUp()
        git(["checkout", "-q", "main"], self.repo)
        self.wt = self.add_worktree("feature/mul")
        os.chdir(self.wt)
        self.rdir_wt = self.ready(self.wt)

    def test_success_reports_detached_and_removed(self):
        code, out = run_cli(["merge"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertEqual(self.gh_merges(), [["pr", "merge", "7", "--squash"]])
        self.assertIn("detached at the merged main, local branch feature/mul removed", out)
        self.assertIn("git worktree remove", out)
        self.assertTrue(mentions_path(out, self.repo), out)       # where to `git pull`
        self.assertNotIn("note:", out)
        self.assertEqual(gitops.current_branch(self.wt), "HEAD")
        self.assertIsNone(gitops.rev_parse("feature/mul", self.wt))
        self.assertEqual(gitops.rev_parse("HEAD", self.wt), gitops.rev_parse("origin/main", self.wt))
        self.assertNotIn("feature/mul", self.remote_heads(self.wt))
        self.assertEqual(gitops.current_branch(self.repo), "main")   # the primary tree is untouched
        self.assertFalse(os.path.isfile(tree_lock_path(self.wt, ".revali")))

    def test_failed_fetch_is_reported_and_nothing_is_claimed(self):
        # the remote vanished between the PR merge and the local follow-up
        git(["remote", "set-url", "origin", os.path.join(self.tmp, "gone.git")], self.wt)
        code, out = run_cli(["merge"])
        self.assertEqual(code, EXIT_OK, out)                        # the PR is merged; that is the result
        self.assertIn("MERGED: PR #7 into main", out)
        self.assertIn("note: could not delete origin/feature/mul", out)
        self.assertIn("note: git fetch failed", out)
        self.assertRegex(out, r"note: git fetch failed: \S")        # carries git's own text
        self.assertIn("still on feature/mul", out)
        self.assertIn("local branch feature/mul kept", out)
        self.assertNotIn("detached at", out)
        self.assertNotIn("removed;", out)
        self.assertEqual(gitops.current_branch(self.wt), "feature/mul")
        self.assertIsNotNone(gitops.rev_parse("feature/mul", self.wt))
        self.assertEqual(gitops.current_branch(self.repo), "main")

    def test_failed_checkout_is_reported_and_the_branch_is_kept(self):
        real_run = merge.run

        def refuse_checkout(cmd, **kw):
            if "checkout" in [str(c) for c in cmd]:
                return Result(cmd=list(cmd), returncode=1, stdout="",
                              stderr="error: fake checkout refusal\n", duration=0.0)
            return real_run(cmd, **kw)

        with mock.patch.object(merge, "run", refuse_checkout):
            code, out = run_cli(["merge"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("note: git checkout --detach failed: error: fake checkout refusal", out)
        self.assertIn("still on feature/mul", out)
        self.assertIn("local branch feature/mul kept", out)
        self.assertNotIn("detached at", out)
        self.assertNotIn("git fetch failed", out)                   # the fetch itself went through
        self.assertEqual(gitops.current_branch(self.wt), "feature/mul")
        self.assertIsNotNone(gitops.rev_parse("feature/mul", self.wt))
        self.assertNotIn("feature/mul", self.remote_heads(self.wt))  # the remote delete did happen
        self.assertEqual(gitops.current_branch(self.repo), "main")


class MergeTreeLockRace(RepoCase):
    """AC-4: the tree lock is taken between the pre-check and the merge; losing that race is
    one ERROR line, exit 1, and the branch lock is let go."""

    def ready(self):
        State(repo="owner/repo", branch="feature/mul", base="main", stage="ready_to_merge",
              message="validation 1 passed", last_exit=EXIT_OK, pr_number=7,
              head_sha=gitops.rev_parse("HEAD", self.repo)).save(self.rdir())

    def test_tree_lock_held_is_an_error_line(self):
        self.ready()

        def held(path, branch, pid=None):
            raise TreeLockHeld(4321, "feature/other", "2026-09-04T00:00:00")

        with mock.patch.object(pipeline, "acquire_tree_lock", held), \
             mock.patch.object(merge, "do_merge", side_effect=AssertionError("do_merge must not run")):
            code, out = run_cli(["merge"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertIn("ERROR: a revali run is already in progress in this working tree on branch feature/other "
                      "(pid 4321)", out)
        self.assertIn("revali wait --branch feature/other", out)
        self.assertNotIn("Traceback", out)
        self.assertFalse(os.path.isfile(lock_path(self.rdir())))
        self.assertEqual(State.load(self.rdir()).stage, "ready_to_merge")
        self.assertEqual(self.fake_calls("gh"), [])

    def test_locks_released_once_after_a_stop(self):
        # the `except Stop` branch and `finally` must not both release: a second release of a
        # lock somebody else took in between would remove theirs
        self.ready()
        from revali.preflight import Stop

        def failing(cwd, rdir, state, log):
            raise Stop(EXIT_ERROR, "gh pr merge failed (fake)")

        with mock.patch.object(merge, "do_merge", failing):
            code, out = run_cli(["merge"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertIn("ERROR: gh pr merge failed (fake)", out)
        self.assertFalse(os.path.isfile(lock_path(self.rdir())))
        self.assertFalse(os.path.isfile(tree_lock_path(self.repo, ".revali")))
        self.assertEqual(State.load(self.rdir()).message, "gh pr merge failed (fake)")


if __name__ == "__main__":
    unittest.main()
