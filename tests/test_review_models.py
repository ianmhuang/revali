"""AC-3 / AC-4: model = "auto" relative to author_model, and the reason is reported."""
import json
import os
import re
import unittest

from tests.helpers import ROOT, RepoCase, claude_entry, run_cli  # noqa: F401
from revali import EXIT_ACTION, EXIT_OK

TIERS = ["haiku", "sonnet", "opus", "fable"]
CHANGE = ".revali/feature__mul/change.md"


def argv_after(argv, flag):
    return argv[argv.index(flag) + 1]


class AutoResolution(unittest.TestCase):
    """revali.models.resolve is the public rule; the pipeline tests below check it is used."""

    def setUp(self):
        from revali import models
        self.m = models

    def reviewer(self, author, requested="auto", fallback="auto", foreign=()):
        return self.m.resolve(self.m.REVIEWER, requested, fallback, author, TIERS, foreign)

    def diagnoser(self, author, requested="auto", fallback="auto"):
        return self.m.resolve(self.m.DIAGNOSER, requested, fallback, author, TIERS)

    def test_reviewer_is_one_tier_above_the_author(self):
        self.assertEqual((self.reviewer("claude-sonnet-5").model, self.reviewer("claude-sonnet-5").fallback),
                         ("opus", "sonnet,haiku"))
        self.assertEqual(self.reviewer("claude-opus-5").model, "fable")
        self.assertEqual(self.reviewer("claude-haiku-4-5-20251001").model, "sonnet")

    def test_reviewer_top_when_author_is_top_unknown_or_foreign(self):
        self.assertEqual(self.reviewer("claude-fable-5").model, "fable")
        self.assertEqual(self.reviewer("fixture").model, "fable")
        self.assertEqual(self.reviewer("").model, "fable")
        r = self.reviewer("gpt-5", foreign=[["gpt-5-mini", "gpt-5"]])
        self.assertEqual((r.model, r.fallback), ("fable", "opus,sonnet,haiku"))
        for r in (self.reviewer("claude-fable-5"), self.reviewer("fixture"), self.reviewer("")):
            self.assertTrue(r.reason, "auto needs a reason")

    def test_diagnoser_is_one_tier_below_the_author(self):
        self.assertEqual((self.diagnoser("claude-fable-5").model, self.diagnoser("claude-fable-5").fallback),
                         ("opus", "sonnet,haiku"))
        self.assertEqual(self.diagnoser("claude-opus-5").model, "sonnet")
        self.assertEqual((self.diagnoser("claude-sonnet-5").model, self.diagnoser("claude-sonnet-5").fallback),
                         ("haiku", ""))
        self.assertEqual(self.diagnoser("claude-haiku-4-5").model, "haiku")
        self.assertEqual(self.diagnoser("fixture").model, "opus")
        self.assertEqual(self.diagnoser("").model, "opus")

    def test_explicit_names_pass_through(self):
        r = self.reviewer("claude-fable-5", requested="sonnet", fallback="opus")
        self.assertEqual((r.model, r.fallback, r.reason), ("sonnet", "opus", ""))
        r = self.diagnoser("", requested="claude-opus-5", fallback="auto")
        self.assertEqual((r.model, r.fallback), ("claude-opus-5", "sonnet,haiku"))
        r = self.reviewer("", requested="my-model", fallback="")
        self.assertEqual((r.model, r.fallback), ("my-model", ""))


