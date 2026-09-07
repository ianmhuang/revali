"""A validation failure diagnosed as a pre-existing bug opens one GitHub issue; `merge`
comments on the issues a branch commit says it fixed; the state records the base rerun."""

import os
import unittest

from revali import EXIT_ACTION, EXIT_ERROR, EXIT_OK
from revali.issues import FIX_REF, IssueRef, already_open, referenced_issues, title
from revali.runners import TIMEOUT_EXIT
from revali.state import State
from tests.helpers import ROOT, RepoCase, claude_entry, git, run_cli

TESTS_MD = ".revali/feature__mul/tests.md"
REVALI_LOG = ".revali/feature__mul/logs/revali.log"
BODY = ".revali/feature__mul/logs/issue-1.md"
TEST_ID = "tests/test_review_mul.py::MulTests::test_product"
BRANCH_OUT = "AssertionError: 12 != 7 (branch)"
BASE_OUT = "AssertionError: 12 != 7 (base)"
SUMMARY = "mul returns a + b instead of a * b, so the product test fails."
RECOMMENDATION = "return a * b"
NOTE = "expected 12, got 7"
ISSUE_URL = "https://github.example/me/sample/issues/41"


def diagnosis(cause="code", introduced_by="base", failures=None):
    return {
        "summary": SUMMARY,
        "cause": cause,
        "introduced_by": introduced_by,
        "failures": (
            failures
            if failures is not None
            else [{"test": TEST_ID, "cause": cause, "introduced_by": introduced_by, "note": NOTE}]
        ),
        "recommendation": RECOMMENDATION,
    }


def failing(round_no=1):
    v, b = "validate-r%d" % round_no, "base-r%d" % round_no
    return {
        "default": 0,
        "results": {v: {"new_test": 1}, b: {"new_test": 1}},
        "outputs": {v: {"new_test": BRANCH_OUT}, b: {"new_test": BASE_OUT}},
    }


class IssueCase(RepoCase):
    def gh_calls(self, *prefix):
        return [c for c in self.fake_calls("gh") if tuple(c["argv"][: len(prefix)]) == prefix]

    def gh(self, *prefix):
        return [c["argv"] for c in self.gh_calls(*prefix)]

    def base_sha(self):
        return git(["rev-parse", "origin/main"], self.repo).strip()

    def set_validate(self, line):
        toml = self.read("revali.toml").replace("[validate]\n", "[validate]\n%s\n" % line)
        self.write("revali.toml", toml)
        self.commit_all("config")

    def fail_run(self, diag=None):
        self.runner_scenario(failing())
        self.claude(claude_entry(), claude_entry(diag or diagnosis(), write_tests=False))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ACTION, out)
        return out


