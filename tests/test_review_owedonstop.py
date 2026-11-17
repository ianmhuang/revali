"""Acceptance tests for fix/fix-due-on-stop (M3.13), AC-1 to AC-5.

A round whose verdict is CHANGES_REQUESTED owes its fix from the moment the verdict is saved.
The runs here stop between that save and the exit-2 summary: the push of the reviewer's test
commit is rejected, the process is interrupted during that push, the author commits under the
run, posting the PR comment raises. The next run must refuse an unchanged HEAD without a
reviewer session and name the round and its review file; once the author has committed, one
fix cycle is counted and the reviewer's test commit is on the remote, once. When the PR is
merged outside revali after such a stop, `revali archive` keeps the local branch while the
recorded test commit is not in the `origin/<branch>` the clone knows, and deletes it when it
is there or when the clone no longer knows `origin/<branch>` (pruned after the merge)."""

import contextlib
import io
import os
import re
import subprocess
import types
from unittest import mock

from revali import EXIT_ACTION, EXIT_ERROR, EXIT_OK, gitops
from revali.preflight import check_tree_unmoved
from revali.state import State
from tests.helpers import RepoCase, approve_response, claude_entry, git, run_cli

BRANCH = "feature/mul"
REVIEW_TEST = "tests/test_review_mul.py"  # the file the fake reviewer writes
NOTHING_CHANGED = "nothing changed since the last review"
COUNTED = "run: fix cycle"
FINDING = {
    "id": "F1",
    "file": "src/calc.py",
    "line": 3,
    "severity": "high",
    "kind": "correctness",
    "text": "mul ignores negative numbers",
    "suggestion": "handle them",
}
RESOLVED = {"id": "F1", "status": "resolved", "note": "negatives handled"}


def changes_requested():
    return approve_response(verdict="CHANGES_REQUESTED", findings=[dict(FINDING)])


def approved_after_fix():
    return claude_entry(approve_response(previous_findings=[dict(RESOLVED)]), write_tests=False)


def head(repo):
    return git(["rev-parse", "HEAD"], repo).strip()


def reviewer_file_in_head(repo):
    listed = git(["ls-tree", "-r", "--name-only", "HEAD", "--", REVIEW_TEST], repo)
    return bool(listed.strip())


def at_the_test_commit_push(action):
    """Every push goes through as it is, except the one that would carry the reviewer's test
    commit (HEAD holds the reviewer's file): `action` runs in its place."""
    real = gitops.push_branch

    def push(branch, cwd, *args, **kwargs):
        if reviewer_file_in_head(cwd):
            return action()
        return real(branch, cwd, *args, **kwargs)

    return mock.patch.object(gitops, "push_branch", push)


def rejected():
    return types.SimpleNamespace(ok=False, text="remote rejected (simulated)")


def interrupted():
    raise KeyboardInterrupt


def comment_raises():
    return mock.patch("revali.pr.post_comment", side_effect=RuntimeError("gh went away"))


class StopCase(RepoCase):
    def run_fg(self):
        with contextlib.redirect_stderr(io.StringIO()):  # an unexpected error's traceback
            return run_cli(["run", "--foreground"])

    def state(self):
        return State.load(self.rdir())

    def sessions(self):
        return len(self.fake_calls("claude"))

    def commit_fix(self, note="handle negatives"):
        self.write("src/calc.py", self.read("src/calc.py") + "\n# %s\n" % note)
        git(["add", "--", "src/calc.py"], self.repo)
        git(["commit", "-q", "-m", note], self.repo)

    def on_remote(self, commit):
        branch = git(["rev-parse", "--abbrev-ref", "HEAD"], self.repo).strip()
        listed = git(["ls-remote", "origin", "refs/heads/" + branch], self.repo).split()
        if not listed:
            return False
        done = subprocess.run(
            ["git", "merge-base", "--is-ancestor", commit, listed[0]],
            cwd=self.repo,
            capture_output=True,
        )
        return done.returncode == 0

    def stopped_round(self, fault, *later):
        """Round 1 asks for changes and commits its tests; `fault` then stops the run (exit 1)
        before its exit-2 summary. Returns the reviewer's test commit."""
        self.claude(claude_entry(changes_requested()), *later)
        with fault:
            code, out = self.run_fg()
        self.assertEqual(code, EXIT_ERROR, out)
        state = self.state()
        self.assertEqual(len(state.rounds), 1, out)
        self.assertEqual(state.last_verdict, "CHANGES_REQUESTED", out)
        commit = state.rounds[-1]["test_commit"]
        self.assertTrue(commit, "round 1 committed no tests")
        self.assertTrue(os.path.isfile(os.path.join(self.rdir(), "review-1.md")))
        self.assertEqual(self.sessions(), 1)
        return commit

    def assert_unchanged_head_refused(self):
        """AC-1"""
        before = self.sessions()
        code, out = self.run_fg()
        self.assertEqual(code, EXIT_ACTION, out)
        self.assertIn(NOTHING_CHANGED, out)
        said = out[out.index(NOTHING_CHANGED) :]
        self.assertRegex(said, re.compile(r"\bround 1\b"))
        self.assertIn("review-1.md", said)
        self.assertEqual(self.sessions(), before, "a reviewer session was started")
        state = self.state()
        self.assertEqual((len(state.rounds), state.fixes), (1, 0))

    def assert_reviewer_tests_kept_once(self, commit):
        """AC-3"""
        self.assertTrue(self.on_remote(commit), "the test commit is not on the remote")
        touching = git(["log", "--format=%H", "--", REVIEW_TEST], self.repo).split()
        self.assertEqual(touching, [commit])
        self.assertEqual(self.state().test_commits.count(commit), 1)
        self.assertTrue(self.exists(REVIEW_TEST))

    def assert_fix_counted_once(self, commit, commit_first=True):
        """AC-2, AC-3"""
        if commit_first:
            self.commit_fix()
        code, out = self.run_fg()
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("run: fix cycle 1 of 2", out)
        self.assertEqual(out.count(COUNTED), 1, out)
        state = self.state()
        self.assertEqual((state.fixes, len(state.rounds)), (1, 2))
        self.assertEqual(self.sessions(), 2)
        self.assert_reviewer_tests_kept_once(commit)


