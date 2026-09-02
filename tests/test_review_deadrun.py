"""AC-2 and AC-3 of fix/state-write-race: an unexpected exception inside the pipeline is
recorded as stage `error` (exit 1, a history line, the lock released, the traceback in the
logs), while a run that vanished before recording anything is reported by `wait` and
`status` as dead at its stage, with the log path, instead of a stale exit code."""
import contextlib
import io
import os
import unittest
from unittest import mock

from tests.helpers import RepoCase, claude_entry, run_cli
from revali import EXIT_ACTION, EXIT_ERROR, EXIT_OK
from revali.state import State, lock_path, read_history, write_json_atomic


class DeadRunCase(RepoCase):
    def run_log(self):
        return os.path.join(self.rdir(), "logs", "run.log")

    def revali_log(self):
        with open(os.path.join(self.rdir(), "logs", "revali.log"), "r", encoding="utf-8", newline="") as fh:
            return fh.read()

    def dead_state(self, stage, last_exit=-1):
        """What a killed process leaves: a non-terminal stage, no handler ran, no lock."""
        State(branch="feature/mul", base="main", stage=stage, message="previous run at %s" % stage,
              last_exit=last_exit).save(self.rdir())


class UnexpectedErrorIsRecorded(DeadRunCase):
    def test_an_os_error_in_the_review_stage_ends_as_stage_error(self):
        # review-1.md cannot be written when a directory sits at its path: an OSError that is
        # neither Stop nor ConfigError, raised from inside the review stage.
        os.makedirs(os.path.join(self.rdir(), "review-1.md"))
        self.claude(claude_entry())
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR, out)                                                # AC-2: exits 1
        self.assertIn("ERROR:", out)                                                           # AC-2: printed
        self.assertIn("review-1.md", out)                                                      # ... with the exception text
        state = State.load(self.rdir())
        self.assertEqual(state.stage, "error")                                                 # AC-2: stage error
        self.assertEqual(state.last_exit, EXIT_ERROR)
        self.assertIn("review-1.md", state.message)                                            # AC-2: exception text in message
        self.assertFalse(os.path.isfile(lock_path(self.rdir())))                               # AC-2: lock released
        rows = read_history(os.path.join(self.home, "history.jsonl"))
        self.assertEqual(rows[-1]["exit"], EXIT_ERROR)                                         # AC-2: history line
        self.assertEqual(rows[-1]["stage"], "error")
        self.assertIn("Traceback (most recent call last)", err.getvalue())                     # AC-2: traceback to the run log (stderr)
        self.assertIn("Traceback (most recent call last)", self.revali_log())
        # a recorded error is a result: wait and status report it, not a death
        code, out = run_cli(["wait", "--timeout", "1s"])
        self.assertEqual(code, EXIT_ERROR)
        self.assertTrue(out.startswith("error:"), out)
        self.assertNotIn("died", out)
        code, out = run_cli(["status"])
        self.assertEqual(code, EXIT_OK)
        self.assertNotIn("without a result", out)


class VanishedRunIsReported(DeadRunCase):
    def test_wait_and_status_report_the_death_not_the_stale_exit_code(self):
        self.dead_state("review", last_exit=EXIT_ACTION)          # exit 2 left over from an earlier, finished round
        code, out = run_cli(["wait", "--timeout", "1s"])
        self.assertEqual(code, EXIT_ERROR, out)                                                # AC-3: returns 1, not 2
        self.assertIn("died at stage 'review'", out)                                           # AC-3: the stage
        self.assertIn(self.run_log(), out)                                                     # AC-3: where the log is
        code, out = run_cli(["status"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("stage: review", out)
        self.assertIn("without a result", out)                                                 # AC-3: status says the same
        self.assertIn(self.run_log(), out)
        self.assertLess(out.index("stage: review"), out.index("without a result"))             # ... after the stage line
        # a terminal stage is still reported as before
        State(branch="feature/mul", base="main", stage="needs_action", message="changes requested in round 1",
              last_exit=EXIT_ACTION).save(self.rdir())
        code, out = run_cli(["wait", "--timeout", "1s"])
        self.assertEqual(code, EXIT_ACTION, out)
        self.assertNotIn("died", out)
        code, out = run_cli(["status"])
        self.assertNotIn("without a result", out)

    def test_every_non_terminal_stage_counts_as_dead(self):
        for stage in ("pr", "review", "validate"):
            with self.subTest(stage=stage):
                self.dead_state(stage)
                code, out = run_cli(["wait", "--timeout", "1s"])
                self.assertEqual(code, EXIT_ERROR, out)                                        # AC-3
                self.assertIn("died at stage '%s'" % stage, out)
                code, out = run_cli(["status"])
                self.assertIn("stopped at stage '%s' without a result" % stage, out)

    def test_a_stale_lock_changes_nothing_but_is_removed(self):
        self.dead_state("validate")
        write_json_atomic(lock_path(self.rdir()), {"pid": 999999999, "since": "2026-09-01T00:00:00"})
        code, out = run_cli(["status"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertNotIn("running: yes", out)
        self.assertIn("without a result", out)                                                 # AC-3: with a stale lock
        code, out = run_cli(["wait", "--timeout", "1s"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertIn("died at stage 'validate'", out)
        self.assertIn(self.run_log(), out)
        self.assertFalse(os.path.isfile(lock_path(self.rdir())))                               # the stale lock is gone
        code, out = run_cli(["wait", "--timeout", "1s"])
        self.assertEqual(code, EXIT_ERROR, out)                                                # AC-3: and without it
        self.assertIn("died at stage 'validate'", out)

    def test_a_finished_dry_run_is_a_result_but_a_kill_during_preflight_is_not(self):
        code, out = run_cli(["run", "--dry-run"])
        self.assertEqual(code, EXIT_OK, out)
        code, out = run_cli(["wait", "--timeout", "1s"])
        self.assertEqual(code, EXIT_OK, out)                                                   # the dry run's own result
        self.assertNotIn("died", out)
        code, out = run_cli(["status"])
        self.assertNotIn("without a result", out)
        # the next run is killed during preflight: the dry run's exit 0 must not survive it
        self.claude(claude_entry())
        with mock.patch("revali.pipeline.preflight", side_effect=KeyboardInterrupt):
            code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertFalse(os.path.isfile(lock_path(self.rdir())))
        code, out = run_cli(["wait", "--timeout", "1s"])
        self.assertEqual(code, EXIT_ERROR, out)                                                # AC-3: not the stale 0
        self.assertIn("died at stage 'preflight'", out)
        code, out = run_cli(["status"])
        self.assertIn("without a result", out)


if __name__ == "__main__":
    unittest.main()
