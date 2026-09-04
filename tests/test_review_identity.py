"""Reviewer tests for feature/run-identity, AC-2 to AC-4: `run`, `wait` and `status` open
with `repo: <working tree root>  branch: <branch>` (AC-2), every line that reports a live or
dead run still carries its pid (AC-3), and the exit code and the wording after the identity
line are the ones the base branch printed (AC-4).

Black-box through the CLI on the fixture repository (fake gh / claude / runner, real git).
The lock and state files are written directly to stage each `wait` / `status` outcome
without running a pipeline. On the base branch the first line is never the identity line,
so every AC-2 assertion fails there."""
import os
import time
import unittest

from tests.helpers import RepoCase, claude_entry, run_cli
from revali import EXIT_ACTION, EXIT_ERROR, EXIT_OK, NAME, VERSION
from revali import gitops
from revali.config import paths_for
from revali.state import State, lock_owner_alive, lock_path, write_json_atomic

EXIT_STILL_RUNNING = 4          # `wait` only, fixed by CONVENTIONS.md
DEAD_PID = 999999999            # no such process on any host


class IdentityCase(RepoCase):
    def root(self):
        return gitops.repo_root(self.repo)

    def identity(self, branch="feature/mul"):
        return "repo: %s  branch: %s" % (self.root(), branch)

    def lines(self, out):
        return out.splitlines()

    def live_lock(self):
        """This test process owns the lock: revali sees a run in progress."""
        os.makedirs(self.rdir(), exist_ok=True)
        write_json_atomic(lock_path(self.rdir()), {"pid": os.getpid(), "since": "2026-09-04T00:00:00"})
        self.addCleanup(self.drop_lock)

    def drop_lock(self):
        path = lock_path(self.rdir())
        if os.path.isfile(path):
            os.remove(path)

    def dead_lock(self):
        write_json_atomic(lock_path(self.rdir()), {"pid": DEAD_PID, "since": "2026-09-04T00:00:00"})

    def state(self, stage, message, last_exit):
        State(branch="feature/mul", base="main", stage=stage, message=message,
              last_exit=last_exit).save(self.rdir())


