"""Acceptance tests for fix/issue-followups, the issue side: the evidence line names the base
sha once (AC-1); a validation whose failing tests two earlier issues cover between them opens
nothing and links the newest of them (AC-4); `FIX_REF` matches exactly GitHub's closing forms,
so `revali merge` comments only on an issue GitHub itself would close (AC-5). End-to-end cases
drive `revali run` / `revali merge` with the fake gh, claude and runner."""

import os
import re
import unittest

from revali import EXIT_ACTION, EXIT_OK
from revali.issues import FIX_REF, IssueRef, already_open
from revali.state import State
from tests.helpers import ROOT, RepoCase, claude_entry, git, run_cli

RDIR = ".revali/feature__mul"
TESTS_MD = RDIR + "/tests.md"
REVALI_LOG = RDIR + "/logs/revali.log"
BODY_1 = RDIR + "/logs/issue-1.md"
TEST_ID = "tests/test_review_mul.py::MulTests::test_product"
OTHER_ID = "tests/test_review_mul.py::MulTests::test_zero"
URL_41 = "https://github.example/me/sample/issues/41"
URL_42 = "https://github.example/me/sample/issues/42"
URL_43 = "https://github.example/me/sample/issues/43"
KEYWORDS = "close closes closed fix fixes fixed resolve resolves resolved".split()


def failure(test, note="n"):
    return {"test": test, "cause": "code", "introduced_by": "base", "note": note}


def diagnosis(tests):
    return {
        "summary": "the base already had this bug",
        "cause": "code",
        "introduced_by": "base",
        "failures": [failure(t) for t in tests],
        "recommendation": "fix the arithmetic",
    }


def failing(round_no):
    v, b = "validate-r%d" % round_no, "base-r%d" % round_no
    return {
        "default": 0,
        "results": {v: {"new_test": 1}, b: {"new_test": 1}},
        "outputs": {v: {"new_test": "boom (branch)\n"}, b: {"new_test": "boom (base)\n"}},
    }


class IssueCase(RepoCase):
    def setUp(self):
        super().setUp()
        self.round_no = 0

    def creates(self):
        return [c for c in self.fake_calls("gh") if c["argv"][:2] == ["issue", "create"]]

    def comments(self):
        return [c for c in self.fake_calls("gh") if c["argv"][:2] == ["issue", "comment"]]

    def base_sha(self):
        return git(["rev-parse", "origin/main"], self.repo).strip()

    def fail_round(self, tests):
        """One more `revali run` whose validation fails with `tests` diagnosed as base bugs.
        Rounds after the first touch a file so the tree differs from the last run."""
        self.round_no += 1
        if self.round_no > 1:
            self.write("src/calc.py", self.read("src/calc.py") + "\n# round %d\n" % self.round_no)
            self.commit_all("touch for round %d" % self.round_no)
        self.runner_scenario(failing(self.round_no))
        reviewer = claude_entry(write_tests=self.round_no == 1)
        self.claude(reviewer, claude_entry(diagnosis(tests), write_tests=False))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ACTION, out)
        return out

    def next_issue(self, number, url):
        self.scenario({"issue_create": {"number": number, "url": url}})


class EvidenceLine(IssueCase):
    def test_base_sha_once_and_the_base_name_present(self):
        self.fail_round([TEST_ID])
        body = self.read(BODY_1)
        evidence = body.split("## Evidence", 1)[1].split("\n## ", 1)[0]
        sha = self.base_sha()[:10]
        base_lines = [ln for ln in evidence.splitlines() if "base" in ln.lower() and sha in ln]
        self.assertEqual(len(base_lines), 1, evidence)  # AC-1: one line carries the sha
        line = base_lines[0]
        self.assertEqual(line.count(sha), 1, line)  # AC-1: and only once in that line
        self.assertIn("`main`", line)  # AC-1: the base branch name is still there
        self.assertIn("new_test exit 1", line)  # from one_line()
        self.assertIn("On the branch: `new_test` exit 1.", evidence)  # the branch line stays

    def test_the_evidence_reads_as_one_sentence_per_run(self):
        self.fail_round([TEST_ID])
        evidence = self.read(BODY_1).split("## Evidence", 1)[1].split("\n## ", 1)[0]
        sha = self.base_sha()[:10]
        # no "at <sha>: on base <sha>" doubling anywhere in the section
        self.assertNotRegex(evidence, r"at %s.*on base %s" % (sha, sha))
        self.assertRegex(evidence, r"`main`.*on base %s: new_test exit 1\." % sha)


