"""fix/fix-due-on-stop (M3.13): a CHANGES_REQUESTED round owes its fix from the moment its
verdict is saved, and the state's head is the reviewer's test commit, whatever stops the run
before its exit-2 summary."""

import types
from unittest import mock

from revali import EXIT_ACTION, EXIT_ERROR, EXIT_OK, gitops
from revali.preflight import Stop
from revali.state import State
from tests.helpers import approve_response, claude_entry, git, run_cli
from tests.test_fix_cycle_owed import APPROVED, CR, OwedCase


def head(repo):
    return git(["rev-parse", "HEAD"], repo).strip()


def failing_second_push():
    """The PR stage's push goes through; the push of the reviewer's test commit fails."""
    real = gitops.push_branch
    calls = []

    def push(*args, **kwargs):
        calls.append(args)
        if len(calls) == 2:
            return types.SimpleNamespace(ok=False, text="remote rejected")
        return real(*args, **kwargs)

    return mock.patch.object(gitops, "push_branch", push)


class StoppedAfterTheVerdict(OwedCase):
    def stopped_round(self, patch, expected):
        """Round 1 asks for changes and the run stops after the verdict with `patch` on."""
        self.claude(claude_entry(CR), claude_entry(APPROVED, write_tests=False))
        with patch:
            code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertIn(expected, out)
        state = State.load(self.rdir())
        self.assertEqual(state.last_verdict, "CHANGES_REQUESTED")
        test_commit = state.rounds[-1]["test_commit"]
        self.assertTrue(test_commit)
        self.assertEqual(head(self.repo), test_commit)
        return state, test_commit

    def assert_owed_then_counted_once(self, test_commit):
        """AC-1, AC-2, AC-3"""
        calls = self.reviewer_calls()
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ACTION, out)
        self.assertIn("nothing changed since the last review", out)
        self.assertIn("round 1 requested changes", out)
        self.assertIn("review-1.md", out)
        self.assertEqual(self.reviewer_calls(), calls)
        self.assert_one_fix_cycle()  # fix on top of the test commit: one cycle, round 2 approves
        state = State.load(self.rdir())
        self.assertEqual(len(state.rounds), 2)
        self.assertEqual(state.test_commits.count(test_commit), 1)
        git(["fetch", "-q", "origin"], self.repo)
        remote = git(["rev-parse", "origin/feature/mul"], self.repo).strip()
        git(["merge-base", "--is-ancestor", test_commit, remote], self.repo)  # raises if not

    def test_the_push_of_the_test_commit_fails(self):
        state, commit = self.stopped_round(
            failing_second_push(), "git push of the test commit failed"
        )
        self.assertTrue(state.fix_due)
        self.assertEqual(state.head_sha, commit)
        self.assert_owed_then_counted_once(commit)

    def test_the_tree_moved_before_the_push(self):
        moved = Stop(
            EXIT_ERROR, "the working tree moved under the run; the test commit was not pushed"
        )
        state, commit = self.stopped_round(
            mock.patch("revali.pipeline.check_tree_unmoved", side_effect=moved),
            "the test commit was not pushed",
        )
        self.assertTrue(state.fix_due)
        self.assert_owed_then_counted_once(commit)

    def test_the_pr_comment_cannot_be_posted(self):
        state, commit = self.stopped_round(
            mock.patch("revali.pr.post_comment", side_effect=Stop(EXIT_ERROR, "gh is down")),
            "gh is down",
        )
        self.assertTrue(state.fix_due)
        self.assert_owed_then_counted_once(commit)


class Unchanged(OwedCase):
    """AC-4"""

    def test_an_exit_two_round_is_counted_once(self):
        self.changes_requested(claude_entry(APPROVED, write_tests=False))
        self.assert_nothing_changed()
        self.assert_one_fix_cycle()

    def test_an_approved_round_stopped_at_its_push_resumes_at_validation(self):
        self.claude(claude_entry(approve_response()))
        with failing_second_push():
            code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR, out)
        state = State.load(self.rdir())
        self.assertFalse(state.fix_due)
        calls_before = self.reviewer_calls()
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("continuing there without a new review", out)
        self.assertEqual(self.reviewer_calls(), calls_before)
        self.assertEqual(State.load(self.rdir()).fixes, 0)