class OpenIssue(IssueCase):
    def test_issue_opened_for_a_pre_existing_bug(self):
        out = self.fail_run()
        creates = self.gh("issue", "create")
        self.assertEqual(len(creates), 1)  # AC-4
        argv = creates[0]
        self.assertEqual(
            argv[argv.index("--title") + 1], "Pre-existing: %s fails on main" % TEST_ID
        )
        self.assertEqual(argv[argv.index("--label") + 1], "bug")
        self.assertEqual(argv[argv.index("--assignee") + 1], "me")
        self.assertEqual(len(self.gh("label", "list")), 1)
        body = self.read(BODY)
        self.assertTrue(body.startswith("opened by revali from PR #7 round 1"), body[:80])  # AC-5
        for section in (
            "## Found by",
            "## Failing test",
            "## Evidence",
            "## Diagnosis",
            "## Acceptance criterion",
            "## Suggested fix",
            "## How to close",
        ):
            self.assertIn(section, body)
        self.assertIn("- `%s`" % TEST_ID, body)
        self.assertIn(BASE_OUT, body)
        self.assertIn("`new_test` exit 1", body)
        self.assertIn("new_test exit 1", body)
        self.assertIn(self.base_sha()[:10], body)
        self.assertIn(SUMMARY, body)
        self.assertIn(NOTE, body)
        self.assertIn(RECOMMENDATION, body)
        self.assertIn("- AC-1: mul(a, b) returns the product of a and b for integers", body)
        self.assertIn("- AC-2: mul with zero returns zero", body)
        self.assertNotRegex(body, r"\$[a-z_]+")  # every placeholder substituted
        # AC-6: recorded everywhere
        state = State.load(self.rdir())
        self.assertEqual(len(state.issues), 1)
        issue = state.issues[0]
        self.assertEqual(issue["number"], 41)
        self.assertEqual(issue["url"], ISSUE_URL)
        self.assertEqual(issue["validation"], 1)
        self.assertEqual(issue["tests"], [TEST_ID])
        self.assertEqual(issue["cause"], "code")
        self.assertEqual(issue["recommendation"], RECOMMENDATION)
        self.assertFalse(issue["withhold"])
        self.assertEqual(state.validations[0]["issue"], 41)
        self.assertEqual(state.pending_effect, "")
        line = "issue: #41 %s" % ISSUE_URL
        self.assertIn(line, self.read(TESTS_MD))
        self.assertIn(line, self.read(".revali/feature__mul/logs/comment-validate-1.md"))
        self.assertIn(line, out)
        self.assertIn("opened issue #41 for the pre-existing failure", self.read(REVALI_LOG))

    def test_evidence_names_the_base_once(self):
        self.fail_run()
        body = self.read(BODY)
        evidence = body.split("## Evidence")[1].split("## ")[0]
        sha = self.base_sha()[:10]
        line = "Base branch `main`, on base %s: new_test exit 1." % sha
        self.assertIn(line, evidence)  # AC-1: one_line() carries the sha
        self.assertEqual(evidence.count(sha), 1)  # AC-1: and it is not repeated
        self.assertIn("On the branch: `new_test` exit 1.", evidence)

    def test_several_tests_share_one_issue(self):
        other = "tests/test_review_mul.py::MulTests::test_zero"
        diag = diagnosis(
            failures=[
                {"test": other, "cause": "code", "introduced_by": "base", "note": "n"},
                {"test": TEST_ID, "cause": "code", "introduced_by": "base", "note": NOTE},
                {
                    "test": "tests/x.py::T::t",
                    "cause": "test",
                    "introduced_by": "branch",
                    "note": "",
                },
            ]
        )
        self.fail_run(diag)
        creates = self.gh("issue", "create")
        self.assertEqual(len(creates), 1)
        argv = creates[0]
        self.assertEqual(
            argv[argv.index("--title") + 1], "Pre-existing: %s and 1 more fail on main" % TEST_ID
        )
        body = self.read(BODY)
        self.assertIn("- `%s`" % TEST_ID, body)
        self.assertIn("- `%s`" % other, body)
        self.assertNotIn("tests/x.py", body)
        self.assertEqual(State.load(self.rdir()).issues[0]["tests"], sorted([TEST_ID, other]))

    def test_non_private_repository_gets_a_summary_body(self):
        self.scenario({"visibility": "PUBLIC"})
        self.fail_run()
        body = self.read(BODY)
        self.assertIn("(withheld: non-private repository)", body)  # AC-5
        for secret in (BASE_OUT, BRANCH_OUT, SUMMARY, NOTE, RECOMMENDATION):
            self.assertNotIn(secret, body)
        self.assertIn("- `%s`" % TEST_ID, body)
        self.assertIn("`new_test` exit 1", body)
        self.assertIn("new_test exit 1", body)
        self.assertIn(self.base_sha()[:10], body)
        self.assertIn("cause **code**, introduced by **base**", body)
        self.assertIn("- AC-1:", body)
        self.assertIn("PR #7", body)
        self.assertTrue(State.load(self.rdir()).issues[0]["withhold"])
        summary = self.read(".revali/feature__mul/logs/comment-validate-1.md")
        self.assertIn("summary only", summary)
        self.assertIn("issue: #41 %s" % ISSUE_URL, summary)

    def test_no_label_when_the_repository_has_none(self):
        self.scenario({"labels": ["enhancement"]})
        self.fail_run()
        argv = self.gh("issue", "create")[0]
        self.assertNotIn("--label", argv)
        self.assertIn("'bug' not in this repository", self.read(REVALI_LOG))

    def test_label_list_failure_is_a_warning(self):
        self.scenario({"label_list_exit": 1})
        self.fail_run()
        argv = self.gh("issue", "create")[0]
        self.assertNotIn("--label", argv)
        self.assertIn("warning: gh label list failed", self.read(REVALI_LOG))  # AC-8

    def test_unassigned_when_configured(self):
        self.set_validate('issue_assignee = ""')
        self.fail_run()
        self.assertNotIn("--assignee", self.gh("issue", "create")[0])  # AC-4

    def test_project_template_replaces_the_body(self):
        self.write("docs/issue.md", "CUSTOM $tests on $base at $base_sha\n")
        self.set_validate('issue_template = "docs/issue.md"')
        self.fail_run()
        body = self.read(BODY)
        self.assertTrue(body.startswith("CUSTOM - `%s` on main at " % TEST_ID), body)

    def test_gh_failure_keeps_the_verdict(self):
        self.scenario({"issue_exit": 1})
        out = self.fail_run()  # still exit 2
        state = State.load(self.rdir())
        self.assertEqual(state.issues, [])  # AC-8
        self.assertEqual(state.validations[0]["issue"], 0)
        self.assertEqual(state.validations[0]["result"], "FAIL")
        self.assertEqual(state.pending_effect, "")
        self.assertEqual(state.stage, "needs_action")
        self.assertIn("warning: could not open the issue", self.read(REVALI_LOG))
        self.assertNotIn("issue: #", out)
        self.assertNotIn("issue: #", self.read(TESTS_MD))

    def test_body_with_a_credential_is_withheld(self):
        self.runner_scenario(
            {
                "default": 0,
                "results": {"validate-r1": {"new_test": 1}, "base-r1": {"new_test": 1}},
                "outputs": {
                    "validate-r1": {"new_test": BRANCH_OUT},
                    "base-r1": {"new_test": "token ghp_" + "A" * 36 + "\n" + BASE_OUT},
                },
            }
        )
        self.claude(claude_entry(), claude_entry(diagnosis(), write_tests=False))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ACTION, out)
        body = self.read(BODY)
        self.assertIn("revali withheld this issue's body", body)  # AC-8
        self.assertNotIn(BASE_OUT, body)
        self.assertEqual(len(self.gh("issue", "create")), 1)