class TwoIssuesCover(IssueCase):
    def test_no_third_issue_when_two_earlier_ones_cover_the_failures(self):
        self.fail_round([TEST_ID])  # issue #41 names TEST_ID
        self.next_issue(42, URL_42)
        self.fail_round([OTHER_ID])  # issue #42 names OTHER_ID
        self.assertEqual(len(self.creates()), 2)
        self.next_issue(43, URL_43)  # would be used if a third issue were opened
        out = self.fail_round([TEST_ID, OTHER_ID])
        self.assertEqual(len(self.creates()), 2)  # AC-4: nothing new
        self.assertNotIn(URL_43, out)
        state = State.load(self.rdir())
        self.assertEqual([i["number"] for i in state.issues], [41, 42])
        self.assertEqual(state.validations[2]["issue"], 42)  # AC-4: the newest covering issue
        line = "issue: #42 (already open, with #41) %s" % URL_42
        self.assertIn(line, out.split("ACTION NEEDED", 1)[1])  # AC-4: the summary
        section = self.read(TESTS_MD).split("## Validation 3", 1)[1]
        self.assertIn(line, section)  # AC-4: tests.md
        log = self.read(REVALI_LOG)
        self.assertRegex(log, r"already has issue #42, #41")  # AC-4: every covering issue
        self.assertNotIn("issue #43", log)

    def test_a_single_covering_issue_behaves_as_before(self):
        self.fail_round([TEST_ID, OTHER_ID])  # issue #41 names both
        self.next_issue(42, URL_42)
        out = self.fail_round([TEST_ID])
        self.assertEqual(len(self.creates()), 1)
        state = State.load(self.rdir())
        self.assertEqual(state.validations[1]["issue"], 41)
        line = "issue: #41 (already open) %s" % URL_41
        self.assertIn(line, out)  # AC-4: no "with" note when one issue covers everything
        self.assertNotIn("with #", out)
        self.assertRegex(self.read(REVALI_LOG), r"already has issue #41 \(")

    def test_a_test_nobody_names_still_opens_an_issue(self):
        self.fail_round([TEST_ID])
        self.next_issue(42, URL_42)
        out = self.fail_round([TEST_ID, OTHER_ID])  # OTHER_ID is not covered by #41
        self.assertEqual(len(self.creates()), 2)  # AC-4 boundary: a new issue
        self.assertEqual(State.load(self.rdir()).validations[1]["issue"], 42)
        self.assertIn("issue: #42 %s" % URL_42, out)


class AlreadyOpenUnit(unittest.TestCase):
    """`already_open` is what decides AC-4; its contract is spelled out in the AC."""

    def setUp(self):
        self.a, self.b, self.c, self.d = "t::a", "t::b", "t::c", "t::d"
        self.state = State(
            issues=[
                {"number": 40, "url": "u40", "tests": [self.a, self.b]},
                {"number": 41, "url": "u41", "tests": [self.c]},
                {"number": 42, "url": "u42", "tests": [self.a]},
            ]
        )

    def numbers(self, tests):
        return [i["number"] for i in already_open(self.state, tests)]

    def test_returns_a_list_newest_first(self):
        got = already_open(self.state, [self.a, self.c])
        self.assertIsInstance(got, list)
        self.assertEqual([i["number"] for i in got], [42, 41])  # AC-4: union, newest first

    def test_single_cover_beats_the_union(self):
        self.assertEqual(self.numbers([self.a]), [42])  # newest single cover
        self.assertEqual(self.numbers([self.a, self.b]), [40])  # #40 alone names both
        self.assertEqual(self.numbers([self.b]), [40])

    def test_union_of_three(self):
        self.assertEqual(self.numbers([self.a, self.b, self.c]), [42, 41, 40])

    def test_uncovered_test_means_nothing(self):
        self.assertEqual(self.numbers([self.a, self.d]), [])  # AC-4: open a new issue
        self.assertEqual(self.numbers([self.d]), [])
        self.assertEqual(already_open(State(), [self.a]), [])

    def test_issue_ref_line_forms(self):
        self.assertEqual(IssueRef(41, "u").line(), "issue: #41 u")
        self.assertEqual(IssueRef(41, "u", existing=True).line(), "issue: #41 (already open) u")
        self.assertEqual(
            IssueRef(42, "u", existing=True, also=(41,)).line(),
            "issue: #42 (already open, with #41) u",
        )  # AC-4
        self.assertEqual(
            IssueRef(43, "u", existing=True, also=(42, 40)).line(),
            "issue: #43 (already open, with #42, #40) u",
        )