class StoppedAfterTheVerdict(StopCase):
    """AC-1, AC-2, AC-3"""

    def test_the_push_of_the_test_commit_is_rejected(self):
        commit = self.stopped_round(at_the_test_commit_push(rejected), approved_after_fix())
        self.assertEqual(head(self.repo), commit)
        self.assertFalse(self.on_remote(commit))  # the run stopped at that push
        self.assertEqual(self.state().head_sha, commit)  # AC-2: the state's head
        self.assert_unchanged_head_refused()
        self.assert_unchanged_head_refused()  # any number of times
        self.assertEqual(head(self.repo), commit)
        self.assert_fix_counted_once(commit)

    def test_the_process_is_interrupted_during_that_push(self):
        # nothing records the run's end: the state is what the round itself saved
        commit = self.stopped_round(at_the_test_commit_push(interrupted), approved_after_fix())
        self.assertFalse(self.on_remote(commit))
        code, out = run_cli(["stop"])  # acknowledges the dead run, as the author would
        self.assertEqual(code, EXIT_OK, out)
        self.assertEqual(self.state().head_sha, commit)
        self.assert_unchanged_head_refused()
        self.assert_fix_counted_once(commit)

    def test_the_author_commits_under_the_run_before_that_push(self):
        def author_commits_first(ctx, *args, **kwargs):
            self.commit_fix("committed while the run was working")
            return check_tree_unmoved(ctx, *args, **kwargs)  # the real check: HEAD moved

        commit = self.stopped_round(
            mock.patch("revali.pipeline.check_tree_unmoved", author_commits_first),
            approved_after_fix(),
        )
        self.assertEqual(git(["rev-parse", "HEAD~1"], self.repo).strip(), commit)
        self.assertFalse(self.on_remote(commit))
        # AC-2: the state's head is the reviewer's commit, so what sits on top of it is the
        # author's change and the next run counts it, once
        self.assertEqual(self.state().head_sha, commit)
        self.assert_fix_counted_once(commit, commit_first=False)

    def test_posting_the_pr_comment_raises(self):
        commit = self.stopped_round(comment_raises(), approved_after_fix())
        self.assertEqual(head(self.repo), commit)
        self.assertTrue(self.on_remote(commit))  # pushed before the comment
        self.assertEqual(self.state().head_sha, commit)
        self.assert_unchanged_head_refused()
        self.assert_fix_counted_once(commit)

    def test_the_rounds_after_the_stop_count_as_before(self):
        commit = self.stopped_round(
            at_the_test_commit_push(rejected),
            claude_entry(changes_requested(), write_tests=False),
            approved_after_fix(),
        )
        self.commit_fix("first try")
        code, out = self.run_fg()  # round 2 asks again and ends with its exit 2
        self.assertEqual(code, EXIT_ACTION, out)
        self.assertIn("run: fix cycle 1 of 2", out)
        self.assertTrue(self.on_remote(commit))
        before = self.sessions()
        code, out = self.run_fg()
        self.assertEqual(code, EXIT_ACTION, out)
        self.assertIn(NOTHING_CHANGED, out)
        self.assertEqual(self.sessions(), before)
        self.commit_fix("second try")
        code, out = self.run_fg()
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("run: fix cycle 2 of 2", out)
        state = self.state()
        self.assertEqual((state.fixes, len(state.rounds)), (2, 3))
        self.assertEqual(self.sessions(), 3)
        self.assert_reviewer_tests_kept_once(commit)


