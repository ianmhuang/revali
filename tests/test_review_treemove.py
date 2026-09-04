"""Reviewer acceptance tests for feature/tree-guard, AC-2: a run stops when the working
tree moves under it.

Before the reviewer is spawned, before its tests are committed, before every push and
before validation the run checks that the checked-out branch and HEAD are still its own.
On a mismatch it exits 1 at stage `error` with `the working tree moved under the run:
expected branch <b> at <sha>, found <b'> at <sha'>; ...`, commits, pushes and posts
nothing from that point, and the reviewer's files are discarded like after an
interruption.

The fixture pipeline runs in the foreground with the fake gh / claude / runner and real
git. Another session's move is simulated by wrapping one function of the review stage so
the branch or HEAD changes at a chosen moment: after the reviewer session returned (its
files are on disk, nothing committed), after the run's own test commit (before the push),
or after the round finished (before validation). On the base branch no check exists, so the
run carries on and every test here fails."""
import os
import unittest
from unittest import mock

from tests.helpers import RepoCase, claude_entry, git, run_cli
from revali import EXIT_ERROR, EXIT_OK
from revali import gitops, pr, review
from revali.state import State, lock_path

MOVED = "the working tree moved under the run: expected branch feature/mul at %s, found %s at %s"


class TreeMoveCase(RepoCase):
    def head(self):
        return gitops.rev_parse("HEAD", self.repo)

    def run_commits(self, ref):
        """The reviewer's test commits reachable from `ref` (the Revali-Round trailer)."""
        return git(["log", "--format=%B", ref], self.repo).count("Revali-Round:")

    def gh_calls(self, *verb):
        return [c for c in self.fake_calls("gh") if c["argv"][:len(verb)] == list(verb)]

    def switch_to_other(self):
        git(["checkout", "-q", "-b", "feature/other"], self.repo)

    def foreign_commit(self):
        self.write("src/other.py", "# committed by another session\n")
        git(["add", "src/other.py"], self.repo)
        git(["commit", "-q", "-m", "someone else's commit"], self.repo)
        return self.head()

    def run_moving_after_the_reviewer(self, move):
        """`run --foreground` where `move` runs after the reviewer session returned."""
        self.claude(claude_entry())
        real = review.spawn_reviewer

        def spawn_then_move(ctx, *a, **kw):
            rr = real(ctx, *a, **kw)
            move()
            return rr

        with mock.patch.object(review, "spawn_reviewer", side_effect=spawn_then_move):
            return run_cli(["run", "--foreground"])

    def assert_error_state(self):
        state = State.load(self.rdir())
        self.assertEqual(state.stage, "error")
        self.assertEqual(state.last_exit, EXIT_ERROR)
        return state

    def assert_locks_released(self):
        self.assertFalse(os.path.isfile(os.path.join(gitops.repo_root(self.repo), ".revali", "tree.lock")))
        self.assertFalse(os.path.isfile(lock_path(self.rdir())))


