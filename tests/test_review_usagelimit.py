"""feature/usage-limit AC-1 to AC-4: a session stopped by its model's usage limit is reported
as that, with the engine's text and the ways on; what it spent is counted; any other failed
session keeps its message. Black box through `revali run --foreground` with the fake gh,
claude and runner."""

import json
import os

from revali import EXIT_ACTION, EXIT_ERROR, EXIT_OK
from revali.state import State, read_history
from tests.helpers import RepoCase, claude_entry, run_cli

RESET = "resets Dec 1, 9am (UTC)"
LIMIT_TEXT = "You've hit your Fable limit - %s. Switch to another model to continue." % RESET
TESTS_MD = ".revali/feature__mul/tests.md"
RUN_LOG = ".revali/feature__mul/logs/revali.log"


def result_object(cost=0.37, text=LIMIT_TEXT, **kw):
    """A failed `claude --output-format json` result; kw adds or overrides keys."""
    payload = {
        "type": "result",
        "subtype": "success",
        "is_error": True,
        "num_turns": 6,
        "total_cost_usd": cost,
        "terminal_reason": "api_error",
        "api_error_status": 429,
        "result": text,
        "modelUsage": {"claude-fable-5": {"costUSD": cost}},
        "permission_denials": [],
    }
    payload.update(kw)
    return json.dumps(payload)


def limit_result(cost=0.37):
    return result_object(cost=cost, api_error="usage_limit_reached")


class Case(RepoCase):
    def reviewer_hits_the_limit(self, cost=0.37):
        self.claude({"exit": 1, "raw_stdout": limit_result(cost)})
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertNotIn("Traceback", out)
        self.assertIn("usage limit", out)
        return out

    def assert_reviewer_limit_line(self, line):
        self.assertIn("reviewer", line)
        # author `fixture` is on no ladder, so auto picks the top tier
        self.assertIn("fable", line)
        self.assertIn("usage limit", line)
        self.assertIn(LIMIT_TEXT, line)  # the engine's own text, reset time included
        self.assertIn("nothing is lost", line)
        self.assertIn("after the reset", line)
        self.assertIn("revali run --reviewer-model <model>", line)

    def history_rows(self):
        return read_history(os.path.join(self.home, "history.jsonl"))


class ReviewerLimit(Case):
    def test_first_line_names_role_model_text_and_both_ways_on(self):  # AC-1
        out = self.reviewer_hits_the_limit()
        errors = [ln for ln in out.splitlines() if ln.startswith("ERROR:")]
        self.assertEqual(len(errors), 1, out)
        self.assert_reviewer_limit_line(errors[0])
        self.assertNotIn("session failed", out)

    def test_recorded_as_stage_error_and_shown_by_wait_and_status(self):  # AC-1
        self.reviewer_hits_the_limit()
        state = State.load(self.rdir())
        self.assertEqual(state.stage, "error")
        self.assertEqual(state.last_exit, EXIT_ERROR)
        self.assert_reviewer_limit_line(state.message.split("\n")[0])
        code, waited = run_cli(["wait", "--timeout", "1s"])
        self.assertEqual(code, EXIT_ERROR, waited)
        self.assertIn(LIMIT_TEXT, waited)
        self.assertIn("--reviewer-model", waited)
        code, shown = run_cli(["status"])
        self.assertIn(LIMIT_TEXT, shown)
        self.assertIn("--reviewer-model", shown)

    def test_the_cost_reported_before_the_limit_is_counted(self):  # AC-3
        self.reviewer_hits_the_limit(cost=0.37)
        self.assertAlmostEqual(State.load(self.rdir()).cost_usd, 0.37)
        rows = self.history_rows()
        self.assertTrue(rows, "no history row")
        self.assertAlmostEqual(rows[-1]["cost_usd"], 0.37)

    def test_the_run_after_the_reset_reviews_again(self):  # AC-1: nothing is lost
        self.reviewer_hits_the_limit()
        self.assertEqual(State.load(self.rdir()).rounds, [])  # the stopped session is no round
        self.claude(claude_entry())
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertNotIn("usage limit", out)
        self.assertEqual(len(State.load(self.rdir()).rounds), 1, out)


