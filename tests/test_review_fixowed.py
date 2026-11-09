"""Acceptance tests for fix/fix-cycle-survives-preflight-stop, AC-1 to AC-6 and AC-8.

The fix cycle the author owes after a CHANGES_REQUESTED round or a failed validation is
followed through runs that stop before the review (a dirty tree in preflight, the refusal
before detaching, an interrupted run closed by `revali stop`, an unexpected error) and
through runs that stop after the cycle was counted."""

import contextlib
import io
import os
from unittest import mock

from revali import EXIT_ACTION, EXIT_ERROR, EXIT_HUMAN, EXIT_OK
from revali.preflight import Stop
from revali.state import State
from tests.helpers import RepoCase, approve_response, claude_entry, git, run_cli

NOT_CLEAN = "working tree is not clean"
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
DIAGNOSIS = {
    "summary": "mul returns a + b instead of a * b, so the product test fails.",
    "cause": "code",
    "failures": [
        {
            "test": "tests/test_review_mul.py::MulTests::test_product",
            "cause": "code",
            "note": "expected 12, got 7",
        }
    ],
    "introduced_by": "unknown",
    "recommendation": "return a * b",
}


def changes_requested():
    return approve_response(verdict="CHANGES_REQUESTED", findings=[dict(FINDING)])


def approved_after_fix():
    return claude_entry(approve_response(previous_findings=[dict(RESOLVED)]), write_tests=False)


class OwedCase(RepoCase):
    def run_fg(self):
        return run_cli(["run", "--foreground"])

    def state(self):
        return State.load(self.rdir())

    def sessions(self):
        return len(self.fake_calls("claude"))

    def changes_requested_round(self, *later):
        """Round 1 asks for changes (exit 2); `later` are the answers of the sessions after."""
        self.claude(claude_entry(changes_requested()), *later)
        code, out = self.run_fg()
        self.assertEqual(code, EXIT_ACTION, out)
        self.assertIn("changes requested", out)
        self.assertEqual(self.state().fixes, 0)

    def edit_without_commit(self):
        path = os.path.join(self.repo, "src", "calc.py")
        with open(path, "a", encoding="utf-8", newline="") as fh:
            fh.write("\n# left uncommitted\n")

    def undo_edit(self):
        git(["checkout", "--", "src/calc.py"], self.repo)

    def dirty_preflight_stop(self):
        """A foreground run that preflight stops (exit 1) on an uncommitted edit; the edit is
        thrown away afterwards, so HEAD and the tree are what they were."""
        self.edit_without_commit()
        code, out = self.run_fg()
        self.undo_edit()
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertIn(NOT_CLEAN, out)

    def commit_fix(self, note="handle negatives"):
        self.write("src/calc.py", self.read("src/calc.py") + "\n# %s\n" % note)
        self.commit_all(note)

    def assert_same_head_refused(self):
        before = self.sessions()
        code, out = self.run_fg()
        self.assertEqual(code, EXIT_ACTION, out)
        self.assertIn(NOTHING_CHANGED, out)
        self.assertEqual(self.sessions(), before, "a reviewer session was started")
        self.assertEqual(self.state().fixes, 0)

    def assert_fix_counted_once(self, commit=True):
        if commit:
            self.commit_fix()
        code, out = self.run_fg()
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("run: fix cycle 1 of 2", out)
        self.assertEqual(out.count(COUNTED), 1, out)
        self.assertIn("fix cycles: 1", out)  # the READY TO MERGE summary
        self.assertEqual(self.state().fixes, 1)


class AfterAPreflightStop(OwedCase):
    """AC-1, AC-2, AC-8"""

    def test_a_fix_committed_after_the_stop_counts_one_cycle(self):
        self.changes_requested_round(approved_after_fix())
        self.dirty_preflight_stop()
        # AC-8: the stop is what `wait` and `status` report
        code, waited = run_cli(["wait", "--timeout", "5s"])
        self.assertEqual(code, EXIT_ERROR, waited)
        self.assertIn("error: " + NOT_CLEAN, waited)
        code, status = run_cli(["status"])
        self.assertEqual(code, EXIT_OK, status)
        self.assertIn("stage: error", status)
        self.assertIn("fixes: 0", status)
        self.assert_fix_counted_once()
        self.assertEqual(self.sessions(), 2)
        self.assertEqual(len(self.state().rounds), 2)

    def test_a_fix_committed_before_the_stop_counts_one_cycle(self):
        self.changes_requested_round(approved_after_fix())
        self.commit_fix()
        self.dirty_preflight_stop()  # the fix is committed, another edit is not
        self.assertEqual(self.state().fixes, 0)  # not counted by a run that did not pass
        self.assert_fix_counted_once(commit=False)

    def test_the_reviewed_head_is_refused_after_the_stop(self):
        self.changes_requested_round(approved_after_fix())
        self.dirty_preflight_stop()
        self.assert_same_head_refused()
        self.dirty_preflight_stop()
        self.dirty_preflight_stop()  # any number of them
        self.assert_same_head_refused()
        self.assertEqual(len(self.state().rounds), 1)


