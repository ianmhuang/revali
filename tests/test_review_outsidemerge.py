"""Reviewer acceptance tests for feature/archive-outside-merge, AC-1 to AC-6 and AC-8.

`revali archive [--branch NAME]` records a PR that ended without `revali merge`: it asks gh
for the PR's state and changes nothing on GitHub or on the remote. MERGED: stage `merged`,
one history row with `merged_outside: true` and `stage_before`, the review directory moved to
the archive, the local branch deleted unless a working tree has it checked out. CLOSED: stage
`closed`, the row without `merged_outside`, the local branch left alone. An open PR, a gh
that fails or gives no known state, a missing state or PR number, and a held lock change
nothing and exit 1.

Black-box through the CLI on the fixture repository (fake gh, real git). The base branch has
no `archive` subcommand, so every test here fails there."""

import json
import os
import subprocess
import sys

from revali import EXIT_ERROR, EXIT_OK, gitops
from revali.config import history_path
from revali.state import (
    State,
    acquire_lock,
    acquire_tree_lock,
    read_lock,
    read_tree_lock,
    tree_lock_path,
)
from tests.helpers import RepoCase, git, run_cli

BRANCH = "feature/mul"
READ_ONLY_GH = (
    ["auth", "status"],
    ["pr", "view"],
    ["pr", "list"],
    ["pr", "checks"],
    ["repo", "view"],
    ["api", "user"],
)


