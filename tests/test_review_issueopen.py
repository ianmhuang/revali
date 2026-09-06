"""Acceptance tests: a validation FAIL diagnosed as a pre-existing bug opens one GitHub
issue (AC-4), with the documented body (AC-5), recorded everywhere (AC-6), never when the
conditions are not met (AC-7), guarded like a PR comment (AC-8), configured by three keys
that are documented (AC-9). Every case drives `revali run` end to end with the fake gh,
the fake claude and the fake runner."""

import os
import unittest

from revali import EXIT_ACTION, EXIT_ERROR, EXIT_OK
from revali.config import load_defaults
from revali.state import State
from tests.helpers import ROOT, RepoCase, claude_entry, git, run_cli

RDIR = ".revali/feature__mul"
TESTS_MD = RDIR + "/tests.md"
REVALI_LOG = RDIR + "/logs/revali.log"
COMMENT = RDIR + "/logs/comment-validate-1.md"
TEST_ID = "tests/test_review_mul.py::MulTests::test_product"
OTHER_ID = "tests/test_review_mul.py::MulTests::test_zero"
BRANCH_OUT = "AssertionError: 12 != 7 [branch run]"
BASE_OUT = "AssertionError: 12 != 7 [base run]"
SUMMARY = "mul adds instead of multiplying, so the product test fails."
NOTE = "expected 12, got 7"
FIX = "return a * b instead of a + b"
WITHHELD = "(withheld: non-private repository)"
ISSUE_URL = "https://github.example/me/sample/issues/41"
PR_URL = "https://github.example/me/sample/pull/7"
SECTIONS = [
    "## Found by",
    "## Failing test",
    "## Evidence",
    "## Diagnosis",
    "## Acceptance criterion",
    "## Suggested fix",
    "## How to close",
]


def failure(test=TEST_ID, cause="code", introduced_by="base", note=NOTE):
    return {"test": test, "cause": cause, "introduced_by": introduced_by, "note": note}


def diagnosis(cause="code", introduced_by="base", failures=None):
    if failures is None:
        failures = [failure(cause=cause, introduced_by=introduced_by)]
    return {
        "summary": SUMMARY,
        "cause": cause,
        "introduced_by": introduced_by,
        "failures": failures,
        "recommendation": FIX,
    }


def failing(round_no=1, base_out=BASE_OUT):
    v, b = "validate-r%d" % round_no, "base-r%d" % round_no
    return {
        "default": 0,
        "results": {v: {"new_test": 1}, b: {"new_test": 1}},
        "outputs": {v: {"new_test": BRANCH_OUT + "\n"}, b: {"new_test": base_out + "\n"}},
    }


class IssueCase(RepoCase):
    def gh_calls(self, *prefix):
        return [c for c in self.fake_calls("gh") if c["argv"][: len(prefix)] == list(prefix)]

    def creates(self):
        return self.gh_calls("issue", "create")

    def sent_body(self):
        creates = self.creates()
        self.assertEqual(len(creates), 1, [c["argv"] for c in self.fake_calls("gh")])
        return creates[0]["body"]

    def base_sha(self):
        return git(["rev-parse", "origin/main"], self.repo).strip()

    def set_validate(self, line):
        cfg = self.read("revali.toml").replace("[validate]\n", "[validate]\n%s\n" % line)
        self.write("revali.toml", cfg)
        self.commit_all("config: " + line)

    def run_failing(self, diag=None, scenario=None):
        self.runner_scenario(scenario or failing())
        self.claude(claude_entry(), claude_entry(diag or diagnosis(), write_tests=False))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ACTION, out)
        return out

    def assert_nothing_opened(self, out):
        self.assertEqual(self.creates(), [])
        self.assertEqual(self.gh_calls("label", "list"), [])
        state = State.load(self.rdir())
        self.assertEqual(state.issues, [])
        self.assertEqual(state.validations[-1]["issue"], 0)
        self.assertNotIn("#41", out)
        self.assertNotIn(ISSUE_URL, self.read(TESTS_MD))


