"""feature/usage-limit (M3.23): a session that hits its model's usage limit is named as such,
with the engine's reset text and the ways on, and `revali run --reviewer-model` picks the
reviewer for one run. Black box through `revali run --foreground` with the fake gh, claude
and runner; the usage-limit result object has the shape claude printed on 2026-11-19, with a
made-up reset date and zone (PR #99 round 1 follow-up)."""

import json
import os
import re
import unittest
from unittest import mock

from revali import EXIT_ACTION, EXIT_ERROR, EXIT_OK, models
from revali.state import State, read_history
from tests.helpers import ROOT, RepoCase, approve_response, claude_entry, run_cli
from tests.test_fix_owed_on_stop import failing_second_push

CHANGE = os.path.join(".revali", "feature__mul", "change.md")
LIMIT_TEXT = (
    "You've hit your Fable limit · resets Jan 5, 9am (UTC). Switch to another "
    "model, or manage usage credits at https://claude.ai/settings/usage, to continue."
)
DEAD_PID = 999999999


def limit_payload(cost=1.14, api_error="usage_limit_reached", text=LIMIT_TEXT):
    return json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "is_error": True,
            "num_turns": 8,
            "total_cost_usd": cost,
            "terminal_reason": "api_error",
            "api_error_status": 429,
            "api_error_type": "rate_limit_error",
            "api_error": api_error,
            "result": text,
            "modelUsage": {"claude-fable-5": {"costUSD": cost}},
            "permission_denials": [],
        }
    )


def argv_after(argv, flag):
    return argv[argv.index(flag) + 1]


class Case(RepoCase):
    def set_author(self, model):
        text = self.read(CHANGE)
        self.write(CHANGE, re.sub(r"author_model: .*", "author_model: " + model, text, count=1))

    def history_row(self):
        rows = read_history(os.path.join(self.home, "history.jsonl"))
        self.assertTrue(rows, "no history row")
        return rows[-1]


