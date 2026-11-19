"""feature/usage-limit (M3.23): a session that hits its model's usage limit is named as such,
with the engine's reset text and the ways on, and `revali run --reviewer-model` picks the
reviewer for one run. Black box through `revali run --foreground` with the fake gh, claude
and runner; the usage-limit result object has the shape claude printed on 2026-11-19, with a
made-up reset date and zone (PR #99 round 1 follow-up)."""

import contextlib
import io
import json
import os
import re
import unittest
from unittest import mock

from revali import EXIT_ACTION, EXIT_ERROR, EXIT_OK, models
from revali.state import State, read_history
from tests import test_bench_tool as bench_tests  # base classes only: no tests of theirs
from tests import test_doc_audit as doc_audit_tests
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
        self.assertNotIn("--fallback-model", argv)  # AC-8: a Reviewer has no auto fallback
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
        # AC-8
        self.assertIn("For the Reviewer `auto` means no fallback", conf)
        self.assertIn("is overloaded", self.flat("docs", "workflow.md"))
        flow = self.flat("docs", "workflow.md")
        self.assertIn("hit its usage limit", flow)
        self.assertIn("the stop itself uses no fix cycle", flow)
        self.assertIn("names the limit and `[validate] model`", flow)

    def test_the_tools_say_how_they_stop(self):  # PR #99 round 2 follow-ups
        self.assertIn("stopped by its model's usage limit", self.flat("bench", "README.md"))
        self.assertIn("stopped by its model's usage limit", self.flat("docs", "releasing.md"))

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


def limited(cost=0.4):
    return {"exit": 1, "raw_stdout": limit_payload(cost=cost), "write_tests": False}


class DocAuditStopsAtTheLimit(doc_audit_tests.AuditCase):
    """PR #99 round 2 follow-up: doc_audit gives its own advice, and the documents after the
    one that hit the limit are not run (--resume runs them)."""

    def test_the_rest_is_not_run(self):
        self.claude(limited(), claude_entry(doc_audit_tests.answer(), write_tests=False))
        code, out = self.run_audit("--budget-usd", "10", "--only", "README.md", "CONVENTIONS.md")
        self.assertEqual(code, 1, out)
        _, by_doc = self.results()
        first = by_doc["README.md"]
        self.assertEqual(first["status"], "error")  # --resume runs every document not done
        self.assertAlmostEqual(first["cost"], 0.4)
        self.assertIn("auditor model fable hit its usage limit", first["message"])
        self.assertIn("continue with `--resume <dir>` after the reset", first["message"])
        self.assertNotIn("revali run", first["message"])
        self.assertEqual(by_doc["CONVENTIONS.md"]["status"], "not-run")
        self.assertIn("usage limit was reached", by_doc["CONVENTIONS.md"]["message"])
        self.assertEqual(len(self.calls()), 1)  # the second document was not sent


class BenchStopsAtTheLimit(bench_tests.BenchCase):
    """PR #99 round 2 follow-up: review_bench gives its own advice, the case that hit the
    limit is not-run (so --resume runs it again), and so are the cases after it."""

    def test_the_rest_is_not_run(self):
        self.make_case("a-one")
        self.make_case("b-two")
        self.claude(limited(), claude_entry(bench_tests.answer(bench_tests.finding())))
        _, out, cases, _ = self.run_bench()
        a, b = cases["a-one"], cases["b-two"]
        self.assertEqual(a["outcome"], "not-run")
        self.assertAlmostEqual(a["cost"], 0.4)
        self.assertIn("hit its usage limit", a["message"])
        self.assertIn("the cases left are not run", a["message"])
        self.assertNotIn("revali run --reviewer-model", a["message"])
        self.assertEqual(b["outcome"], "not-run")
        self.assertIn("usage limit was reached", b["message"])
        self.assertEqual(len(self.fake_calls("claude")), 1)

    def test_a_resumed_session_in_progress_keeps_the_known_spend(self):  # PR #99 round 4 F1
        self.make_case("a-one")
        self.claude(limited(cost=0.4))
        self.run_bench()
        seen = {}

        def spawn(*a, **kw):  # the file as a killed run would leave it, then stop the run
            with open(os.path.join(self.out, "results.json"), encoding="utf-8") as fh:
                seen.update({c["case"]: c for c in json.load(fh)["cases"]})
            raise KeyboardInterrupt

        buf = io.StringIO()
        with mock.patch.object(bench_tests.bench, "spawn_reviewer", spawn):
            with contextlib.redirect_stdout(buf):
                bench_tests.bench.main(["--resume", self.out, "--budget-usd", "10"])
        a = seen["a-one"]
        self.assertEqual(a["outcome"], "started")
        self.assertAlmostEqual(a["cost"], 0.4)
        self.assertAlmostEqual(a["charged"], 1.4)  # the fixture's $1.00 cap + the $0.40

    def test_a_limit_that_reported_no_cost_charges_nothing(self):  # PR #99 round 3 F2
        self.make_case("a-one")
        self.claude(limited(cost=0))
        _, _, cases, _ = self.run_bench()
        a = cases["a-one"]
        self.assertEqual((a["outcome"], a["cost"], a["charged"]), ("not-run", 0, 0))
        self.assertNotIn("cost unknown", a["message"])

    def test_a_resume_keeps_what_the_limited_session_spent(self):  # PR #99 round 3 F1
        self.make_case("a-one")
        self.claude(limited(cost=0.4), claude_entry(bench_tests.answer(bench_tests.finding())))
        self.run_bench()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            bench_tests.bench.main(["--resume", self.out, "--budget-usd", "10"])
        with open(os.path.join(self.out, "results.json"), encoding="utf-8") as fh:
            a = {c["case"]: c for c in json.load(fh)["cases"]}["a-one"]
        self.assertTrue(a["outcome"].startswith("caught"), a)
        self.assertAlmostEqual(a["cost"], a["charged"])  # both include the earlier $0.40
        self.assertGreater(a["cost"], 0.4)
        self.assertIn("includes $0.40 spent by an earlier session of this case", a["message"])
        self.assertNotIn("cost unknown", a["message"])


