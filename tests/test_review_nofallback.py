"""feature/usage-limit AC-8: the Reviewer runs on the model it was given and on no other.
`fallback_model = "auto"` gives the Reviewer no fallback, a fallback the user names is used
as written, the diagnosis keeps its own, and a Reviewer model that is overloaded
(`api_error_status` 529) stops the run with exit 1 and one line naming the model, the
engine's text and the ways on. Black box through `revali run --foreground` with the fake gh,
claude and runner; the ladder is the default one (haiku, sonnet, opus, fable)."""

import json
import os
import re

from revali import EXIT_ACTION, EXIT_ERROR, EXIT_OK
from revali.state import State
from tests.helpers import RepoCase, claude_entry, run_cli

CHANGE = ".revali/feature__mul/change.md"
BUSY_TEXT = "API Error: Overloaded"
DIAGNOSIS = {
    "summary": "product wrong",
    "cause": "code",
    "failures": [],
    "introduced_by": "unknown",
    "recommendation": "return a * b",
}


def value_after(argv, flag):
    return argv[argv.index(flag) + 1]


def failed_result(status, text, cost=0.0):
    """A failed `claude --output-format json` result that ended on an API error."""
    return json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "is_error": True,
            "num_turns": 1,
            "total_cost_usd": cost,
            "terminal_reason": "api_error",
            "api_error_status": status,
            "result": text,
            "modelUsage": {},
            "permission_denials": [],
        }
    )


class Case(RepoCase):
    def set_author(self, model):
        text = self.read(CHANGE)
        self.write(CHANGE, re.sub(r"author_model: .*", "author_model: " + model, text, count=1))

    def user_config(self, text):
        with open(
            os.path.join(self.home, "config.toml"), "w", encoding="utf-8", newline="\n"
        ) as fh:
            fh.write(text)

    def claude_argvs(self):
        return [call["argv"] for call in self.fake_calls("claude")]


class TheReviewerHasNoAutoFallback(Case):
    def reviewed_on(self, author, model):
        self.set_author(author)
        self.claude(claude_entry(model="claude-%s-5" % model))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        argvs = self.claude_argvs()
        self.assertEqual(len(argvs), 1, out)
        self.assertEqual(value_after(argvs[0], "--model"), model)
        self.assertNotIn("--fallback-model", argvs[0])

    def test_one_tier_above_a_sonnet_author(self):
        self.reviewed_on("claude-sonnet-5", "opus")

    def test_one_tier_above_an_opus_author(self):
        # the case of round 5: the fallback list would have held the author's own tier
        self.reviewed_on("claude-opus-5-5", "fable")

    def test_an_author_on_no_ladder(self):
        self.reviewed_on("fixture", "fable")

    def test_the_diagnosis_keeps_its_fallback(self):
        self.set_author("claude-fable-5")
        self.runner_scenario({"default": 0, "results": {"validate-r1": {"new_test": 1}}})
        self.claude(
            claude_entry(),
            claude_entry(DIAGNOSIS, write_tests=False, model="claude-opus-5", cost=0.2),
        )
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ACTION, out)
        argvs = self.claude_argvs()
        self.assertEqual(len(argvs), 2, out)
        reviewer, diagnoser = argvs
        self.assertEqual(value_after(reviewer, "--model"), "fable")
        self.assertNotIn("--fallback-model", reviewer)
        # one tier below the author, then the tiers below that one, strongest first
        self.assertEqual(value_after(diagnoser, "--model"), "opus")
        self.assertEqual(value_after(diagnoser, "--fallback-model"), "sonnet,haiku")


class AFallbackTheUserNamedIsUsedAsWritten(Case):
    def test_with_the_option_for_this_run(self):
        self.user_config('[review]\nfallback_model = "sonnet"\n')
        self.claude(claude_entry(model="claude-opus-5"))
        code, out = run_cli(["run", "--foreground", "--reviewer-model", "opus"])
        self.assertEqual(code, EXIT_OK, out)
        argv = self.claude_argvs()[0]
        self.assertEqual(value_after(argv, "--model"), "opus")
        self.assertEqual(value_after(argv, "--fallback-model"), "sonnet")


class TheReviewerModelIsBusy(Case):
    def busy_run(self, argv=("run", "--foreground")):
        self.claude({"exit": 1, "raw_stdout": failed_result(529, BUSY_TEXT)})
        code, out = run_cli(list(argv))
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertNotIn("Traceback", out)
        errors = [ln for ln in out.splitlines() if ln.startswith("ERROR:")]
        self.assertEqual(len(errors), 1, out)
        return errors[0], out

    def assert_busy_line(self, line, model):
        self.assertIn("reviewer", line)
        self.assertIn(model, line)
        self.assertIn("overloaded", line)
        self.assertIn(BUSY_TEXT, line)  # the engine's own text
        self.assertIn("again later", line)
        self.assertIn("revali run --reviewer-model <model>", line)
        self.assertNotIn("usage limit", line)

    def test_one_line_names_the_model_the_text_and_the_ways_on(self):
        line, out = self.busy_run()
        # author `fixture` is on no ladder, so auto picks the top tier
        self.assert_busy_line(line, "fable")
        self.assertNotIn("session failed", out)
        self.assertNotIn("usage limit", out)

    def test_no_other_model_is_tried(self):
        self.busy_run()
        argvs = self.claude_argvs()
        self.assertEqual(len(argvs), 1)
        self.assertEqual(value_after(argvs[0], "--model"), "fable")
        self.assertNotIn("--fallback-model", argvs[0])

    def test_recorded_as_stage_error_and_shown_by_wait(self):
        self.busy_run()
        state = State.load(self.rdir())
        self.assertEqual(state.stage, "error")
        self.assertEqual(state.last_exit, EXIT_ERROR)
        self.assert_busy_line(state.message.split("\n")[0], "fable")
        code, waited = run_cli(["wait", "--timeout", "1s"])
        self.assertEqual(code, EXIT_ERROR, waited)
        self.assertIn(BUSY_TEXT, waited)
        self.assertIn("--reviewer-model", waited)

    def test_the_line_names_the_model_the_option_gave(self):
        line, _ = self.busy_run(("run", "--foreground", "--reviewer-model", "opus"))
        self.assert_busy_line(line, "opus")
        named = line.split(BUSY_TEXT)[0]  # what comes before the engine's text
        self.assertIn("opus", named)
        self.assertNotIn("fable", named)

    def test_the_run_after_it_reviews_again(self):
        self.busy_run()
        self.assertEqual(State.load(self.rdir()).rounds, [])  # the stopped session is no round
        self.claude(claude_entry())
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertNotIn("overloaded", out)
        self.assertEqual(len(State.load(self.rdir()).rounds), 1, out)

    def test_another_api_status_keeps_the_session_failed_message(self):
        # AC-4 beside AC-8: the difference is the status alone
        self.busy_run()
        self.claude({"exit": 1, "raw_stdout": failed_result(500, "API Error: Internal error")})
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertIn("reviewer session failed (exit 1): API Error: Internal error", out)
        self.assertNotIn("overloaded", out)
        self.assertNotIn("--reviewer-model", out)