class NoIssue(IssueCase):
    def assert_nothing_opened(self, out):
        self.assertEqual(self.gh("issue", "create"), [])  # AC-7
        self.assertEqual(self.gh("label", "list"), [])
        state = State.load(self.rdir())
        self.assertEqual(state.issues, [])
        self.assertEqual(state.validations[0]["issue"], 0)
        self.assertNotIn("issue: #", out)

    def test_branch_introduced_failure(self):
        self.assert_nothing_opened(self.fail_run(diagnosis(introduced_by="branch")))

    def test_unknown_origin(self):
        self.assert_nothing_opened(self.fail_run(diagnosis(introduced_by="unknown")))

    def test_wrong_test_on_base(self):
        self.assert_nothing_opened(self.fail_run(diagnosis(cause="test")))

    def test_overall_base_but_no_failure_marked(self):
        diag = diagnosis()
        diag["failures"] = [
            {"test": TEST_ID, "cause": "code", "introduced_by": "branch", "note": NOTE}
        ]
        self.assert_nothing_opened(self.fail_run(diag))

    def test_switched_off(self):
        self.set_validate("issue_on = false")
        self.assert_nothing_opened(self.fail_run())

    def test_no_diagnosis(self):
        self.runner_scenario(failing())
        self.claude(claude_entry(), claude_entry({"nonsense": 1}, write_tests=False))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ACTION, out)
        self.assertIn("diagnosis unavailable", out)
        self.assert_nothing_opened(out)

    def test_second_validation_links_the_open_issue(self):
        self.fail_run()
        self.write("src/calc.py", self.read("src/calc.py") + "\n# touched\n")
        self.commit_all("touch")
        self.runner_scenario(failing(2))
        self.claude(claude_entry(write_tests=False), claude_entry(diagnosis(), write_tests=False))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ACTION, out)
        self.assertEqual(len(self.gh("issue", "create")), 1)  # AC-7: still the one
        state = State.load(self.rdir())
        self.assertEqual(len(state.issues), 1)
        self.assertEqual(state.validations[1]["issue"], 41)
        self.assertIn("already has issue #41", self.read(REVALI_LOG))
        link = "issue: #41 (already open) %s" % ISSUE_URL
        self.assertIn(link, out)
        self.assertIn(link, self.read(TESTS_MD).split("## Validation 2")[1])

    def test_two_issues_together_cover_the_failures(self):
        other = "tests/test_review_mul.py::MulTests::test_zero"
        url42 = ISSUE_URL.replace("41", "42")

        def touch_and_fail(round_no, tests):
            self.write("src/calc.py", self.read("src/calc.py") + "\n# touched %d\n" % round_no)
            self.commit_all("touch %d" % round_no)
            self.runner_scenario(failing(round_no))
            diag = diagnosis(
                failures=[
                    {"test": t, "cause": "code", "introduced_by": "base", "note": "n"}
                    for t in tests
                ]
            )
            self.claude(claude_entry(write_tests=False), claude_entry(diag, write_tests=False))
            code, out = run_cli(["run", "--foreground"])
            self.assertEqual(code, EXIT_ACTION, out)
            return out

        self.fail_run()  # issue #41 names TEST_ID
        self.scenario({"issue_create": {"number": 42, "url": url42}})
        touch_and_fail(2, [other])  # issue #42 names the other test
        self.assertEqual(len(self.gh("issue", "create")), 2)
        out = touch_and_fail(3, [TEST_ID, other])  # both fail: nothing new
        self.assertEqual(len(self.gh("issue", "create")), 2)  # AC-4: no third issue
        state = State.load(self.rdir())
        self.assertEqual([i["number"] for i in state.issues], [41, 42])
        self.assertEqual(state.validations[2]["issue"], 42)  # the newest covering issue
        self.assertIn("already has issue #42, #41", self.read(REVALI_LOG))
        link = "issue: #42 (already open, with #41) %s" % url42
        self.assertIn(link, out)
        self.assertIn(link, self.read(TESTS_MD).split("## Validation 3")[1])