class ArchiveAfterTheStop(StopCase):
    """AC-5: the PR is merged outside revali after the stopped run."""

    def archive_the_merged_pr(self):
        number = self.state().pr_number
        self.assertTrue(number, "the stopped run recorded no PR")
        merged = {
            "number": number,
            "url": "https://github.example/me/sample/pull/%d" % number,
            "state": "MERGED",
        }
        self.scenario({"pr_create": merged})
        git(["checkout", "-q", "main"], self.repo)
        code, out = run_cli(["archive", "--branch", BRANCH])
        self.assertEqual(code, EXIT_OK, out)
        return out

    def local_tip(self):
        return gitops.rev_parse("refs/heads/" + BRANCH, self.repo)

    def kept_lines(self, out):
        return [line for line in out.splitlines() if BRANCH in line and "kept" in line]

    def test_a_test_commit_whose_push_was_rejected_keeps_the_branch(self):
        commit = self.stopped_round(at_the_test_commit_push(rejected))
        self.assertFalse(self.on_remote(commit))
        self.assertEqual(self.state().head_sha, commit)  # the recorded head is that commit
        out = self.archive_the_merged_pr()
        self.assertEqual(self.local_tip(), commit, out)  # the only copy of the reviewer's tests
        # "and says so": a kept line names the commit, what it is, and where it is missing
        named = [line for line in self.kept_lines(out) if commit[:7] in line]
        self.assertTrue(named, out)
        self.assertIn("test commit", named[0])
        self.assertIn("origin/" + BRANCH, named[0])

    def test_a_test_commit_the_remote_holds_lets_the_branch_go(self):
        commit = self.stopped_round(comment_raises())  # stopped after the push
        self.assertTrue(self.on_remote(commit))
        self.assertEqual(self.state().head_sha, commit)
        out = self.archive_the_merged_pr()
        self.assertFalse(self.local_tip(), out)
        self.assertFalse(self.kept_lines(out), out)

    def test_a_pushed_test_commit_whose_remote_ref_was_pruned_lets_the_branch_go(self):
        commit = self.stopped_round(comment_raises())  # stopped after the push
        self.assertTrue(self.on_remote(commit))
        self.assertEqual(self.state().head_sha, commit)
        tracking = "refs/remotes/origin/" + BRANCH
        # what a pruning fetch does once the remote branch is deleted with the merge
        git(["update-ref", "-d", tracking], self.repo)
        self.assertFalse(gitops.rev_parse(tracking, self.repo))
        out = self.archive_the_merged_pr()
        self.assertFalse(self.local_tip(), out)
        self.assertFalse(self.kept_lines(out), out)

    def test_an_author_commit_after_the_stop_keeps_the_branch(self):
        commit = self.stopped_round(at_the_test_commit_push(rejected))
        self.commit_fix("made after the stopped run")  # never pushed, never seen by the PR
        tip = head(self.repo)
        self.assertNotEqual(tip, commit)
        out = self.archive_the_merged_pr()
        self.assertEqual(self.local_tip(), tip, out)
        self.assertTrue(self.kept_lines(out), out)


class UnchangedElsewhere(StopCase):
    """AC-4"""

    def test_a_round_that_ended_with_exit_two_is_owed_and_counted_once(self):
        self.claude(claude_entry(changes_requested()), approved_after_fix())
        code, out = self.run_fg()
        self.assertEqual(code, EXIT_ACTION, out)
        commit = self.state().rounds[-1]["test_commit"]
        self.assertEqual(head(self.repo), commit)
        code, out = self.run_fg()
        self.assertEqual(code, EXIT_ACTION, out)
        self.assertIn(NOTHING_CHANGED, out)
        self.assertEqual(self.sessions(), 1)
        self.assert_fix_counted_once(commit)

    def test_an_approved_round_stopped_at_its_push_resumes_at_validation(self):
        self.claude(claude_entry())
        with at_the_test_commit_push(rejected):
            code, out = self.run_fg()
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertEqual(self.state().last_verdict, "APPROVE")
        code, out = self.run_fg()  # HEAD unchanged: nothing is owed, validation follows
        self.assertEqual(code, EXIT_OK, out)
        self.assertNotIn(NOTHING_CHANGED, out)
        self.assertNotIn(COUNTED, out)
        self.assertIn("without a new review", out)
        self.assertEqual(self.sessions(), 1)
        self.assertEqual(self.state().fixes, 0)

    def test_a_needs_info_round_stopped_at_its_comment_owes_nothing(self):
        question = approve_response(
            verdict="NEEDS_INFO", questions=["Are floats in scope?"], tests=[]
        )
        self.claude(claude_entry(question, write_tests=False), claude_entry())
        with comment_raises():
            code, out = self.run_fg()
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertEqual(self.state().last_verdict, "NEEDS_INFO")
        self.commit_fix("answer: integers only")
        code, out = self.run_fg()
        self.assertEqual(code, EXIT_OK, out)
        self.assertNotIn(NOTHING_CHANGED, out)
        self.assertNotIn(COUNTED, out)
        self.assertEqual(self.state().fixes, 0)