class OutsideCase(RepoCase):
    def setUp(self):
        super().setUp()
        git(["push", "-q", "-u", "origin", BRANCH], self.repo)  # the PR's branch is on origin
        self.remote_before = self.remote_refs()
        self.main_before = gitops.rev_parse("refs/heads/main", self.repo)
        self.history = history_path(None)

    # ---- staging ----

    def record(self, stage="ready_to_merge", pr_number=7, last_exit=EXIT_OK):
        """The review record a finished run leaves: state.json and a review file."""
        State(
            repo="me/sample",
            branch=BRANCH,
            base="main",
            stage=stage,
            message="what the last run said",
            last_exit=last_exit,
            pr_number=pr_number,
            pr_url="https://github.example/me/sample/pull/%d" % pr_number,
            cost_usd=1.25,
            head_sha=gitops.rev_parse("refs/heads/" + BRANCH, self.repo) or "",
        ).save(self.rdir())
        with open(os.path.join(self.rdir(), "review-1.md"), "w", encoding="utf-8") as fh:
            fh.write("# review 1\n")

    def pr_is(self, state, number=7, **scenario):
        pr = {"number": number, "url": "https://github.example/me/sample/pull/%d" % number}
        if state is not None:
            pr["state"] = state
        self.scenario(dict({"pr_create": pr, "auth_exit": 0, "pr_view_exit": 0}, **scenario))

    def leave_branch(self):
        git(["checkout", "-q", "main"], self.repo)

    def live_pid(self):
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(120)"], cwd=os.path.dirname(self.tmp)
        )

        def end():
            child.kill()
            child.wait()

        self.addCleanup(end)
        return child.pid

    # ---- looking ----

    def remote_refs(self):
        return git(["ls-remote", "origin"], self.repo)

    def archived(self, number=7):
        return os.path.join(self.home, "archive", "me__sample", "%d-feature__mul" % number)

    def rows(self):
        if not os.path.isfile(self.history):
            return []
        with open(self.history, encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def branch_exists(self):
        return bool(gitops.rev_parse("refs/heads/" + BRANCH, self.repo))

    def state_bytes(self):
        with open(State.path(self.rdir()), "rb") as fh:
            return fh.read()

    def gh_argv(self):
        return [c["argv"] for c in self.fake_calls("gh")]

    def assert_pr_and_remote_untouched(self):
        """AC-2, AC-5: gh was only read from, and no ref on origin or the base moved."""
        for argv in self.gh_argv():
            self.assertIn(argv[:2], READ_ONLY_GH, "gh was asked to change something: %s" % argv)
        self.assertEqual(self.remote_refs(), self.remote_before)
        self.assertEqual(gitops.rev_parse("refs/heads/main", self.repo), self.main_before)

    def assert_asked_for_the_state(self, number=7):
        self.assertIn(["pr", "view", str(number), "--json", "state"], self.gh_argv())

    def assert_nothing_changed(self, code, out, before):
        """AC-6: exit 1, the state file byte for byte, no history row, nothing archived."""
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertEqual(self.state_bytes(), before)
        self.assertTrue(os.path.isfile(os.path.join(self.rdir(), "review-1.md")))
        self.assertEqual(self.rows(), [])
        self.assertFalse(os.path.exists(os.path.join(self.home, "archive")))
        self.assertTrue(self.branch_exists())
        self.assert_pr_and_remote_untouched()

    def assert_recorded(self, stage, before="ready_to_merge", number=7):
        """AC-3 / AC-4: the record is in the archive with the new stage, and one row says so."""
        self.assertFalse(os.path.exists(self.rdir()))
        dest = self.archived(number)
        self.assertTrue(os.path.isfile(os.path.join(dest, "review-1.md")), os.listdir(self.home))
        self.assertEqual(State.load(dest).stage, stage)
        rows = self.rows()
        self.assertEqual(len(rows), 1, rows)
        self.assertEqual(rows[0]["stage"], stage)
        self.assertEqual(rows[0]["stage_before"], before)
        self.assertEqual(rows[0]["pr"], number)
        self.assertEqual(rows[0]["branch"], BRANCH)
        self.assertEqual(rows[0]["repo"], "me/sample")
        return rows[0]


class TheCommandExists(OutsideCase):
    """AC-1"""

    def test_listed_in_help_and_takes_a_branch(self):
        code, out = run_cli(["--help"])
        self.assertEqual(code, 0)
        self.assertIn("archive", out)
        code, out = run_cli(["archive", "--help"])
        self.assertEqual(code, 0, out)
        self.assertIn("--branch", out)

    def test_without_branch_it_acts_on_the_checked_out_branch(self):
        self.record()
        self.pr_is("CLOSED")
        code, out = run_cli(["archive"])
        self.assertEqual(code, EXIT_OK, out)
        self.assert_recorded("closed")

    def test_with_branch_from_another_branch(self):
        self.record()
        self.pr_is("CLOSED")
        self.leave_branch()
        code, out = run_cli(["archive", "--branch", BRANCH])
        self.assertEqual(code, EXIT_OK, out)
        self.assert_recorded("closed")
        self.assertEqual(gitops.current_branch(self.repo), "main")

    def test_with_branch_from_a_detached_head(self):
        self.record()
        self.pr_is("MERGED")
        git(["checkout", "-q", "--detach", "main"], self.repo)
        code, out = run_cli(["archive", "--branch", BRANCH])
        self.assertEqual(code, EXIT_OK, out)
        self.assert_recorded("merged")
        self.assertEqual(gitops.current_branch(self.repo), "HEAD")

    def test_the_git_branch_may_be_gone(self):
        self.record()
        self.pr_is("MERGED")
        self.leave_branch()
        git(["branch", "-q", "-D", BRANCH], self.repo)
        code, out = run_cli(["archive", "--branch", BRANCH])
        self.assertEqual(code, EXIT_OK, out)
        self.assert_recorded("merged")
        self.assert_pr_and_remote_untouched()


class AMergedPr(OutsideCase):
    """AC-2, AC-3, AC-5"""

    def test_recorded_archived_and_the_local_branch_deleted(self):
        self.record()
        self.pr_is("MERGED")
        self.leave_branch()
        code, out = run_cli(["archive", "--branch", BRANCH])
        self.assertEqual(code, EXIT_OK, out)
        row = self.assert_recorded("merged")
        self.assertIs(row["merged_outside"], True)
        self.assertEqual(row["exit"], EXIT_OK)
        state = State.load(self.archived())
        self.assertIn("outside", state.message)
        self.assertIn("revali merge", state.message)
        # the summary names the PR and the archive
        self.assertIn("#7", out)
        self.assertIn(self.archived(), out)
        self.assertFalse(self.branch_exists())
        self.assertEqual(gitops.current_branch(self.repo), "main")
        self.assert_asked_for_the_state()
        self.assert_pr_and_remote_untouched()

    def test_any_recorded_stage_is_accepted(self):
        self.leave_branch()
        cases = (
            (11, "needs_action", EXIT_OK),
            (12, "needs_human", EXIT_OK),
            (13, "error", EXIT_ERROR),
            (14, "stopped", EXIT_ERROR),
            (15, "review", -1),  # a run that died without a result
        )
        for number, stage, last_exit in cases:
            with self.subTest(stage=stage):
                self.record(stage=stage, pr_number=number, last_exit=last_exit)
                self.pr_is("MERGED", number=number)
                code, out = run_cli(["archive", "--branch", BRANCH])
                self.assertEqual(code, EXIT_OK, out)
                self.assertEqual(State.load(self.archived(number)).stage, "merged")
                row = self.rows()[-1]
                self.assertEqual((row["pr"], row["stage"]), (number, "merged"))
                self.assertEqual(row["stage_before"], stage)
                self.assertIs(row["merged_outside"], True)
                self.assertFalse(os.path.exists(self.rdir()))
        self.assertEqual(len(self.rows()), len(cases))

    def test_the_branch_checked_out_here_is_kept_and_the_output_says_so(self):
        self.record()
        self.pr_is("MERGED")
        code, out = run_cli(["archive"])
        self.assertEqual(code, EXIT_OK, out)
        self.assert_recorded("merged")
        self.assertTrue(self.branch_exists())
        self.assertEqual(gitops.current_branch(self.repo), BRANCH)
        kept = [line for line in out.splitlines() if BRANCH in line and "kept" in line]
        self.assertTrue(kept, out)

    def test_the_branch_checked_out_in_another_worktree_is_kept(self):
        self.record()
        self.pr_is("MERGED")
        self.leave_branch()
        other = os.path.join(self.tmp, "other tree")
        git(["worktree", "add", "--quiet", other, BRANCH], self.repo)
        code, out = run_cli(["archive", "--branch", BRANCH])
        self.assertEqual(code, EXIT_OK, out)
        self.assert_recorded("merged")
        self.assertTrue(self.branch_exists())
        self.assertEqual(gitops.current_branch(other), BRANCH)
        kept = [line for line in out.splitlines() if BRANCH in line and "kept" in line]
        self.assertTrue(kept, out)

    def test_an_empty_archive_dir_deletes_the_directory_as_merge_does(self):
        self.record()
        self.pr_is("MERGED")
        with open(os.path.join(self.home, "config.toml"), "w", encoding="utf-8") as fh:
            fh.write('[paths]\narchive_dir = ""\n')
        self.leave_branch()
        code, out = run_cli(["archive", "--branch", BRANCH])
        self.assertEqual(code, EXIT_OK, out)
        self.assertFalse(os.path.exists(self.rdir()))
        self.assertFalse(os.path.exists(os.path.join(self.home, "archive")))
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["stage"], "merged")
        self.assertIn("#7", out)


