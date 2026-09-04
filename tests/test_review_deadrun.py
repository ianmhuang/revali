"""AC-2 and AC-3 of fix/state-write-race: an unexpected exception inside the pipeline is
recorded as stage `error` (exit 1, a history line, the lock released, the traceback in the
logs), while a run that vanished before recording anything is reported by `wait` and
`status` as dead at its stage, with the log path, instead of a stale exit code. When the
state file itself is what cannot be written, the crash handler records the error as soon
as the file is released, and still ends the run with `ERROR:` lines and exit 1 when it
never is (round 1, F2). A run that dies before recording its first stage is a death even
when the stage on disk is a previous run's terminal one, and the handler's message says
which case it is in (round 2, F1); a `Stop` whose record is blocked keeps its exit code."""
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
        self.assertTrue(out.split("\n", 1)[1].startswith("error:"), out)
        self.assertNotIn("died", out)
        code, out = run_cli(["status"])
        self.assertEqual(code, EXIT_OK)
        self.assertNotIn("without a result", out)


class DenyStateJson:
    """os.replace that refuses to rename over state.json: every attempt of the first such
    write only (`first_write_only`, the reader lets go before the crash handler writes), or
    every attempt of every write (the reader never lets go)."""

    def __init__(self, first_write_only):
        self.first_write_only = first_write_only
        self.real = os.replace
        self.first_src = None
        self.denied = 0

    def __call__(self, src, dst):
        if os.path.basename(dst) == "state.json":
            if self.first_src is None:
                self.first_src = src
            if not self.first_write_only or src == self.first_src:
                self.denied += 1
                raise PermissionError(13, "Access is denied")
        self.real(src, dst)


