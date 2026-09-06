"""Acceptance tests for the PR #31 follow-ups: the base rerun result in every validation
record (AC-1), one named timeout exit code (AC-2), the diagnosis prompt's `$base_rerun`
text and the `_base_rerun_for_prompt` signature (AC-3)."""

import inspect
import os
import re
import unittest

from revali import EXIT_ACTION, EXIT_OK
from revali.runners import TIMEOUT_EXIT, render_script
from revali.state import State
from revali.validate import BaseRerun, _base_rerun_for_prompt
from tests.helpers import ROOT, RepoCase, claude_entry, git, run_cli

RDIR = ".revali/feature__mul"
PROMPT = RDIR + "/logs/prompt-diagnose-1.md"
BASE_OUT = "AssertionError: 12 != 7 [base run]"
NO_RESULT = (
    "revali has no `new_test` result from the base branch (%s). Answer `introduced_by: unknown`."
)


def diagnosis(introduced_by="base"):
    return {
        "summary": "the product test fails because mul is wrong.",
        "cause": "code",
        "introduced_by": introduced_by,
        "failures": [
            {
                "test": "tests/test_review_mul.py::MulTests::test_product",
                "cause": "code",
                "introduced_by": introduced_by,
                "note": "expected 12, got 7",
            }
        ],
        "recommendation": "return a * b",
    }


def scenario(base_results=None):
    return {
        "default": 0,
        "results": {"validate-r1": {"new_test": 1}, "base-r1": base_results or {"new_test": 1}},
        "outputs": {"validate-r1": {"new_test": "branch\n"}, "base-r1": {"new_test": BASE_OUT}},
    }


class Case(RepoCase):
    def base_sha(self):
        return git(["rev-parse", "origin/main"], self.repo).strip()

    def set_validate(self, line):
        cfg = self.read("revali.toml").replace("[validate]\n", "[validate]\n%s\n" % line)
        self.write("revali.toml", cfg)
        self.commit_all("config: " + line)

    def run_failing(self, diag=None, sc=None):
        self.runner_scenario(sc or scenario())
        self.claude(claude_entry(), claude_entry(diag or diagnosis(), write_tests=False))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ACTION, out)
        return out

    def record(self):
        entry = State.load(self.rdir()).validations[-1]
        self.assertIn("base_rerun", entry)
        return entry["base_rerun"]


class BaseRerunField(Case):
    def test_null_after_a_pass(self):
        self.claude(claude_entry())
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIsNone(self.record())  # AC-1

    def test_usable_result(self):
        self.run_failing()
        rec = self.record()
        self.assertEqual(rec, {"sha": self.base_sha(), "ran": True, "exit": 1, "reason": ""})

    def test_usable_result_that_passed_on_base(self):
        self.run_failing(diagnosis("branch"), scenario({"new_test": 0}))
        rec = self.record()
        self.assertEqual(rec["exit"], 0)
        self.assertEqual(rec["reason"], "")
        self.assertTrue(rec["ran"])

    def test_not_run(self):
        self.set_validate("rerun_on_base = false")
        self.run_failing(diagnosis("unknown"))
        rec = self.record()
        self.assertEqual(rec["sha"], self.base_sha())
        self.assertFalse(rec["ran"])
        self.assertIsNone(rec["exit"])
        self.assertIn("rerun_on_base", rec["reason"])

    def test_not_run_when_the_existing_suite_failed(self):
        self.set_validate("reuse_baseline = false")
        sc = {
            "default": 0,
            "results": {"validate-r1": {"test": 1}},
            "outputs": {"validate-r1": {"test": "FAILED test_add\n"}},
        }
        self.run_failing(diagnosis("unknown"), sc)
        rec = self.record()
        self.assertFalse(rec["ran"])
        self.assertIsNone(rec["exit"])
        self.assertTrue(rec["reason"])

    def test_ran_without_a_usable_result(self):
        self.run_failing(diagnosis("unknown"), scenario({"new_test": TIMEOUT_EXIT}))
        rec = self.record()
        self.assertEqual(rec["sha"], self.base_sha())
        self.assertTrue(rec["ran"])
        self.assertIsNone(rec["exit"])
        self.assertIn("timed out", rec["reason"])

    def test_setup_failing_on_base(self):
        self.write(
            "revali.toml",
            self.read("revali.toml").replace('setup = ""', 'setup = "pip install nothing"'),
        )
        self.commit_all("setup step")
        self.run_failing(diagnosis("unknown"), scenario({"setup": 1}))
        rec = self.record()
        self.assertTrue(rec["ran"])
        self.assertIsNone(rec["exit"])
        self.assertIn("setup", rec["reason"])


class TimeoutExit(unittest.TestCase):
    def test_one_constant(self):
        self.assertEqual(TIMEOUT_EXIT, 124)  # AC-2

    def test_sandbox_script_uses_it(self):
        text = render_script("src", "/l", "/e", "abc", [("new_test", "x")], "v", "/sb", 60)
        self.assertNotIn("__TIMEOUT_EXIT__", text)
        self.assertIn('"$rc" -eq %d' % TIMEOUT_EXIT, text)

    def test_no_other_literal_under_revali(self):
        offenders = []
        package = os.path.join(ROOT, "revali")
        for dirpath, _, names in os.walk(package):
            for name in names:
                if not name.endswith(".py"):
                    continue
                path = os.path.join(dirpath, name)
                with open(path, "r", encoding="utf-8") as fh:
                    lines = fh.read().splitlines()
                for no, line in enumerate(lines, 1):
                    if re.search(r"(?<![\w.])124(?![\w.])", line):
                        offenders.append("%s:%d" % (os.path.relpath(path, package), no))
        self.assertEqual(offenders, ["runners.py:%d" % _constant_line()])


def _constant_line():
    with open(os.path.join(ROOT, "revali", "runners.py"), "r", encoding="utf-8") as fh:
        for no, line in enumerate(fh.read().splitlines(), 1):
            if line.startswith("TIMEOUT_EXIT ="):
                return no
    return 0


class PromptText(Case):
    def test_takes_a_base_rerun_not_none(self):
        # AC-3
        param = inspect.signature(_base_rerun_for_prompt).parameters["rerun"]
        self.assertIs(param.annotation, BaseRerun)
        self.assertIs(param.default, inspect.Parameter.empty)
        text = _base_rerun_for_prompt(BaseRerun(sha="a" * 40, reason="[validate] off"))
        self.assertEqual(text, NO_RESULT % "[validate] off")

    def test_ran_with_a_usable_result(self):
        self.run_failing()
        prompt = self.read(PROMPT)
        self.assertIn("`new_test` exited 1 on base", prompt)
        self.assertIn(BASE_OUT, prompt)
        self.assertNotIn("Answer `introduced_by: unknown`", prompt)

    def test_ran_without_a_usable_result(self):
        self.run_failing(diagnosis("unknown"), scenario({"new_test": TIMEOUT_EXIT}))
        prompt = self.read(PROMPT)
        reason = "new_test timed out on base (exit %d)" % TIMEOUT_EXIT
        self.assertIn(NO_RESULT % reason, prompt)
        self.assertNotIn(BASE_OUT, prompt)

    def test_not_run(self):
        self.set_validate("rerun_on_base = false")
        self.run_failing(diagnosis("unknown"))
        self.assertIn(NO_RESULT % "[validate] rerun_on_base is false", self.read(PROMPT))


if __name__ == "__main__":
    unittest.main()