class OneIssue(IssueCase):
    def test_one_create_with_title_label_and_assignee(self):
        # AC-4: the default configuration on a repository that has the `bug` label
        self.run_failing()
        creates = self.creates()
        self.assertEqual(len(creates), 1)
        argv = creates[0]["argv"]
        title = argv[argv.index("--title") + 1]
        self.assertEqual(title, "Pre-existing: %s fails on main" % TEST_ID)
        self.assertEqual(argv.count("--label"), 1)
        self.assertEqual(argv[argv.index("--label") + 1], "bug")
        self.assertEqual(argv[argv.index("--assignee") + 1], "me")  # the author's gh login
        self.assertEqual(len(self.gh_calls("label", "list")), 1)

    def test_title_counts_the_other_tests(self):
        # AC-4: several base-introduced failures, one issue; a branch failure is not counted
        diag = diagnosis(
            failures=[
                failure(test=OTHER_ID, note="zero case"),
                failure(),
                failure(test="tests/x.py::T::t", cause="test", introduced_by="branch", note=""),
            ]
        )
        self.run_failing(diag)
        creates = self.creates()
        self.assertEqual(len(creates), 1)
        argv = creates[0]["argv"]
        title = argv[argv.index("--title") + 1]
        self.assertTrue(title.startswith("Pre-existing: "), title)
        self.assertTrue(title.endswith(" and 1 more fail on main"), title)
        body = creates[0]["body"]
        self.assertIn("`%s`" % TEST_ID, body)
        self.assertIn("`%s`" % OTHER_ID, body)
        self.assertNotIn("tests/x.py", body)
        self.assertEqual(sorted(State.load(self.rdir()).issues[0]["tests"]), [TEST_ID, OTHER_ID])

    def test_no_label_when_the_repository_lacks_it(self):
        self.scenario({"labels": ["enhancement", "question"]})
        self.run_failing()
        argv = self.creates()[0]["argv"]
        self.assertNotIn("--label", argv)  # AC-4: revali creates no label
        self.assertIn("--assignee", argv)

    def test_no_assignee_when_configured_empty(self):
        self.set_validate('issue_assignee = ""')
        self.run_failing()
        argv = self.creates()[0]["argv"]
        self.assertNotIn("--assignee", argv)  # AC-4
        self.assertIn("--label", argv)

    def test_project_template_replaces_the_body(self):
        # AC-4: `issue_template` names a project file with the same placeholders
        self.write("docs/bug.md", "PROJECT TEMPLATE $tests on $base ($base_sha) for PR #$pr\n")
        self.set_validate('issue_template = "docs/bug.md"')
        self.run_failing()
        body = self.sent_body()
        self.assertTrue(body.startswith("PROJECT TEMPLATE - `%s` on main (" % TEST_ID), body)
        self.assertIn(self.base_sha()[:10], body)
        self.assertIn("for PR #7", body)
        self.assertNotIn("## Found by", body)


class Body(IssueCase):
    def test_private_repository_gets_the_full_body(self):
        self.run_failing()
        body = self.sent_body()
        self.assertTrue(body.startswith("opened by revali from PR #7 round 1"), body[:80])  # AC-5
        positions = [body.index(s) for s in SECTIONS]  # every section, in this order
        self.assertEqual(positions, sorted(positions))
        self.assertIn("- `%s`" % TEST_ID, body)
        self.assertIn(BASE_OUT, body)  # the evidence output
        self.assertRegex(body, r"`new_test` exit 1")  # exit code on the branch
        self.assertRegex(body, r"new_test exit 1")  # exit code on base
        self.assertIn(self.base_sha()[:10], body)
        self.assertIn(SUMMARY, body)
        self.assertIn(NOTE, body)
        self.assertIn(FIX, body)
        self.assertIn("AC-1", body)
        self.assertIn("returns the product of a and b", body)
        self.assertIn(PR_URL, body)
        self.assertNotIn(WITHHELD, body)
        self.assertNotRegex(body, r"\$[a-z_]+")  # no placeholder left unsubstituted

    def test_non_private_repository_gets_the_summary_body(self):
        self.scenario({"visibility": "PUBLIC"})
        self.run_failing()
        body = self.sent_body()
        self.assertTrue(body.startswith("opened by revali from PR #7 round 1"), body[:80])
        for section in SECTIONS:
            self.assertIn(section, body)
        self.assertIn(WITHHELD, body)  # AC-5
        for secret in (BASE_OUT, BRANCH_OUT, SUMMARY, NOTE, FIX):
            self.assertNotIn(secret, body)
        # what stays
        self.assertIn("- `%s`" % TEST_ID, body)
        self.assertRegex(body, r"`new_test` exit 1")
        self.assertRegex(body, r"new_test exit 1")
        self.assertIn(self.base_sha()[:10], body)
        self.assertIn("**code**", body)
        self.assertIn("**base**", body)
        self.assertIn("AC-1", body)
        self.assertIn(PR_URL, body)

    def test_internal_visibility_counts_as_not_private(self):
        self.scenario({"visibility": "INTERNAL"})
        self.run_failing()
        body = self.sent_body()
        self.assertIn(WITHHELD, body)
        self.assertNotIn(FIX, body)