class StateFileCannotBeWritten(DeadRunCase):
    """The failure that motivated the change, at its worst: the write that fails is the state
    file's own, so the crash handler has nothing safe to write to either."""

    def setUp(self):
        super().setUp()
        self.write("revali.toml", self.read("revali.toml") + "\n[paths]\nwrite_retry_s = 0.05\n")
        self.commit_all("short state write window")

    def run_with(self, replace):
        self.claude(claude_entry())
        err = io.StringIO()
        with mock.patch("revali.state.os.replace", replace):
            with contextlib.redirect_stderr(err):
                code, out = run_cli(["run", "--foreground"])                 # returns: nothing escapes to the caller
        return code, out, err.getvalue()

    def test_the_handler_records_the_error_once_the_reader_lets_go(self):
        replace = DenyStateJson(first_write_only=True)
        code, out, err = self.run_with(replace)
        self.assertEqual(code, EXIT_ERROR, out)                                                # AC-2: exits 1
        self.assertIn("ERROR: the run stopped", out)                                           # AC-2: ERROR printed
        self.assertIn("PermissionError", out)
        self.assertNotIn("could not be updated", out)                                          # the handler's write landed
        self.assertGreater(replace.denied, 1)                                                  # AC-1: retried before giving up
        state = State.load(self.rdir())
        self.assertEqual(state.stage, "error")                                                 # AC-2: stage error recorded
        self.assertEqual(state.last_exit, EXIT_ERROR)
        self.assertIn("PermissionError", state.message)                                        # AC-2: exception text
        self.assertFalse(os.path.isfile(lock_path(self.rdir())))                               # AC-2: lock released
        rows = read_history(os.path.join(self.home, "history.jsonl"))
        self.assertEqual(rows[-1]["exit"], EXIT_ERROR)                                         # AC-2: history line
        self.assertEqual(rows[-1]["stage"], "error")
        self.assertIn("PermissionError", err)                                                  # AC-2: traceback kept
        self.assertIn("PermissionError", self.revali_log())
        self.assertEqual(self.fake_calls("claude"), [])                                        # no reviewer was paid for
        code, out = run_cli(["wait", "--timeout", "1s"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertTrue(out.split("\n", 1)[1].startswith("error:"), out)                                         # the recorded result, not a death
        self.assertNotIn("died", out)

    def test_a_state_file_that_never_frees_still_ends_with_error_lines_and_exit_1(self):
        replace = DenyStateJson(first_write_only=False)
        code, out, err = self.run_with(replace)
        self.assertEqual(code, EXIT_ERROR, out)                                                # AC-2: exits 1, no raw traceback escapes
        self.assertIn("ERROR: the run stopped", out)                                           # AC-2: ERROR printed
        self.assertIn("ERROR:", out.split("ERROR: the run stopped", 1)[1])                     # ... and the failed record is said, too
        self.assertIn("could not be updated", out)
        self.assertIn("before its first stage", out)                                           # round 2 F1: no stage was reached
        self.assertNotIn("last recorded stage", out)
        self.assertIn("previous run's result", out)                                            # ... and what wait will show
        self.assertNotIn("as dead", out)
        self.assertFalse(os.path.isfile(lock_path(self.rdir())))                               # AC-2: lock released
        self.assertIsNone(State.load(self.rdir()))                                             # nothing could be recorded
        self.assertIn("PermissionError", err)                                                  # AC-2: traceback to the run log
        self.assertIn("could not be updated", self.revali_log())
        self.assertEqual(self.fake_calls("claude"), [])
        code, out = run_cli(["wait", "--timeout", "1s"])
        self.assertEqual(code, EXIT_ERROR, out)                                                # no stale success either way

    def test_a_blocked_first_save_leaves_the_previous_result_and_says_so(self):
        # round 2 F1: the previous run finished (exit 2); this run's very first write is what
        # fails, so nothing of it lands, and the note must promise the previous result, not a death
        self.dead_state("needs_action", last_exit=EXIT_ACTION)
        code, out, err = self.run_with(DenyStateJson(first_write_only=False))
        self.assertEqual(code, EXIT_ERROR, out)                                                # AC-2: this run exits 1
        self.assertIn("ERROR: the run stopped with PermissionError", out)
        self.assertIn("before its first stage", out)                                           # not "stage 'needs_action'"
        self.assertNotIn("needs_action", out.split("could not be updated", 1)[0])
        self.assertIn("still show the previous run's result", out)
        self.assertFalse(os.path.isfile(lock_path(self.rdir())))
        state = State.load(self.rdir())
        self.assertEqual((state.stage, state.last_exit), ("needs_action", EXIT_ACTION))        # untouched on disk
        code, out = run_cli(["wait", "--timeout", "1s"])
        self.assertEqual(code, EXIT_ACTION, out)                                               # the note was true
        self.assertTrue(out.split("\n", 1)[1].startswith("needs_action:"), out)
        self.assertNotIn("died", out)
        code, out = run_cli(["status"])
        self.assertNotIn("without a result", out)

    def test_a_crash_after_the_start_mark_names_the_last_recorded_stage(self):
        self.claude(claude_entry())
        err = io.StringIO()
        with mock.patch("revali.review.run_round", side_effect=RuntimeError("boom in review")):
            with contextlib.redirect_stderr(err):
                code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertIn("boom in review", out)                                                   # AC-2: the exception text
        self.assertIn("last recorded stage 'review'", out)                                     # round 2 F1: the stage it reached
        self.assertNotIn("before its first stage", out)
        state = State.load(self.rdir())
        self.assertEqual(state.stage, "error")
        self.assertIn("boom in review", state.message)
        self.assertIn("'review'", state.message)


def deny_state_json_after(n):
    """os.replace that lets the first `n` state.json writes through and refuses every later one
    (a reader that arrives after the run's start mark and never lets go)."""
    real = os.replace
    seen = []

    def replace(src, dst):
        if os.path.basename(dst) == "state.json":
            if src not in seen:
                seen.append(src)
            if len(seen) > n:
                raise PermissionError(13, "Access is denied")
        real(src, dst)
    return replace


class StopWithABlockedRecord(DeadRunCase):
    """A refusal (Stop) whose own record cannot be written: the exit code and message still
    reach the caller, nothing escapes as a traceback, and `wait` reports a death because the
    start mark did land."""

    def setUp(self):
        super().setUp()
        self.write("revali.toml", self.read("revali.toml") + "\n[paths]\nwrite_retry_s = 0.05\n")
        self.commit_all("short state write window")

    def test_a_refused_dirty_tree_keeps_its_exit_code_and_reads_as_dead(self):
        self.write("src/calc.py", self.read("src/calc.py") + "\n# uncommitted\n")
        self.claude(claude_entry())
        err = io.StringIO()
        with mock.patch("revali.state.os.replace", deny_state_json_after(1)):
            with contextlib.redirect_stderr(err):
                code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR, out)                                                # the Stop's own code
        self.assertIn("not clean", out)                                                        # the Stop's own message
        self.assertIn("could not be updated", out)
        self.assertIn("as dead", out)                                                          # the start mark is on disk
        self.assertNotIn("Traceback", err.getvalue())                                          # not the catch-all path
        self.assertNotIn("Traceback", out)
        self.assertFalse(os.path.isfile(lock_path(self.rdir())))
        self.assertEqual(self.fake_calls("claude"), [])
        state = State.load(self.rdir())
        self.assertEqual(state.last_exit, -1)                                                  # only the start mark landed
        code, out = run_cli(["wait", "--timeout", "1s"])
        self.assertEqual(code, EXIT_ERROR, out)                                                # AC-3: a death, exit 1
        self.assertIn("without a result", out)
        code, out = run_cli(["status"])
        self.assertIn("without a result", out)


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

    def test_a_kill_during_preflight_after_a_finished_round_is_a_death(self):
        # round 2 F1: the stage on disk is the previous run's terminal one; its exit 2 must not
        # come back from `wait` once a new run started and vanished
        self.dead_state("needs_action", last_exit=EXIT_ACTION)
        code, out = run_cli(["wait", "--timeout", "1s"])
        self.assertEqual(code, EXIT_ACTION, out)                                               # before: the recorded result
        self.claude(claude_entry())
        with mock.patch("revali.pipeline.preflight", side_effect=KeyboardInterrupt):
            code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertFalse(os.path.isfile(lock_path(self.rdir())))
        state = State.load(self.rdir())
        self.assertEqual(state.last_exit, -1)                                                  # a run began, no result
        self.assertEqual(state.stage, "needs_action")                                          # the stage is not rewritten
        code, out = run_cli(["wait", "--timeout", "1s"])
        self.assertEqual(code, EXIT_ERROR, out)                                                # AC-3: not the stale 2
        self.assertIn("without a result", out)
        self.assertIn(self.run_log(), out)
        code, out = run_cli(["status"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("without a result", out)
        # and a stale lock from that run changes nothing but is removed
        write_json_atomic(lock_path(self.rdir()), {"pid": 999999999, "since": "2026-09-01T00:00:00"})
        code, out = run_cli(["wait", "--timeout", "1s"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertIn("without a result", out)
        self.assertFalse(os.path.isfile(lock_path(self.rdir())))


if __name__ == "__main__":
    unittest.main()