class AClosedPr(OutsideCase):
    """AC-2, AC-4, AC-5"""

    def test_gets_the_stage_closed_and_the_local_branch_stays(self):
        self.record()
        self.pr_is("CLOSED")
        self.leave_branch()
        tip = gitops.rev_parse("refs/heads/" + BRANCH, self.repo)
        code, out = run_cli(["archive", "--branch", BRANCH])
        self.assertEqual(code, EXIT_OK, out)
        row = self.assert_recorded("closed")
        self.assertFalse(row.get("merged_outside"))
        self.assertIn("#7", out)
        self.assertIn(self.archived(), out)
        # no working tree has the branch checked out, and it is still not deleted
        self.assertEqual(gitops.rev_parse("refs/heads/" + BRANCH, self.repo), tip)
        self.assert_asked_for_the_state()
        self.assert_pr_and_remote_untouched()

    def test_closed_from_any_stage_on_the_checked_out_branch(self):
        self.record(stage="needs_action")
        self.pr_is("CLOSED")
        code, out = run_cli(["archive"])
        self.assertEqual(code, EXIT_OK, out)
        row = self.assert_recorded("closed", before="needs_action")
        self.assertFalse(row.get("merged_outside"))
        self.assertEqual(gitops.current_branch(self.repo), BRANCH)
        self.assert_pr_and_remote_untouched()


