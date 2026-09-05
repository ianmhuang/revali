"""Acceptance tests for the public-repository change (AC-1, AC-2).

A repository the user owns that is not PRIVATE is accepted by preflight with a
note, and every PR comment revali posts on it is a summary: verdict, model,
cost, finding id / severity / kind / file:line, counts of questions and scope
notes, test files, AC coverage, validation exit codes, diagnosis cause. The
finding text, suggestions, questions, scope notes, failure output and
diagnosis text never reach the comment. A PRIVATE repository still gets the
full text. Black-box through the CLI and the fake gh / claude / runner.
"""

import os
import unittest

from revali import EXIT_ACTION, EXIT_ERROR, EXIT_OK
from revali.preflight import Stop, preflight
from tests.helpers import RepoCase, approve_response, claude_entry, run_cli

FINDING_TEXT = "mul silently truncates floats to int before multiplying"
SUGGESTION = "multiply the operands as given and let the caller convert"
LOW_TEXT = "the docstring still says the module has two functions"
QUESTION = "should mul accept a float operand at all?"
SCOPE_NOTE = "the diff also touches the module docstring, which change.md does not mention"
FAILURE_OUTPUT = "AssertionError: 7 != 12 in test_product"
DIAG_SUMMARY = "mul adds instead of multiplying; the product test is right."
DIAG_NOTE = "expected 12, got 7"
RECOMMENDATION = "return a * b instead of a + b"


def changes_requested():
    return approve_response(
        verdict="CHANGES_REQUESTED",
        findings=[
            {
                "id": "F1",
                "severity": "high",
                "kind": "correctness",
                "file": "src/calc.py",
                "line": 3,
                "text": FINDING_TEXT,
                "suggestion": SUGGESTION,
            },
            {
                "id": "F2",
                "severity": "low",
                "kind": "convention",
                "file": "src/calc.py",
                "line": 1,
                "text": LOW_TEXT,
                "suggestion": "",
            },
        ],
        scope_mismatch=[SCOPE_NOTE],
    )


def needs_info():
    return approve_response(
        verdict="NEEDS_INFO", questions=[QUESTION, QUESTION + " (and for ints?)"]
    )


def diagnosis():
    return {
        "summary": DIAG_SUMMARY,
        "cause": "code",
        "failures": [
            {
                "test": "tests/test_review_mul.py::MulTests::test_product",
                "cause": "code",
                "note": DIAG_NOTE,
            }
        ],
        "recommendation": RECOMMENDATION,
    }


FAILING_VALIDATION = {
    "default": 0,
    "results": {"validate-r1": {"new_test": 1}},
    "outputs": {"validate-r1": {"new_test": FAILURE_OUTPUT + "\n"}},
}


class CommentCase(RepoCase):
    def comment(self, name):
        return self.read(os.path.join(".revali", "feature__mul", "logs", "comment-%s.md" % name))

    def posted_comment_files(self):
        """Basenames of the files gh was asked to post as PR comments, in order."""
        names = []
        for call in self.fake_calls("gh"):
            argv = call["argv"]
            if argv[:2] == ["pr", "comment"] and "--body-file" in argv:
                names.append(os.path.basename(argv[argv.index("--body-file") + 1]))
        return names


# ---- AC-1 -------------------------------------------------------------------