class PipelineUsesAuto(RepoCase):
    def set_author(self, model):
        text = self.read(CHANGE)
        self.write(CHANGE, re.sub(r"author_model: .*", "author_model: " + model, text, count=1))

    def test_reviewer_model_and_reason_are_logged_and_recorded(self):
        self.set_author("claude-sonnet-5")
        self.claude(claude_entry())
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        argv = self.fake_calls("claude")[0]["argv"]
        self.assertEqual(argv_after(argv, "--model"), "opus")
        self.assertEqual(argv_after(argv, "--fallback-model"), "sonnet,haiku")
        # AC-4: the run log states the model and why
        self.assertIn("reviewer opus (auto: one tier above author claude-sonnet-5)", out)
        log = self.read(".revali/feature__mul/logs/revali.log")
        self.assertIn("reviewer opus (auto: one tier above author claude-sonnet-5)", log)
        # AC-4: the review header states the model and why
        head = self.read(".revali/feature__mul/review-1.md").split("# Review round")[0]
        self.assertIn("model_requested: opus", head)
        self.assertIn("model_reason: auto: one tier above author claude-sonnet-5", head)
        meta = json.loads(self.read(".revali/feature__mul/review-1.json"))["meta"]
        self.assertEqual(meta["model_reason"], "auto: one tier above author claude-sonnet-5")

    def test_unknown_author_reviewer_is_top_tier(self):
        self.claude(claude_entry())   # fixture author_model is "fixture"
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        argv = self.fake_calls("claude")[0]["argv"]
        self.assertEqual(argv_after(argv, "--model"), "fable")
        self.assertEqual(argv_after(argv, "--fallback-model"), "opus,sonnet,haiku")
        self.assertIn("model_reason: auto:", self.read(".revali/feature__mul/review-1.md"))

    def test_diagnoser_model_and_reason(self):
        self.set_author("claude-opus-5")
        self.runner_scenario({"default": 0, "results": {"validate-r1": {"new_test": 1}},
                              "outputs": {"validate-r1": {"new_test": "AssertionError: 12 != 7"}}})
        diag = {"summary": "product wrong", "cause": "code", "failures": [], "recommendation": "return a * b"}
        self.claude(claude_entry(), claude_entry(diag, write_tests=False, model="claude-sonnet-5", cost=0.2))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ACTION, out)
        reviewer, diagnoser = [c["argv"] for c in self.fake_calls("claude")]
        self.assertEqual(argv_after(reviewer, "--model"), "fable")
        self.assertEqual(argv_after(diagnoser, "--model"), "sonnet")
        self.assertEqual(argv_after(diagnoser, "--fallback-model"), "haiku")
        self.assertIn("diagnoser sonnet (auto: one tier below author claude-opus-5)", out)
        self.assertIn("diagnoser sonnet (auto: one tier below author claude-opus-5)",
                      self.read(".revali/feature__mul/logs/revali.log"))
        meta = json.loads(self.read(".revali/feature__mul/diagnose-1.json"))["meta"]
        self.assertEqual(meta["model_requested"], "sonnet")
        self.assertEqual(meta["model_reason"], "auto: one tier below author claude-opus-5")

    def test_explicit_models_from_the_user_layer_pass_through(self):
        with open(os.path.join(self.home, "config.toml"), "w", encoding="utf-8") as fh:
            fh.write('[review]\nmodel = "claude-opus-5"\nfallback_model = "sonnet"\n'
                     '[validate]\nmodel = "opus"\nfallback_model = ""\n')
        self.runner_scenario({"default": 0, "results": {"validate-r1": {"new_test": 1}}})
        diag = {"summary": "product wrong", "cause": "code", "failures": [], "recommendation": "return a * b"}
        self.claude(claude_entry(model="claude-opus-5"), claude_entry(diag, write_tests=False, model="claude-opus-5"))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ACTION, out)
        reviewer, diagnoser = [c["argv"] for c in self.fake_calls("claude")]
        self.assertEqual(argv_after(reviewer, "--model"), "claude-opus-5")
        self.assertEqual(argv_after(reviewer, "--fallback-model"), "sonnet")
        self.assertEqual(argv_after(diagnoser, "--model"), "opus")
        self.assertNotIn("--fallback-model", diagnoser)
        self.assertIn("model_reason: explicit", self.read(".revali/feature__mul/review-1.md"))
        meta = json.loads(self.read(".revali/feature__mul/diagnose-1.json"))["meta"]
        self.assertEqual(meta["model_reason"], "explicit")

    def test_dry_run_states_the_resolved_model_and_why(self):
        # AC-4: the dry-run message carries the model and the reason (round-1 F1)
        self.set_author("claude-sonnet-5")
        code, out = run_cli(["run", "--dry-run"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("run reviewer opus (auto: one tier above author claude-sonnet-5)", out)
        self.assertNotIn("run reviewer auto", out)
        log = self.read(".revali/feature__mul/logs/revali.log")
        self.assertIn("run reviewer opus (auto: one tier above author claude-sonnet-5)", log)
        state = json.loads(self.read(".revali/feature__mul/state.json"))
        self.assertIn("(auto: one tier above author claude-sonnet-5)", state["message"])

    def test_dry_run_with_an_explicit_model_has_no_reason(self):
        self.write("revali.toml", self.read("revali.toml").replace("[review]\n", '[review]\nmodel = "opus"\n'))
        self.commit_all("pin")
        code, out = run_cli(["run", "--dry-run"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("run reviewer opus (round 1)", out)
        self.assertNotIn("auto:", out)


if __name__ == "__main__":
    unittest.main()