class NothingChanges(OutsideCase):
    """AC-6"""

    def test_an_open_pr(self):
        self.record()
        self.pr_is("OPEN")
        before = self.state_bytes()
        code, out = run_cli(["archive"])
        self.assert_nothing_changed(code, out, before)
        self.assertIn("revali merge", out)
        self.assert_asked_for_the_state()

    def test_an_open_pr_from_another_branch_keeps_the_local_branch(self):
        self.record()
        self.pr_is("OPEN")
        before = self.state_bytes()
        self.leave_branch()
        code, out = run_cli(["archive", "--branch", BRANCH])
        self.assert_nothing_changed(code, out, before)

    def test_gh_failing_logged_out_or_without_a_known_state(self):
        self.record()
        before = self.state_bytes()
        self.leave_branch()
        cases = (
            ("pr view fails", "MERGED", {"pr_view_exit": 1}),
            ("not logged in", "MERGED", {"auth_exit": 1}),
            ("no state in the answer", None, {}),
            ("a state that is not known", "LOCKED", {}),
            ("an empty state", "", {}),
        )
        for name, state, scenario in cases:
            with self.subTest(name):
                self.pr_is(state, **scenario)
                code, out = run_cli(["archive", "--branch", BRANCH])
                self.assert_nothing_changed(code, out, before)

    def test_no_review_directory(self):
        self.pr_is("MERGED")
        self.leave_branch()
        code, out = run_cli(["archive", "--branch", "feature/none"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertFalse(os.path.exists(os.path.join(self.repo, ".revali", "feature__none")))
        self.assertEqual(self.rows(), [])
        self.assertFalse(os.path.exists(os.path.join(self.home, "archive")))
        self.assert_pr_and_remote_untouched()

    def test_a_directory_without_a_state(self):
        self.pr_is("MERGED")  # the fixture's directory holds change.md only
        code, out = run_cli(["archive"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertTrue(os.path.isfile(self.change_md()))
        self.assertFalse(os.path.exists(State.path(self.rdir())))
        self.assertEqual(self.rows(), [])
        self.assertFalse(os.path.exists(os.path.join(self.home, "archive")))

    def test_a_state_without_a_pr_number_points_at_clean(self):
        self.record(pr_number=0)
        self.pr_is("MERGED")
        before = self.state_bytes()
        self.leave_branch()
        code, out = run_cli(["archive", "--branch", BRANCH])
        self.assert_nothing_changed(code, out, before)
        self.assertIn("revali clean", out)

    def test_a_run_holding_the_branch_lock(self):
        self.record()
        self.pr_is("MERGED")
        before = self.state_bytes()
        self.leave_branch()
        pid = self.live_pid()
        acquire_lock(self.rdir(), pid=pid)
        code, out = run_cli(["archive", "--branch", BRANCH])
        self.assert_nothing_changed(code, out, before)
        self.assertEqual(read_lock(self.rdir())["pid"], pid)  # the run still holds its lock

    def test_a_run_holding_the_working_tree_lock(self):
        self.record()
        self.pr_is("MERGED")
        before = self.state_bytes()
        self.leave_branch()
        pid = self.live_pid()
        tpath = tree_lock_path(gitops.repo_root(self.repo), ".revali")
        acquire_tree_lock(tpath, "feature/other", pid=pid)
        code, out = run_cli(["archive", "--branch", BRANCH])
        self.assert_nothing_changed(code, out, before)
        held = read_tree_lock(tpath)
        self.assertEqual((held["pid"], held["branch"]), (pid, "feature/other"))


class StatusHint(OutsideCase):
    """AC-8"""

    def gone(self):
        self.record()
        self.leave_branch()
        git(["branch", "-q", "-D", BRANCH], self.repo)

    def test_status_names_archive_beside_clean(self):
        self.gone()
        code, out = run_cli(["status"])
        self.assertEqual(code, EXIT_OK, out)
        hint = [line for line in out.splitlines() if "feature__mul" in line]
        self.assertEqual(len(hint), 1, out)
        self.assertIn("revali archive --branch", hint[0])
        self.assertIn("revali clean", hint[0])

    def test_the_name_status_lists_is_one_archive_takes(self):
        """`status` lists the directory name (feature__mul); `archive --branch` with that name
        records the branch the state names, so the row joins the pipeline's earlier rows."""
        self.gone()
        self.pr_is("MERGED")
        code, out = run_cli(["archive", "--branch", "feature__mul"])
        self.assertEqual(code, EXIT_OK, out)
        row = self.assert_recorded("merged")
        self.assertIs(row["merged_outside"], True)
        code, out = run_cli(["status"])
        self.assertNotIn("feature__mul", out)