class OnlyTheUsageLimitIsReportedAsOne(Case):
    """AC-4, against the limit itself so the difference is the `api_error` value alone."""

    def failed_run(self, raw):
        self.claude({"exit": 1, "raw_stdout": raw})
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertNotIn("usage limit", out)
        self.assertNotIn("--reviewer-model", out)
        return out

    def test_another_api_error_keeps_the_session_failed_message(self):
        self.reviewer_hits_the_limit()
        out = self.failed_run(result_object(text="Overloaded", api_error="overloaded_error"))
        self.assertIn("reviewer session failed (exit 1): Overloaded", out)

    def test_a_failure_without_api_error_keeps_the_session_failed_message(self):
        self.reviewer_hits_the_limit()
        out = self.failed_run(result_object(text="Connection reset"))
        self.assertIn("reviewer session failed (exit 1): Connection reset", out)

    def test_budget_exhaustion_keeps_the_budget_message(self):
        self.reviewer_hits_the_limit()
        raw = result_object(
            cost=1.05,
            text=None,
            subtype="error_max_budget_usd",
            terminal_reason="budget_exhausted",
            errors=["Reached maximum budget"],
        )
        out = self.failed_run(raw)
        # fixture: [review] budget_usd = 1.0
        self.assertIn("reviewer ran out of budget ($1.00) after 6 turns, spent $1.05", out)


class DiagnosisLimit(Case):
    """AC-2 as amended (the validation result stands), AC-3 for the diagnosis session."""

    def failed_validation(self):
        self.runner_scenario({"default": 0, "results": {"validate-r1": {"new_test": 1}}})
        self.claude(claude_entry(), {"exit": 1, "raw_stdout": limit_result(cost=0.2)})
        code, out = run_cli(["run", "--foreground"])
        self.assertNotIn("Traceback", out)
        return code, out

    def assert_diagnosis_limit_line(self, line):
        self.assertIn("usage limit", line)
        self.assertIn(LIMIT_TEXT, line)
        self.assertIn("[validate] model", line)
        self.assertNotIn("--reviewer-model", line)

    def test_the_fail_stands_with_exit_2(self):
        code, out = self.failed_validation()
        self.assertEqual(code, EXIT_ACTION, out)
        state = State.load(self.rdir())
        self.assertEqual(state.last_verdict, "FAIL")
        self.assertEqual(state.stage, "needs_action")
        self.assertIn("usage limit", out)

    def test_the_unavailable_line_names_the_limit_and_the_setting(self):
        code, out = self.failed_validation()
        self.assertEqual(code, EXIT_ACTION, out)
        recorded = [
            ln for ln in self.read(TESTS_MD).splitlines() if ln.startswith("diagnosis unavailable:")
        ]
        self.assertEqual(len(recorded), 1, self.read(TESTS_MD))
        self.assert_diagnosis_limit_line(recorded[0])
        # what the author is told to act on
        self.assertIn("ACTION NEEDED:", out)
        action = out.split("ACTION NEEDED:", 1)[1]
        told = [ln for ln in action.splitlines() if "diagnosis unavailable:" in ln]
        self.assertEqual(len(told), 1, out)
        self.assert_diagnosis_limit_line(told[0])
        self.assertNotIn("--reviewer-model", out)

    def test_the_stage_line_says_the_same_as_tests_md(self):
        # round 1 F1: the line in the log (and on a foreground run's output), not only
        # tests.md and the ACTION NEEDED text
        code, out = self.failed_validation()
        self.assertEqual(code, EXIT_ACTION, out)
        logged = [
            ln for ln in self.read(RUN_LOG).splitlines() if "validate: diagnosis unavailable:" in ln
        ]
        self.assertEqual(len(logged), 1, self.read(RUN_LOG))
        self.assert_diagnosis_limit_line(logged[0])
        printed = [ln for ln in out.splitlines() if "diagnosis unavailable:" in ln]
        self.assertGreaterEqual(len(printed), 2, out)  # the stage line and the ACTION NEEDED one
        for line in printed:
            self.assert_diagnosis_limit_line(line)

    def test_the_diagnosis_cost_is_counted(self):
        code, out = self.failed_validation()
        self.assertEqual(code, EXIT_ACTION, out)
        self.assertIn("usage limit", out)
        state = State.load(self.rdir())
        self.assertAlmostEqual(state.validations[0]["cost_usd"], 0.2)
        # the reviewer's 0.5, then what the diagnosis spent before the limit
        self.assertAlmostEqual(state.cost_usd, 0.5 + 0.2)
        rows = self.history_rows()
        self.assertTrue(rows, "no history row")
        self.assertAlmostEqual(rows[-1]["cost_usd"], 0.5 + 0.2)
