"""AC-3 and AC-5 of fix/leftover-lows: `gitops.commit_paths` asks `git diff-tree` without
`--root` and still returns the same paths for a commit in base..HEAD (a root commit, which no
caller passes, now reads as empty); `_close_stopped` puts every field of the state back when
the write fails, not only the stage, message, exit code and timestamps, and a successful stop
still records the outcome."""

import os
import unittest
from dataclasses import asdict
from unittest import mock

from revali import EXIT_ERROR, gitops, pipeline
from revali.pipeline import _close_stopped
from revali.state import State
from tests.helpers import TEST_REVIEW_MUL, RepoCase, captured, claude_entry, git, run_cli

DENIED = PermissionError(13, "Access is denied")


class CommitPathsWithoutRoot(RepoCase):
    def test_a_branch_commit_lists_the_same_paths(self):  # AC-3
        self.write("tests/test_review_mul.py", TEST_REVIEW_MUL)
        self.write("docs/notes.md", "notes\n")
        self.write("src/calc.py", self.read("src/calc.py") + "\n# touched\n")
        self.commit_all("three paths")
        sha = git(["rev-parse", "HEAD"], self.repo).strip()
        with mock.patch("revali.gitops.git_ok", wraps=gitops.git_ok) as spy:
            paths = gitops.commit_paths(sha, self.repo)
        argv = spy.call_args[0][0]
        self.assertEqual(argv[0], "diff-tree")
        self.assertNotIn("--root", argv)
        self.assertEqual(paths, ["docs/notes.md", "src/calc.py", "tests/test_review_mul.py"])
        self.assertEqual(
            gitops.commit_paths(sha, self.repo, diff_filter="A"),
            ["docs/notes.md", "tests/test_review_mul.py"],
        )
        self.assertEqual(gitops.commit_paths(sha, self.repo, diff_filter="M"), ["src/calc.py"])

    def test_the_root_commit_is_no_longer_expanded(self):  # AC-3: the observable difference
        root = git(["rev-list", "--max-parents=0", "HEAD"], self.repo).strip()
        # without --root, diff-tree has nothing to compare the initial commit against
        self.assertEqual(gitops.commit_paths(root, self.repo), [])

    def test_the_reviewers_own_commit_still_lists_its_file(self):  # AC-3: the caller's case
        self.claude(claude_entry())  # approves and writes tests/test_review_mul.py
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, 0, out)
        sha = State.load(self.rdir()).test_commits[0]
        self.assertEqual(gitops.commit_paths(sha, self.repo), ["tests/test_review_mul.py"])


def full_state():
    return State(
        repo="owner/name",
        branch="feature/mul",
        base="main",
        stage="validate",
        round=2,
        fixes=1,
        pr_number=7,
        pr_url="https://example.invalid/pr/7",
        head_sha="a" * 40,
        base_sha="b" * 40,
        rounds=[{"round": 1, "verdict": "CHANGES_REQUESTED"}, {"round": 2, "verdict": "APPROVE"}],
        validations=[{"round": 1, "result": "FAIL"}],
        test_commits=["c" * 40],
        test_files=["tests/test_review_mul.py"],
        baseline_sha="d" * 40,
        issues=[{"number": 3}],
        cost_usd=1.25,
        models_used=["claude-fable-5"],
        last_verdict="APPROVE",
        last_exit=-1,
        message="validating",
        started_at="2026-01-01T00:00:00+0000",
        updated_at="2026-01-01T00:05:00+0000",
    )


class CloseStoppedRestoresTheWholeState(RepoCase):
    """RepoCase for its private REVALI_HOME: a successful stop appends a history row there."""

    def test_a_refused_write_leaves_no_field_changed(self):  # AC-5
        state = full_state()
        before = asdict(state)
        with mock.patch("revali.state.write_json_atomic", side_effect=DENIED):
            with captured() as out:
                ok = _close_stopped(state, self.rdir(), "stopped by user at stage 'validate'")
        self.assertFalse(ok)
        self.assertEqual(asdict(state), before)  # AC-5: every field, asdict comparison
        self.assertIn("could not be updated", out.getvalue())
        self.assertNotIn("Traceback", out.getvalue())
        self.assertFalse(os.path.exists(State.path(self.rdir())))

    def test_fields_beyond_the_outcome_are_restored_too(self):  # AC-5: the whole object
        """A save that assigns more than today's five fields before the write fails: the
        snapshot of the whole object puts those back as well."""
        state = full_state()
        before = asdict(state)

        def save_that_touches_more(self_, rdir):
            self_.updated_at = "2026-09-07T00:00:00+0000"
            self_.fixes += 1
            self_.cost_usd += 0.5
            self_.rounds = self_.rounds + [{"round": 3}]
            self_.models_used = []
            raise DENIED

        with mock.patch.object(State, "save", save_that_touches_more):
            with captured():
                ok = _close_stopped(state, self.rdir(), "stopped by user at stage 'validate'")
        self.assertFalse(ok)
        self.assertEqual(asdict(state), before)

    def test_a_successful_stop_records_the_outcome(self):  # AC-5: unchanged on success
        state = full_state()
        with mock.patch("revali.pipeline._record_history", wraps=pipeline._record_history) as h:
            with captured():
                ok = _close_stopped(state, self.rdir(), "stopped by user at stage 'validate'")
        self.assertTrue(ok)
        self.assertEqual(h.call_count, 1)
        self.assertEqual(h.call_args[0][1], EXIT_ERROR)
        saved = State.load(self.rdir())
        self.assertEqual(saved.stage, "stopped")
        self.assertEqual(saved.last_exit, EXIT_ERROR)
        self.assertEqual(saved.message, "stopped by user at stage 'validate'")
        self.assertEqual(saved.rounds, full_state().rounds)
        self.assertEqual(saved.test_files, ["tests/test_review_mul.py"])
        self.assertEqual(saved.fixes, 1)
        self.assertNotEqual(saved.updated_at, "2026-01-01T00:05:00+0000")


if __name__ == "__main__":
    unittest.main()