class PreflightAcceptsOwnNonPrivateRepo(RepoCase):
    def test_public_repo_passes_preflight(self):
        self.scenario({"visibility": "PUBLIC"})
        ctx = preflight(self.repo)
        self.assertEqual(ctx.repo.visibility, "PUBLIC")
        self.assertEqual(ctx.branch, "feature/mul")

    def test_internal_repo_passes_preflight(self):
        self.scenario({"visibility": "INTERNAL"})
        ctx = preflight(self.repo)
        self.assertEqual(ctx.repo.visibility, "INTERNAL")

    def test_public_repo_cli_preflight_says_comments_are_summaries(self):
        self.scenario({"visibility": "PUBLIC"})
        code, out = run_cli(["preflight"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("preflight OK", out)
        self.assertIn("summaries", out)
        self.assertIn("public", out.lower())

    def test_private_repo_cli_preflight_has_no_summary_note(self):
        code, out = run_cli(["preflight"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertNotIn("summaries", out)

    def test_public_repo_owned_by_someone_else_is_still_refused(self):
        self.scenario({"visibility": "PUBLIC", "owner": "someone-else"})
        with self.assertRaises(Stop) as cm:
            preflight(self.repo)
        self.assertEqual(cm.exception.exit_code, EXIT_ERROR)
        self.assertIn("your own repos", cm.exception.message)

    def test_public_repo_dry_run_pushes_nothing(self):
        self.scenario({"visibility": "PUBLIC"})
        code, out = run_cli(["run", "--dry-run"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("summaries", out)
        self.assertEqual(
            [c for c in self.fake_calls("gh") if c["argv"][:2] == ["pr", "create"]], []
        )


# ---- AC-2: public repository -------------------------------------------------


class PublicRepoReviewComment(CommentCase):
    def setUp(self):
        super().setUp()
        self.scenario({"visibility": "PUBLIC"})

    def test_changes_requested_comment_lists_findings_without_text(self):
        self.claude(claude_entry(changes_requested()))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ACTION, out)
        c = self.comment("review-1")
        self.assertIn("# Review round 1: CHANGES_REQUESTED", c)
        self.assertIn("claude-fable-5", c)
        self.assertIn("$0.50", c)
        self.assertIn("F1 [high correctness] `src/calc.py:3`", c)
        self.assertIn("F2 [low convention] `src/calc.py:1`", c)
        for secret in (FINDING_TEXT, SUGGESTION, LOW_TEXT, SCOPE_NOTE):
            self.assertNotIn(secret, c)
        self.assertIn("1 scope note(s)", c)
        self.assertIn("`tests/test_review_mul.py` covers AC-1, AC-2", c)
        self.assertIn("- AC-1: covered", c)
        self.assertIn("- AC-2: covered", c)
        # the author still gets the full review locally
        full = self.read(os.path.join(".revali", "feature__mul", "review-1.md"))
        self.assertIn(FINDING_TEXT, full)
        self.assertIn(SUGGESTION, full)
        self.assertIn(SCOPE_NOTE, full)
        self.assertIn(FINDING_TEXT, out)

    def test_the_summary_is_what_gh_posts(self):
        self.claude(claude_entry(changes_requested()))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ACTION, out)
        self.assertEqual(self.posted_comment_files(), ["comment-review-1.md"])

    def test_needs_info_comment_counts_questions_only(self):
        self.claude(claude_entry(needs_info(), write_tests=False))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ACTION, out)
        c = self.comment("review-1")
        self.assertIn("# Review round 1: NEEDS_INFO", c)
        self.assertIn("2 question(s)", c)
        self.assertNotIn(QUESTION, c)
        self.assertIn(QUESTION, out)

    def test_pr_body_withholds_the_request(self):
        self.claude(claude_entry(changes_requested()))
        run_cli(["run", "--foreground"])
        body = self.read(os.path.join(".revali", "feature__mul", "logs", "pr-body.md"))
        self.assertIn("## Request", body)
        self.assertIn("withheld", body)
        self.assertNotIn("multiplies two numbers", body)
        self.assertIn("AC-1", body)  # the acceptance criteria are still public


class PublicRepoValidationComment(CommentCase):
    def setUp(self):
        super().setUp()
        self.scenario({"visibility": "PUBLIC"})

    def test_pass_comment_has_exit_codes_and_no_logs(self):
        self.claude(claude_entry())
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        v = self.comment("validate-1")
        self.assertIn("## Validation 1: PASS", v)
        # since the baseline-reuse change (feature/skip-unchanged-suite) round-1 validation
        # leaves `test` out and says why instead of listing it
        self.assertIn("existing suite not rerun", v)
        self.assertNotIn("| test | 0", v)
        self.assertIn("| new_test | 0", v)
        self.assertNotIn("fake test output", v)
        self.assertNotIn("fake new_test output", v)
        self.assertNotIn(".log", v)
        self.assertEqual(
            self.posted_comment_files(), ["comment-review-1.md", "comment-validate-1.md"]
        )

    def test_fail_comment_names_the_cause_without_the_text(self):
        self.runner_scenario(FAILING_VALIDATION)
        self.claude(claude_entry(), claude_entry(diagnosis(), write_tests=False))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ACTION, out)
        v = self.comment("validate-1")
        self.assertIn("## Validation 1: FAIL", v)
        self.assertIn("| new_test | 1", v)
        self.assertIn("new_test", v)
        self.assertIn("**code**", v)
        for secret in (FAILURE_OUTPUT, DIAG_SUMMARY, DIAG_NOTE, RECOMMENDATION):
            self.assertNotIn(secret, v)
        # locally the author still sees the diagnosis
        tests_md = self.read(os.path.join(".revali", "feature__mul", "tests.md"))
        self.assertIn(DIAG_SUMMARY, tests_md)
        self.assertIn(RECOMMENDATION, tests_md)
        self.assertIn(RECOMMENDATION, out)

    def test_fail_without_diagnosis_leaks_nothing(self):
        self.runner_scenario(FAILING_VALIDATION)
        self.claude(claude_entry(), claude_entry({"unexpected": "shape"}, write_tests=False))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ACTION, out)
        v = self.comment("validate-1")
        self.assertIn("## Validation 1: FAIL", v)
        self.assertNotIn(FAILURE_OUTPUT, v)


class InternalRepoGetsSummariesToo(CommentCase):
    def test_internal_repo_review_comment_is_a_summary(self):
        self.scenario({"visibility": "INTERNAL"})
        self.claude(claude_entry(changes_requested()))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ACTION, out)
        c = self.comment("review-1")
        self.assertIn("F1 [high correctness] `src/calc.py:3`", c)
        self.assertNotIn(FINDING_TEXT, c)
        self.assertNotIn(SUGGESTION, c)


# ---- AC-2: private repository unchanged -------------------------------------


class PrivateRepoStillPostsFullText(CommentCase):
    def test_review_comment_has_the_full_text(self):
        self.claude(claude_entry(changes_requested()))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ACTION, out)
        c = self.comment("review-1")
        self.assertIn(FINDING_TEXT, c)
        self.assertIn(SUGGESTION, c)
        self.assertIn(SCOPE_NOTE, c)
        self.assertNotIn("summary only", c)
        body = self.read(os.path.join(".revali", "feature__mul", "logs", "pr-body.md"))
        self.assertIn("multiplies two numbers", body)
        self.assertNotIn("withheld", body)

    def test_needs_info_comment_has_the_questions(self):
        self.claude(claude_entry(needs_info(), write_tests=False))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ACTION, out)
        self.assertIn(QUESTION, self.comment("review-1"))

    def test_validation_comment_has_the_diagnosis_text(self):
        self.runner_scenario(FAILING_VALIDATION)
        self.claude(claude_entry(), claude_entry(diagnosis(), write_tests=False))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ACTION, out)
        v = self.comment("validate-1")
        self.assertIn(FAILURE_OUTPUT, v)
        self.assertIn(DIAG_SUMMARY, v)
        self.assertIn(RECOMMENDATION, v)
        self.assertNotIn("summary only", v)


if __name__ == "__main__":
    unittest.main()
