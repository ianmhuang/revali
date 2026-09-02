"""Reviewer acceptance tests for fix/stop-dead-run: `revali stop` on a run that died without
a result records stage `stopped` (exit 1), removes a stale lock, and leaves everything the
next `run` needs; a state that already holds a result, or no state, still gets
`no run in progress`; a live run is stopped as before; the README says so."""
import contextlib
import io
import json
import os
import subprocess
import sys
import unittest
from unittest import mock

from tests.helpers import RepoCase, claude_entry, git, run_cli
from revali import EXIT_ACTION, EXIT_ERROR, EXIT_OK
from revali.state import State, acquire_lock, lock_path, write_json_atomic

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEAD_PID = 999999999


class StopDeadCase(RepoCase):
    def dead_state(self, stage="review", last_exit=-1, **fields):
        """What a killed process leaves behind: the start mark (last_exit -1) or, for a state
        file from before that mark existed, a non-terminal stage with an old exit code."""
        State(branch="feature/mul", base="main", stage=stage, message="reviewer round 1",
              last_exit=last_exit, **fields).save(self.rdir())

    def state_json(self):
        with open(State.path(self.rdir()), "r", encoding="utf-8", newline="") as fh:
            return json.load(fh)

    def state_bytes(self):
        with open(State.path(self.rdir()), "rb") as fh:
            return fh.read()


