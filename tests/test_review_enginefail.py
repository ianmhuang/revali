"""AC-4 / AC-5: a session that dies says why (budget, CLI error text) and a review
round that stops before its tests are committed leaves the tree as it found it,
so the next run is not blocked."""
import json
import unittest

from tests.helpers import RepoCase, TEST_REVIEW_MUL, claude_entry, git, run_cli
from revali import EXIT_ACTION, EXIT_ERROR, EXIT_OK
from revali.state import State


def cli_result(**kw):
    """A claude `--output-format json` result object; kw overrides."""
    payload = {"type": "result", "subtype": "success", "is_error": False, "num_turns": 3,
               "duration_ms": 1000, "total_cost_usd": 0.1, "result": "", "errors": [],
               "modelUsage": {"claude-fable-5": {"costUSD": 0.1}}, "permission_denials": []}
    payload.update(kw)
    return json.dumps(payload)


def budget_exhausted(spent=1.05, turns=9):
    return cli_result(subtype="error_max_budget_usd", is_error=True, num_turns=turns,
                      total_cost_usd=spent, result=None, errors=["Reached maximum budget"],
                      terminal_reason="budget_exhausted")


class AC4FailureIsNamed(RepoCase):
    def test_reviewer_budget_exhaustion(self):
        self.claude({"exit": 1, "raw_stdout": budget_exhausted(spent=1.05, turns=9)})
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR, out)
        # fixture: [review] budget_usd = 1.0
        self.assertIn("reviewer ran out of budget ($1.00) after 9 turns, spent $1.05", out)
        self.assertIn("raise budget_usd", out)
        self.assertNotIn("session failed (exit 1): None", out)
        state = State.load(self.rdir())
        self.assertEqual(state.stage, "error")
        self.assertIn("ran out of budget", state.message)

    def test_other_cli_error_quotes_its_text(self):
        self.claude({"exit": 1, "raw_stdout": cli_result(
            subtype="error_during_execution", is_error=True, result="",
            errors=["Invalid API key. Fix external API key"])})
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertIn("reviewer session failed (exit 1): Invalid API key. Fix external API key", out)

    def test_null_result_without_errors_says_no_error_text(self):
        # A JSON null result must not be quoted as the text "None".
        self.claude({"exit": 1, "raw_stdout": cli_result(
            subtype="error_during_execution", is_error=True, result=None, errors=[])})
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertIn("reviewer session failed (exit 1): no error text", out)
        self.assertNotIn("(exit 1): None", out)

    def test_plain_text_cli_error_is_quoted(self):
        # The CLI printed no JSON at all (bad flag, auth prompt): its own words are the message.
        self.claude({"exit": 2, "raw_stdout": "error: unknown option '--json-schema'\n"})
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertIn("reviewer returned invalid JSON (exit 2): error: unknown option '--json-schema'", out)
        self.assertIn("raw output saved to", out)

    def test_diagnoser_budget_exhaustion_is_named_in_tests_md(self):
        self.runner_scenario({"default": 0, "results": {"validate-r1": {"new_test": 1}},
                              "outputs": {"validate-r1": {"new_test": "AssertionError: 12 != 7"}}})
        self.claude(claude_entry(), {"exit": 1, "raw_stdout": budget_exhausted(spent=0.61, turns=4)})
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ACTION, out)
        # fixture: [validate] budget_usd = 0.5; the validation still reports FAIL
        expected = "diagnoser ran out of budget ($0.50) after 4 turns, spent $0.61"
        self.assertIn(expected, out)
        self.assertIn(expected, self.read(".revali/feature__mul/tests.md"))
        self.assertEqual(State.load(self.rdir()).last_verdict, "FAIL")


