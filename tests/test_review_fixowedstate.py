"""Acceptance tests for fix/fix-cycle-survives-preflight-stop, AC-7: a state file written
before the change (no `fix_due` key) owes a fix exactly when the old rule said so (stage
`needs_action` with last_verdict CHANGES_REQUESTED or FAIL), and STATE_VERSION is bumped."""

import json
import os

from revali import EXIT_ACTION, EXIT_ERROR, EXIT_OK, STATE_VERSION
from revali.state import State
from tests.helpers import RepoCase, approve_response, claude_entry, git, run_cli

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


class OldStateCase(RepoCase):
    def run_fg(self):
        return run_cli(["run", "--foreground"])

    def state(self):
        return State.load(self.rdir())

    def synthetic(self, **fields):
        """The state loaded from a minimal file of the old layout."""
        os.makedirs(self.rdir(), exist_ok=True)
        with open(State.path(self.rdir()), "w", encoding="utf-8", newline="") as fh:
            fh.write(json.dumps(dict({"version": 6}, **fields)) + "\n")
        return self.state()

    def changes_requested_round(self):
        """A real CHANGES_REQUESTED round; the next reviewer session approves."""
        cr = approve_response(verdict="CHANGES_REQUESTED", findings=[dict(FINDING)])
        approved = approve_response(previous_findings=[dict(RESOLVED)])
        self.claude(claude_entry(cr), claude_entry(approved, write_tests=False))
        code, out = self.run_fg()
        self.assertEqual(code, EXIT_ACTION, out)

    def rewrite_as_version_6(self, **fields):
        """Make the state on disk the one the previous release would have written."""
        path = State.path(self.rdir())
        with open(path, "r", encoding="utf-8", newline="") as fh:
            data = json.load(fh)
        data.pop("fix_due", None)
        data["version"] = 6
        data.update(fields)
        with open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write(json.dumps(data, indent=2) + "\n")

    def dirty_preflight_stop(self):
        path = os.path.join(self.repo, "src", "calc.py")
        with open(path, "a", encoding="utf-8", newline="") as fh:
            fh.write("\n# left uncommitted\n")
        code, out = self.run_fg()
        git(["checkout", "--", "src/calc.py"], self.repo)
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertIn("working tree is not clean", out)


class OldStateFile(OldStateCase):
    def test_state_version_is_bumped(self):
        self.assertGreaterEqual(STATE_VERSION, 7)

    def test_the_old_rule_reads_as_owing(self):
        for verdict in ("CHANGES_REQUESTED", "FAIL"):
            with self.subTest(last_verdict=verdict):
                state = self.synthetic(stage="needs_action", last_verdict=verdict)
                self.assertIs(state.fix_due, True)

    def test_any_other_old_state_reads_as_owing_nothing(self):
        for stage, verdict in (
            ("needs_action", "NEEDS_INFO"),
            ("needs_action", "APPROVE"),
            ("needs_action", ""),
            ("error", "CHANGES_REQUESTED"),
            ("error", "FAIL"),
            ("stopped", "CHANGES_REQUESTED"),
            ("needs_human", "CHANGES_REQUESTED"),
            ("review", "CHANGES_REQUESTED"),
            ("validate", "FAIL"),
            ("ready_to_merge", "PASS"),
            ("preflight", ""),
        ):
            with self.subTest(stage=stage, last_verdict=verdict):
                state = self.synthetic(stage=stage, last_verdict=verdict)
                self.assertIs(state.fix_due, False)

    def test_an_old_changes_requested_state_keeps_its_cycle_through_a_preflight_stop(self):
        self.changes_requested_round()
        self.rewrite_as_version_6()
        state = self.state()
        self.assertEqual((state.stage, state.last_verdict), ("needs_action", "CHANGES_REQUESTED"))
        self.assertTrue(state.fix_due)
        self.dirty_preflight_stop()  # the first run of the new release on the old file
        sessions = len(self.fake_calls("claude"))
        code, out = self.run_fg()
        self.assertEqual(code, EXIT_ACTION, out)
        self.assertIn(NOTHING_CHANGED, out)
        self.assertEqual(len(self.fake_calls("claude")), sessions)
        self.dirty_preflight_stop()
        self.write("src/calc.py", self.read("src/calc.py") + "\n# handle negatives\n")
        self.commit_all("handle negatives")
        code, out = self.run_fg()
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("run: fix cycle 1 of 2", out)
        self.assertEqual(self.state().fixes, 1)

    def test_an_old_state_already_overwritten_by_an_error_owes_nothing(self):
        self.changes_requested_round()
        self.rewrite_as_version_6(stage="error", message="working tree is not clean", last_exit=1)
        self.assertFalse(self.state().fix_due)
        code, out = self.run_fg()  # the HEAD the reviewer saw: reviewed again, as before
        self.assertEqual(code, EXIT_OK, out)
        self.assertNotIn(NOTHING_CHANGED, out)
        self.assertNotIn(COUNTED, out)
        self.assertEqual(self.state().fixes, 0)
