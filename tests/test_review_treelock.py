"""Reviewer acceptance tests for feature/tree-guard, AC-1: one revali run per working tree.

`run` takes `<state_dir>/tree.lock` ({pid, branch, since}) next to the branch lock and
releases both however the pipeline ends; a second `run` in the same checkout on another
branch is refused after the identity line with the running branch and pid and the
`wait --branch` hint; a lock whose pid is dead is removed and ignored; a second worktree of
the same repository has its own lock.

Black-box through the CLI on the fixture repository (fake gh / claude / runner, real git).
The tree lock is written directly, as the AC defines it, to stage a busy tree; the only
patches are seams to look at the lock while a foreground run holds it. On the base branch
there is no tree lock, so every test here fails there."""
import json
import os
import subprocess
import sys
import unittest
from unittest import mock

from tests.helpers import RepoCase, claude_entry, git, run_cli
from revali import EXIT_ERROR, EXIT_OK
from revali import gitops, pipeline, review
from revali.preflight import Stop
from revali.state import State, lock_path

DEAD_PID = 999999999            # no such process on any host
TREE_MSG = ("ERROR: a revali run is already in progress in this working tree on branch %s (pid %d); "
            "use `revali wait --branch %s` or `revali stop`")


class TreeLockCase(RepoCase):
    def root(self):
        return gitops.repo_root(self.repo)

    def tree_lock(self, root=None):
        return os.path.join(root or self.root(), ".revali", "tree.lock")

    def hold_tree_lock(self, branch, pid):
        path = self.tree_lock()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            json.dump({"pid": pid, "branch": branch, "since": "2026-09-04T00:00:00"}, fh)

    def read_tree_lock(self):
        path = self.tree_lock()
        if not os.path.isfile(path):
            return None
        with open(path, "r", encoding="utf-8", newline="") as fh:
            return json.load(fh)

    def identity(self, branch="feature/mul", root=None):
        return "repo: %s  branch: %s" % (root or self.root(), branch)

    def live_child(self):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"],
                                 start_new_session=True)
        self.addCleanup(lambda: child.poll() is None and child.kill())
        return child