class ReviewerHitsTheLimit(Case):
    """AC-1, AC-3"""

    def run_into_the_limit(self):
        self.claude({"exit": 1, "raw_stdout": limit_payload(), "write_tests": False})
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertNotIn("Traceback", out)
        return out

    def test_the_line_names_the_limit_the_reset_and_the_ways_on(self):
        out = self.run_into_the_limit()
        line = next(ln for ln in out.splitlines() if "hit its usage limit" in ln)
        self.assertIn("reviewer model fable hit its usage limit: You've hit your Fable limit", line)
        self.assertIn("resets Jan 5, 9am (UTC)", line)
        self.assertIn("nothing is lost", line)
        self.assertIn("run `revali run` again after the reset", line)
        self.assertIn("`revali run --reviewer-model <model>` for this run only", line)
        self.assertNotIn("session failed", out)

    def test_stage_error_and_wait_and_status_show_it(self):
        self.run_into_the_limit()
        self.assertEqual(State.load(self.rdir()).stage, "error")
        code, out = run_cli(["wait", "--timeout", "1s"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertIn("hit its usage limit", out)
        code, out = run_cli(["status"])
        self.assertIn("hit its usage limit", out)
        # logs/run.log is a detached run's standard output: the ERROR line a foreground run
        # prints, which the test above reads

    def test_what_the_session_spent_is_counted(self):  # AC-3
        self.run_into_the_limit()
        self.assertAlmostEqual(State.load(self.rdir()).cost_usd, 1.14)
        self.assertAlmostEqual(self.history_row()["cost_usd"], 1.14)


class OtherFailuresKeepTheirMessage(Case):
    """AC-4"""

    def test_another_api_error_is_not_a_usage_limit(self):
        payload = limit_payload(api_error="overloaded_error", text="Overloaded")
        self.claude({"exit": 1, "raw_stdout": payload, "write_tests": False})
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertIn("reviewer session failed (exit 1): Overloaded", out)
        self.assertNotIn("usage limit", out)

    def test_a_plain_failed_session(self):
        self.claude(claude_entry(is_error=True, exit=1, write_tests=False))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertIn("reviewer session failed (exit 1)", out)
        self.assertNotIn("usage limit", out)


class DiagnosisHitsTheLimit(Case):
    """AC-2 (as amended): the validation result stands; the diagnosis line names the limit."""

    def test_the_fail_stands_and_the_diagnosis_line_names_the_setting(self):
        self.runner_scenario({"default": 0, "results": {"validate-r1": {"new_test": 1}}})
        self.claude(
            claude_entry(),
            {"exit": 1, "raw_stdout": limit_payload(cost=0.2), "write_tests": False},
        )
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ACTION, out)
        self.assertIn("diagnosis unavailable: diagnoser model", out)
        self.assertIn("hit its usage limit: You've hit your Fable limit", out)
        self.assertIn("the validation FAIL stands without a diagnosis", out)
        self.assertIn("on another model if `[validate] model` is changed", out)
        # PR #99 F1: the stage line itself, not only tests.md and the summary
        line = next(ln for ln in out.splitlines() if "diagnosis unavailable:" in ln)
        self.assertIn("`[validate] model`", line)
        self.assertNotIn("nothing is lost", line)  # PR #99 F2
        self.assertNotIn("--reviewer-model", out)
        state = State.load(self.rdir())
        self.assertAlmostEqual(state.validations[0]["cost_usd"], 0.2)


class ReviewerModelOption(Case):
    """AC-5, AC-6"""

    def test_the_run_uses_it_and_says_where_it_stands(self):
        self.set_author("claude-opus-5")
        self.claude(claude_entry())
        code, out = run_cli(["run", "--foreground", "--reviewer-model", "opus"])
        self.assertEqual(code, EXIT_OK, out)
        argv = self.fake_calls("claude")[0]["argv"]
        self.assertEqual(argv_after(argv, "--model"), "opus")
        self.assertEqual(argv_after(argv, "--fallback-model"), "sonnet,haiku")
        reason = "--reviewer-model, the same tier as author claude-opus-5"
        self.assertIn("reviewer opus (%s)" % reason, out)
        head = self.read(os.path.join(".revali", "feature__mul", "review-1.md"))
        self.assertIn("model_reason: %s" % reason, head.split("# Review round")[0])

    def test_the_next_run_goes_back_to_the_configuration(self):
        self.set_author("claude-opus-5")
        self.claude(claude_entry(), claude_entry())
        run_cli(["run", "--foreground", "--reviewer-model", "sonnet"])
        self.write("src/extra.py", "X = 1\n")
        self.commit_all("another change")
        run_cli(["run", "--foreground"])
        calls = self.fake_calls("claude")
        self.assertEqual(argv_after(calls[0]["argv"], "--model"), "sonnet")
        self.assertEqual(argv_after(calls[-1]["argv"], "--model"), "fable")

    def test_public_comment_carries_the_reason(self):
        self.scenario({"visibility": "PUBLIC"})
        self.set_author("claude-opus-5")
        self.claude(claude_entry())
        code, out = run_cli(["run", "--foreground", "--reviewer-model", "sonnet"])
        self.assertEqual(code, EXIT_OK, out)
        comment = self.read(os.path.join(".revali", "feature__mul", "logs", "comment-review-1.md"))
        self.assertIn("(--reviewer-model, below author claude-opus-5)", comment)

    def test_public_comment_without_the_option_is_unchanged(self):
        self.scenario({"visibility": "PUBLIC"})
        self.claude(claude_entry())
        run_cli(["run", "--foreground"])
        comment = self.read(os.path.join(".revali", "feature__mul", "logs", "comment-review-1.md"))
        self.assertNotIn("--reviewer-model", comment)
        self.assertRegex(comment, r"model: [^ ,]+, cost: \$")

    def test_dry_run_prints_the_reviewer(self):
        self.set_author("claude-opus-5")
        code, out = run_cli(["run", "--dry-run", "--reviewer-model", "fable"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("run reviewer fable (--reviewer-model, above author claude-opus-5)", out)

    def test_blank_value_is_refused_before_anything(self):
        for value in ("", "   "):
            with self.subTest(value=value):
                with mock.patch("revali.pipeline.spawn_detached", return_value=DEAD_PID) as spawn:
                    code, out = run_cli(["run", "--reviewer-model", value])
                self.assertEqual(code, EXIT_ERROR, out)
                self.assertIn("--reviewer-model needs a model name", out)
                spawn.assert_not_called()
        self.assertEqual(self.fake_calls("gh"), [])

    def test_the_detached_child_gets_the_option(self):
        with mock.patch("revali.pipeline.spawn_detached", return_value=DEAD_PID) as spawn:
            code, out = run_cli(["run", "--reviewer-model", " opus "])
        self.assertEqual(code, EXIT_OK, out)
        cmd = spawn.call_args[0][0]
        self.assertEqual(argv_after(cmd, "--reviewer-model"), "opus")


class Docs(unittest.TestCase):
    """AC-7"""

    @staticmethod
    def flat(*parts):
        with open(os.path.join(ROOT, *parts), encoding="utf-8", newline="") as fh:
            return " ".join(fh.read().split())

    def test_readme_usage(self):
        self.assertIn("run [--base <b>] [--reviewer-model <m>]", self.flat("README.md"))

    def test_configuration_and_workflow(self):
        conf = self.flat("docs", "configuration.md")
        self.assertIn("`revali run --reviewer-model <model>` puts `<model>` in place of", conf)
        self.assertIn("revali never lowers the Reviewer's tier on its own", conf)
        flow = self.flat("docs", "workflow.md")
        self.assertIn("hit its usage limit", flow)
        self.assertIn("the stop itself uses no fix cycle", flow)
        self.assertIn("names the limit and `[validate] model`", flow)

    def test_skill_tells_the_session_to_ask(self):
        text = self.flat("skill", "SKILL.md")
        self.assertIn('"hit its usage limit"', text)
        self.assertIn("`--reviewer-model` only when the user names the model", text)


class ResumeAtValidation(Case):
    """PR #99 round 1 follow-up: a run that only validates says the option did nothing."""

    def test_the_option_is_reported_unused(self):
        self.claude(claude_entry(approve_response()))
        with failing_second_push():
            code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR, out)
        calls_before = len(self.fake_calls("claude"))
        code, out = run_cli(["run", "--foreground", "--reviewer-model", "opus"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("continuing there without a new review", out)
        self.assertIn("--reviewer-model opus is not used: this run has no review round", out)
        self.assertEqual(len(self.fake_calls("claude")), calls_before)


class AgainstAuthor(unittest.TestCase):
    """AC-6: the tier comparison on the default ladder."""

    TIERS = ["haiku", "sonnet", "opus", "fable"]

    def test_each_relation(self):
        f = models.against_author
        self.assertEqual(f("fable", "claude-opus-5", self.TIERS), "above author claude-opus-5")
        self.assertEqual(
            f("opus", "claude-opus-5", self.TIERS), "the same tier as author claude-opus-5"
        )
        self.assertEqual(f("haiku", "claude-opus-5", self.TIERS), "below author claude-opus-5")
        self.assertEqual(
            f("my-model", "claude-opus-5", self.TIERS), "my-model is not on the ladder"
        )
        self.assertEqual(f("opus", "", self.TIERS), "author model not given")
        self.assertEqual(f("opus", "fixture", self.TIERS), "author fixture is not on the ladder")


if __name__ == "__main__":
    unittest.main()