class ClosingKeywords(unittest.TestCase):
    def test_every_github_closing_form_matches(self):
        for word in KEYWORDS:
            for form in (word, word.capitalize(), word.upper()):
                for text in ("%s #12" % form, "%s  #12" % form, "wip\n\n%s\t#12\n" % form):
                    m = FIX_REF.search(text)
                    self.assertIsNotNone(m, text)  # AC-5
                    self.assertEqual(m.group(1), "12", text)
        self.assertEqual(FIX_REF.search("fix #7, fixes #8").group(1), "7")

    def test_forms_github_does_not_close_on_do_not_match(self):
        for text in (
            "fix#12",
            "fixes: #12",
            "Closes: #12",
            "Resolved:#12",
            "fixes 12",
            "fixes # 12",
            "see #12",
            "refs #12",
            "prefix #12",
            "prefixes #12",
            "unfixed #12",
            "fixing #12",
            "closure #12",
            "resolver #12",
        ):
            self.assertIsNone(FIX_REF.search(text), text)  # AC-5

    def test_the_forms_the_docs_name_all_match(self):
        with open(os.path.join(ROOT, "docs", "side-effects.md"), "r", encoding="utf-8") as fh:
            effects = fh.read()
        forms = re.findall(r"`((?:Fixes|Closes|Resolves) #n)`", effects)
        self.assertGreaterEqual(len(forms), 3, effects[:200])
        for form in forms:
            self.assertIsNotNone(FIX_REF.search(form.replace("#n", "#12")), form)
        with open(os.path.join(ROOT, "templates", "issue.md"), "r", encoding="utf-8") as fh:
            template = fh.read()
        self.assertIn("`Fixes #", template)  # AC-5: the wording revali tells the user to use
        self.assertNotIn("Fixes: #", template)


class MergeComments(IssueCase):
    def setUp(self):
        super().setUp()
        os.environ["REVALI_POLL_SECONDS"] = "0.01"

    def fix_and_pass(self, message):
        self.write("src/calc.py", self.read("src/calc.py") + "\n# fixed\n")
        self.commit_all(message)
        self.runner_scenario({"default": 0})
        self.claude(claude_entry(write_tests=False))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)

    def test_a_colon_form_is_not_a_fix_reference(self):
        self.fail_round([TEST_ID])
        self.fix_and_pass("fix: multiply\n\nFixes: #41\n")  # GitHub leaves #41 open on this
        code, out = run_cli(["merge"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertEqual(self.comments(), [])  # AC-5: so revali does not say "fixed"
        self.assertNotIn("commented on issue", out)

    def test_no_space_form_is_not_a_fix_reference(self):
        self.fail_round([TEST_ID])
        self.fix_and_pass("fix#41 multiply")
        code, out = run_cli(["merge"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertEqual(self.comments(), [])  # AC-5

    def test_a_github_form_still_gets_the_comment(self):
        self.fail_round([TEST_ID])
        self.fix_and_pass("multiply properly\n\nResolved #41\n")
        code, out = run_cli(["merge"])
        self.assertEqual(code, EXIT_OK, out)
        comments = self.comments()
        self.assertEqual(len(comments), 1)  # AC-5: the positive case is unchanged
        self.assertEqual(comments[0]["argv"][2], "41")
        self.assertIn("commented on issue #41", out)


if __name__ == "__main__":
    unittest.main()