class AfterOtherStops(OwedCase):
    """AC-3: the same two outcomes whatever owed the cycle and whatever stopped the run."""

    def check(self, stop):
        stop()
        self.assert_same_head_refused()
        stop()  # again: the refusal above recorded exit 2, this puts the stop back on top
        self.assert_fix_counted_once()

    def refused_before_detaching(self):
        self.edit_without_commit()
        with mock.patch("revali.pipeline.spawn_detached") as spawn:
            code, out = run_cli(["run"])
        self.undo_edit()
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertIn(NOT_CLEAN, out)
        spawn.assert_not_called()
        self.assertEqual(self.state().stage, "error")

    def interrupted_then_marked_stopped(self):
        with mock.patch("revali.pipeline.preflight", side_effect=KeyboardInterrupt):
            code, out = self.run_fg()
        self.assertEqual(code, EXIT_ERROR, out)
        code, out = run_cli(["stop"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertEqual(self.state().stage, "stopped")
        self.assertIn("stage: stopped", run_cli(["status"])[1])  # AC-8

    def unexpected_error_in_preflight(self):
        with mock.patch("revali.pipeline.preflight", side_effect=RuntimeError("boom")):
            with contextlib.redirect_stderr(io.StringIO()):
                code, out = self.run_fg()
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertIn("RuntimeError", out)
        self.assertEqual(self.state().stage, "error")

    def test_a_failed_validation_then_a_preflight_stop(self):
        self.runner_scenario(
            {
                "default": 0,
                "results": {"validate-r1": {"new_test": 1}},
                "outputs": {"validate-r1": {"new_test": "AssertionError: 12 != 7"}},
            }
        )
        self.claude(
            claude_entry(),
            claude_entry(dict(DIAGNOSIS), write_tests=False, model="claude-opus-5", cost=0.2),
        )
        code, out = self.run_fg()
        self.assertEqual(code, EXIT_ACTION, out)
        self.assertEqual(self.state().last_verdict, "FAIL")
        self.runner_scenario({"default": 0})
        self.claude(claude_entry(approve_response(), write_tests=False))
        self.check(self.dirty_preflight_stop)

    def test_the_refusal_before_detaching(self):
        self.changes_requested_round(approved_after_fix())
        self.check(self.refused_before_detaching)

    def test_an_interrupted_run_marked_stopped(self):
        self.changes_requested_round(approved_after_fix())
        self.check(self.interrupted_then_marked_stopped)

    def test_an_unexpected_error_before_the_bookkeeping(self):
        self.changes_requested_round(approved_after_fix())
        self.check(self.unexpected_error_in_preflight)


class CountedOnce(OwedCase):
    """AC-4"""

    def test_stops_after_the_count_leave_nothing_owed(self):
        self.changes_requested_round(approved_after_fix())
        self.commit_fix()
        with mock.patch("revali.pr.ensure_pr", side_effect=Stop(EXIT_ERROR, "gh is down")):
            code, out = self.run_fg()
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertIn("run: fix cycle 1 of 2", out)
        state = self.state()
        self.assertEqual((state.stage, state.fixes), ("error", 1))
        # the next run is stopped by preflight with exit 2 (lint red), which records
        # `needs_action` over a last verdict that is still CHANGES_REQUESTED
        lint_red = Stop(EXIT_ACTION, "lint failed (exit 1); fix and run again:\nE501")
        with mock.patch("revali.pipeline.preflight", side_effect=lint_red):
            code, out = self.run_fg()
        self.assertEqual(code, EXIT_ACTION, out)
        self.assertEqual(self.state().stage, "needs_action")
        # same HEAD as the run that counted: reviewed, not refused, not counted again
        code, out = self.run_fg()
        self.assertEqual(code, EXIT_OK, out)
        self.assertNotIn(NOTHING_CHANGED, out)
        self.assertNotIn(COUNTED, out)
        self.assertIn("fix cycles: 1", out)
        self.assertEqual(self.state().fixes, 1)

    def test_revali_stop_after_the_count_leaves_nothing_owed(self):
        self.changes_requested_round(approved_after_fix())
        self.dirty_preflight_stop()
        self.commit_fix()
        with mock.patch("revali.review.run_round", side_effect=KeyboardInterrupt):
            code, out = self.run_fg()
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertIn("run: fix cycle 1 of 2", out)
        self.assertEqual(run_cli(["stop"])[0], EXIT_OK)
        state = self.state()
        self.assertEqual((state.stage, state.fixes), ("stopped", 1))
        code, out = self.run_fg()
        self.assertEqual(code, EXIT_OK, out)
        self.assertNotIn(NOTHING_CHANGED, out)
        self.assertNotIn(COUNTED, out)
        self.assertEqual(self.state().fixes, 1)


class LimitAcrossStops(OwedCase):
    """AC-5"""

    def test_two_fixes_each_after_a_preflight_stop_reach_the_limit(self):
        toml = self.read("revali.toml")
        self.assertIn("max_fixes = 2", toml)
        self.write("revali.toml", toml.replace("max_fixes = 2", "max_fixes = 1"))
        self.commit_all("one fix cycle allowed")
        self.changes_requested_round(
            claude_entry(changes_requested(), write_tests=False), approved_after_fix()
        )
        self.dirty_preflight_stop()
        self.commit_fix("first try")
        code, out = self.run_fg()
        self.assertEqual(code, EXIT_ACTION, out)
        self.assertIn("run: fix cycle 1 of 1", out)
        self.dirty_preflight_stop()
        self.commit_fix("second try")
        code, out = self.run_fg()
        self.assertEqual(code, EXIT_HUMAN, out)
        self.assertIn("2 fix cycles used (limit 1)", out)
        self.assertEqual(self.sessions(), 2)  # the third run never reached the reviewer
        self.assertEqual(self.state().fixes, 2)


class NothingOwed(OwedCase):
    """AC-6"""

    def test_a_needs_info_round_adds_no_cycle(self):
        question = approve_response(
            verdict="NEEDS_INFO",
            questions=["Are floats in scope?"],
            tests=[],
            previous_findings=[dict(RESOLVED)],
        )
        self.changes_requested_round(
            claude_entry(question, write_tests=False), approved_after_fix()
        )
        self.dirty_preflight_stop()
        self.commit_fix()
        code, out = self.run_fg()  # counts the fix for round 1; round 2 asks a question
        self.assertEqual(code, EXIT_ACTION, out)
        self.assertIn("run: fix cycle 1 of 2", out)
        self.assertEqual(self.state().last_verdict, "NEEDS_INFO")
        self.dirty_preflight_stop()
        self.commit_fix("answer: integers only")
        code, out = self.run_fg()
        self.assertEqual(code, EXIT_OK, out)
        self.assertNotIn(COUNTED, out)
        self.assertEqual(self.state().fixes, 1)

    def test_a_restart_after_a_history_rewrite_clears_what_was_owed(self):
        question = approve_response(
            verdict="NEEDS_INFO", questions=["Are floats in scope?"], tests=[]
        )
        self.changes_requested_round(claude_entry(question, write_tests=False), claude_entry())
        self.dirty_preflight_stop()
        head = git(["rev-parse", "HEAD"], self.repo).strip()
        self.assertEqual(self.state().test_commits[-1], head)
        git(["reset", "--hard", "HEAD~1"], self.repo)  # drops the reviewer's test commit
        code, out = self.run_fg()  # the reviewed HEAD again, but the review starts over
        self.assertEqual(code, EXIT_ACTION, out)
        self.assertIn("starts over from round 1", out)
        self.assertNotIn(NOTHING_CHANGED, out)
        self.commit_fix("answer: integers only")
        code, out = self.run_fg()
        self.assertEqual(code, EXIT_OK, out)
        self.assertNotIn(COUNTED, out)
        self.assertEqual(self.state().fixes, 0)

    def test_reset_clears_what_was_owed(self):
        self.changes_requested_round(claude_entry())
        self.dirty_preflight_stop()
        self.assertEqual(run_cli(["reset"])[0], EXIT_OK)
        code, out = self.run_fg()  # the HEAD the reviewer saw
        self.assertEqual(code, EXIT_OK, out)
        self.assertNotIn(NOTHING_CHANGED, out)
        self.assertNotIn(COUNTED, out)
        self.assertEqual(self.state().fixes, 0)
