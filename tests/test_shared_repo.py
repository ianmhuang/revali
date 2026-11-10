"""Worktrees of one repository running at the same time (fix/shared-repo-fetch-and-issues,
M3.4.7).

They share `refs/remotes/origin/*`: two fetches of the base at the same moment can fail one
of them (measured: every round of 40 when the remote had moved), so a failed fetch is tried
once more. And two branches that hit the same pre-existing bug must not open two issues:
an open issue with the same title is reused.
"""

import json
import os
import shutil
import sys
import unittest

from revali import EXIT_OK
from revali.state import State
from tests import test_issues as ti
from tests import test_review_worktreemerge as wtm
from tests.helpers import RepoCase, _quote, claude_entry, git, run_cli

# module aliases, not names: a TestCase imported by name is collected and run here again
ISSUE_URL, REVALI_LOG, TEST_ID, TESTS_MD = ti.ISSUE_URL, ti.REVALI_LOG, ti.TEST_ID, ti.TESTS_MD

# git through REVALI_GIT_CMD: the first `fails` fetches print what git 2.53 prints when another
# worktree moved the ref first and exit 1; everything else goes to the real git
FLAKY_FETCH = """import os, subprocess, sys
args = sys.argv[1:]
if "fetch" in args:
    counter = %(counter)r
    n = int(open(counter).read()) if os.path.exists(counter) else 0
    open(counter, "w").write(str(n + 1))
    if n < %(fails)d:
        sys.stderr.write(
            "error: fetching ref refs/remotes/origin/main failed: incorrect old value provided\\n"
            "second line of the error\\n"
        )
        sys.exit(1)
sys.exit(subprocess.call([%(git)r] + args))
"""
RACE_ERROR = "error: fetching ref refs/remotes/origin/main failed: incorrect old value provided"


