"""Acceptance tests for `revali merge` after a pre-existing bug was reported: a comment on
every issue a branch commit says the PR fixes (AC-10), and the issue count in history and
`revali stats` (AC-11)."""

import os
import unittest

from revali import EXIT_ACTION, EXIT_OK
from revali.config import history_path, load_user_config
from revali.state import State, read_history
from tests.helpers import RepoCase, claude_entry, git, run_cli

TEST_ID = "tests/test_review_mul.py::MulTests::test_product"
OTHER_ID = "tests/test_review_mul.py::MulTests::test_zero"
SUMMARY = "mul adds instead of multiplying, so the product test fails."
FIX = "return a * b instead of a + b"
WITHHELD = "(withheld: non-private repository)"


def diagnosis(test=TEST_ID):
    return {
        "summary": SUMMARY,
        "cause": "code",
        "introduced_by": "base",
        "failures": [{"test": test, "cause": "code", "introduced_by": "base", "note": "n"}],
        "recommendation": FIX,
    }


def failing(round_no):
    v, b = "validate-r%d" % round_no, "base-r%d" % round_no
    return {
        "default": 0,
        "results": {v: {"new_test": 1}, b: {"new_test": 1}},
        "outputs": {v: {"new_test": "branch failure\n"}, b: {"new_test": "base failure\n"}},
    }


class MergeCase(RepoCase):
    def setUp(self):
        super().setUp()
        os.environ["REVALI_POLL_SECONDS"] = "0.01"
        self.round_no = 0

    def comments(self):
        return [c for c in self.fake_calls("gh") if c["argv"][:2] == ["issue", "comment"]]

    def touch(self, message):
        subject = message.splitlines()[0]
        self.write("src/calc.py", self.read("src/calc.py") + "\n# %s\n" % subject)
        self.commit_all(message)
        return git(["rev-parse", "HEAD"], self.repo).strip()

    def report_bug(self, diag=None):
        """One run whose validation fails and is diagnosed as a pre-existing bug."""
        self.round_no += 1
        if self.round_no > 1:
            self.touch("another try")
        self.runner_scenario(failing(self.round_no))
        entry = claude_entry(write_tests=self.round_no == 1)
        self.claude(entry, claude_entry(diag or diagnosis(), write_tests=False))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ACTION, out)

    def fix_and_pass(self, message):
        """A commit with `message`, then a run that passes validation. Returns its sha."""
        self.round_no += 1
        sha = self.touch(message)
        self.runner_scenario({"default": 0})
        self.claude(claude_entry(write_tests=self.round_no == 1))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        return sha

    def merge(self):
        code, out = run_cli(["merge"])
        self.assertEqual(code, EXIT_OK, out)
        return out


class CommentOnMerge(MergeCase):
    def test_fixes_keyword(self):
        self.report_bug()
        sha = self.fix_and_pass("fix: multiply properly\n\nFixes #41\n")
        out = self.merge()
        comments = self.comments()
        self.assertEqual(len(comments), 1)  # AC-10
        self.assertEqual(comments[0]["argv"][2], "41")
        body = comments[0]["body"]
        self.assertIn("#7", body)  # the merged PR
        self.assertIn(sha[:10], body)  # the referencing commit
        self.assertIn("fix: multiply properly", body)
        self.assertIn("code", body)  # the recorded cause
        self.assertIn(FIX, body)  # the recorded recommendation
        self.assertIn("#41", out)

    def test_closes_and_resolves_case_insensitive(self):
        self.report_bug()
        self.fix_and_pass("CLOSES #41")
        self.merge()
        self.assertEqual([c["argv"][2] for c in self.comments()], ["41"])

    def test_resolves_in_the_body_of_an_earlier_commit(self):
        self.report_bug()
        first = self.touch("wip\n\nresolves #41 (the base bug)\n")
        self.fix_and_pass("more work")
        self.merge()
        comments = self.comments()
        self.assertEqual(len(comments), 1)
        self.assertIn(first[:10], comments[0]["body"])

    def test_no_reference_no_comment(self):
        self.report_bug()
        self.fix_and_pass("fix: multiply (issue #41 is related but not fixed here)")
        self.merge()
        self.assertEqual(self.comments(), [])  # AC-10

    def test_only_the_referenced_issue_is_commented(self):
        self.report_bug()
        url = "https://github.example/me/sample/issues/42"
        self.scenario({"issue_create": {"number": 42, "url": url}})
        self.report_bug(diagnosis(OTHER_ID))
        self.assertEqual([i["number"] for i in State.load(self.rdir()).issues], [41, 42])
        self.fix_and_pass("fix: zero case\n\nFixes #42\n")
        self.merge()
        self.assertEqual([c["argv"][2] for c in self.comments()], ["42"])

    def test_a_number_that_is_not_ours_is_ignored(self):
        self.report_bug()
        self.fix_and_pass("Fixes #99")
        self.merge()
        self.assertEqual(self.comments(), [])

    def test_failed_comment_does_not_fail_the_merge(self):
        self.scenario({"issue_comment_exit": 1})
        self.report_bug()
        self.fix_and_pass("Fixes #41")
        out = self.merge()  # AC-10: exit 0
        self.assertIn("MERGED", out)
        self.assertIn("warning", out)
        self.assertIn("#41", out)

    def test_non_private_repository_withholds_the_fix(self):
        self.scenario({"visibility": "PUBLIC"})
        self.report_bug()
        self.fix_and_pass("Fixes #41")
        self.merge()
        body = self.comments()[0]["body"]
        self.assertIn("#7", body)
        self.assertIn("code", body)
        self.assertIn(WITHHELD, body)  # AC-10
        self.assertNotIn(FIX, body)
        self.assertNotIn(SUMMARY, body)


class History(MergeCase):
    def rows(self):
        return read_history(history_path(load_user_config()))

    def test_rows_and_stats_count_the_issues(self):
        self.report_bug()
        rows = self.rows()
        self.assertEqual(rows[-1]["issues"], 1)  # AC-11: the `run` row
        self.fix_and_pass("Fixes #41")
        self.merge()
        rows = self.rows()
        self.assertEqual(rows[-1]["stage"], "merged")
        self.assertEqual(rows[-1]["issues"], 1)  # the `merge` row
        code, out = run_cli(["stats"])
        self.assertEqual(code, 0, out)
        self.assertIn("issues opened: 1", out)

    def test_zero_without_an_issue(self):
        self.fix_and_pass("plain")
        self.assertEqual(self.rows()[-1]["issues"], 0)
        code, out = run_cli(["stats"])
        self.assertEqual(code, 0, out)
        self.assertIn("issues opened: 0", out)


if __name__ == "__main__":
    unittest.main()
