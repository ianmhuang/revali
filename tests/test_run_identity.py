"""feature/run-identity: a detached run opens no console windows on Windows (AC-1; since
round 1 F1 this covers run_shell, the lint line and the LocalRunner steps, as well), and
`run`, `wait` and `status` open with an identity line naming the working tree and the
branch (AC-2), keeping the pid in every message that reports a process (AC-3) and the
exit codes and wording that follow (AC-4)."""
import os
import subprocess
import unittest
from unittest import mock

from tests.helpers import RepoCase, claude_entry, run_cli
from revali import EXIT_ACTION, EXIT_ERROR, EXIT_OK
from revali import gitops, procs
from revali.state import State, lock_owner_alive, lock_path, write_json_atomic

NO_WINDOW = 0x08000000


class NoWindowTests(unittest.TestCase):
    """AC-1: procs.run passes CREATE_NO_WINDOW on Windows and no creationflags elsewhere."""

    def _capture(self):
        calls = []

        def fake_run(argv, **kw):
            calls.append(kw)
            return subprocess.CompletedProcess(argv, 0, "", "")

        return calls, fake_run

    def test_windows_passes_create_no_window(self):
        calls, fake_run = self._capture()
        with mock.patch("os.name", "nt"), mock.patch("subprocess.run", fake_run):
            res = procs.run(["git", "--version"])
        self.assertTrue(res.ok)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].get("creationflags", 0) & NO_WINDOW, NO_WINDOW)

    def test_posix_passes_no_creationflags(self):
        calls, fake_run = self._capture()
        with mock.patch("os.name", "posix"), mock.patch("subprocess.run", fake_run):
            procs.run(["git", "--version"])
        self.assertEqual(len(calls), 1)
        self.assertNotIn("creationflags", calls[0])

    def test_run_retry_goes_through_the_same_flags(self):
        calls, fake_run = self._capture()
        with mock.patch("os.name", "nt"), mock.patch("subprocess.run", fake_run):
            procs.run_retry(["gh", "auth", "status"], retries=0)
        self.assertEqual(calls[0].get("creationflags", 0) & NO_WINDOW, NO_WINDOW)

    def test_run_shell_windows_passes_create_no_window(self):
        # the lint line and every LocalRunner step go through run_shell (round 1, F1)
        calls, fake_run = self._capture()
        with mock.patch("os.name", "nt"), mock.patch("subprocess.run", fake_run):
            res = procs.run_shell("echo lint")
        self.assertTrue(res.ok)
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0].get("shell"))
        self.assertEqual(calls[0].get("creationflags", 0) & NO_WINDOW, NO_WINDOW)

    def test_run_shell_posix_passes_no_creationflags(self):
        calls, fake_run = self._capture()
        with mock.patch("os.name", "posix"), mock.patch("subprocess.run", fake_run):
            procs.run_shell("echo lint")
        self.assertNotIn("creationflags", calls[0])


class IdentityLineTests(RepoCase):
    """AC-2 / AC-3 / AC-4: the first line of run / wait / status names the tree and branch."""

    def identity(self):
        return "repo: %s  branch: feature/mul" % gitops.repo_root(self.repo)

    def first_line(self, out):
        return out.splitlines()[0] if out else ""

    def hold_lock(self):
        """A live lock owned by this test process: the run looks in progress."""
        os.makedirs(self.rdir(), exist_ok=True)
        write_json_atomic(lock_path(self.rdir()), {"pid": os.getpid(), "since": "2026-09-04T00:00:00"})
        self.addCleanup(lambda: os.path.isfile(lock_path(self.rdir())) and os.remove(lock_path(self.rdir())))

    def test_status_without_state(self):
        code, out = run_cli(["status"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertEqual(self.first_line(out), self.identity())
        self.assertIn("state: none", out)

    def test_status_with_branch_flag_names_that_branch(self):
        code, out = run_cli(["status", "--branch", "feature/other"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertEqual(self.first_line(out),
                         "repo: %s  branch: feature/other" % gitops.repo_root(self.repo))

    def test_status_running_keeps_the_pid(self):
        self.hold_lock()
        code, out = run_cli(["status"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertEqual(self.first_line(out), self.identity())
        self.assertIn("running: yes (pid %d)" % os.getpid(), out)

    def test_wait_without_run(self):
        code, out = run_cli(["wait", "--timeout", "1s"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertEqual(self.first_line(out), self.identity())
        self.assertIn("no revali run recorded for this branch", out)

    def test_wait_still_running_keeps_the_pid(self):
        self.hold_lock()
        code, out = run_cli(["wait", "--timeout", "1s"])
        self.assertEqual(code, EXIT_OK + 4, out)
        self.assertEqual(self.first_line(out), self.identity())
        self.assertIn("still running (pid %d)" % os.getpid(), out)

    def test_wait_died_keeps_the_pid(self):
        State(branch="feature/mul", base="main", stage="review", message="reviewer round 1",
              last_exit=-1).save(self.rdir())
        write_json_atomic(lock_path(self.rdir()), {"pid": 999999999, "since": "2026-09-04T00:00:00"})
        code, out = run_cli(["wait", "--timeout", "1s"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertEqual(self.first_line(out), self.identity())
        self.assertIn("died at stage 'review'", out)
        self.assertIn("(pid 999999999)", out)

    def test_wait_with_a_result(self):
        State(branch="feature/mul", base="main", stage="needs_action", message="changes requested in round 1",
              last_exit=EXIT_ACTION).save(self.rdir())
        code, out = run_cli(["wait", "--timeout", "1s"])
        self.assertEqual(code, EXIT_ACTION, out)
        self.assertEqual(self.first_line(out), self.identity())
        self.assertIn("needs_action: changes requested in round 1", out)

    def test_run_refused_names_tree_branch_and_pid(self):
        self.hold_lock()
        code, out = run_cli(["run"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertEqual(self.first_line(out), self.identity())
        self.assertIn("already in progress (pid %d)" % os.getpid(), out)

    def test_detached_run_opens_with_the_identity_line(self):
        self.claude(claude_entry())
        code, out = run_cli(["run"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertEqual(self.first_line(out), self.identity())
        self.assertIn("started revali run (pid ", out)
        code, out = run_cli(["wait", "--timeout", "90s"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertEqual(self.first_line(out), self.identity())
        self.assertIn("validation 1 passed", out)
        self.assertIsNone(lock_owner_alive(self.rdir()))

    def test_outside_a_repository_stays_an_error(self):
        os.chdir(self.tmp)
        for argv in (["run"], ["wait", "--timeout", "1s"], ["status"]):
            with self.subTest(argv=argv):
                code, out = run_cli(argv)
                self.assertEqual(code, EXIT_ERROR, out)
                self.assertNotIn("repo:", out)


if __name__ == "__main__":
    unittest.main()
