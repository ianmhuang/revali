"""Acceptance tests for fix/shared-repo-fetch-and-issues, the issue side: before opening an
issue for a pre-existing bug revali lists the repository's open issues and reuses one that has
exactly the title it would create (AC-4, AC-5); a listing that fails is a warning and the
issue is opened (AC-6); a reused issue is recorded with `found: true` and is not counted as
opened (AC-8).

Black-box through `revali run`, `revali merge` and `revali stats` with the fake gh (scenario
key `issues` is what `gh issue list` reports), the fake claude and the fake runner."""

import json
import os

from revali import EXIT_ACTION, EXIT_OK, STATE_VERSION
from revali.state import State
from tests.helpers import RepoCase, claude_entry, run_cli

RDIR = ".revali/feature__mul"
TESTS_MD = RDIR + "/tests.md"
REVALI_LOG = RDIR + "/logs/revali.log"
STATE_JSON = RDIR + "/state.json"
TEST_ID = "tests/test_review_mul.py::MulTests::test_product"
OTHER_ID = "tests/test_review_mul.py::MulTests::test_zero"
URL = "https://github.example/me/sample/issues/%d"
ONE_TITLE = "Pre-existing: %s fails on main" % TEST_ID
TWO_TITLE = "Pre-existing: %s and 1 more fail on main" % TEST_ID


def listed(number, title, **more):
    """One issue of the fake repository, as `gh issue list` reports it."""
    return dict({"number": number, "title": title, "url": URL % number}, **more)


def diagnosis(tests):
    return {
        "summary": "the base already had this bug",
        "cause": "code",
        "introduced_by": "base",
        "failures": [
            {"test": t, "cause": "code", "introduced_by": "base", "note": "n"} for t in tests
        ],
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
        os.environ["REVALI_POLL_SECONDS"] = "0.01"
        self.round_no = 0

    def gh(self, *prefix):
        return [
            c["argv"] for c in self.fake_calls("gh") if tuple(c["argv"][: len(prefix)]) == prefix
        ]

    def fail_round(self, tests=(TEST_ID,)):
        """One more `revali run` whose validation fails with `tests` diagnosed as base bugs.
        Rounds after the first touch a file so the tree differs from the last run."""
        self.round_no += 1
        if self.round_no > 1:
            self.write("src/calc.py", self.read("src/calc.py") + "\n# round %d\n" % self.round_no)
            self.commit_all("touch for round %d" % self.round_no)
        self.runner_scenario(failing(self.round_no))
        reviewer = claude_entry(write_tests=self.round_no == 1)
        self.claude(reviewer, claude_entry(diagnosis(list(tests)), write_tests=False))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ACTION, out)
        return out

    def fix_and_pass(self, message):
        self.write("src/calc.py", self.read("src/calc.py") + "\n# fixed\n")
        self.commit_all(message)
        self.runner_scenario({"default": 0})
        self.claude(claude_entry(write_tests=False))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)

    def issues_opened(self):
        """The count `revali stats` prints."""
        code, out = run_cli(["stats"])
        self.assertEqual(code, 0, out)
        lines = [line for line in out.splitlines() if line.startswith("issues opened:")]
        self.assertEqual(len(lines), 1, out)
        return int(lines[0].split(":", 1)[1])