class AC5TreeLeftClean(RepoCase):
    def assert_clean_and_rerun_completes(self, expected_claude_calls):
        self.assertEqual(git(["status", "--porcelain"], self.repo).strip(), "")
        self.claude(claude_entry())
        code, out = run_cli(["run", "--foreground"])
        self.assertNotIn("working tree is not clean", out)
        self.assertEqual(code, EXIT_OK, out)
        self.assertEqual(len(self.fake_calls("claude")), expected_claude_calls)

    def test_pattern_files_removed_named_and_rerun_unblocked(self):
        self.claude({"exit": 1, "raw_stdout": budget_exhausted(),
                     "write_files": {"tests/test_review_mul.py": TEST_REVIEW_MUL,
                                     "tests/sub/test_review_deep.py": "import unittest\n"}})
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertFalse(self.exists("tests/test_review_mul.py"))
        self.assertFalse(self.exists("tests/sub/test_review_deep.py"))
        self.assertIn("removed 2 unfinished test file(s)", out)
        self.assertIn("tests/test_review_mul.py", out)
        self.assertIn("tests/sub/test_review_deep.py", out)
        log = self.read(".revali/feature__mul/logs/revali.log")
        self.assertIn("tests/sub/test_review_deep.py", log)
        self.assert_clean_and_rerun_completes(expected_claude_calls=2)

    def test_unusable_output_cleans_up(self):
        # exit 0 but the answer fails the shape check: the session "succeeded" from the
        # CLI's point of view, yet the round stops before any commit.
        self.claude({"exit": 0, "structured_output": {"verdict": "MAYBE"},
                     "write_files": {"tests/test_review_mul.py": TEST_REVIEW_MUL}})
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertIn("does not match the schema", out)
        self.assertIn("removed 1 unfinished test file(s)", out)
        self.assertIn("tests/test_review_mul.py", out)
        self.assertFalse(self.exists("tests/test_review_mul.py"))
        self.assert_clean_and_rerun_completes(expected_claude_calls=2)

    def test_smoke_failing_twice_cleans_up(self):
        self.runner_scenario({"default": 0, "results": {"smoke-r1-1": {"new_test": 2},
                                                        "smoke-r1-2": {"new_test": 2}}})
        self.claude(claude_entry(), claude_entry())
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertIn("still cannot run", out)
        self.assertIn("removed 1 unfinished test file(s)", out)
        self.assertFalse(self.exists("tests/test_review_mul.py"))
        self.runner_scenario({"default": 0})
        self.assert_clean_and_rerun_completes(expected_claude_calls=3)

    def test_guard_offender_cleans_up(self):
        # The reviewer wrote outside test_dir: the guard reverts that and stops the round;
        # its half-written test file must go too, or the next run is blocked.
        entry = claude_entry()
        entry["write_files"]["src/evil.py"] = "print('hi')\n"
        self.claude(entry)
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertIn("outside tests", out)
        self.assertFalse(self.exists("src/evil.py"))
        self.assertFalse(self.exists("tests/test_review_mul.py"))
        self.assertIn("removed 1 unfinished test file(s)", out)
        self.assert_clean_and_rerun_completes(expected_claude_calls=2)

    def test_untracked_files_outside_the_pattern_are_kept(self):
        self.claude({"exit": 1, "raw_stdout": budget_exhausted(),
                     "write_files": {"tests/notes.txt": "keep\n", "tests/helper_review.py": "keep\n",
                                     "src/scratch.py": "keep\n"}})
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertNotIn("unfinished test file", out)
        for rel in ("tests/notes.txt", "tests/helper_review.py", "src/scratch.py"):
            self.assertTrue(self.exists(rel), rel)

    def test_matching_files_outside_test_dir_are_kept(self):
        # Only test_dir is cleaned: a pattern match elsewhere is not revali's to delete.
        self.claude({"exit": 1, "raw_stdout": budget_exhausted(),
                     "write_files": {"src/test_review_x.py": "keep\n"}})
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertTrue(self.exists("src/test_review_x.py"))
        self.assertNotIn("unfinished test file", out)

    def test_successful_session_keeps_its_tests(self):
        self.claude(claude_entry())
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertNotIn("unfinished test file", out)
        self.assertIn("tests/test_review_mul.py", git(["show", "--stat", "--format=", "HEAD"], self.repo))


if __name__ == "__main__":
    unittest.main()
