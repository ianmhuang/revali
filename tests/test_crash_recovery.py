"""A state.json write that races a reader, a run that dies without a result, and the
rerun that continues at validation instead of paying for another review."""
import contextlib
import io
import json
import os
import tempfile
import time
import unittest
from unittest import mock

from tests.helpers import RepoCase, claude_entry, git, run_cli
from revali import EXIT_ERROR, EXIT_OK
from revali.state import State, lock_owner_alive, lock_path, read_history, write_json_atomic


def _tmp_files(directory):
    return [f for f in os.listdir(directory) if f.startswith(".tmp-")]


class AtomicWriteRetry(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="revali race ")
        from tests.helpers import rmtree_force
        self.addCleanup(rmtree_force, self.tmp)
        self.path = os.path.join(self.tmp, "state.json")

    def test_retries_until_the_reader_lets_go(self):
        real_replace = os.replace
        attempts = []

        def busy_then_free(src, dst):
            attempts.append(dst)
            if len(attempts) < 3:
                raise PermissionError(13, "Access is denied")
            real_replace(src, dst)

        with mock.patch("revali.state.os.replace", side_effect=busy_then_free):
            write_json_atomic(self.path, {"stage": "validate"}, retry_s=2.0)
        self.assertEqual(len(attempts), 3)
        with open(self.path, "r", encoding="utf-8") as fh:
            self.assertEqual(json.load(fh), {"stage": "validate"})
        self.assertEqual(_tmp_files(self.tmp), [])

    def test_gives_up_after_the_window(self):
        denied = PermissionError(13, "Access is denied")
        started = time.monotonic()
        with mock.patch("revali.state.os.replace", side_effect=denied):
            with self.assertRaises(PermissionError):
                write_json_atomic(self.path, {"stage": "validate"}, retry_s=0.3)
        elapsed = time.monotonic() - started
        self.assertGreaterEqual(elapsed, 0.3)
        self.assertLess(elapsed, 5.0)
        self.assertFalse(os.path.exists(self.path))
        self.assertEqual(_tmp_files(self.tmp), [])

    def test_other_errors_are_not_retried(self):
        with mock.patch("revali.state.os.replace", side_effect=IsADirectoryError(21, "Is a directory")) as rep:
            with self.assertRaises(IsADirectoryError):
                write_json_atomic(self.path, {"a": 1}, retry_s=2.0)
        self.assertEqual(rep.call_count, 1)
        self.assertEqual(_tmp_files(self.tmp), [])

    def test_default_window_comes_from_defaults_toml(self):
        from revali.config import load_defaults, paths_for
        configured = load_defaults()["paths"]["write_retry_s"]
        self.assertGreater(configured, 0)
        self.assertEqual(paths_for(self.tmp).write_retry_s, configured)
        # the default path is exercised: one denial, then success, inside the window
        real_replace = os.replace
        attempts = []

        def once_busy(src, dst):
            attempts.append(dst)
            if len(attempts) == 1:
                raise PermissionError(13, "Access is denied")
            real_replace(src, dst)

        with mock.patch("revali.state.os.replace", side_effect=once_busy):
            write_json_atomic(self.path, {"a": 1})
        self.assertEqual(len(attempts), 2)


class CrashReporting(RepoCase):
    def _run_crashing_in(self, target, message):
        self.claude(claude_entry())
        err = io.StringIO()
        with mock.patch(target, side_effect=RuntimeError(message)):
            with contextlib.redirect_stderr(err):
                code, out = run_cli(["run", "--foreground"])
        return code, out, err.getvalue()

    def test_unexpected_exception_ends_as_error(self):
        code, out, err = self._run_crashing_in("revali.review.run_round", "boom in review")
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertIn("ERROR:", out)
        self.assertIn("boom in review", out)
        state = State.load(self.rdir())
        self.assertEqual(state.stage, "error")
        self.assertEqual(state.last_exit, EXIT_ERROR)
        self.assertIn("boom in review", state.message)
        self.assertIsNone(lock_owner_alive(self.rdir()))
        self.assertFalse(os.path.isfile(lock_path(self.rdir())))
        rows = read_history(os.path.join(self.home, "history.jsonl"))
        self.assertEqual(rows[-1]["exit"], EXIT_ERROR)
        self.assertEqual(rows[-1]["stage"], "error")
        # the traceback is kept: on stderr (the detached child's run.log) and in revali.log
        self.assertIn("RuntimeError: boom in review", err)
        with open(os.path.join(self.rdir(), "logs", "revali.log"), "r", encoding="utf-8") as fh:
            self.assertIn("RuntimeError: boom in review", fh.read())
        # and wait reports the recorded result, not a dead run
        code, out = run_cli(["wait", "--timeout", "1s"])
        self.assertEqual(code, EXIT_ERROR)
        self.assertIn("error:", out)
        self.assertNotIn("died", out)