class BaseRerunRecord(IssueCase):
    def test_recorded_on_fail(self):
        self.fail_run()
        rec = State.load(self.rdir()).validations[0]["base_rerun"]
        self.assertEqual(
            rec, {"sha": self.base_sha(), "ran": True, "exit": 1, "reason": ""}
        )  # AC-1

    def test_null_on_pass(self):
        self.claude(claude_entry())
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        rec = State.load(self.rdir()).validations[0]
        self.assertIsNone(rec["base_rerun"])
        self.assertEqual(rec["issue"], 0)

    def test_reason_when_not_run(self):
        self.set_validate("rerun_on_base = false")
        self.fail_run(diagnosis(introduced_by="unknown"))
        rec = State.load(self.rdir()).validations[0]["base_rerun"]
        self.assertEqual(
            rec,
            {
                "sha": self.base_sha(),
                "ran": False,
                "exit": None,
                "reason": "[validate] rerun_on_base is false",
            },
        )

    def test_reason_when_unusable(self):
        sc = failing()
        sc["results"]["base-r1"] = {"new_test": TIMEOUT_EXIT}
        self.runner_scenario(sc)
        self.claude(
            claude_entry(), claude_entry(diagnosis(introduced_by="unknown"), write_tests=False)
        )
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ACTION, out)
        rec = State.load(self.rdir()).validations[0]["base_rerun"]
        self.assertTrue(rec["ran"])
        self.assertIsNone(rec["exit"])
        self.assertIn("timed out", rec["reason"])


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

    def test_comment_on_the_issue_a_commit_fixes(self):
        self.fail_run()
        self.fix_and_pass("fix: multiply\n\nFixes #41\n")
        code, out = run_cli(["merge"])
        self.assertEqual(code, EXIT_OK, out)
        # merge removed .revali/<branch>/: the body comes from the stub's record, the log
        # line from stdout
        comments = self.gh_calls("issue", "comment")
        self.assertEqual(len(comments), 1)  # AC-10
        self.assertEqual(comments[0]["argv"][2], "41")
        body = comments[0]["body"]
        self.assertIn("Fixed by PR #7, merged into `main`", body)
        self.assertIn("fix: multiply", body)
        self.assertIn("cause **code**", body)
        self.assertIn(RECOMMENDATION, body)
        self.assertIn("commented on issue #41", out)
        code, out = run_cli(["stats"])
        self.assertEqual(code, 0)
        self.assertIn("issues opened: 1", out)  # AC-11

    def test_no_comment_without_a_reference(self):
        self.fail_run()
        self.fix_and_pass("fix: multiply (see #41 later)")
        code, out = run_cli(["merge"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertEqual(self.gh("issue", "comment"), [])

    def test_failed_comment_does_not_fail_the_merge(self):
        self.scenario({"issue_comment_exit": 1})
        self.fail_run()
        self.fix_and_pass("Closes #41")
        code, out = run_cli(["merge"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("warning: could not comment on issue #41", out)

    def test_public_repository_withholds_the_fix_text(self):
        self.scenario({"visibility": "PUBLIC"})
        self.fail_run()
        self.fix_and_pass("resolves #41")
        code, out = run_cli(["merge"])
        self.assertEqual(code, EXIT_OK, out)
        body = self.gh_calls("issue", "comment")[0]["body"]
        self.assertIn("cause **code**", body)
        self.assertNotIn(RECOMMENDATION, body)
        self.assertIn("(withheld: non-private repository)", body)


class ConfigKeys(IssueCase):
    def test_bad_assignee_is_a_config_error(self):
        self.set_validate('issue_assignee = "owner"')
        code, out = run_cli(["preflight"])
        self.assertEqual(code, EXIT_ERROR)  # AC-9
        self.assertIn("issue_assignee", out)

    def test_missing_template_is_a_config_error(self):
        self.set_validate('issue_template = "docs/none.md"')
        code, out = run_cli(["preflight"])
        self.assertEqual(code, EXIT_ERROR)
        self.assertIn("issue_template", out)


class Units(unittest.TestCase):
    def test_title(self):
        self.assertEqual(title(["a::b"], "main"), "Pre-existing: a::b fails on main")
        self.assertEqual(title(["a", "b", "c"], "dev"), "Pre-existing: a and 2 more fail on dev")

    def test_fix_reference_words(self):
        # AC-5: GitHub's nine closing keywords, whitespace, `#n`; nothing looser
        for word in "close closes closed fix fixes fixed resolve resolves resolved".split():
            for text in (word + " #12", word.upper() + "  #12", "wip\n\n%s\t#12\n" % word):
                self.assertEqual(FIX_REF.search(text).group(1), "12", text)
        for text in (
            "see #12",
            "prefix #12",
            "fixes 12",
            "refs #12",
            "fix#12",
            "fixes: #12",
            "Closes: #12",
            "fixing #12",
            "fixes #12a",
        ):
            self.assertIsNone(FIX_REF.search(text), text)

    def test_already_open_prefers_one_issue_then_the_union(self):
        a, b, c = "t::a", "t::b", "t::c"
        state = State(
            issues=[
                {"number": 40, "tests": [a]},
                {"number": 41, "tests": [b]},
                {"number": 42, "tests": [a, c]},
            ]
        )
        numbers = lambda tests: [i["number"] for i in already_open(state, tests)]  # noqa: E731
        self.assertEqual(numbers([a]), [42])  # newest single cover wins
        self.assertEqual(numbers([a, c]), [42])
        self.assertEqual(numbers([a, b]), [42, 41])  # AC-4: two issues together, newest first
        self.assertEqual(numbers([a, b, c]), [42, 41])  # #40 adds nothing #42 does not name
        self.assertEqual(numbers([b, "t::new"]), [])  # one test nobody names: open a new one
        state.issues = [{"number": 40, "tests": [a, b]}, {"number": 41, "tests": [a, b]}]
        self.assertEqual(numbers([a, b]), [41])  # a single newest cover, not both

    def test_already_open_drops_an_issue_the_older_ones_make_redundant(self):
        a, b, c, d = "t::a", "t::b", "t::c", "t::d"
        numbers = lambda issues, tests: [  # noqa: E731
            i["number"] for i in already_open(State(issues=issues), tests)
        ]
        # #42 joins for c, #41 for a, #40 for b; #40 names a too, so #41 goes
        issues = [
            {"number": 40, "tests": [a, b]},
            {"number": 41, "tests": [a]},
            {"number": 42, "tests": [c]},
        ]
        self.assertEqual(numbers(issues, [a, b, c]), [42, 40])
        # the oldest issue kept always stays; a newer one goes only against older kept ones
        issues = [
            {"number": 40, "tests": [a, b]},
            {"number": 41, "tests": [c]},
            {"number": 42, "tests": [a]},
        ]
        self.assertEqual(numbers(issues, [a, b, c]), [41, 40])
        # an issue that names two tests newer ones split between them stays: it is what covers b
        issues = [
            {"number": 40, "tests": [a, b, d]},
            {"number": 41, "tests": [b]},
            {"number": 42, "tests": [c]},
            {"number": 43, "tests": [a]},
        ]
        self.assertEqual(numbers(issues, [a, b, c]), [43, 42, 41])  # #40 never joins
        self.assertEqual(numbers(issues, [a, b, d]), [40])  # a single cover, no union

    def test_issue_ref_line(self):
        self.assertEqual(IssueRef(7, "u").line(), "issue: #7 u")
        self.assertEqual(IssueRef(7, "u", existing=True).line(), "issue: #7 (already open) u")
        self.assertEqual(
            IssueRef(7, "u", existing=True, also=(5, 3)).line(),
            "issue: #7 (already open, with #5, #3) u",
        )  # AC-4

    def test_referenced_issues_first_commit_wins(self):
        issues = [{"number": 41}, {"number": 42}]
        msgs = [
            ("a" * 40, "wip\n\nfixes #41"),
            ("b" * 40, "again fixes #41 and closes #43"),
            ("c" * 40, "Resolves #42"),
        ]
        got = referenced_issues(msgs, issues)
        self.assertEqual(
            [(i["number"], s[0], subj) for i, s, subj in got],
            [(41, "a", "wip"), (42, "c", "Resolves #42")],
        )

    def test_timeout_exit_is_one_constant(self):
        self.assertEqual(TIMEOUT_EXIT, 124)  # AC-2
        for name in os.listdir(os.path.join(ROOT, "revali")):
            if not name.endswith(".py"):
                continue
            with open(os.path.join(ROOT, "revali", name), "r", encoding="utf-8") as fh:
                text = fh.read()
            expected = 1 if name == "runners.py" else 0
            self.assertEqual(text.count("124"), expected, name)


if __name__ == "__main__":
    unittest.main()