def flaky_fetch(case, fails):
    """Install the flaky git for this test; returns a function giving the fetch count."""
    counter = os.path.join(case.tmp, "fetches.txt")
    script = os.path.join(case.tmp, "flaky git.py")
    with open(script, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(FLAKY_FETCH % {"counter": counter, "fails": fails, "git": shutil.which("git")})
    os.environ["REVALI_GIT_CMD"] = "%s %s" % (_quote(sys.executable), _quote(script))
    case.addCleanup(os.environ.pop, "REVALI_GIT_CMD", None)

    def count():
        if not os.path.exists(counter):
            return 0
        with open(counter, encoding="utf-8") as fh:
            return int(fh.read())

    return count


class PreflightFetch(RepoCase):
    """AC-1, AC-2: preflight's fetch of the base."""

    def move_origin(self):
        # the remote gets a commit the local clone has not seen; the branch still descends from
        # the old tip, so a comparison against origin/main says "behind" and one against the
        # local main does not
        other = os.path.join(self.tmp, "other")
        # -b: the fixture's bare remote has a HEAD that names no branch
        git(["clone", "-q", "-b", "main", self.info["remote"], other], self.tmp)
        git(
            ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty"]
            + ["-m", "moved"],
            other,
        )
        git(["push", "-q", "origin", "main"], other)

    def test_retry_succeeds(self):
        count = flaky_fetch(self, fails=1)
        code, out = run_cli(["preflight"])
        self.assertEqual(count(), 2, out)
        self.assertNotIn("could not fetch", out)
        self.assertEqual(code, EXIT_OK, out)

    def test_retry_reaches_the_moved_remote(self):
        self.move_origin()
        count = flaky_fetch(self, fails=1)
        code, out = run_cli(["preflight"])
        self.assertEqual(count(), 2, out)
        # compared against the fetched origin/main: the branch is behind it
        self.assertIn("branch is behind origin/main", out)

    def test_both_attempts_fail(self):
        self.move_origin()
        count = flaky_fetch(self, fails=5)
        code, out = run_cli(["preflight"])
        self.assertEqual(count(), 2, out)
        note = [ln for ln in out.splitlines() if "could not fetch origin/main" in ln]
        self.assertEqual(len(note), 1, out)
        self.assertIn("comparing against local main", note[0])
        self.assertIn(RACE_ERROR, note[0])
        self.assertNotIn("second line of the error", out)
        self.assertEqual(code, EXIT_OK, out)  # the local base is used, as before
        self.assertNotIn("branch is behind", out)


class MergeFetch(wtm.WorktreeCase):
    """AC-3: the fetch after `gh pr merge`, from a linked worktree (the primary tree on main,
    as in test_review_worktreemerge.LinkedWorktreeFollowUp)."""

    def setUp(self):
        super().setUp()
        git(["checkout", "-q", "main"], self.repo)
        self.wt = self.add_worktree("feature/mul")
        os.chdir(self.wt)
        self.ready(self.wt)

    def test_retry_detaches_at_the_merged_base(self):
        count = flaky_fetch(self, fails=1)
        code, out = run_cli(["merge"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertEqual(count(), 2, out)
        self.assertNotIn("note: git fetch failed", out)
        self.assertIn("detached at the merged main", out)
        self.assertEqual(git(["rev-parse", "--abbrev-ref", "HEAD"], self.wt).strip(), "HEAD")
        self.assertEqual(
            git(["rev-parse", "HEAD"], self.wt).strip(),
            git(["rev-parse", "origin/main"], self.wt).strip(),
        )

    def test_both_attempts_fail(self):
        count = flaky_fetch(self, fails=5)
        code, out = run_cli(["merge"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertEqual(count(), 2, out)
        self.assertIn("note: git fetch failed: %s" % RACE_ERROR, out)
        self.assertIn("still on feature/mul", out)


OTHER_URL = "https://github.example/me/sample/issues/17"
SAME_TITLE = "Pre-existing: %s fails on main" % TEST_ID
ELSEWHERE = [{"number": 17, "title": SAME_TITLE, "url": OTHER_URL}]


class OpenElsewhere(ti.IssueCase):
    """AC-4 to AC-6: an open issue with the same title, opened outside this branch."""

    def issue_lists(self):
        return self.gh("issue", "list")

    def test_reused_instead_of_opened(self):
        self.scenario({"issues": ELSEWHERE})
        out = self.fail_run()
        self.assertEqual(self.gh("issue", "create"), [])
        lists = self.issue_lists()
        self.assertEqual(len(lists), 1)
        argv = lists[0]
        self.assertEqual(argv[argv.index("--state") + 1], "open")
        self.assertEqual(argv[argv.index("--limit") + 1], "200")
        self.assertEqual(set(argv[argv.index("--json") + 1].split(",")), {"number", "title", "url"})
        line = "issue: #17 (already open) %s" % OTHER_URL
        self.assertIn(line, out)
        self.assertIn(line, self.read(TESTS_MD))
        self.assertIn(
            "already has open issue #17, opened outside this branch", self.read(REVALI_LOG)
        )
        state = State.load(self.rdir())
        self.assertEqual(len(state.issues), 1)
        issue = state.issues[0]
        self.assertEqual((issue["number"], issue["url"]), (17, OTHER_URL))
        self.assertEqual(issue["tests"], [TEST_ID])
        self.assertTrue(issue["found"])
        self.assertEqual(state.validations[0]["issue"], 17)

    def test_second_validation_does_not_list_again(self):
        self.scenario({"issues": ELSEWHERE})
        self.fail_run()
        self.write("src/calc.py", self.read("src/calc.py") + "\n# touched\n")
        self.commit_all("touch")
        self.runner_scenario(ti.failing(2))
        self.claude(
            claude_entry(write_tests=False), claude_entry(ti.diagnosis(), write_tests=False)
        )
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(len(self.issue_lists()), 1, out)
        self.assertEqual(self.gh("issue", "create"), [])
        self.assertIn("issue: #17 (already open) %s" % OTHER_URL, out)

    def test_merge_comments_on_it_and_stats_do_not_count_it(self):
        os.environ["REVALI_POLL_SECONDS"] = "0.01"
        self.scenario({"issues": ELSEWHERE})
        self.fail_run()
        self.write("src/calc.py", self.read("src/calc.py") + "\n# fixed\n")
        self.commit_all("fix: multiply\n\nFixes #17\n")
        self.runner_scenario({"default": 0})
        self.claude(claude_entry(write_tests=False))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        code, out = run_cli(["merge"])
        self.assertEqual(code, EXIT_OK, out)
        comments = self.gh_calls("issue", "comment")
        self.assertEqual([c["argv"][2] for c in comments], ["17"])
        code, out = run_cli(["stats"])
        self.assertIn("issues opened: 0", out)

    def test_other_titles_do_not_count(self):
        # AC-5: another test, another base, another case, a longer title; a closed one with
        # the very title (gh is asked for open issues only)
        self.scenario(
            {
                "issues": [
                    {"number": 11, "title": "Pre-existing: tests/x.py::T::t fails on main"},
                    {"number": 12, "title": "Pre-existing: %s fails on develop" % TEST_ID},
                    {"number": 13, "title": SAME_TITLE.lower()},
                    {"number": 14, "title": SAME_TITLE + " (again)"},
                    {"number": 15, "title": SAME_TITLE, "state": "CLOSED"},
                ]
            }
        )
        out = self.fail_run()
        self.assertEqual(len(self.gh("issue", "create")), 1)
        self.assertIn("issue: #41 %s" % ISSUE_URL, out)
        self.assertFalse(State.load(self.rdir()).issues[0].get("found", False))

    def list_fails(self, scenario):
        # AC-6
        self.scenario(scenario)
        out = self.fail_run()
        self.assertEqual(len(self.gh("issue", "create")), 1, out)
        self.assertIn("issue: #41 %s" % ISSUE_URL, out)
        self.assertIn("warning: could not list the open issues", self.read(REVALI_LOG))

    def test_list_exit_1_opens_as_before(self):
        self.list_fails({"issue_list_exit": 1})

    def test_list_not_json_opens_as_before(self):
        self.list_fails({"issue_list_raw": "not json"})

    def test_list_not_a_list_opens_as_before(self):
        self.list_fails({"issue_list_raw": json.dumps({"number": 17})})


class StateVersion(unittest.TestCase):
    def test_bumped_for_the_found_flag(self):
        from revali import STATE_VERSION

        self.assertGreaterEqual(STATE_VERSION, 8)


class Docs(unittest.TestCase):
    """AC-7."""

    def read(self, *rel):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, *rel), "r", encoding="utf-8") as fh:
            return " ".join(fh.read().split())

    def test_side_effects(self):
        text = self.read("docs", "side-effects.md")
        self.assertIn("gh issue list", text)
        self.assertIn("opened outside this branch", text)

    def test_workflow(self):
        text = self.read("docs", "workflow.md")
        self.assertIn("git fetch origin <base>", text)
        self.assertIn("tried once more", text)


if __name__ == "__main__":
    unittest.main()
