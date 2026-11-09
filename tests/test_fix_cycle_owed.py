"""A fix cycle owed after CHANGES_REQUESTED or a validation FAIL survives a run that stops
before the bookkeeping, and is not counted twice after a run that stops after it
(fix/fix-cycle-survives-preflight-stop)."""

import json
import os
from unittest import mock

from revali import EXIT_ACTION, EXIT_ERROR, EXIT_HUMAN, EXIT_OK, STATE_VERSION
from revali.preflight import Stop
from revali.state import State
from tests.helpers import RepoCase, approve_response, claude_entry, git, run_cli
from tests.test_validate import diagnosis

HEADER = "working tree is not clean; commit or stash first:"
DEAD_PID = 999999999


def finding():
    return {
        "id": "F1",
        "file": "src/calc.py",
        "line": 3,
        "severity": "high",
        "kind": "correctness",
        "text": "mul ignores negative numbers",
        "suggestion": "handle them",
    }


CR = approve_response(verdict="CHANGES_REQUESTED", findings=[finding()])
APPROVED = approve_response(previous_findings=[{"id": "F1", "status": "resolved", "note": "x"}])


class OwedCase(RepoCase):
    def changes_requested(self, *later):
        """Round 1 asks for changes; `later` are the reviewer's answers after it."""
        self.claude(claude_entry(CR), *later)
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ACTION, out)
        self.assertEqual(State.load(self.rdir()).stage, "needs_action")

    def dirty(self):
        path = os.path.join(self.repo, "src", "calc.py")
        with open(path, "a", encoding="utf-8", newline="") as fh:
            fh.write("\n# not committed\n")

    def clean(self):
        git(["checkout", "--", "src/calc.py"], self.repo)

    def preflight_stop(self):
        """A run that stops in preflight on a dirty tree, which is then put back as it was."""
        self.dirty()
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertIn(HEADER, out)
        self.assertEqual(State.load(self.rdir()).stage, "error")
        self.clean()

    def fix(self, note="handles negatives"):
        self.write("src/calc.py", self.read("src/calc.py") + "\n# %s\n" % note)
        self.commit_all(note)

    def reviewer_calls(self):
        return len(self.fake_calls("claude"))

    def assert_nothing_changed(self):
        calls = self.reviewer_calls()
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ACTION, out)
        self.assertIn("nothing changed since the last review", out)
        self.assertEqual(self.reviewer_calls(), calls)

    def assert_one_fix_cycle(self):
        self.fix()
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("fix cycle 1 of 2", out)
        self.assertEqual(State.load(self.rdir()).fixes, 1)


class AfterPreflightStop(OwedCase):
    """AC-1, AC-2, AC-8"""

    def test_the_fix_after_a_preflight_stop_is_counted_once(self):
        self.changes_requested(claude_entry(APPROVED, write_tests=False))
        self.preflight_stop()
        self.assert_one_fix_cycle()

    def test_an_unchanged_head_is_still_refused(self):
        self.changes_requested()
        self.preflight_stop()
        self.assert_nothing_changed()

    def test_status_and_wait_report_the_preflight_stop(self):
        self.changes_requested()
        self.dirty()
        self.assertEqual(run_cli(["run", "--foreground"])[0], EXIT_ERROR)
        code, waited = run_cli(["wait", "--timeout", "5s"])
        self.assertEqual(code, EXIT_ERROR, waited)
        self.assertIn(HEADER, waited)
        code, status = run_cli(["status"])
        self.assertIn("stage: error", status)