class StatusOpensWithTheIdentityLine(IdentityCase):
    def test_no_state(self):                                                          # AC-2, AC-4
        code, out = run_cli(["status"])
        self.assertEqual(code, EXIT_OK, out)
        lines = self.lines(out)
        self.assertEqual(lines[0], self.identity())
        self.assertEqual(lines[1], "%s %s" % (NAME, VERSION))                        # the old first line, unchanged
        self.assertIn("branch: feature/mul", lines)
        self.assertIn("state: none", lines)
        self.assertEqual(out.count("repo: "), 1)                                      # printed once, not per section

    def test_branch_flag_names_that_branch(self):                                     # AC-2
        code, out = run_cli(["status", "--branch", "feature/other"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertEqual(self.lines(out)[0], self.identity("feature/other"))
        self.assertIn("branch: feature/other", out)

    def test_running_keeps_the_pid(self):                                             # AC-2, AC-3
        self.live_lock()
        code, out = run_cli(["status"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertEqual(self.lines(out)[0], self.identity())
        self.assertIn("running: yes (pid %d)" % os.getpid(), self.lines(out))

    def test_with_a_recorded_result(self):                                            # AC-2, AC-4
        self.state("needs_action", "changes requested in round 1", EXIT_ACTION)
        code, out = run_cli(["status"])
        self.assertEqual(code, EXIT_OK, out)
        lines = self.lines(out)
        self.assertEqual(lines[0], self.identity())
        self.assertIn("stage: needs_action", lines)
        self.assertIn("message: changes requested in round 1", lines)

    def test_dead_run_wording_is_unchanged(self):                                     # AC-4
        self.state("review", "reviewer round 1", -1)
        code, out = run_cli(["status"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertEqual(self.lines(out)[0], self.identity())
        self.assertIn("running: no; the last run stopped at stage 'review' without a result", out)


class WaitOpensWithTheIdentityLine(IdentityCase):
    def test_no_run_recorded(self):                                                   # AC-2, AC-4
        code, out = run_cli(["wait", "--timeout", "1s"])
        self.assertEqual(code, EXIT_ERROR, out)
        lines = self.lines(out)
        self.assertEqual(lines[0], self.identity())
        self.assertEqual(lines[1], "no revali run recorded for this branch")

    def test_still_running_keeps_the_pid_and_exit_4(self):                            # AC-2, AC-3, AC-4
        self.live_lock()
        start = time.monotonic()
        code, out = run_cli(["wait", "--timeout", "1s"])
        self.assertEqual(code, EXIT_STILL_RUNNING, out)
        self.assertGreaterEqual(time.monotonic() - start, 0.9)                        # it did wait for the timeout
        lines = self.lines(out)
        self.assertEqual(lines[0], self.identity())
        self.assertTrue(lines[1].startswith("still running (pid %d), stage starting" % os.getpid()), out)
        self.assertIn("call `revali wait` again", lines[1])

    def test_still_running_with_a_stage_on_disk(self):                                # AC-3, AC-4
        self.state("review", "reviewer round 1", -1)
        self.live_lock()
        code, out = run_cli(["wait", "--timeout", "1s"])
        self.assertEqual(code, EXIT_STILL_RUNNING, out)
        self.assertEqual(self.lines(out)[0], self.identity())
        self.assertIn("still running (pid %d), stage review" % os.getpid(), out)

    def test_died_keeps_the_pid_from_the_stale_lock(self):                            # AC-2, AC-3, AC-4
        self.state("review", "reviewer round 1", -1)
        self.dead_lock()
        code, out = run_cli(["wait", "--timeout", "1s"])
        self.assertEqual(code, EXIT_ERROR, out)
        lines = self.lines(out)
        self.assertEqual(lines[0], self.identity())
        self.assertTrue(lines[1].startswith("error: the run (pid %d) died at stage 'review' without a result"
                                            % DEAD_PID), out)
        self.assertFalse(os.path.isfile(lock_path(self.rdir())))                     # the stale lock still goes

    def test_died_without_a_lock_has_no_pid_to_show(self):                            # AC-4
        self.state("validate", "validating", -1)
        code, out = run_cli(["wait", "--timeout", "1s"])
        self.assertEqual(code, EXIT_ERROR, out)
        lines = self.lines(out)
        self.assertEqual(lines[0], self.identity())
        self.assertTrue(lines[1].startswith("error: the run died at stage 'validate' without a result"), out)

    def test_result_needs_action(self):                                               # AC-2, AC-4
        self.state("needs_action", "changes requested in round 1 (2 findings)", EXIT_ACTION)
        code, out = run_cli(["wait", "--timeout", "1s"])
        self.assertEqual(code, EXIT_ACTION, out)
        lines = self.lines(out)
        self.assertEqual(lines[0], self.identity())
        self.assertEqual(lines[1], "needs_action: changes requested in round 1 (2 findings)")

    def test_result_ready_to_merge(self):                                             # AC-2, AC-4
        self.state("ready_to_merge", "validation 1 passed", EXIT_OK)
        code, out = run_cli(["wait", "--timeout", "1s"])
        self.assertEqual(code, EXIT_OK, out)
        lines = self.lines(out)
        self.assertEqual(lines[0], self.identity())
        self.assertEqual(lines[1], "ready_to_merge: validation 1 passed")

    def test_result_error(self):                                                      # AC-2, AC-4
        self.state("error", "configuration: bad key", EXIT_ERROR)
        code, out = run_cli(["wait", "--timeout", "1s"])
        self.assertEqual(code, EXIT_ERROR, out)
        lines = self.lines(out)
        self.assertEqual(lines[0], self.identity())
        self.assertEqual(lines[1], "error: configuration: bad key")


class RunOpensWithTheIdentityLine(IdentityCase):
    def test_refusal_names_tree_branch_and_pid(self):                                 # AC-2, AC-3, AC-4
        self.live_lock()
        code, out = run_cli(["run"])
        self.assertEqual(code, EXIT_ERROR, out)
        lines = self.lines(out)
        self.assertEqual(lines[0], self.identity())
        self.assertEqual(lines[1], "ERROR: a revali run is already in progress (pid %d); "
                                   "use `revali wait` or `revali stop`" % os.getpid())
        self.assertNotIn("started revali run", out)

    def test_detached_start_and_the_child_log(self):                                  # AC-2, AC-3, AC-4
        self.claude(claude_entry())
        code, out = run_cli(["run"])
        self.assertEqual(code, EXIT_OK, out)
        lines = self.lines(out)
        self.assertEqual(lines[0], self.identity())
        self.assertTrue(lines[1].startswith("started revali run (pid "), out)
        self.assertIn("; log: ", lines[1])
        self.assertIn("next: `revali wait --timeout 9m`", out)
        code, out = run_cli(["wait", "--timeout", "90s"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertEqual(self.lines(out)[0], self.identity())
        self.assertIn("ready_to_merge: validation 1 passed", out)
        self.assertIsNone(lock_owner_alive(self.rdir()))
        # the foreground child writes its stdout to run.log; it opens with the same line
        log_path = os.path.join(self.rdir(), paths_for(self.root()).logs_dir, "run.log")
        text = ""
        for _ in range(100):                                                          # the child may still be flushing
            if os.path.isfile(log_path):
                with open(log_path, "r", encoding="utf-8", errors="replace") as fh:
                    text = fh.read()
                if text.strip():
                    break
            time.sleep(0.1)
        self.assertEqual(text.splitlines()[0], self.identity(), text[:300])

    def test_foreground_dry_run_opens_with_the_identity_line(self):                   # AC-2, AC-4
        code, out = run_cli(["run", "--dry-run"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertEqual(self.lines(out)[0], self.identity())
        self.assertIn("DRY RUN OK:", out)


class OutsideARepository(IdentityCase):
    def test_each_command_stays_an_error_without_an_identity_line(self):              # AC-4
        os.chdir(self.tmp)
        for argv in (["run"], ["wait", "--timeout", "1s"], ["status"]):
            with self.subTest(argv=argv):
                code, out = run_cli(argv)
                self.assertEqual(code, EXIT_ERROR, out)
                self.assertIn("not inside a git repository", out)
                self.assertNotIn("repo: ", out)


if __name__ == "__main__":
    unittest.main()