class DeadRunReporting(RepoCase):
    """The process vanished (kill, power) before any handler could record a result."""

    def _dead_state(self, stage="review", last_exit=-1):
        st = State(branch="feature/mul", base="main", stage=stage, message="reviewer round 1",
                   last_exit=last_exit)
        st.save(self.rdir())

    def test_wait_reports_a_dead_run_without_a_lock(self):
        self._dead_state()
        code, out = run_cli(["wait", "--timeout", "1s"])
        self.assertEqual(code, EXIT_ERROR)
        self.assertIn("died at stage 'review'", out)
        self.assertIn("run.log", out)

    def test_wait_reports_a_dead_run_with_a_stale_lock(self):
        self._dead_state()
        write_json_atomic(lock_path(self.rdir()), {"pid": 999999999, "since": "x"})
        code, out = run_cli(["wait", "--timeout", "1s"])
        self.assertEqual(code, EXIT_ERROR)
        self.assertIn("died at stage 'review'", out)
        self.assertFalse(os.path.isfile(lock_path(self.rdir())))

    def test_wait_does_not_report_a_stale_exit_code(self):
        # a state file whose last_exit is left over from an earlier, finished run
        self._dead_state(stage="review", last_exit=2)
        code, out = run_cli(["wait", "--timeout", "1s"])
        self.assertEqual(code, EXIT_ERROR)
        self.assertIn("died at stage 'review'", out)

    def test_a_kill_during_preflight_after_a_finished_run_is_a_death(self):
        # the rerun reset last_exit but died before recording its first stage: the stage on
        # disk is the previous run's terminal one
        self._dead_state(stage="needs_action", last_exit=-1)
        code, out = run_cli(["wait", "--timeout", "1s"])
        self.assertEqual(code, EXIT_ERROR)
        self.assertIn("died at stage 'needs_action' without a result", out)
        code, out = run_cli(["status"])
        self.assertIn("without a result", out)

    def test_a_stop_error_with_a_blocked_state_file_is_contained(self):
        # the Stop handler shares the guard: a dirty tree is refused, the refusal cannot be
        # recorded, the exit code still stands
        self.write("src/calc.py", self.read("src/calc.py") + "\n# dirty\n")
        writes = []

        def deny_after_the_first(src, dst):  # the "no result yet" save lands, the refusal's does not
            if os.path.basename(dst) == "state.json":
                writes.append(dst)
                if len(writes) > 1:
                    raise PermissionError(13, "Access is denied")
            return _REAL_REPLACE(src, dst)

        err = io.StringIO()
        with mock.patch("revali.state.os.replace", side_effect=deny_after_the_first):
            with contextlib.redirect_stderr(err):
                code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertIn("ERROR: working tree is not clean", out)
        self.assertIn("ERROR: the state file could not be updated either", out)
        self.assertIn("will report the run as dead", out)
        self.assertNotIn("Traceback", err.getvalue())  # the Stop path, not the catch-all
        self.assertFalse(os.path.isfile(lock_path(self.rdir())))
        code, out = run_cli(["wait", "--timeout", "1s"])
        self.assertEqual(code, EXIT_ERROR)
        self.assertIn("died", out)

    def test_status_reports_a_dead_run(self):
        self._dead_state()
        code, out = run_cli(["status"])
        self.assertEqual(code, EXIT_OK)
        self.assertIn("stage: review", out)
        self.assertIn("without a result", out)
        self.assertIn("run.log", out)

    def test_finished_dry_run_is_not_a_dead_run(self):
        code, out = run_cli(["run", "--dry-run"])
        self.assertEqual(code, EXIT_OK, out)
        code, out = run_cli(["wait", "--timeout", "1s"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertNotIn("died", out)
        code, out = run_cli(["status"])
        self.assertNotIn("without a result", out)

    def test_run_start_clears_the_previous_exit_code(self):
        # a run killed during preflight must not look like a finished dry run
        self._dead_state(stage="preflight", last_exit=0)
        self.claude(claude_entry())
        with mock.patch("revali.pipeline.preflight", side_effect=KeyboardInterrupt):
            code, out = run_cli(["run", "--foreground"])  # cli.main turns Ctrl-C into "interrupted", exit 1
        self.assertEqual(code, EXIT_ERROR)
        self.assertIn("interrupted", out)
        state = State.load(self.rdir())
        self.assertEqual(state.last_exit, -1)
        code, out = run_cli(["wait", "--timeout", "1s"])
        self.assertEqual(code, EXIT_ERROR)
        self.assertIn("died at stage 'preflight'", out)


class ResumeAtValidation(RepoCase):
    def _approve_then_die_before_validation(self):
        self.claude(claude_entry())
        with mock.patch("revali.validate.run_validation", side_effect=RuntimeError("power cut")):
            with contextlib.redirect_stderr(io.StringIO()):
                code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR, out)
        state = State.load(self.rdir())
        self.assertEqual(state.stage, "error")
        self.assertEqual([r["verdict"] for r in state.rounds], ["APPROVE"])
        self.assertEqual(state.validations, [])
        self.assertEqual(len(state.test_commits), 1)
        self.assertEqual(git(["rev-parse", "HEAD"], self.repo).strip(), state.test_commits[0])
        return state

    def _comments(self):
        return [a for a in (c["argv"] for c in self.fake_calls("gh")) if a[:2] == ["pr", "comment"]]

    def test_rerun_validates_without_a_new_review(self):
        self._approve_then_die_before_validation()
        self.assertEqual(len(self._comments()), 1)
        self.claude()  # nothing left for a reviewer to answer with
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("READY TO MERGE", out)
        self.assertIn("stopped before validation", out)
        self.assertIn("round 1", out)
        state = State.load(self.rdir())
        self.assertEqual(state.stage, "ready_to_merge")
        self.assertEqual(len(state.rounds), 1)
        self.assertAlmostEqual(state.cost_usd, 0.5)
        self.assertEqual([v["round"] for v in state.validations], [1])
        self.assertEqual(state.last_verdict, "PASS")
        self.assertEqual(len(self.fake_calls("claude")), 1)
        labels = [r["label"] for r in self.fake_calls("runner")]
        self.assertEqual(labels, ["baseline", "smoke-r1-1", "validate-r1"])
        # one review comment from the first run, one validation comment from the second
        self.assertEqual(len(self._comments()), 2)
        self.assertTrue(any(a[:2] == ["pr", "ready"] for a in (c["argv"] for c in self.fake_calls("gh"))))
        rows = read_history(os.path.join(self.home, "history.jsonl"))
        self.assertEqual([r["exit"] for r in rows], [EXIT_ERROR, EXIT_OK])

    def test_resumed_validation_can_still_fail(self):
        self._approve_then_die_before_validation()
        self.runner_scenario({"default": 0, "results": {"validate-r1": {"test": 1}},
                              "outputs": {"validate-r1": {"test": "FAIL: test_add"}}})
        diagnosis = {"summary": "add regressed.", "cause": "code",
                     "failures": [{"test": "tests/test_calc.py::test_add", "cause": "code", "note": "7 != 12"}],
                     "recommendation": "fix add"}
        self.claude(claude_entry(diagnosis, write_tests=False, model="claude-opus-5", cost=0.2))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, 2, out)
        self.assertIn("ACTION NEEDED", out)
        state = State.load(self.rdir())
        self.assertEqual(len(state.rounds), 1)
        self.assertEqual(state.stage, "needs_action")
        self.assertEqual(state.validations[0]["result"], "FAIL")

    def test_new_head_gets_a_new_round(self):
        self._approve_then_die_before_validation()
        self.write("src/calc.py", self.read("src/calc.py") + "\n# touched after the approval\n")
        self.commit_all("touch calc")
        self.claude(claude_entry())
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertNotIn("stopped before validation", out)
        state = State.load(self.rdir())
        self.assertEqual(len(state.rounds), 2)
        self.assertEqual(len(self.fake_calls("claude")), 2)

    def test_validated_round_is_not_resumed(self):
        state = self._approve_then_die_before_validation()
        state.validations.append({"number": 1, "result": "FAIL", "round": 1, "failed_step": "test"})
        state.save(self.rdir())
        self.claude(claude_entry())
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertNotIn("stopped before validation", out)
        self.assertEqual(len(State.load(self.rdir()).rounds), 2)
        self.assertEqual(len(self.fake_calls("claude")), 2)

    def test_changes_requested_round_is_not_resumed(self):
        finding = {"id": "F1", "file": "src/calc.py", "line": 3, "severity": "high", "kind": "correctness",
                   "text": "mul ignores negative numbers", "suggestion": "handle them"}
        from tests.helpers import approve_response
        self.claude(claude_entry(approve_response(verdict="CHANGES_REQUESTED", findings=[finding])))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, 2, out)
        # same HEAD, rerun: the existing "nothing changed" refusal, not a validation
        self.claude(claude_entry())
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, 2, out)
        self.assertIn("nothing changed", out)
        self.assertEqual(State.load(self.rdir()).validations, [])