class Recorded(IssueCase):
    def test_number_and_url_everywhere(self):
        out = self.run_failing()
        state = State.load(self.rdir())
        self.assertEqual(len(state.issues), 1)  # AC-6
        issue = state.issues[0]
        self.assertEqual(issue["number"], 41)
        self.assertEqual(issue["url"], ISSUE_URL)
        self.assertEqual(issue["validation"], 1)
        self.assertEqual(issue["tests"], [TEST_ID])
        self.assertEqual(state.validations[0]["issue"], 41)
        section = self.read(TESTS_MD).split("## Validation 1")[1]
        self.assertIn("#41", section)
        self.assertIn(ISSUE_URL, section)
        comment = self.read(COMMENT)  # the full PR comment (private repository)
        self.assertIn("#41", comment)
        self.assertIn(ISSUE_URL, comment)
        summary = out.split("ACTION NEEDED")[1]
        self.assertIn("#41", summary)
        self.assertIn(ISSUE_URL, summary)

    def test_summary_comment_carries_the_link(self):
        self.scenario({"visibility": "PUBLIC"})
        out = self.run_failing()
        comment = self.read(COMMENT)
        self.assertIn("summary only", comment)
        self.assertIn("#41", comment)
        self.assertIn(ISSUE_URL, comment)
        self.assertIn(ISSUE_URL, out.split("ACTION NEEDED")[1])


class NoIssue(IssueCase):
    def test_switched_off(self):
        self.set_validate("issue_on = false")
        self.assert_nothing_opened(self.run_failing())  # AC-7

    def test_branch_introduced(self):
        self.assert_nothing_opened(self.run_failing(diagnosis(introduced_by="branch")))

    def test_unknown_origin(self):
        self.assert_nothing_opened(self.run_failing(diagnosis(introduced_by="unknown")))

    def test_base_but_the_test_is_wrong(self):
        self.assert_nothing_opened(self.run_failing(diagnosis(cause="test")))

    def test_overall_base_without_a_failure_marked_base(self):
        diag = diagnosis(failures=[failure(introduced_by="branch")])
        self.assert_nothing_opened(self.run_failing(diag))

    def test_diagnosis_unavailable(self):
        self.runner_scenario(failing())
        self.claude(claude_entry(), claude_entry({"nonsense": True}, write_tests=False))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ACTION, out)
        self.assertIn("diagnosis unavailable", out)
        self.assert_nothing_opened(out)

    def test_validation_pass(self):
        self.claude(claude_entry())
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertEqual(self.creates(), [])
        self.assertEqual(State.load(self.rdir()).validations[0]["issue"], 0)

    def test_second_validation_links_the_existing_issue(self):
        self.run_failing()
        self.write("src/calc.py", self.read("src/calc.py") + "\n# still wrong\n")
        self.commit_all("try again")
        self.runner_scenario(failing(2))
        self.claude(claude_entry(write_tests=False), claude_entry(diagnosis(), write_tests=False))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ACTION, out)
        self.assertEqual(len(self.creates()), 1)  # AC-7: nothing new
        state = State.load(self.rdir())
        self.assertEqual(len(state.issues), 1)
        self.assertEqual(state.validations[1]["issue"], 41)
        second = self.read(TESTS_MD).split("## Validation 2")[1]
        self.assertIn("#41", second)
        self.assertIn(ISSUE_URL, second)
        self.assertIn(ISSUE_URL, out.split("ACTION NEEDED")[1])

    def test_a_new_failing_test_opens_another_issue(self):
        # AC-7 boundary: the second validation names a test no issue covers yet
        self.run_failing()
        self.write("src/calc.py", self.read("src/calc.py") + "\n# still wrong\n")
        self.commit_all("try again")
        url = "https://github.example/me/sample/issues/42"
        self.scenario({"issue_create": {"number": 42, "url": url}})
        self.runner_scenario(failing(2))
        diag = diagnosis(failures=[failure(), failure(test=OTHER_ID, note="zero case")])
        self.claude(claude_entry(write_tests=False), claude_entry(diag, write_tests=False))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ACTION, out)
        self.assertEqual(len(self.creates()), 2)
        state = State.load(self.rdir())
        self.assertEqual([i["number"] for i in state.issues], [41, 42])
        self.assertEqual(state.validations[1]["issue"], 42)
        self.assertIn(url, out.split("ACTION NEEDED")[1])