class ReuseAnOpenIssue(IssueCase):
    def test_same_title_is_reused_and_nothing_is_opened(self):
        self.scenario({"issues": [listed(17, ONE_TITLE)]})
        out = self.fail_round()
        self.assertEqual(self.gh("issue", "create"), [])  # AC-4: no issue is opened
        lists = self.gh("issue", "list")
        self.assertEqual(len(lists), 1)  # AC-4: the open issues were listed
        argv = lists[0]
        self.assertEqual(argv[argv.index("--state") + 1], "open")
        self.assertEqual(argv[argv.index("--limit") + 1], "200")
        fields = set(argv[argv.index("--json") + 1].split(","))
        self.assertLessEqual({"number", "title", "url"}, fields)
        line = "issue: #17 (already open) %s" % (URL % 17)
        self.assertIn(line, out)  # AC-4: the validation result names it
        self.assertIn(line, self.read(TESTS_MD))
        self.assertRegex(  # AC-4: the log
            self.read(REVALI_LOG), r"already has open issue #17,? opened outside this branch"
        )
        state = State.load(self.rdir())
        self.assertEqual(len(state.issues), 1)  # AC-4: recorded like one revali opened
        issue = state.issues[0]
        self.assertEqual((issue["number"], issue["url"]), (17, URL % 17))
        self.assertEqual(issue["tests"], [TEST_ID])
        self.assertIs(issue.get("found"), True)  # AC-8
        self.assertEqual(state.validations[0]["issue"], 17)

    def test_the_title_for_several_tests_is_matched_as_a_whole(self):
        # the title revali would create names the first test and a count; the open issue
        # titled for that test alone is another title (AC-5), the one with the count is it
        self.scenario({"issues": [listed(16, ONE_TITLE), listed(18, TWO_TITLE)]})
        out = self.fail_round([OTHER_ID, TEST_ID])
        self.assertEqual(self.gh("issue", "create"), [])
        self.assertIn("issue: #18 (already open) %s" % (URL % 18), out)
        issue = State.load(self.rdir()).issues[0]
        self.assertEqual(issue["number"], 18)
        self.assertEqual(issue["tests"], sorted([TEST_ID, OTHER_ID]))

    def test_a_later_validation_reuses_it_without_listing_again(self):
        self.scenario({"issues": [listed(17, ONE_TITLE)]})
        self.fail_round()
        out = self.fail_round()
        self.assertEqual(len(self.gh("issue", "list")), 1, out)  # AC-4: not listed again
        self.assertEqual(self.gh("issue", "create"), [])
        self.assertIn("issue: #17 (already open) %s" % (URL % 17), out)
        state = State.load(self.rdir())
        self.assertEqual([i["number"] for i in state.issues], [17])
        self.assertEqual(state.validations[1]["issue"], 17)

    def test_a_fixes_commit_gets_the_comment_at_merge(self):
        self.scenario({"issues": [listed(17, ONE_TITLE)]})
        self.fail_round()
        self.fix_and_pass("fix: multiply\n\nFixes #17\n")
        code, out = run_cli(["merge"])
        self.assertEqual(code, EXIT_OK, out)
        comments = self.gh("issue", "comment")
        self.assertEqual([argv[2] for argv in comments], ["17"])  # AC-4
        self.assertIn("commented on issue #17", out)
        self.assertEqual(self.gh("issue", "create"), [])
        # the comment names the merged PR; revali did not open #17, so it does not say it did
        bodies = [
            call.get("body", "")
            for call in self.fake_calls("gh")
            if call["argv"][:2] == ["issue", "comment"]
        ]
        self.assertEqual(len(bodies), 1)
        self.assertIn("Fixed by PR #7", bodies[0])
        self.assertNotIn("when the issue was opened", bodies[0])
        self.assertEqual(self.issues_opened(), 0)  # AC-8: a reused issue is not an opened one


class TitlesThatDoNotCount(IssueCase):
    def test_other_titles_and_a_closed_issue_open_a_new_one(self):
        self.scenario(
            {
                "issues": [
                    listed(11, "Pre-existing: %s fails on main" % OTHER_ID),  # another test
                    listed(12, "Pre-existing: %s fails on develop" % TEST_ID),  # another base
                    listed(13, ONE_TITLE.lower()),  # another case
                    listed(14, ONE_TITLE.upper()),
                    listed(15, ONE_TITLE + " (again)"),  # the title is only a part
                    listed(16, "Re: " + ONE_TITLE),
                    listed(18, TWO_TITLE),  # the same first test, another set
                    listed(20, ONE_TITLE, state="CLOSED"),  # the very title, closed
                ]
            }
        )
        out = self.fail_round()
        lists = self.gh("issue", "list")
        self.assertEqual(len(lists), 1)  # the list was asked for, and for open issues only
        self.assertEqual(lists[0][lists[0].index("--state") + 1], "open")
        creates = self.gh("issue", "create")
        self.assertEqual(len(creates), 1)  # AC-5: none of them counts
        self.assertEqual(creates[0][creates[0].index("--title") + 1], ONE_TITLE)
        self.assertIn("issue: #41 %s" % (URL % 41), out)
        self.assertNotIn("already open", out)
        state = State.load(self.rdir())
        self.assertEqual([i["number"] for i in state.issues], [41])
        self.assertFalse(state.issues[0].get("found", False))


class ListingFails(IssueCase):
    def opened_as_before(self, scenario):
        self.scenario(dict(scenario, issues=[listed(17, ONE_TITLE)]))
        out = self.fail_round()
        self.assertGreaterEqual(len(self.gh("issue", "list")), 1, out)  # it was tried
        creates = self.gh("issue", "create")
        self.assertEqual(len(creates), 1, out)  # AC-6: opened as today
        self.assertEqual(creates[0][creates[0].index("--title") + 1], ONE_TITLE)
        self.assertIn("issue: #41 %s" % (URL % 41), out)
        warnings = [
            line
            for line in self.read(REVALI_LOG).lower().splitlines()
            if "warning" in line and "list" in line and "issue" in line
        ]
        self.assertTrue(warnings, self.read(REVALI_LOG))  # AC-6: the log has a warning
        state = State.load(self.rdir())
        self.assertEqual([i["number"] for i in state.issues], [41])
        self.assertFalse(state.issues[0].get("found", False))
        self.assertEqual(state.validations[0]["result"], "FAIL")  # the verdict stands

    def test_gh_issue_list_exits_non_zero(self):
        self.opened_as_before({"issue_list_exit": 1})

    def test_output_is_not_json(self):
        self.opened_as_before({"issue_list_raw": "this is not JSON"})

    def test_output_is_json_but_not_a_list(self):
        self.opened_as_before({"issue_list_raw": json.dumps(listed(17, ONE_TITLE))})