class AfterOtherStops(OwedCase):
    """AC-3"""

    def test_validation_fail(self):
        self.runner_scenario(
            {
                "default": 0,
                "results": {"validate-r1": {"new_test": 1}},
                "outputs": {"validate-r1": {"new_test": "AssertionError: 12 != 7"}},
            }
        )
        self.claude(
            claude_entry(approve_response()),
            claude_entry(diagnosis(), write_tests=False, model="claude-opus-5", cost=0.2),
        )
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ACTION, out)
        self.assertEqual(State.load(self.rdir()).last_verdict, "FAIL")
        self.preflight_stop()
        self.assert_nothing_changed()
        self.runner_scenario({"default": 0})
        self.claude(claude_entry(APPROVED, write_tests=False))
        self.assert_one_fix_cycle()

    def test_refused_before_detaching(self):
        self.changes_requested(claude_entry(APPROVED, write_tests=False))
        self.dirty()
        with mock.patch("revali.pipeline.spawn_detached", return_value=DEAD_PID) as spawn:
            code, out = run_cli(["run"])
        self.assertEqual(code, EXIT_ERROR, out)
        spawn.assert_not_called()
        self.assertEqual(State.load(self.rdir()).stage, "error")
        self.clean()
        self.assert_nothing_changed()
        self.assert_one_fix_cycle()

    def test_run_found_dead_and_marked_stopped(self):
        self.changes_requested(claude_entry(APPROVED, write_tests=False))
        state = State.load(self.rdir())
        state.last_exit = -1  # a run began and died in preflight
        state.save(self.rdir())
        code, out = run_cli(["stop"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertEqual(State.load(self.rdir()).stage, "stopped")
        self.assert_nothing_changed()
        self.assert_one_fix_cycle()

    def test_unexpected_error_before_the_bookkeeping(self):
        self.changes_requested(claude_entry(APPROVED, write_tests=False))
        with mock.patch("revali.pipeline.preflight", side_effect=RuntimeError("boom")):
            code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertEqual(State.load(self.rdir()).stage, "error")
        self.assert_nothing_changed()
        self.assert_one_fix_cycle()


class CountedOnce(OwedCase):
    """AC-4"""

    def test_a_stop_after_the_count_does_not_count_again(self):
        self.changes_requested(claude_entry(APPROVED, write_tests=False))
        self.fix()
        with mock.patch("revali.pr.ensure_pr", side_effect=Stop(EXIT_ERROR, "gh is down")):
            code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertIn("fix cycle 1 of 2", out)
        state = State.load(self.rdir())
        self.assertEqual((state.stage, state.fixes), ("error", 1))
        # same HEAD: not "nothing changed", and the cycle is not counted again
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertNotIn("nothing changed", out)
        self.assertNotIn("run: fix cycle", out)
        self.assertEqual(State.load(self.rdir()).fixes, 1)


class LimitAcrossStops(OwedCase):
    """AC-5"""

    def test_max_fixes_counts_through_preflight_stops(self):
        self.write(
            "revali.toml", self.read("revali.toml").replace("max_fixes = 2", "max_fixes = 1")
        )
        self.commit_all("limit")
        self.changes_requested(claude_entry(CR, write_tests=False), claude_entry(CR))
        self.preflight_stop()
        self.fix("try 1")
        self.assertEqual(run_cli(["run", "--foreground"])[0], EXIT_ACTION)
        self.preflight_stop()
        self.fix("try 2")
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_HUMAN, out)
        self.assertIn("2 fix cycles used (limit 1)", out)
        self.assertEqual(self.reviewer_calls(), 2)


class NothingOwed(OwedCase):
    """AC-6"""

    def test_needs_info_owes_nothing(self):
        q = approve_response(verdict="NEEDS_INFO", questions=["Floats?"], tests=[])
        self.claude(claude_entry(q, write_tests=False), claude_entry(APPROVED, write_tests=False))
        self.assertEqual(run_cli(["run", "--foreground"])[0], EXIT_ACTION)
        self.assertFalse(State.load(self.rdir()).fix_due)
        self.preflight_stop()
        self.fix("answered")
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertNotIn("run: fix cycle", out)
        self.assertEqual(State.load(self.rdir()).fixes, 0)

    def test_history_rewrite_restart_clears_it(self):
        q = approve_response(verdict="NEEDS_INFO", questions=["Floats?"], tests=[])
        self.changes_requested(
            claude_entry(q, write_tests=False), claude_entry(APPROVED, write_tests=False)
        )
        self.assertTrue(State.load(self.rdir()).fix_due)
        git(["reset", "--hard", "HEAD~1"], self.repo)  # drops the reviewer's test commit
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ACTION, out)
        self.assertIn("starts over from round 1", out)
        self.assertFalse(State.load(self.rdir()).fix_due)
        self.fix("answered")
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertNotIn("run: fix cycle", out)
        self.assertEqual(State.load(self.rdir()).fixes, 0)

    def test_reset_clears_it(self):
        self.changes_requested(claude_entry(APPROVED))
        self.assertEqual(run_cli(["reset"])[0], EXIT_OK)
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertNotIn("nothing changed", out)
        self.assertNotIn("run: fix cycle", out)


class OldStateFile(RepoCase):
    """AC-7"""

    def load_old(self, **fields):
        data = {"version": 6, "stage": "needs_action", "last_verdict": "CHANGES_REQUESTED"}
        data.update(fields)
        os.makedirs(self.rdir(), exist_ok=True)
        with open(State.path(self.rdir()), "w", encoding="utf-8", newline="") as fh:
            fh.write(json.dumps(data) + "\n")
        return State.load(self.rdir())

    def test_version_bumped(self):
        self.assertGreaterEqual(STATE_VERSION, 7)

    def test_owed_when_the_old_rule_said_so(self):
        self.assertTrue(self.load_old().fix_due)
        self.assertTrue(self.load_old(last_verdict="FAIL").fix_due)

    def test_not_owed_otherwise(self):
        for fields in (
            {"last_verdict": "NEEDS_INFO"},
            {"last_verdict": "APPROVE"},
            {"stage": "error"},
            {"stage": "ready_to_merge", "last_verdict": "PASS"},
        ):
            self.assertFalse(self.load_old(**fields).fix_due, fields)

    def test_a_written_field_wins(self):
        self.assertFalse(self.load_old(fix_due=False).fix_due)
        self.assertTrue(self.load_old(stage="error", fix_due=True).fix_due)


class Docs(RepoCase):
    """AC-9"""

    def test_workflow_md(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "docs", "workflow.md"), "r", encoding="utf-8") as fh:
            text = " ".join(fh.read().split())
        self.assertIn("neither loses nor repeats a fix cycle", text)