class Guarded(IssueCase):
    def test_credential_in_the_body_is_withheld(self):
        token = "ghp_" + "A" * 36
        self.run_failing(scenario=failing(base_out="token " + token + "\n" + BASE_OUT))
        body = self.sent_body()  # AC-8: what gh received
        self.assertNotIn(token, body)
        self.assertNotIn(BASE_OUT, body)
        self.assertIn("withheld", body)
        self.assertEqual(State.load(self.rdir()).issues[0]["number"], 41)

    def test_failed_create_is_a_warning(self):
        self.scenario({"issue_exit": 1})
        out = self.run_failing()  # still exit 2
        state = State.load(self.rdir())
        self.assertEqual(state.issues, [])  # AC-8
        self.assertEqual(state.validations[0]["result"], "FAIL")
        self.assertEqual(state.validations[0]["issue"], 0)
        self.assertEqual(state.stage, "needs_action")
        self.assertEqual(state.pending_effect, "")
        self.assertIn("warning", self.read(REVALI_LOG))
        self.assertNotIn("#41", out)
        self.assertNotIn("#41", self.read(TESTS_MD))
        self.assertIn("diagnosis", self.read(TESTS_MD).lower())  # the diagnosis still landed

    def test_failed_label_list_is_a_warning(self):
        self.scenario({"label_list_exit": 1})
        out = self.run_failing()
        argv = self.creates()[0]["argv"]  # AC-8: the issue is still opened
        self.assertNotIn("--label", argv)
        self.assertIn("warning", self.read(REVALI_LOG))
        self.assertIn("#41", out)
        self.assertEqual(State.load(self.rdir()).issues[0]["number"], 41)


class Configuration(IssueCase):
    def test_defaults_template_and_docs(self):
        # AC-9
        defaults = load_defaults()["validate"]
        self.assertIs(defaults["issue_on"], True)
        self.assertEqual(defaults["issue_assignee"], "author")
        self.assertEqual(defaults["issue_template"], "")
        with open(os.path.join(ROOT, "templates", "revali.toml"), "r", encoding="utf-8") as fh:
            template = fh.read()
        with open(os.path.join(ROOT, "docs", "configuration.md"), "r", encoding="utf-8") as fh:
            configuration = fh.read()
        for key in ("issue_on", "issue_assignee", "issue_template"):
            self.assertIn(key, template, key)
            self.assertIn(key, configuration, key)
        with open(os.path.join(ROOT, "docs", "side-effects.md"), "r", encoding="utf-8") as fh:
            effects = fh.read()
        self.assertIn("gh issue create", effects)
        self.assertIn("gh issue comment", effects)
        with open(os.path.join(ROOT, "docs", "files.md"), "r", encoding="utf-8") as fh:
            files = fh.read()
        self.assertIn("templates/issue.md", files)
        self.assertIn("issues", files)
        self.assertTrue(os.path.isfile(os.path.join(ROOT, "templates", "issue.md")))

    def test_bad_assignee_is_a_config_error(self):
        self.set_validate('issue_assignee = "owner"')
        code, out = run_cli(["preflight"])
        self.assertEqual(code, EXIT_ERROR, out)  # AC-9
        self.assertIn("issue_assignee", out)

    def test_missing_template_is_a_config_error(self):
        self.set_validate('issue_template = "docs/absent.md"')
        code, out = run_cli(["preflight"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertIn("issue_template", out)

    def test_author_and_empty_are_accepted(self):
        self.set_validate('issue_assignee = "author"')
        code, out = run_cli(["preflight"])
        self.assertEqual(code, EXIT_OK, out)


if __name__ == "__main__":
    unittest.main()