class ASecondRunInTheSameTreeIsRefused(TreeLockCase):
    def test_detached_run_from_another_branch(self):                                  # AC-1
        self.hold_tree_lock("feature/other", os.getpid())
        code, out = run_cli(["run"])
        self.assertEqual(code, EXIT_ERROR, out)
        lines = out.splitlines()
        self.assertEqual(lines[0], self.identity())
        self.assertEqual(lines[1], TREE_MSG % ("feature/other", os.getpid(), "feature/other"))
        self.assertNotIn("started revali run", out)                                    # nothing was spawned
        self.assertFalse(os.path.isfile(lock_path(self.rdir())))                       # no branch lock reserved
        self.assertIsNone(State.load(self.rdir()))                                     # no state written
        self.assertEqual(self.read_tree_lock()["pid"], os.getpid())                    # the owner's lock is left alone

    def test_foreground_and_dry_run_are_refused_the_same_way(self):                    # AC-1
        child = self.live_child()                                                      # a pid that is not ours
        self.hold_tree_lock("feature/other", child.pid)
        for argv in (["run", "--foreground"], ["run", "--dry-run"]):
            with self.subTest(argv=argv):
                code, out = run_cli(argv)
                self.assertEqual(code, EXIT_ERROR, out)
                lines = out.splitlines()
                self.assertEqual(lines[0], self.identity())
                self.assertEqual(lines[1], TREE_MSG % ("feature/other", child.pid, "feature/other"))
                self.assertNotIn("DRY RUN OK", out)
                self.assertFalse(os.path.isfile(lock_path(self.rdir())))               # the branch lock is let go
                self.assertEqual(self.read_tree_lock()["pid"], child.pid)

    def test_a_dead_owner_is_removed_and_ignored(self):                                # AC-1
        self.hold_tree_lock("feature/other", DEAD_PID)
        code, out = run_cli(["run", "--dry-run"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("DRY RUN OK", out)
        self.assertNotIn("already in progress", out)
        self.assertFalse(os.path.isfile(self.tree_lock()))                             # stale file gone, own lock released


class TheRunHoldsAndReleasesBothLocks(TreeLockCase):
    def test_held_while_the_reviewer_runs_and_released_at_the_end(self):              # AC-1
        self.claude(claude_entry())
        seen = {}
        real = review.spawn_reviewer

        def look_then_spawn(ctx, *a, **kw):
            seen["tree"] = self.read_tree_lock()
            seen["branch_lock"] = os.path.isfile(lock_path(self.rdir()))
            return real(ctx, *a, **kw)

        with mock.patch.object(review, "spawn_reviewer", side_effect=look_then_spawn):
            code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIsNotNone(seen.get("tree"), "tree.lock was not held while the reviewer ran")
        self.assertEqual(seen["tree"]["pid"], os.getpid())                             # the foreground run's pid
        self.assertEqual(seen["tree"]["branch"], "feature/mul")
        self.assertIn("since", seen["tree"])
        self.assertTrue(seen["branch_lock"])                                           # together with the branch lock
        self.assertFalse(os.path.isfile(self.tree_lock()))                             # released ...
        self.assertFalse(os.path.isfile(lock_path(self.rdir())))                       # ... both

    def test_released_when_the_pipeline_stops_early(self):                             # AC-1
        seen = {}

        def look_then_stop(*a, **kw):
            seen["tree"] = self.read_tree_lock()
            raise Stop(EXIT_ERROR, "stopped by the test during preflight")

        with mock.patch.object(pipeline, "preflight", side_effect=look_then_stop):
            code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertIn("stopped by the test during preflight", out)
        self.assertIsNotNone(seen.get("tree"), "tree.lock was not held during preflight")
        self.assertEqual(seen["tree"]["pid"], os.getpid())
        self.assertFalse(os.path.isfile(self.tree_lock()))                             # whichever way it ends
        self.assertFalse(os.path.isfile(lock_path(self.rdir())))


class ASecondWorktreeIsIndependent(TreeLockCase):
    def test_the_other_worktree_has_its_own_lock(self):                                # AC-1
        child = self.live_child()                                                      # a run that is not this process
        self.hold_tree_lock("feature/mul", child.pid)                                  # the primary tree is busy
        wt = os.path.join(self.tmp, "wt")
        git(["worktree", "add", "--quiet", "-b", "feature/wt", wt, "feature/mul"], self.repo)

        def drop_worktree():
            os.chdir(self.repo)                                                        # not from inside it
            subprocess.run(["git", "worktree", "remove", "--force", wt], cwd=self.repo, capture_output=True)

        self.addCleanup(drop_worktree)
        with open(self.change_md(), "r", encoding="utf-8", newline="") as fh:
            doc = fh.read()
        target = os.path.join(wt, ".revali", "feature__wt", "change.md")
        os.makedirs(os.path.dirname(target))
        with open(target, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(doc)

        code, out = run_cli(["run", "--dry-run"])                                      # the primary tree: refused
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertIn(TREE_MSG % ("feature/mul", child.pid, "feature/mul"), out)

        os.chdir(wt)                                                                   # the second worktree: not
        wt_root = gitops.repo_root(wt)
        code, out = run_cli(["run", "--dry-run"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertEqual(out.splitlines()[0], self.identity("feature/wt", root=wt_root))
        self.assertIn("DRY RUN OK", out)
        self.assertNotIn("already in progress", out)
        self.assertFalse(os.path.isfile(self.tree_lock(wt_root)))                      # its own lock, released
        self.assertEqual(self.read_tree_lock()["branch"], "feature/mul")               # the primary's is untouched


if __name__ == "__main__":
    unittest.main()
