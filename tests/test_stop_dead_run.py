"""`revali stop` on a run that died without a result: the state is closed as `stopped`, the
stale lock goes, and nothing the next run needs is touched."""
import contextlib
import io
import json
import os
import unittest
from unittest import mock

from tests.helpers import RepoCase, claude_entry, git, run_cli
from revali import EXIT_ERROR, EXIT_OK
from revali.state import State, lock_path, write_json_atomic


class StopAcknowledgesADeadRun(RepoCase):
    def _dead_state(self, stage="review", last_exit=-1, **fields):
        st = State(branch="feature/mul", base="main", stage=stage, message="reviewer round 1",
                   last_exit=last_exit, **fields)
        st.save(self.rdir())
        return st

    def _state_bytes(self):
        with open(State.path(self.rdir()), "rb") as fh:
            return fh.read()

    def test_a_dead_run_is_recorded_as_stopped(self):                                     # AC-1
        self._dead_state(stage="review")
        code, out = run_cli(["stop"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("dead at stage 'review'", out)
        self.assertIn("recorded as stopped", out)
        self.assertNotIn("no run in progress", out)
        state = State.load(self.rdir())
        self.assertEqual(state.stage, "stopped")
        self.assertEqual(state.last_exit, EXIT_ERROR)
        self.assertIn("dead at stage 'review'", state.message)

    def test_the_stale_lock_is_removed(self):                                             # AC-1
        self._dead_state(stage="validate")
        write_json_atomic(lock_path(self.rdir()), {"pid": 999999999, "since": "x"})
        code, out = run_cli(["stop"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertFalse(os.path.exists(lock_path(self.rdir())))
        self.assertEqual(State.load(self.rdir()).stage, "stopped")

    def test_a_killed_rerun_that_never_recorded_a_stage_counts_too(self):                 # AC-1
        # the rerun reset last_exit but died in preflight: the stage is still the old result's
        self._dead_state(stage="needs_action", last_exit=-1)
        code, out = run_cli(["stop"])
        self.assertEqual(code, EXIT_OK, out)
        state = State.load(self.rdir())
        self.assertEqual(state.stage, "stopped")
        self.assertIn("dead at stage 'needs_action'", state.message)

    def test_a_finished_run_is_left_alone(self):                                          # AC-2
        self._dead_state(stage="needs_action", last_exit=2)
        before = self._state_bytes()
        code, out = run_cli(["stop"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("no run in progress", out)
        self.assertEqual(self._state_bytes(), before)

    def test_a_finished_dry_run_is_left_alone(self):                                      # AC-2
        code, out = run_cli(["run", "--dry-run"])
        self.assertEqual(code, EXIT_OK, out)
        before = self._state_bytes()
        code, out = run_cli(["stop"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("no run in progress", out)
        self.assertEqual(self._state_bytes(), before)
        self.assertEqual(State.load(self.rdir()).stage, "preflight")

    def test_no_state_file_means_no_run_in_progress(self):                                # AC-2
        code, out = run_cli(["stop"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("no run in progress", out)
        self.assertFalse(os.path.exists(State.path(self.rdir())))

    def test_wait_and_status_report_a_stopped_run_afterwards(self):                       # AC-3
        self._dead_state(stage="review")
        run_cli(["stop"])
        code, out = run_cli(["wait", "--timeout", "1s"])
        self.assertEqual(code, EXIT_ERROR)
        self.assertTrue(out.split("\n", 1)[1].startswith("stopped: "), out)
        self.assertNotIn("died at stage", out)
        self.assertNotIn("error:", out)
        code, out = run_cli(["status"])
        self.assertEqual(code, EXIT_OK)
        self.assertIn("stage: stopped", out)
        self.assertNotIn("without a result", out)

    def test_only_the_outcome_fields_change(self):                                        # AC-4
        rounds = [{"round": 1, "verdict": "APPROVE", "head_sha": "a" * 40, "test_commit": "b" * 40}]
        self._dead_state(stage="validate", reviewer_running=True,
                         pending_test_files=["tests/test_review_x.py"], rounds=rounds,
                         validations=[], head_sha="b" * 40, fixes=2, test_files=["tests/test_review_x.py"],
                         cost_usd=1.25)
        with open(State.path(self.rdir()), "r", encoding="utf-8", newline="") as fh:
            before = json.load(fh)
        run_cli(["stop"])
        with open(State.path(self.rdir()), "r", encoding="utf-8", newline="") as fh:
            after = json.load(fh)
        changed = {k for k in before if before[k] != after.get(k)}
        self.assertLessEqual(changed, {"stage", "message", "last_exit", "updated_at"})  # same-second saves keep updated_at
        self.assertLessEqual({"stage", "message", "last_exit"}, changed)
        self.assertEqual(after["stage"], "stopped")
        self.assertTrue(after["reviewer_running"])
        self.assertEqual(after["pending_test_files"], ["tests/test_review_x.py"])
        self.assertEqual(after["rounds"], rounds)
        self.assertEqual(after["fixes"], 2)

    def test_the_next_run_still_cleans_a_stopped_round_leftovers(self):                   # AC-4
        self._dead_state(stage="review", reviewer_running=True)
        self.write("tests/test_review_left.py", "# half written\n")
        run_cli(["stop"])
        self.claude(claude_entry())
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertFalse(self.exists("tests/test_review_left.py"))

    def test_the_next_run_still_resumes_an_approved_round_at_validation(self):            # AC-4
        self.claude(claude_entry())
        with mock.patch("revali.validate.run_validation", side_effect=RuntimeError("power cut")):
            with contextlib.redirect_stderr(io.StringIO()):
                code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR, out)
        state = State.load(self.rdir())
        self.assertEqual([r["verdict"] for r in state.rounds], ["APPROVE"])
        self.assertEqual(git(["rev-parse", "HEAD"], self.repo).strip(), state.test_commits[0])
        # make it a death rather than a recorded error: the handler never got to write
        state.stage, state.last_exit = "validate", -1
        state.save(self.rdir())
        code, out = run_cli(["stop"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertEqual(State.load(self.rdir()).stage, "stopped")
        self.claude()  # nothing left for a reviewer to answer with
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("READY TO MERGE", out)
        self.assertIn("stopped before validation", out)
        self.assertEqual(len(self.fake_calls("claude")), 1)
        state = State.load(self.rdir())
        self.assertEqual(state.stage, "ready_to_merge")
        self.assertEqual(len(state.rounds), 1)
        self.assertEqual([v["round"] for v in state.validations], [1])


if __name__ == "__main__":
    unittest.main()