class DeadRunIsRecordedAsStopped(StopDeadCase):
    def test_stop_marks_a_dead_run_stopped_with_exit_1(self):
        self.dead_state(stage="review")
        code, out = run_cli(["stop"])
        self.assertEqual(code, EXIT_OK, out)                                                   # AC-1: returns 0
        self.assertIn("'review'", out)                                                         # AC-1: names the stage
        self.assertIn("stopped", out)                                                          # AC-1: says it marked it
        self.assertNotIn("no run in progress", out)
        state = State.load(self.rdir())
        self.assertEqual(state.stage, "stopped")                                               # AC-1: stage stopped
        self.assertEqual(state.last_exit, EXIT_ERROR)                                          # AC-1: exit 1
        self.assertIn("'review'", state.message)                                               # AC-1: message names the stage
        self.assertFalse(os.path.isfile(lock_path(self.rdir())))

    def test_every_non_terminal_stage_is_closed(self):
        for stage in ("pr", "review", "validate"):
            with self.subTest(stage=stage):
                self.dead_state(stage=stage)
                code, out = run_cli(["stop"])
                self.assertEqual(code, EXIT_OK, out)
                state = State.load(self.rdir())
                self.assertEqual(state.stage, "stopped")                                       # AC-1
                self.assertEqual(state.last_exit, EXIT_ERROR)
                self.assertIn("'%s'" % stage, state.message)

    def test_a_stale_lock_is_removed_and_the_run_marked(self):
        self.dead_state(stage="validate")
        write_json_atomic(lock_path(self.rdir()), {"pid": DEAD_PID, "since": "2026-09-01T00:00:00"})
        code, out = run_cli(["stop"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertFalse(os.path.isfile(lock_path(self.rdir())))                               # AC-1: stale lock gone
        self.assertEqual(State.load(self.rdir()).stage, "stopped")                             # AC-1: and still marked
        self.assertNotIn("stopped pid", out)                                                   # nothing was killed

    def test_a_run_killed_during_preflight_after_a_finished_round_is_closed(self):
        # the start mark landed, the stage on disk is still the previous run's terminal one
        self.dead_state(stage="needs_action", last_exit=-1)
        code, out = run_cli(["stop"])
        self.assertEqual(code, EXIT_OK, out)
        state = State.load(self.rdir())
        self.assertEqual(state.stage, "stopped")                                               # AC-1: run_died says dead
        self.assertEqual(state.last_exit, EXIT_ERROR)
        self.assertIn("'needs_action'", state.message)

    def test_a_pre_start_mark_state_at_a_non_terminal_stage_is_closed(self):
        # a state file from before last_exit -1 existed: an old exit code at a non-terminal stage
        self.dead_state(stage="review", last_exit=EXIT_ACTION)
        code, out = run_cli(["stop"])
        self.assertEqual(code, EXIT_OK, out)
        state = State.load(self.rdir())
        self.assertEqual(state.stage, "stopped")                                               # AC-1
        self.assertEqual(state.last_exit, EXIT_ERROR)                                          # not the stale 2


class StatesWithAResultAreLeftAlone(StopDeadCase):
    def test_a_terminal_stage_with_a_result_is_not_rewritten(self):
        for stage, code_ in (("needs_action", EXIT_ACTION), ("error", EXIT_ERROR), ("ready_to_merge", EXIT_OK)):
            with self.subTest(stage=stage):
                self.dead_state(stage=stage, last_exit=code_)
                before = self.state_bytes()
                code, out = run_cli(["stop"])
                self.assertEqual(code, EXIT_OK, out)                                           # AC-2: returns 0
                self.assertIn("no run in progress", out)                                       # AC-2: the old answer
                self.assertEqual(self.state_bytes(), before)                                   # AC-2: not rewritten

    def test_a_finished_dry_run_is_not_rewritten(self):
        code, out = run_cli(["run", "--dry-run"])
        self.assertEqual(code, EXIT_OK, out)
        before = self.state_bytes()
        code, out = run_cli(["stop"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("no run in progress", out)                                               # AC-2: dry run is a result
        self.assertEqual(self.state_bytes(), before)
        self.assertEqual(State.load(self.rdir()).stage, "preflight")

    def test_no_state_file_is_no_run_in_progress(self):
        code, out = run_cli(["stop"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("no run in progress", out)                                               # AC-2
        self.assertFalse(os.path.exists(State.path(self.rdir())))                              # nothing created

    def test_a_second_stop_after_the_mark_changes_nothing(self):
        self.dead_state(stage="review")
        run_cli(["stop"])
        before = self.state_bytes()
        code, out = run_cli(["stop"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("no run in progress", out)                                               # AC-2: stopped is a result
        self.assertEqual(self.state_bytes(), before)


class WaitAndStatusAfterTheMark(StopDeadCase):
    def test_wait_reports_a_stopped_run_not_a_death(self):
        self.dead_state(stage="review")
        code, out = run_cli(["wait", "--timeout", "1s"])
        self.assertIn("died at stage", out)                                                    # before: a death
        run_cli(["stop"])
        code, out = run_cli(["wait", "--timeout", "1s"])
        self.assertEqual(code, EXIT_ERROR, out)                                                # AC-3: returns 1
        self.assertTrue(out.startswith("stopped: "), out)                                      # AC-3: the stage and message
        self.assertIn("'review'", out)
        self.assertNotIn("died at stage", out)                                                 # AC-3: no death wording
        self.assertNotIn("without a result", out)

    def test_status_no_longer_prints_the_without_a_result_line(self):
        self.dead_state(stage="validate")
        code, out = run_cli(["status"])
        self.assertIn("without a result", out)                                                 # before
        run_cli(["stop"])
        code, out = run_cli(["status"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("stage: stopped", out)                                                   # AC-3
        self.assertNotIn("without a result", out)                                              # AC-3
        self.assertNotIn("running: yes", out)


class OnlyTheOutcomeFieldsChange(StopDeadCase):
    def test_everything_the_next_run_needs_is_kept(self):
        rounds = [{"round": 1, "verdict": "APPROVE", "head_sha": "a" * 40, "test_commit": "b" * 40,
                   "data": {"summary": "ok"}}]
        validations = [{"round": 0, "result": "PASS"}]
        self.dead_state(stage="validate", reviewer_running=True,
                        pending_test_files=["tests/test_review_x.py"], rounds=rounds,
                        validations=validations, head_sha="b" * 40, base_sha="c" * 40, fixes=2,
                        round=1, pr_number=7, pr_url="https://example.invalid/pr/7",
                        test_commits=["b" * 40], test_files=["tests/test_review_x.py"],
                        cost_usd=1.25, models_used=["claude-fable-5"], pending_effect="push",
                        needs_info_used=True, force_push=True, last_verdict="APPROVE")
        before = self.state_json()
        code, out = run_cli(["stop"])
        self.assertEqual(code, EXIT_OK, out)
        after = self.state_json()
        changed = {k for k in before if before[k] != after.get(k)}
        self.assertLessEqual(changed, {"stage", "message", "last_exit", "updated_at"})         # AC-4: nothing else
        self.assertLessEqual({"stage", "message", "last_exit"}, changed)                       # ... and these did
        self.assertEqual(set(after), set(before))                                              # no key added or dropped
        self.assertEqual(after["stage"], "stopped")
        self.assertEqual(after["last_exit"], EXIT_ERROR)
        self.assertTrue(after["reviewer_running"])                                             # AC-4: cleanup flag kept
        self.assertEqual(after["pending_test_files"], ["tests/test_review_x.py"])              # AC-4
        self.assertEqual(after["rounds"], rounds)                                              # AC-4
        self.assertEqual(after["validations"], validations)                                    # AC-4
        self.assertEqual(after["head_sha"], "b" * 40)                                          # AC-4
        self.assertEqual(after["fixes"], 2)                                                    # AC-4

    def test_the_next_run_cleans_a_stopped_reviewer_round_leftovers(self):
        self.dead_state(stage="review", reviewer_running=True)
        self.write("tests/test_review_left.py", "# half written by the dead reviewer\n")
        code, out = run_cli(["stop"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertTrue(State.load(self.rdir()).reviewer_running)                              # AC-4: flag survives
        self.claude(claude_entry())
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)                                                   # AC-4: tree not refused
        self.assertFalse(self.exists("tests/test_review_left.py"))                             # AC-4: leftover removed

    def test_the_next_run_resumes_an_approved_round_at_validation(self):
        # round 1 approves and commits its tests; the process dies as validation starts
        self.claude(claude_entry())
        with mock.patch("revali.validate.run_validation", side_effect=RuntimeError("power cut")):
            with contextlib.redirect_stderr(io.StringIO()):
                code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR, out)
        state = State.load(self.rdir())
        self.assertEqual([r["verdict"] for r in state.rounds], ["APPROVE"])
        self.assertEqual(state.validations, [])
        self.assertEqual(git(["rev-parse", "HEAD"], self.repo).strip(), state.test_commits[0])
        # the crash handler recorded stage error; make it the death case: no result written
        state.stage, state.last_exit = "validate", -1
        state.save(self.rdir())
        code, out = run_cli(["wait", "--timeout", "1s"])
        self.assertIn("died at stage 'validate'", out)
        code, out = run_cli(["stop"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertEqual(State.load(self.rdir()).stage, "stopped")
        self.claude()                                                                          # no reviewer answer left
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)                                                   # AC-4: resumed
        self.assertIn("READY TO MERGE", out)
        self.assertIn("before validation", out)                                                # AC-4: says why
        self.assertEqual(len(self.fake_calls("claude")), 1)                                    # AC-4: no second review
        state = State.load(self.rdir())
        self.assertEqual(state.stage, "ready_to_merge")
        self.assertEqual(len(state.rounds), 1)
        self.assertEqual([v["round"] for v in state.validations], [1])


class LiveRunStopIsUnchanged(StopDeadCase):
    def test_stop_kills_the_live_run_and_records_stopped_by_user(self):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"],
                                 start_new_session=True)   # own group: kill_tree uses killpg on POSIX
        self.addCleanup(lambda: child.poll() is None and child.kill())
        self.dead_state(stage="review", reviewer_running=True)
        acquire_lock(self.rdir(), pid=child.pid)
        code, out = run_cli(["stop"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("stopped pid %d" % child.pid, out)                                       # AC-5: killed
        self.assertIsNotNone(child.poll())
        state = State.load(self.rdir())
        self.assertEqual(state.stage, "stopped")                                               # AC-5
        self.assertEqual(state.last_exit, EXIT_ERROR)                                          # AC-5: exit 1
        self.assertIn("stopped by user", state.message)                                        # AC-5: the old wording
        self.assertIn("'review'", state.message)
        self.assertTrue(state.reviewer_running)
        self.assertFalse(os.path.isfile(lock_path(self.rdir())))


class ReadmeSaysSo(unittest.TestCase):
    def test_the_dead_run_paragraph_mentions_stop(self):
        with open(os.path.join(ROOT, "README.md"), "r", encoding="utf-8", newline="") as fh:
            readme = fh.read()
        self.assertIn("without a result", readme)
        after = readme.split("without a result", 1)[1][:1500]
        self.assertIn("revali stop", after)                                                    # AC-6: stop is described there
        self.assertIn("stopped", after)                                                        # AC-6: ... as recording stopped


if __name__ == "__main__":
    unittest.main()
