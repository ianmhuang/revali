"""feature/archive-outside-merge (M2.5, issue #79): `revali archive` records a PR merged or
closed without `revali merge`, archives the branch's review files and never merges."""

import json
import os
from unittest import mock

from revali import EXIT_ERROR, EXIT_OK, gitops, merge
from revali.cli import build_parser
from revali.config import history_path
from revali.state import STAGES, TERMINAL_STAGES, State, acquire_lock, release_lock
from revali.stats import summarise
from tests.helpers import RepoCase, git, run_cli

PR = {"number": 7, "url": "https://github.example/me/sample/pull/7"}


class Archive(RepoCase):
    def setUp(self):
        super().setUp()
        self.history = history_path(None)
        git(["push", "-q", "-u", "origin", "feature/mul"], self.repo)  # what the pr stage does
        self.remote_before = self.remote_heads()

    def remote_heads(self):
        return git(["ls-remote", "--heads", "origin"], self.repo)

    def recorded(self, stage="ready_to_merge", pr_number=7):
        State(
            repo="me/sample",
            branch="feature/mul",
            base="main",
            stage=stage,
            message="validation 1 passed",
            last_exit=EXIT_OK,
            pr_number=pr_number,
            cost_usd=1.5,
            head_sha=gitops.rev_parse("HEAD", self.repo),
        ).save(self.rdir())

    def pr_is(self, state):
        self.scenario({"pr_create": dict(PR, state=state)})

    def archived(self):
        return os.path.join(self.home, "archive", "me__sample", "7-feature__mul")

    def rows(self):
        if not os.path.isfile(self.history):
            return []
        with open(self.history, encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def branch_exists(self):
        return bool(gitops.rev_parse("refs/heads/feature/mul", self.repo))

    def gh_argv(self):
        return [c["argv"] for c in self.fake_calls("gh")]

    def assert_nothing_changed(self, code, before):
        self.assertEqual(code, EXIT_ERROR)
        self.assertEqual(State.load(self.rdir()).stage, before)
        self.assertFalse(os.path.exists(self.archived()))
        self.assertEqual(self.rows(), [])

    def assert_never_merged(self):
        for argv in self.gh_argv():
            self.assertNotEqual(argv[:2], ["pr", "merge"], argv)
            self.assertNotEqual(argv[:2], ["pr", "edit"], argv)
            self.assertNotEqual(argv[:2], ["pr", "close"], argv)
        self.assertEqual(self.remote_heads(), self.remote_before)

    # AC-1 -----------------------------------------------------------------------------------

    def test_listed_in_help_and_in_the_readme(self):
        help_text = build_parser().format_help()
        self.assertIn("archive", help_text)
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(here, "README.md"), encoding="utf-8") as fh:
            self.assertIn("| archive [--branch <b>] |", fh.read())

    # AC-3, AC-5 -----------------------------------------------------------------------------

    def test_merged_from_another_branch_records_archives_and_drops_the_branch(self):
        self.recorded()
        self.pr_is("MERGED")
        git(["checkout", "-q", "main"], self.repo)
        code, out = run_cli(["archive", "--branch", "feature/mul"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn(
            "ARCHIVED: PR #7 merged outside `revali merge` (stage was ready_to_merge)", out
        )
        self.assertIn("local branch feature/mul removed", out)
        self.assertIn("archived to %s" % self.archived(), out)
        self.assertFalse(os.path.exists(self.rdir()))
        state = State.load(self.archived())
        self.assertEqual(state.stage, "merged")
        self.assertIn("merged outside `revali merge`", state.message)
        self.assertFalse(self.branch_exists())
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["stage"], "merged")
        self.assertIs(rows[0]["merged_outside"], True)
        self.assertEqual(rows[0]["stage_before"], "ready_to_merge")
        self.assertEqual(rows[0]["pr"], 7)
        self.assertIn(["pr", "view", "7", "--json", "state"], self.gh_argv())
        self.assert_never_merged()

    def test_merged_while_checked_out_keeps_the_branch_and_says_so(self):
        self.recorded()
        self.pr_is("MERGED")
        code, out = run_cli(["archive"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("local branch feature/mul kept: checked out in %s" % self.repo, out)
        self.assertTrue(self.branch_exists())
        self.assertEqual(State.load(self.archived()).stage, "merged")

    def test_merged_while_another_worktree_has_it_keeps_the_branch(self):
        self.recorded()
        self.pr_is("MERGED")
        git(["checkout", "-q", "main"], self.repo)
        wt = os.path.join(self.tmp, "wt")
        git(["worktree", "add", "--quiet", wt, "feature/mul"], self.repo)
        self.addCleanup(git, ["worktree", "remove", "--force", wt], self.repo)
        code, out = run_cli(["archive", "--branch", "feature/mul"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn(
            "local branch feature/mul kept: checked out in %s" % os.path.normpath(wt), out
        )
        self.assertTrue(self.branch_exists())

    def test_any_recorded_stage_is_accepted(self):
        self.recorded(stage="needs_human")
        self.pr_is("MERGED")
        git(["checkout", "-q", "main"], self.repo)
        code, out = run_cli(["archive", "--branch", "feature/mul"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertEqual(self.rows()[0]["stage_before"], "needs_human")

    def test_from_a_detached_head(self):
        self.recorded()
        self.pr_is("MERGED")
        git(["checkout", "-q", "--detach", "main"], self.repo)
        code, out = run_cli(["archive", "--branch", "feature/mul"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertFalse(self.branch_exists())

    def test_the_git_branch_may_be_gone(self):
        self.recorded()
        self.pr_is("MERGED")
        git(["checkout", "-q", "main"], self.repo)
        git(["branch", "-q", "-D", "feature/mul"], self.repo)
        code, out = run_cli(["archive", "--branch", "feature/mul"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("local branch feature/mul: none", out)
        self.assertTrue(os.path.isdir(self.archived()))

    def test_an_empty_archive_dir_deletes_the_directory(self):
        self.recorded()
        self.pr_is("MERGED")
        with open(os.path.join(self.home, "config.toml"), "w", encoding="utf-8") as fh:
            fh.write('[paths]\narchive_dir = ""\n')
        code, out = run_cli(["archive"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("removed .revali/feature__mul/", out)
        self.assertFalse(os.path.exists(self.rdir()))
        self.assertFalse(os.path.exists(self.archived()))
        self.assertEqual(len(self.rows()), 1)

    def test_a_branch_with_commits_the_pr_did_not_show_is_kept(self):
        """Round 1 F1: a commit after the last run, or a new branch of the same name."""
        self.recorded()
        self.pr_is("MERGED")
        self.write("later.txt", "not pushed\n")
        self.commit_all("after the last run")
        tip = gitops.rev_parse("HEAD", self.repo)
        git(["checkout", "-q", "main"], self.repo)
        code, out = run_cli(["archive", "--branch", "feature/mul"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("local branch feature/mul kept: its tip %s is not in" % tip[:10], out)
        self.assertEqual(gitops.rev_parse("refs/heads/feature/mul", self.repo), tip)

    def test_without_a_recorded_head_the_remote_branch_vouches_for_the_tip(self):
        self.recorded()
        state = State.load(self.rdir())
        state.head_sha = ""
        state.save(self.rdir())
        self.pr_is("MERGED")
        git(["checkout", "-q", "main"], self.repo)
        code, out = run_cli(["archive", "--branch", "feature/mul"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("local branch feature/mul removed", out)

    def test_a_rerun_after_its_own_failed_move_stays_merged_outside(self):
        """Round 2 F6: the stage `merged` was written by the first `archive`."""
        self.recorded()
        self.pr_is("MERGED")
        git(["checkout", "-q", "main"], self.repo)
        failed = "could not archive x (disk full); left in place: %s" % self.rdir()
        with mock.patch.object(merge, "archive_review_dir", lambda *a: failed):
            code, out = run_cli(["archive", "--branch", "feature/mul"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn(failed + "; `revali archive` again finishes the move", out)
        self.assertTrue(State.load(self.rdir()).merged_outside)
        code, out = run_cli(["archive", "--branch", "feature/mul"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("ARCHIVED: PR #7 merged outside `revali merge` (stage was merged)", out)
        rows = self.rows()
        self.assertEqual([r.get("merged_outside") for r in rows], [True, True])
        self.assertIn("merged outside `revali merge`: 1,", summarise(rows))

    def test_finishing_an_interrupted_revali_merge_is_not_merged_outside(self):
        """Round 1 F3."""
        self.recorded(stage="merged")
        state = State.load(self.rdir())
        state.pending_effect = "merge"
        state.save(self.rdir())
        self.pr_is("MERGED")
        git(["checkout", "-q", "main"], self.repo)
        code, out = run_cli(["archive", "--branch", "feature/mul"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("merged by `revali merge`, which stopped before its archive", out)
        row = self.rows()[0]
        self.assertEqual(row["stage_before"], "merged")
        self.assertNotIn("merged_outside", row)
        archived = State.load(self.archived())
        self.assertEqual(archived.pending_effect, "")
        self.assertNotIn("outside", archived.message)

    # AC-4 -----------------------------------------------------------------------------------

    def test_closed_gets_its_own_stage_and_leaves_the_branch(self):
        self.assertIn("closed", STAGES)
        self.assertIn("closed", TERMINAL_STAGES)
        self.recorded()
        self.pr_is("CLOSED")
        git(["checkout", "-q", "main"], self.repo)
        code, out = run_cli(["archive", "--branch", "feature/mul"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("ARCHIVED: PR #7 closed without merging (stage was ready_to_merge)", out)
        self.assertNotIn("local branch", out)
        self.assertTrue(self.branch_exists())
        self.assertEqual(State.load(self.archived()).stage, "closed")
        row = self.rows()[0]
        self.assertEqual(row["stage"], "closed")
        self.assertEqual(row["stage_before"], "ready_to_merge")
        self.assertNotIn("merged_outside", row)
        self.assert_never_merged()

    # AC-6 -----------------------------------------------------------------------------------

    def test_an_open_pr_changes_nothing(self):
        self.recorded()
        self.pr_is("OPEN")
        code, out = run_cli(["archive"])
        self.assert_nothing_changed(code, "ready_to_merge")
        self.assertIn("PR #7 is still open; `revali merge` merges it", out)
        self.assertTrue(self.branch_exists())
        self.assert_never_merged()

    def test_gh_failing_or_vague_changes_nothing(self):
        self.recorded()
        for scenario, words in (
            ({"pr_create": dict(PR, state="MERGED"), "pr_view_exit": 1}, "pr view failed"),
            ({"pr_create": dict(PR), "pr_view_exit": 0}, "no state in gh's answer"),
            ({"pr_create": dict(PR, state="DRAFTY")}, "gh reports state DRAFTY"),
            ({"pr_create": dict(PR, state="MERGED"), "auth_exit": 1}, "gh is not logged in"),
        ):
            with self.subTest(words=words):
                self.scenario(dict({"auth_exit": 0, "pr_view_exit": 0}, **scenario))
                code, out = run_cli(["archive"])
                self.assert_nothing_changed(code, "ready_to_merge")
                self.assertIn(words, out)

    def test_a_gh_that_cannot_run_is_one_error_line(self):
        """Round 1 F2."""
        self.recorded()
        os.environ["REVALI_GH_CMD"] = os.path.join(self.tmp, "no-such-gh")
        code, out = run_cli(["archive"])
        self.assert_nothing_changed(code, "ready_to_merge")
        self.assertIn("ERROR: gh could not be run", out)
        self.assertNotIn("Traceback", out)

    def test_a_detached_head_without_branch_names_the_option(self):
        self.recorded()
        git(["checkout", "-q", "--detach", "HEAD"], self.repo)
        code, out = run_cli(["archive"])
        self.assertEqual(code, EXIT_ERROR)
        self.assertIn("`revali archive --branch <branch>`", out)

    def test_no_state_or_no_pr_number_changes_nothing(self):
        code, out = run_cli(["archive"])
        self.assertEqual(code, EXIT_ERROR)
        self.assertIn("no review state for feature/mul", out)
        self.recorded(pr_number=0)
        self.pr_is("MERGED")
        code, out = run_cli(["archive"])
        self.assert_nothing_changed(code, "ready_to_merge")
        self.assertIn("`revali clean feature__mul`", out)
        self.assertFalse([a for a in self.gh_argv() if a[:2] == ["pr", "view"]])

    def test_a_run_in_progress_changes_nothing(self):
        self.recorded()
        self.pr_is("MERGED")
        acquire_lock(self.rdir(), pid=os.getpid())
        try:
            code, out = run_cli(["archive"])
        finally:
            release_lock(self.rdir())
        self.assert_nothing_changed(code, "ready_to_merge")
        self.assertIn("a run is in progress", out)

    # AC-8 -----------------------------------------------------------------------------------

    def test_status_names_archive_for_a_directory_whose_branch_is_gone(self):
        self.recorded()
        git(["checkout", "-q", "main"], self.repo)
        git(["branch", "-q", "-D", "feature/mul"], self.repo)
        code, out = run_cli(["status"])
        self.assertIn("feature__mul", out)
        self.assertIn("`revali archive --branch <name>`", out)
        self.assertIn("`revali clean <name>`", out)


class Stats(RepoCase):
    """AC-7"""

    def test_merged_outside_counts_as_merged_and_closed_as_neither(self):
        def row(branch, pr, stage, **extra):
            return dict(
                repo="me/sample", branch=branch, pr=pr, stage=stage, fixes=0, rounds=1, **extra
            )

        rows = [
            row("a", 1, "ready_to_merge"),
            row("a", 1, "merged"),
            row("b", 2, "ready_to_merge"),
            row("b", 2, "merged", merged_outside=True, stage_before="ready_to_merge"),
            row("c", 3, "ready_to_merge"),
            row("c", 3, "closed", stage_before="ready_to_merge"),
        ]
        text = summarise(rows)
        table = [line for line in text.splitlines() if line.startswith("| me/sample")][0]
        cells = [c.strip() for c in table.strip("|").split("|")]
        # repo | runs | reached verdict | first-try pass | merged | needs human | ...
        self.assertEqual(cells[1], "3")
        self.assertEqual(cells[3], "2/2")
        self.assertEqual(cells[4], "2")
        self.assertIn("merged outside `revali merge`: 1, closed without merging: 1", text)

    def test_merged_outside_stays_when_a_later_row_lacks_it(self):
        """Round 2 F6: `pipelines` keeps the flag like `fallback`."""
        rows = [
            dict(repo="me/sample", branch="b", pr=2, stage="merged", merged_outside=True),
            dict(repo="me/sample", branch="b", pr=2, stage="merged", stage_before="merged"),
        ]
        self.assertIn("merged outside `revali merge`: 1,", summarise(rows))
