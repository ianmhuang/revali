"""A repository that is not private gets summary comments; a private one gets the full text."""

import unittest

from revali import EXIT_ACTION, EXIT_OK
from tests.helpers import RepoCase, approve_response, claude_entry, run_cli

FINDING_TEXT = "the loop never terminates when the list is empty"
SUGGESTION = "guard the empty list before the loop"
QUESTION = "is mul expected to accept floats?"


def review_with_a_low_finding():
    return approve_response(
        findings=[
            {
                "id": "F1",
                "severity": "low",
                "kind": "convention",
                "file": "src/calc.py",
                "line": 7,
                "text": FINDING_TEXT,
                "suggestion": SUGGESTION,
            }
        ],
        scope_mismatch=["the diff also renames a helper"],
    )


def diagnosis():
    return {
        "summary": "mul returns a + b; the product test fails.",
        "cause": "code",
        "failures": [
            {
                "test": "tests/test_review_mul.py::MulTests::test_product",
                "cause": "code",
                "note": "expected 12, got 7",
            }
        ],
        "recommendation": "return a * b",
    }


class PublicRepoComments(RepoCase):
    def setUp(self):
        super().setUp()
        self.scenario({"visibility": "PUBLIC"})

    def comment(self, name):
        return self.read(".revali/feature__mul/logs/comment-%s.md" % name)

    def test_review_comment_is_a_summary(self):
        self.claude(claude_entry(review_with_a_low_finding()))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("public repository: PR comments will carry summaries only", out)
        c = self.comment("review-1")
        self.assertIn("# Review round 1: APPROVE", c)
        self.assertIn("F1 [low convention] `src/calc.py:7`", c)
        self.assertNotIn(FINDING_TEXT, c)
        self.assertNotIn(SUGGESTION, c)
        self.assertNotIn("renames a helper", c)
        self.assertIn("1 scope note(s)", c)
        self.assertIn("- AC-1: covered", c)
        self.assertIn("`tests/test_review_mul.py` covers AC-1, AC-2", c)
        self.assertIn("summary only", c)
        # the full text is still on disk
        self.assertIn(FINDING_TEXT, self.read(".revali/feature__mul/review-1.md"))
        # the PR body withholds the request
        body = self.read(".revali/feature__mul/logs/pr-body.md")
        self.assertIn("(withheld: public repository)", body)
        self.assertNotIn("multiplies two numbers", body)
        # the validation comment shows exit codes, no logs
        v = self.comment("validate-1")
        self.assertIn("## Validation 1: PASS", v)
        self.assertIn("| new_test | 0 |", v)
        self.assertNotIn("fake new_test output", v)

    def test_questions_and_diagnosis_text_are_withheld(self):
        self.claude(
            claude_entry(
                approve_response(verdict="NEEDS_INFO", questions=[QUESTION]), write_tests=False
            )
        )
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ACTION, out)
        c = self.comment("review-1")
        self.assertIn("1 question(s) for the author", c)
        self.assertNotIn(QUESTION, c)
        self.assertIn(QUESTION, out)  # the author still sees it locally

    def test_failed_validation_comment_names_the_cause_only(self):
        self.runner_scenario(
            {
                "default": 0,
                "results": {"validate-r1": {"new_test": 1}},
                "outputs": {"validate-r1": {"new_test": "FAIL: test_product expected 12 got 7"}},
            }
        )
        self.claude(claude_entry(), claude_entry(diagnosis(), write_tests=False))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ACTION, out)
        v = self.comment("validate-1")
        self.assertIn("## Validation 1: FAIL", v)
        self.assertIn("failed at step `new_test`", v)
        self.assertIn("cause **code**, 1 failure(s) examined", v)
        self.assertNotIn("expected 12", v)
        self.assertNotIn("return a * b", v)


class PrivateRepoComments(RepoCase):
    def test_private_repo_posts_the_full_text(self):
        self.claude(claude_entry(review_with_a_low_finding()))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertNotIn("summaries only", out)
        c = self.read(".revali/feature__mul/logs/comment-review-1.md")
        self.assertIn(FINDING_TEXT, c)
        self.assertIn(SUGGESTION, c)
        self.assertNotIn("summary only", c)
        body = self.read(".revali/feature__mul/logs/pr-body.md")
        self.assertNotIn("withheld", body)


if __name__ == "__main__":
    unittest.main()
