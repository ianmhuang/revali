"""Acceptance tests for feature/pr-body-no-request (AC-1 .. AC-4).

The body revali hands to `gh pr create` and to every `gh pr edit` after a round is
change.md without its `## Request` section, on a private repository as on a public one;
the other sections and the status table are unchanged. The section goes wherever it sits
in change.md, and only a heading line counts. The local change.md is not touched and is
still validated (an empty Request stops the run before the push), and the PR comment
rules (summaries on a non-private repository, full text on a private one) stay as they
were. Black-box through the CLI and the fake gh / claude / runner.
"""

import re
import unittest

from revali import EXIT_ERROR, EXIT_OK
from tests.fixtures.make_sample_repo import CHANGE_MD
from tests.helpers import RepoCase, approve_response, claude_entry, git, run_cli

REQUEST_LINE = "add a mul(a, b) function to calc that multiplies two numbers"
REQUEST_BLOCK = "## Request\n" + REQUEST_LINE + "\n\n"
# the fixture change.md with its Request section (heading, text, blank line) removed
WITHOUT_REQUEST = CHANGE_MD.replace(REQUEST_BLOCK, "", 1)
STATUS_HEADING = "\n## revali status\n"
SUMMARIES_LINE = "public repository: PR comments will carry summaries only"
FINDING_TEXT = "mul returns a float when both operands are large ints"


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
                "suggestion": "cast the product back to int",
            }
        ]
    )


class BodyCase(RepoCase):
    def bodies(self, verb):
        """The bodies gh received with `pr <verb> ... --body-file`, in order."""
        return [
            call["body"]
            for call in self.fake_calls("gh")
            if call["argv"][:2] == ["pr", verb] and "body" in call
        ]

    def assert_no_request(self, body):
        # a heading line, not the substring: `## Requests handled` is another section and stays
        # (test_a_longer_heading_is_not_the_request asserts that); edited by the author, see
        # response-1.md
        self.assertIsNone(re.search(r"^##\s+Request\s*$", body, re.M | re.I), body)
        self.assertNotIn(REQUEST_LINE, body)
        self.assertNotIn("withheld", body)

    def assert_other_sections_verbatim(self, body):
        """Everything but the Request section is on the PR, unchanged."""
        self.assertTrue(body.startswith("---\ntitle: Add mul to calc\nkind: feature\n"), body)
        for section in (
            "## What\nAdded `mul(a, b)` to `src/calc.py`.\n",
            "## Why\nThe calculator only had add and sub.\n",
            "## Goal\n`mul` multiplies two integers.\n",
            "## Acceptance criteria\n"
            "- AC-1: mul(a, b) returns the product of a and b for integers\n"
            "- AC-2: mul with zero returns zero\n",
            "## Out of scope\nDivision.\n",
            "## Dependencies\nnone\n",
        ):
            self.assertIn(section, body)

    def run_ok(self, response=None):
        self.claude(claude_entry(response if response is not None else approve_response()))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        return out


# ---- AC-1: the created and the edited body, public and private -----------------


class PublicRepoBody(BodyCase):
    def setUp(self):
        super().setUp()
        self.scenario({"visibility": "PUBLIC"})

    def test_created_body_is_change_md_without_the_request(self):  # AC-1
        self.run_ok()
        created = self.bodies("create")
        self.assertEqual(len(created), 1, self.fake_calls("gh"))
        self.assert_no_request(created[0])
        self.assert_other_sections_verbatim(created[0])
        self.assertEqual(created[0], WITHOUT_REQUEST)

    def test_every_edited_body_keeps_the_status_table_and_no_request(self):  # AC-1
        self.run_ok()
        edited = self.bodies("edit")
        self.assertTrue(edited, self.fake_calls("gh"))
        for body in edited:
            self.assert_no_request(body)
            self.assert_other_sections_verbatim(body)
            # the sections come first, verbatim, then the status table
            self.assertTrue(body.startswith(WITHOUT_REQUEST + STATUS_HEADING), body)
            self.assertIn("| round | verdict | model | cost |", body)
            self.assertIn("| 1 | APPROVE | claude-fable-5 | $0.50 |", body)
            self.assertIn("stage: `", body)
        # the copy on disk is the last body sent
        self.assertEqual(self.read(".revali/feature__mul/logs/pr-body.md"), edited[-1])

    def test_comments_stay_summaries_and_the_log_line_stays(self):  # AC-1, AC-4
        out = self.run_ok(review_with_a_low_finding())
        self.assertIn(SUMMARIES_LINE, out)
        comment = self.read(".revali/feature__mul/logs/comment-review-1.md")
        self.assertIn("summary only", comment)
        self.assertNotIn(FINDING_TEXT, comment)
        self.assertIn(FINDING_TEXT, self.read(".revali/feature__mul/review-1.md"))
        for body in self.bodies("create") + self.bodies("edit"):
            self.assert_no_request(body)


