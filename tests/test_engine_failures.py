"""A Reviewer session that dies (budget, crash) is reported plainly and leaves the tree clean."""
import json
import unittest

from tests.helpers import RepoCase, TEST_REVIEW_MUL, git, run_cli
from revali import EXIT_ERROR


def budget_payload():
    return json.dumps({
        "type": "result", "subtype": "error_max_budget_usd", "is_error": True, "num_turns": 9,
        "duration_ms": 90000, "total_cost_usd": 1.05, "errors": ["Reached maximum budget ($1)"],
        "terminal_reason": "budget_exhausted", "result": None,
        "modelUsage": {"claude-fable-5": {"costUSD": 1.05}}, "permission_denials": [],
    })


class ReviewerDiedTests(RepoCase):
    def test_budget_exhaustion_is_named_and_leftovers_removed(self):
        self.claude({"exit": 1, "raw_stdout": budget_payload(),
                     "write_files": {"tests/test_review_mul.py": TEST_REVIEW_MUL,
                                     "tests/test_review_half.py": "import unittest\n"}})
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

    def test_untracked_files_outside_the_pattern_are_kept(self):
        self.claude({"exit": 1, "raw_stdout": budget_payload(),
                     "write_files": {"tests/helper_notes.txt": "keep me\n"}})
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertNotIn("unfinished test file", out)
        self.assertTrue(self.exists("tests/helper_notes.txt"))


if __name__ == "__main__":
    unittest.main()
