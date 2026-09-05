"""A Reviewer session that dies (budget, crash) is reported plainly and leaves the tree clean."""

import json
import unittest

from revali import EXIT_ERROR
from tests.helpers import TEST_REVIEW_MUL, RepoCase, git, run_cli


def budget_payload():
    return json.dumps(
        {
            "type": "result",
            "subtype": "error_max_budget_usd",
            "is_error": True,
            "num_turns": 9,
            "duration_ms": 90000,
            "total_cost_usd": 1.05,
            "errors": ["Reached maximum budget ($1)"],
            "terminal_reason": "budget_exhausted",
            "result": None,
            "modelUsage": {"claude-fable-5": {"costUSD": 1.05}},
            "permission_denials": [],
        }
    )


class ReviewerDiedTests(RepoCase):
    def test_budget_exhaustion_is_named_and_leftovers_removed(self):
        self.claude(
            {
                "exit": 1,
                "raw_stdout": budget_payload(),
                "write_files": {
                    "tests/test_review_mul.py": TEST_REVIEW_MUL,
                    "tests/test_review_half.py": "import unittest\n",
                },
            }
        )
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertIn("reviewer ran out of budget ($1.00) after 9 turns, spent $1.05", out)
        self.assertIn("removed 2 unfinished test file(s)", out)
        self.assertFalse(self.exists("tests/test_review_mul.py"))
        self.assertFalse(self.exists("tests/test_review_half.py"))
        self.assertEqual(git(["status", "--porcelain"], self.repo).strip(), "")
        # a rerun is not blocked by a dirty tree
        code, out = run_cli(["run", "--foreground"])
        self.assertNotIn("working tree is not clean", out)

    def test_unusable_output_also_cleans_up(self):
        self.claude(
            {
                "exit": 0,
                "structured_output": {"verdict": "MAYBE"},
                "write_files": {"tests/test_review_mul.py": TEST_REVIEW_MUL},
            }
        )
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertIn("does not match the schema", out)
        self.assertIn("removed 1 unfinished test file(s)", out)
        self.assertEqual(git(["status", "--porcelain"], self.repo).strip(), "")

    def test_plain_text_error_is_quoted(self):
        self.claude({"exit": 2, "raw_stdout": "error: unknown option --json-schema\n"})
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertIn("returned invalid JSON (exit 2): error: unknown option --json-schema", out)

    def test_unimplemented_engine_stops_in_preflight(self):
        cfg = self.read("revali.toml").replace("[validate]\n", '[validate]\nengine = "codex"\n')
        self.write("revali.toml", cfg + '\n[engines.codex]\ntiers = ["mini", "max"]\n')
        self.commit_all("codex")
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertIn("validate.engine: engine 'codex' is not implemented (available: claude)", out)
        self.assertFalse(any(c["argv"][:2] == ["pr", "create"] for c in self.fake_calls("gh")))
        self.assertEqual(self.fake_calls("claude"), [])

    def test_untracked_files_outside_the_pattern_are_kept(self):
        self.claude(
            {
                "exit": 1,
                "raw_stdout": budget_payload(),
                "write_files": {"tests/helper_notes.txt": "keep me\n"},
            }
        )
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertNotIn("unfinished test file", out)
        self.assertTrue(self.exists("tests/helper_notes.txt"))


if __name__ == "__main__":
    unittest.main()