class EntriesThatAreNotIssues(IssueCase):
    """A JSON list whose entries are not all usable issues: an entry without a positive
    integer number or without a text title is not an open issue with the title (AC-5), and
    it neither stops the listing nor the run (AC-6: the validation result stands)."""

    BROKEN = [
        "text",
        None,
        5,
        {"title": ONE_TITLE, "url": URL % 1},  # no number
        {"number": "n/a", "title": ONE_TITLE, "url": URL % 2},
        {"number": True, "title": ONE_TITLE, "url": URL % 3},
        {"number": 0, "title": ONE_TITLE, "url": URL % 4},
        {"number": [17], "title": ONE_TITLE, "url": URL % 5},
        {"number": 19, "title": [ONE_TITLE], "url": URL % 19},
        {"number": 21, "url": URL % 21},  # no title
    ]

    def test_they_are_passed_over_and_the_issue_behind_them_is_reused(self):
        self.scenario({"issue_list_raw": json.dumps(self.BROKEN + [listed(17, ONE_TITLE)])})
        out = self.fail_round()
        self.assertNotIn("Traceback", out)
        self.assertEqual(self.gh("issue", "create"), [])
        self.assertIn("issue: #17 (already open) %s" % (URL % 17), out)
        state = State.load(self.rdir())
        self.assertEqual([i["number"] for i in state.issues], [17])
        self.assertIs(state.issues[0].get("found"), True)

    def test_alone_they_match_nothing_and_the_issue_is_opened(self):
        self.scenario({"issue_list_raw": json.dumps(self.BROKEN)})
        out = self.fail_round()
        self.assertNotIn("Traceback", out)
        creates = self.gh("issue", "create")
        self.assertEqual(len(creates), 1, out)
        self.assertEqual(creates[0][creates[0].index("--title") + 1], ONE_TITLE)
        self.assertIn("issue: #41 %s" % (URL % 41), out)
        state = State.load(self.rdir())
        self.assertEqual([i["number"] for i in state.issues], [41])
        self.assertFalse(state.issues[0].get("found", False))
        self.assertEqual(state.validations[0]["result"], "FAIL")  # the verdict stands


class OpenedCount(IssueCase):
    """AC-8: `issues opened` in `revali stats` is the history row's `issues` count."""

    def test_only_the_issue_revali_opened_is_counted(self):
        self.scenario(
            {"issues": [listed(17, ONE_TITLE)], "issue_create": {"number": 42, "url": URL % 42}}
        )
        self.fail_round([TEST_ID])  # reuses #17
        out = self.fail_round([OTHER_ID])  # nothing open has that title: opens #42
        self.assertEqual(len(self.gh("issue", "create")), 1, out)
        self.assertIn("issue: #42 %s" % (URL % 42), out)
        state = State.load(self.rdir())
        self.assertEqual([i["number"] for i in state.issues], [17, 42])
        self.assertEqual([bool(i.get("found")) for i in state.issues], [True, False])
        self.assertEqual(self.issues_opened(), 1)

    def test_a_state_from_before_counts_every_issue_as_opened(self):
        self.assertGreaterEqual(STATE_VERSION, 8)  # AC-8: the layout change bumps it
        self.fail_round()  # nothing listed: revali opens #41
        path = os.path.join(self.repo, STATE_JSON)
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        self.assertEqual(data["version"], STATE_VERSION)
        for issue in data["issues"]:
            self.assertFalse(issue.get("found", False))  # revali opened it
            issue.pop("found", None)  # a state written before has no such key
        data["version"] = 7  # as a revali from before this change wrote it
        with open(path, "w", encoding="utf-8", newline="") as fh:
            json.dump(data, fh)
        out = self.fail_round()  # the same test again: the branch's own issue
        self.assertIn("issue: #41 (already open) %s" % (URL % 41), out)
        self.assertEqual(len(self.gh("issue", "create")), 1)
        self.assertEqual(self.issues_opened(), 1)  # AC-8: counted as opened, as before
        with open(path, "r", encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["version"], STATE_VERSION)