class TheTreeMovesWhileTheReviewerWorks(TreeMoveCase):
    def test_branch_switched_nothing_is_committed_on_either_branch(self):              # AC-2
        head = self.head()
        code, out = self.run_moving_after_the_reviewer(self.switch_to_other)
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertIn("ERROR: " + MOVED % (head[:10], "feature/other", head[:10]), out)
        self.assertIn("nothing was committed or pushed", out)
        state = self.assert_error_state()
        self.assertEqual(self.run_commits("feature/mul"), 0)                           # no commit from the run
        self.assertEqual(self.run_commits("feature/other"), 0)                         # ... on either branch
        self.assertEqual(state.test_commits, [])
        self.assertFalse(self.exists("tests/test_review_mul.py"))                      # discarded like an interruption
        self.assertFalse(state.reviewer_running)                                       # this run cleaned up itself
        self.assertEqual(self.gh_calls("pr", "comment"), [])                           # no PR comment
        self.assertEqual(self.gh_calls("pr", "edit"), [])                              # no body update
        self.assertEqual(gitops.current_branch(self.repo), "feature/other")            # the other session's move stays
        self.assert_locks_released()

    def test_foreign_commit_is_kept_and_named_in_the_message(self):                    # AC-2
        head = self.head()
        foreign = {}
        code, out = self.run_moving_after_the_reviewer(lambda: foreign.setdefault("sha", self.foreign_commit()))
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertIn(MOVED % (head[:10], "feature/mul", foreign["sha"][:10]), out)
        self.assertIn("nothing was committed or pushed", out)
        self.assert_error_state()
        self.assertEqual(self.head(), foreign["sha"])                                  # the other commit is untouched
        self.assertEqual(self.run_commits("feature/mul"), 0)
        self.assertFalse(self.exists("tests/test_review_mul.py"))
        self.assertTrue(self.exists("src/other.py"))                                   # not reverted by the cleanup
        self.assertEqual(self.gh_calls("pr", "comment"), [])
        self.assert_locks_released()

    def test_the_next_run_on_the_branch_starts_clean(self):                            # AC-2
        code, out = self.run_moving_after_the_reviewer(self.switch_to_other)
        self.assertEqual(code, EXIT_ERROR, out)
        git(["checkout", "-q", "feature/mul"], self.repo)
        git(["branch", "-q", "-D", "feature/other"], self.repo)
        self.claude(claude_entry())
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)                                           # nothing dirty was left behind
        self.assertNotIn("moved under the run", out)
        self.assertEqual(State.load(self.rdir()).stage, "ready_to_merge")
        self.assertEqual(self.run_commits("feature/mul"), 1)


class TheTreeMovesAfterTheRunsOwnCommit(TreeMoveCase):
    def test_moved_before_the_push_of_the_test_commit(self):                           # AC-2
        self.claude(claude_entry())
        real = review.commit_tests
        own = {}

        def commit_then_switch(ctx, *a, **kw):
            sha = real(ctx, *a, **kw)
            own["sha"] = sha
            self.switch_to_other()
            return sha

        with mock.patch.object(review, "commit_tests", side_effect=commit_then_switch):
            code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertIn(MOVED % (own["sha"][:10], "feature/other", own["sha"][:10]), out)   # expects its own commit
        self.assertIn("the test commit was not pushed", out)
        self.assert_error_state()
        self.assertEqual(self.run_commits("feature/mul"), 1)                           # made before the move, stays
        self.assertEqual(self.run_commits("origin/feature/mul"), 0)                    # but was never pushed
        self.assertEqual(self.gh_calls("pr", "comment"), [])                           # no review comment either
        self.assertEqual(self.gh_calls("pr", "edit"), [])
        self.assert_locks_released()

    def test_moved_before_validation(self):                                            # AC-2
        # the move lands after the test commit was pushed and the review comment posted:
        # the next thing the run would do is validate
        self.claude(claude_entry())
        real = pr.post_comment
        moved = []

        def comment_then_switch(*a, **kw):
            posted = real(*a, **kw)
            if not moved:
                moved.append(True)
                self.switch_to_other()
            return posted

        with mock.patch.object(pr, "post_comment", side_effect=comment_then_switch):
            code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertIn("the working tree moved under the run: expected branch feature/mul", out)
        self.assertIn("found feature/other", out)
        self.assertIn("validation was not started", out)
        state = self.assert_error_state()
        self.assertEqual(state.validations, [])                                        # validation did not run
        self.assertEqual(self.run_commits("feature/mul"), 1)                           # the run's own commit, before the move
        self.assertEqual(self.run_commits("origin/feature/mul"), 1)                    # pushed before the move
        self.assertEqual(len(self.gh_calls("pr", "comment")), 1)                       # the review comment, before the move
        self.assertNotIn("validate", " ".join(self.gh_calls("pr", "comment")[0]["argv"]))
        self.assertEqual(self.gh_calls("pr", "edit"), [])                              # no body update after the move
        self.assertEqual(self.gh_calls("pr", "ready"), [])                             # never marked ready
        self.assert_locks_released()


if __name__ == "__main__":
    unittest.main()
