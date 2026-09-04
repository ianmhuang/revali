"""Review tests for feature/sandbox-per-branch: `stop` kills the tree lock's live pid whatever
branch it names (AC-3), a lock reserved for a pid that already wrote it is not a conflict
(AC-4), the `wait --branch` hint repeats the branch (AC-5), `merge` holds the tree lock
(AC-6), and `merge` from a linked worktree whose base is checked out elsewhere (AC-9).
"""
import os
import subprocess
import sys
import unittest
from unittest import mock

from tests.helpers import RepoCase, claude_entry, git, run_cli
from revali import EXIT_ERROR, EXIT_OK
from revali import gitops, merge
from revali.preflight import Stop
from revali.state import (LockHeld, State, TreeLockHeld, acquire_lock, acquire_tree_lock, lock_path,
                          read_lock, read_tree_lock, tree_lock_path, write_json_atomic)

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def sleeper(case):
    """A live process to stand in for a detached run; killed at the end if still there."""
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"], start_new_session=True)
    case.addCleanup(lambda: child.poll() is None and child.kill())
    return child


class StopTests(RepoCase):
    """AC-3"""

    def tlock(self):
        return tree_lock_path(self.repo, ".revali")

    def write_tree_lock(self, pid, branch):
        os.makedirs(os.path.dirname(self.tlock()), exist_ok=True)
        write_json_atomic(self.tlock(), {"pid": pid, "branch": branch, "since": "x"})

    def test_same_branch_live_tree_lock_without_a_branch_lock_is_killed(self):
        child = sleeper(self)
        State(repo="me/sample", branch="feature/mul", base="main", stage="validate",
              message="validating", last_exit=-1).save(self.rdir())
        self.write_tree_lock(child.pid, "feature/mul")
        self.assertFalse(os.path.isfile(lock_path(self.rdir())))
        code, out = run_cli(["stop"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("stopped pid %d" % child.pid, out)
        self.assertIsNotNone(child.wait(timeout=10))                    # really gone
        self.assertFalse(os.path.isfile(self.tlock()))                   # removed only after the kill
        state = State.load(self.rdir())
        self.assertEqual(state.stage, "stopped")
        self.assertEqual(state.last_exit, EXIT_ERROR)

    def test_other_branch_live_tree_lock_is_killed_and_its_state_closed(self):
        child = sleeper(self)
        other = os.path.join(self.repo, ".revali", "feature__other")
        State(repo="me/sample", branch="feature/other", base="main", stage="review",
              message="reviewer round 1", last_exit=-1).save(other)
        self.write_tree_lock(child.pid, "feature/other")
        code, out = run_cli(["stop"])   # run from feature/mul
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("branch: feature/other", out)
        self.assertIsNotNone(child.wait(timeout=10))
        self.assertEqual(State.load(other).stage, "stopped")
        self.assertFalse(os.path.isfile(self.tlock()))

    def test_live_tree_lock_with_no_state_is_still_killed_not_dropped(self):
        child = sleeper(self)
        self.write_tree_lock(child.pid, "feature/mul")
        code, out = run_cli(["stop"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("stopped pid %d" % child.pid, out)
        self.assertIsNotNone(child.wait(timeout=10))
        self.assertFalse(os.path.isfile(self.tlock()))

    def test_stale_tree_lock_is_just_removed(self):
        child = sleeper(self)
        pid = child.pid
        child.kill()
        child.wait(timeout=10)
        self.write_tree_lock(pid, "feature/mul")
        code, out = run_cli(["stop"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("no run in progress", out)
        self.assertFalse(os.path.isfile(self.tlock()))


class LockReservationTests(RepoCase):
    """AC-4"""

    def tlock(self):
        return tree_lock_path(self.repo, ".revali")

    def test_child_first_then_parent_reserves(self):
        child = sleeper(self)
        os.makedirs(self.rdir(), exist_ok=True)
        write_json_atomic(lock_path(self.rdir()), {"pid": child.pid, "since": "x"})
        acquire_lock(self.rdir(), pid=child.pid)              # must not raise LockHeld
        self.assertEqual(read_lock(self.rdir())["pid"], child.pid)
        os.makedirs(os.path.dirname(self.tlock()), exist_ok=True)
        write_json_atomic(self.tlock(), {"pid": child.pid, "branch": "feature/mul", "since": "x"})
        acquire_tree_lock(self.tlock(), "feature/mul", pid=child.pid)   # must not raise TreeLockHeld
        self.assertEqual(read_tree_lock(self.tlock())["pid"], child.pid)
        self.assertEqual(read_tree_lock(self.tlock())["branch"], "feature/mul")

    def test_parent_first_then_child_takes_its_own(self):
        # the child's view: the record carries its pid, written by the parent
        os.makedirs(self.rdir(), exist_ok=True)
        write_json_atomic(lock_path(self.rdir()), {"pid": os.getpid(), "since": "x"})
        acquire_lock(self.rdir())
        self.assertEqual(read_lock(self.rdir())["pid"], os.getpid())
        write_json_atomic(self.tlock(), {"pid": os.getpid(), "branch": "feature/mul", "since": "x"})
        acquire_tree_lock(self.tlock(), "feature/mul")
        self.assertEqual(read_tree_lock(self.tlock())["pid"], os.getpid())

    def test_a_foreign_live_pid_is_still_a_conflict(self):
        child = sleeper(self)
        other = sleeper(self)
        os.makedirs(self.rdir(), exist_ok=True)
        write_json_atomic(lock_path(self.rdir()), {"pid": other.pid, "since": "x"})
        with self.assertRaises(LockHeld):
            acquire_lock(self.rdir(), pid=child.pid)
        with self.assertRaises(LockHeld):
            acquire_lock(self.rdir())
        write_json_atomic(self.tlock(), {"pid": other.pid, "branch": "feature/x", "since": "x"})
        with self.assertRaises(TreeLockHeld):
            acquire_tree_lock(self.tlock(), "feature/mul", pid=child.pid)
        with self.assertRaises(TreeLockHeld):
            acquire_tree_lock(self.tlock(), "feature/mul")

    def test_detached_run_starts_without_a_traceback(self):
        self.claude(claude_entry())
        code, out = run_cli(["run"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("started revali run", out)
        self.assertNotIn("Traceback", out)
        self.assertNotIn("LockHeld", out)
        code, out = run_cli(["wait", "--timeout", "120s"])
        self.assertEqual(code, EXIT_OK, out)


class WaitHintTests(RepoCase):
    """AC-5"""

    def hold(self):
        os.makedirs(self.rdir(), exist_ok=True)
        write_json_atomic(lock_path(self.rdir()), {"pid": os.getpid(), "since": "x"})
        self.addCleanup(lambda: os.path.isfile(lock_path(self.rdir())) and os.remove(lock_path(self.rdir())))

    def test_branch_form_repeats_the_branch(self):
        self.hold()
        git(["checkout", "-q", "main"], self.repo)
        code, out = run_cli(["wait", "--branch", "feature/mul", "--timeout", "0.2s"])
        self.assertEqual(code, EXIT_OK + 4, out)
        self.assertIn("still running", out)
        self.assertIn("call `revali wait --branch feature/mul` again", out)

    def test_plain_form_stays_plain(self):
        self.hold()
        code, out = run_cli(["wait", "--timeout", "0.2s"])
        self.assertEqual(code, EXIT_OK + 4, out)
        self.assertIn("call `revali wait` again", out)
        self.assertNotIn("--branch", out)


class MergeTreeLockTests(RepoCase):
    """AC-6"""

    def tlock(self):
        return tree_lock_path(self.repo, ".revali")

    def ready(self):
        State(repo="me/sample", branch="feature/mul", base="main", stage="ready_to_merge",
              message="validation 1 passed", last_exit=EXIT_OK, pr_number=7).save(self.rdir())

    def test_merge_holds_the_tree_lock_and_a_run_elsewhere_is_refused(self):
        self.ready()
        seen = {}

        def fake_merge(cwd, rdir, state, log):
            seen["tree"] = read_tree_lock(self.tlock())
            seen["branch"] = read_lock(rdir)
            git(["checkout", "-q", "main"], self.repo)      # another session, another branch
            try:
                # the foreground path in a process of its own (the lock is keyed by pid)
                proc = subprocess.run([sys.executable, os.path.join(ROOT, "revali.py"), "run", "--dry-run"],
                                      cwd=self.repo, capture_output=True, text=True, encoding="utf-8",
                                      errors="replace", timeout=120)
                seen["fg"] = (proc.returncode, proc.stdout + proc.stderr)
                seen["bg"] = run_cli(["run"])                # the detached path checks before spawning
            finally:
                git(["checkout", "-q", "feature/mul"], self.repo)
            return EXIT_OK

        with mock.patch.object(merge, "do_merge", fake_merge), \
             mock.patch.object(merge, "merge_summary", lambda state, base: "MERGED (fake)"), \
             mock.patch.object(merge, "remove_tree", lambda path: None):
            code, out = run_cli(["merge"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertEqual(seen["tree"]["branch"], "feature/mul")
        self.assertEqual(seen["tree"]["pid"], os.getpid())
        self.assertEqual(seen["branch"]["pid"], os.getpid())
        for key in ("fg", "bg"):
            self.assertEqual(seen[key][0], EXIT_ERROR, seen[key][1])
            self.assertIn("already in progress in this working tree on branch feature/mul", seen[key][1])
        self.assertFalse(os.path.isfile(self.tlock()))           # released on success
        self.assertFalse(os.path.isfile(lock_path(self.rdir())))

    def test_both_locks_released_when_merge_fails(self):
        self.ready()

        def failing(cwd, rdir, state, log):
            self.assertTrue(os.path.isfile(self.tlock()))
            raise Stop(EXIT_ERROR, "gh pr merge failed (fake)")

        with mock.patch.object(merge, "do_merge", failing):
            code, out = run_cli(["merge"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertFalse(os.path.isfile(self.tlock()))
        self.assertFalse(os.path.isfile(lock_path(self.rdir())))

    def test_merge_refused_while_a_run_holds_the_tree(self):
        self.ready()
        child = sleeper(self)
        os.makedirs(os.path.dirname(self.tlock()), exist_ok=True)
        write_json_atomic(self.tlock(), {"pid": child.pid, "branch": "feature/other", "since": "x"})
        with mock.patch.object(merge, "do_merge", side_effect=AssertionError("must not run")):
            code, out = run_cli(["merge"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertIn("ERROR: a run is in progress", out)
        self.assertTrue(os.path.isfile(self.tlock()))            # someone else's, left alone
        self.assertEqual(State.load(self.rdir()).stage, "ready_to_merge")


class WorktreeMergeTests(RepoCase):
    """AC-9: the primary tree keeps main checked out; feature/mul lives in a linked worktree
    and `merge` runs there."""

    def setUp(self):
        super().setUp()
        git(["push", "-q", "-u", "origin", "feature/mul"], self.repo)   # what the pr stage does
        git(["checkout", "-q", "main"], self.repo)
        self.wt = os.path.join(self.tmp, "wt")
        git(["worktree", "add", "--quiet", self.wt, "feature/mul"], self.repo)
        os.chdir(self.wt)

        def drop():
            os.chdir(self.repo)
            subprocess.run(["git", "worktree", "remove", "--force", self.wt], cwd=self.repo, capture_output=True)

        self.addCleanup(drop)

    def wt_rdir(self):
        return os.path.join(self.wt, ".revali", "feature__mul")

    def ready(self):
        State(repo="me/sample", branch="feature/mul", base="main", stage="ready_to_merge",
              message="validation 1 passed", last_exit=EXIT_OK, pr_number=7,
              head_sha=gitops.rev_parse("HEAD", self.wt)).save(self.wt_rdir())

    def remote_heads(self):
        out = git(["ls-remote", "--heads", "origin"], self.repo)
        return sorted(line.split("refs/heads/")[1] for line in out.splitlines())

    def test_merge_from_the_linked_worktree(self):
        self.ready()
        self.assertIn("feature/mul", self.remote_heads())
        code, out = run_cli(["merge"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("MERGED: PR #7 into main", out)
        merges = [c["argv"] for c in self.fake_calls("gh") if c["argv"][:2] == ["pr", "merge"]]
        self.assertEqual(merges, [["pr", "merge", "7", "--squash"]])          # no --delete-branch
        self.assertNotIn("feature/mul", self.remote_heads())                  # remote branch deleted
        self.assertEqual(gitops.current_branch(self.wt), "HEAD")              # detached
        self.assertEqual(gitops.rev_parse("HEAD", self.wt), gitops.rev_parse("origin/main", self.wt))
        self.assertIsNone(gitops.rev_parse("feature/mul", self.wt))           # local branch gone
        self.assertEqual(gitops.current_branch(self.repo), "main")            # primary untouched
        self.assertIn("git worktree remove", out)
        self.assertIn("git pull", out)
        # the hint names the tree holding main; git may print it with the other slash or case
        primary = os.path.normcase(os.path.realpath(self.repo))
        self.assertIn(primary, os.path.normcase(out.replace("/", os.sep)), out)
        self.assertFalse(os.path.isfile(tree_lock_path(self.wt, ".revali")))
        self.assertFalse(os.path.isdir(self.wt_rdir()))

    def test_gh_error_after_the_pr_merged_counts_as_merged(self):
        self.ready()
        self.scenario({"merge_exit": 1,
                       "pr_create": {"number": 7, "url": "https://github.example/me/sample/pull/7",
                                     "state": "MERGED", "isDraft": False}})
        code, out = run_cli(["merge"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("MERGED: PR #7 into main", out)
        self.assertTrue(any(c["argv"][:2] == ["pr", "view"] for c in self.fake_calls("gh")))
        self.assertEqual(gitops.current_branch(self.wt), "HEAD")
        self.assertNotIn("feature/mul", self.remote_heads())

    def test_gh_error_on_an_open_pr_is_an_error(self):
        self.ready()
        self.scenario({"merge_exit": 1,
                       "pr_create": {"number": 7, "url": "https://github.example/me/sample/pull/7",
                                     "state": "OPEN", "isDraft": False}})
        code, out = run_cli(["merge"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertIn("gh pr merge failed", out)
        self.assertEqual(gitops.current_branch(self.wt), "feature/mul")
        self.assertIn("feature/mul", self.remote_heads())
        self.assertEqual(State.load(self.wt_rdir()).stage, "ready_to_merge")
        self.assertFalse(os.path.isfile(tree_lock_path(self.wt, ".revali")))


class PrimaryTreeMergedFallbackTests(RepoCase):
    """AC-9, second sentence, in the ordinary layout: gh fails after GitHub merged the PR."""

    def test_gh_error_after_the_pr_merged_runs_the_local_follow_up(self):
        self.claude(claude_entry())
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        self.scenario({"merge_exit": 1,
                       "pr_create": {"number": 7, "url": "https://github.example/me/sample/pull/7",
                                     "state": "MERGED", "isDraft": False}})
        code, out = run_cli(["merge"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("MERGED: PR #7 into main", out)
        self.assertEqual(gitops.current_branch(self.repo), "main")
        self.assertIsNone(gitops.rev_parse("feature/mul", self.repo))
        self.assertFalse(os.path.isdir(self.rdir()))


if __name__ == "__main__":
    unittest.main()