class CarriedNote(unittest.TestCase):
    """PR #99 round 4 F1: what a rerun bench case says about earlier sessions."""

    def test_each_mix_of_known_and_unknown(self):
        note = bench_tests.bench._carried_note
        self.assertEqual(note(0.4, 0.4), "includes $0.40 spent by an earlier session of this case")
        self.assertEqual(
            note(1.4, 0.4),
            "includes $0.40 spent and $1.00 charged for an interrupted earlier session "
            "(cost unknown)",
        )
        self.assertEqual(
            note(1.0, 0.0),
            "includes $1.00 charged for an interrupted earlier session (cost unknown)",
        )


class ReviewerHasNoAutoFallback(unittest.TestCase):
    """AC-8 (Ian 2026-11-19): the Reviewer runs on the model it was given or stops; only a
    fallback the user names is used. PR #99 round 5 had run on Opus through `--fallback-model
    opus,sonnet,haiku`."""

    TIERS = ["haiku", "sonnet", "opus", "fable"]

    def reviewer(self, author, requested="auto", fallback="auto"):
        return models.resolve(models.REVIEWER, requested, fallback, author, self.TIERS)

    def test_auto_is_none_whatever_the_author(self):
        for author in ("claude-opus-5-5", "claude-sonnet-5", "claude-haiku-4-5", "fixture", ""):
            with self.subTest(author=author):
                self.assertEqual(self.reviewer(author).fallback, "")
        self.assertEqual(self.reviewer("claude-haiku-4-5", requested="fable").fallback, "")

    def test_an_explicit_fallback_is_used_as_written(self):
        self.assertEqual(self.reviewer("claude-opus-5-5", fallback="opus").fallback, "opus")

    def test_the_diagnosis_is_unchanged(self):
        r = models.resolve(models.DIAGNOSER, "auto", "auto", "claude-opus-5-5", self.TIERS)
        self.assertEqual((r.model, r.fallback), ("sonnet", "haiku"))


class ReviewerRunNoFallbackAndBusy(Case):
    """AC-8 end to end: the claude call carries no fallback, and a busy Reviewer model stops
    the run with its own line."""

    def test_an_opus_author_gives_fable_no_fallback(self):
        self.set_author("claude-opus-5-5")
        self.claude(claude_entry())
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        argv = self.fake_calls("claude")[0]["argv"]
        self.assertEqual(argv_after(argv, "--model"), "fable")
        self.assertNotIn("--fallback-model", argv)

    def test_a_busy_reviewer_model_stops_the_run(self):
        busy = json.dumps(
            {
                "type": "result",
                "subtype": "success",
                "is_error": True,
                "num_turns": 1,
                "total_cost_usd": 0.0,
                "terminal_reason": "api_error",
                "api_error_status": 529,
                "result": "API Error: Overloaded",
                "modelUsage": {},
            }
        )
        self.claude({"exit": 1, "raw_stdout": busy, "write_tests": False})
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR, out)
        line = next(ln for ln in out.splitlines() if "is overloaded" in ln)
        self.assertIn("reviewer model fable is overloaded: API Error: Overloaded", line)
        self.assertIn("nothing is lost: run `revali run` again later", line)
        self.assertIn("`revali run --reviewer-model <model>` for this run only", line)
        self.assertNotIn("usage limit", out)


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
