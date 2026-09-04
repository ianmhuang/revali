"""Review of feature/worktree-docs: AC-8 (`revali stop` works from a detached HEAD through
tree.lock, the other commands keep refusing), AC-9 (taskkill without a console window), and
AC-5 (`review-n.json` carries the reviewed HEAD as `head_sha`)."""
import json
import os
import subprocess
import sys
import unittest
from unittest import mock

from tests.helpers import RepoCase, claude_entry, git, run_cli
from revali import EXIT_ERROR, EXIT_OK
from revali import gitops, procs
from revali.state import State, lock_path, tree_lock_path, write_json_atomic

CREATE_NO_WINDOW = 0x08000000


def live_child(case):
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"], start_new_session=True)
    case.addCleanup(lambda: child.poll() is None and child.kill())
    return child


def dead_pid():
    """The pid of a process that has already exited."""
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


class DetachedStop(RepoCase):
    """AC-8"""

    def tree_lock(self):
        return tree_lock_path(self.repo, ".revali")

    def identity(self, branch):
        return "repo: %s  branch: %s" % (gitops.repo_root(self.repo), branch)

    def test_stop_finds_the_trees_run_through_tree_lock(self):
        child = live_child(self)
        State(repo="owner/repo", branch="feature/mul", base="main", stage="review",
              message="reviewer round 1", last_exit=-1).save(self.rdir())
        write_json_atomic(lock_path(self.rdir()), {"pid": child.pid, "since": "x"})
        write_json_atomic(self.tree_lock(), {"pid": child.pid, "branch": "feature/mul", "since": "x"})
        git(["checkout", "-q", "--detach"], self.repo)
        self.assertEqual(gitops.current_branch(self.repo), "HEAD")

        code, out = run_cli(["stop"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertEqual(out.splitlines()[0], self.identity("feature/mul"))
        self.assertIn("stopped pid %d" % child.pid, out)
        self.assertIsNotNone(child.wait(timeout=10))
        self.assertEqual(State.load(self.rdir()).stage, "stopped")
        self.assertEqual(State.load(self.rdir()).last_exit, EXIT_ERROR)
        self.assertFalse(os.path.isfile(self.tree_lock()))
        self.assertFalse(os.path.isfile(lock_path(self.rdir())))
        self.assertEqual(gitops.current_branch(self.repo), "HEAD")   # stop does not touch the checkout

    def test_stop_with_nothing_running(self):
        git(["checkout", "-q", "--detach"], self.repo)
        code, out = run_cli(["stop"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertEqual(out.splitlines()[0], self.identity("HEAD"))
        self.assertIn("no run in progress", out)
        self.assertNotIn("ERROR", out)
        # no state directory for the pseudo-branch is left behind
        self.assertFalse(os.path.exists(os.path.join(self.repo, ".revali", "HEAD")))

    def test_stop_with_a_stale_tree_lock_reports_no_run(self):
        pid = dead_pid()
        write_json_atomic(self.tree_lock(), {"pid": pid, "branch": "feature/mul", "since": "x"})
        git(["checkout", "-q", "--detach"], self.repo)
        code, out = run_cli(["stop"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("no run in progress", out)
        self.assertFalse(os.path.isfile(self.tree_lock()))   # the stale record is cleared

    def test_the_other_commands_keep_refusing_a_detached_head(self):
        git(["checkout", "-q", "--detach"], self.repo)
        for argv in (["run"], ["run", "--dry-run"], ["wait", "--timeout", "1s"], ["status"], ["reset"], ["merge"]):
            with self.subTest(argv=argv):
                code, out = run_cli(argv)
                self.assertEqual(code, EXIT_ERROR, out)
                self.assertIn("ERROR: detached HEAD; check out a branch first", out)
                self.assertNotIn("Traceback", out)


class TaskkillWindow(unittest.TestCase):
    """AC-9"""

    def test_taskkill_carries_create_no_window(self):
        calls = []

        def fake_run(argv, **kw):
            calls.append((list(argv), kw))
            return subprocess.CompletedProcess(argv, 0, b"", b"")

        with mock.patch("os.name", "nt"), mock.patch.object(procs, "pid_alive", lambda pid: True), \
             mock.patch("subprocess.run", fake_run):
            procs.kill_tree(4242)
        self.assertEqual(len(calls), 1, calls)
        argv, kw = calls[0]
        self.assertEqual(argv, ["taskkill", "/PID", "4242", "/T", "/F"])
        self.assertTrue(kw.get("capture_output"), kw)
        self.assertEqual(kw.get("creationflags", 0) & CREATE_NO_WINDOW, CREATE_NO_WINDOW, kw)

    def test_dead_pid_spawns_nothing(self):
        with mock.patch("os.name", "nt"), mock.patch.object(procs, "pid_alive", lambda pid: False), \
             mock.patch("subprocess.run", side_effect=AssertionError("must not run")):
            procs.kill_tree(4242)


class ReviewJsonHead(RepoCase):
    """AC-5"""

    def test_review_json_has_head_sha_next_to_commit(self):
        self.claude(claude_entry())
        head_before = gitops.rev_parse("HEAD", self.repo)
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        path = os.path.join(self.rdir(), "review-1.json")
        self.assertTrue(os.path.isfile(path), os.listdir(self.rdir()))
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        self.assertEqual(data["head_sha"], head_before)                     # what the reviewer saw
        self.assertEqual(data["commit"], gitops.rev_parse("HEAD", self.repo))  # the test commit on top
        self.assertNotEqual(data["head_sha"], data["commit"])
        state = State.load(self.rdir())
        self.assertEqual(state.rounds[0]["head_sha"], data["head_sha"])
        for key in ("meta", "verdict", "reasons", "data", "test_files", "bounces"):
            self.assertIn(key, data)


if __name__ == "__main__":
    unittest.main()