class ConfiguredWindow(RepoCase):
    """[paths] write_retry_s from the layered config of the repository that holds the file."""

    def setUp(self):
        super().setUp()
        self.write("revali.toml", self.read("revali.toml") + "\n[paths]\nwrite_retry_s = 0.05\n")
        self.commit_all("shorten the state write window")

    _deny_state_json = staticmethod(lambda src, dst: _deny_state_json(src, dst))

    def test_project_config_sets_the_window(self):
        started = time.monotonic()
        with mock.patch("revali.state.os.replace", side_effect=self._deny_state_json):
            with self.assertRaises(PermissionError):
                State(branch="feature/mul", stage="review").save(self.rdir())
        elapsed = time.monotonic() - started
        self.assertGreaterEqual(elapsed, 0.05)
        self.assertLess(elapsed, 1.0)  # the 2.0 s default would still be retrying
        # the same write outside any repository keeps the default
        from revali.config import load_defaults
        outside = tempfile.mkdtemp(prefix="revali outside ")
        from tests.helpers import rmtree_force
        self.addCleanup(rmtree_force, outside)
        started = time.monotonic()
        with mock.patch("revali.state.os.replace", side_effect=self._deny_state_json):
            with self.assertRaises(PermissionError):
                write_json_atomic(os.path.join(outside, "state.json"), {"a": 1})
        self.assertGreaterEqual(time.monotonic() - started, load_defaults()["paths"]["write_retry_s"])

    def test_state_write_failure_in_the_crash_handler_is_contained(self):
        self.claude(claude_entry())
        err = io.StringIO()
        with mock.patch("revali.state.os.replace", side_effect=self._deny_state_json):
            with contextlib.redirect_stderr(err):
                code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertIn("ERROR: the run stopped with PermissionError", out)
        self.assertIn("before its first stage", out)  # the initial save is what failed
        self.assertIn("ERROR: the state file could not be updated either", out)
        self.assertIn("still show the previous run's result", out)
        self.assertFalse(os.path.isfile(lock_path(self.rdir())))
        self.assertIn("PermissionError", err.getvalue())
        # nothing was recorded, so the run reads as dead
        self.assertIsNone(State.load(self.rdir()))
        with open(os.path.join(self.rdir(), "logs", "revali.log"), "r", encoding="utf-8") as fh:
            self.assertIn("could not be updated", fh.read())


_REAL_REPLACE = os.replace


def _deny_state_json(src, dst):
    """os.replace that refuses every state.json and lets other files through."""
    if os.path.basename(dst) == "state.json":
        raise PermissionError(13, "Access is denied")
    return _REAL_REPLACE(src, dst)


if __name__ == "__main__":
    unittest.main()