class PrivateRepoBody(BodyCase):
    def test_created_and_edited_bodies_have_no_request(self):  # AC-1
        self.run_ok()
        created, edited = self.bodies("create"), self.bodies("edit")
        self.assertEqual(len(created), 1, self.fake_calls("gh"))
        self.assertTrue(edited, self.fake_calls("gh"))
        self.assertEqual(created[0], WITHOUT_REQUEST)
        for body in created + edited:
            self.assert_no_request(body)
            self.assert_other_sections_verbatim(body)
        self.assertTrue(edited[-1].startswith(WITHOUT_REQUEST + STATUS_HEADING), edited[-1])

    def test_comments_keep_the_full_text(self):  # AC-1, AC-4
        out = self.run_ok(review_with_a_low_finding())
        self.assertNotIn("summaries only", out)
        comment = self.read(".revali/feature__mul/logs/comment-review-1.md")
        self.assertIn(FINDING_TEXT, comment)
        self.assertNotIn("summary only", comment)
        for body in self.bodies("create") + self.bodies("edit"):
            self.assert_no_request(body)


# ---- AC-2: wherever the section sits; only a heading line counts ----------------


class RequestPlacement(BodyCase):
    def body_for(self, change_md):
        self.write(".revali/feature__mul/change.md", change_md)
        self.run_ok()
        created = self.bodies("create")
        self.assertEqual(len(created), 1, self.fake_calls("gh"))
        return created[0]

    def test_request_in_the_middle_goes(self):  # AC-2
        text = WITHOUT_REQUEST.replace("## Goal\n", REQUEST_BLOCK + "## Goal\n", 1)
        self.assertIn(REQUEST_LINE, text)
        body = self.body_for(text)
        self.assert_no_request(body)
        self.assertEqual(body, WITHOUT_REQUEST)

    def test_request_last_with_no_heading_after_it_goes(self):  # AC-2
        body = self.body_for(WITHOUT_REQUEST + "\n## Request\n" + REQUEST_LINE + "\n")
        self.assert_no_request(body)
        self.assertEqual(body, WITHOUT_REQUEST)

    def test_request_last_without_a_trailing_newline_goes(self):  # AC-2
        body = self.body_for(WITHOUT_REQUEST + "\n## Request\n" + REQUEST_LINE)
        self.assert_no_request(body)
        self.assertEqual(body, WITHOUT_REQUEST)

    def test_multi_paragraph_request_goes_whole(self):  # AC-1, AC-2
        second = "and keep the signature (a, b), no keyword arguments"
        text = CHANGE_MD.replace(REQUEST_BLOCK, REQUEST_BLOCK + second + "\n\n", 1)
        body = self.body_for(text)
        self.assert_no_request(body)
        self.assertNotIn(second, body)
        self.assertEqual(body, WITHOUT_REQUEST)

    def test_heading_with_trailing_spaces_still_counts(self):  # AC-2
        text = CHANGE_MD.replace("## Request\n", "## Request  \n", 1)
        body = self.body_for(text)
        self.assert_no_request(body)
        self.assertEqual(body, WITHOUT_REQUEST)

    def test_the_words_inside_another_section_stay(self):  # AC-2
        why = "## Why\nThe calculator only had add and sub; see ## Request above.\n"
        text = CHANGE_MD.replace("## Why\nThe calculator only had add and sub.\n", why, 1)
        self.assertIn(why, text)
        body = self.body_for(text)
        self.assertIn(why, body)  # the Why section is untouched
        self.assertNotIn("\n## Request", body)  # no Request heading line
        self.assertNotIn(REQUEST_LINE, body)
        self.assertNotIn("withheld", body)
        expected = WITHOUT_REQUEST.replace("## Why\nThe calculator only had add and sub.\n", why, 1)
        self.assertEqual(body, expected)

    def test_a_longer_heading_is_not_the_request(self):  # AC-2
        # `## Requests handled` is another section; the Request itself is still dropped
        extra = "## Requests handled\nnone so far\n\n"
        text = CHANGE_MD.replace("## Out of scope\n", extra + "## Out of scope\n", 1)
        body = self.body_for(text)
        self.assert_no_request(body)
        self.assertIn(extra, body)
        expected = WITHOUT_REQUEST.replace("## Out of scope\n", extra + "## Out of scope\n", 1)
        self.assertEqual(body, expected)


# ---- AC-3: the local change.md is untouched and still validated -----------------


class LocalChangeMd(BodyCase):
    def test_empty_request_stops_before_the_push_and_the_file_is_never_rewritten(self):
        # AC-3, then AC-1 once the request is filled in
        empty = CHANGE_MD.replace(REQUEST_LINE + "\n", "", 1)
        self.assertIn("## Request\n\n## What", empty)
        self.write(".revali/feature__mul/change.md", empty)
        self.claude(claude_entry())
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertIn("## Request", out)
        self.assertEqual(self.bodies("create"), [])
        self.assertEqual(git(["ls-remote", "--heads", "origin", "feature/mul"], self.repo), "")
        self.assertEqual(self.read(".revali/feature__mul/change.md"), empty)

        self.write(".revali/feature__mul/change.md", CHANGE_MD)
        self.run_ok()
        # the request is stripped from the PR body only; the local file keeps it
        self.assertEqual(self.read(".revali/feature__mul/change.md"), CHANGE_MD)
        created = self.bodies("create")
        self.assertEqual(created, [WITHOUT_REQUEST])
        for body in self.bodies("edit"):
            self.assert_no_request(body)


if __name__ == "__main__":
    unittest.main()
