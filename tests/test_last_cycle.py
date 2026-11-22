"""After the round that used the last fix cycle, the exit 2 messages say that the next run
stops for a human instead of telling the author to commit and run again (fix/last-cycle-message)."""

import unittest

from revali import EXIT_ACTION, EXIT_HUMAN
from revali.state import State
from tests.helpers import RepoCase, approve_response, claude_entry, run_cli
from tests.test_validate import diagnosis

RUN_AGAIN = "commit, run again"
STOP = "the next run on a new commit stops for a human to decide"
NOTE = "last fix cycle: the next run stops for a human"


def finding(fid, line, sev="high", kind="correctness"):
    return {
        "id": fid,
        "file": "src/calc.py",
        "line": line,
        "severity": sev,
        "kind": kind,
        "text": "%s text" % fid,
        "suggestion": "",
    }


CR = approve_response(verdict="CHANGES_REQUESTED", findings=[finding("F1", 13)])
RESOLVED = [{"id": "F1", "status": "resolved", "note": "fixed"}]
FAIL_R2 = {
    "default": 0,
    "results": {"validate-r2": {"new_test": 1}},
    "outputs": {"validate-r2": {"new_test": "AssertionError: 12 != 7"}},
}
DIAGNOSIS = claude_entry(diagnosis(), write_tests=False, model="claude-opus-5", cost=0.2)


class LastCycleCase(RepoCase):
    def max_fixes(self, n):
        self.write(
            "revali.toml", self.read("revali.toml").replace("max_fixes = 2", "max_fixes = %d" % n)
        )
        self.commit_all("max_fixes %d" % n)

    def fix(self, text="handle negatives"):
        self.write("src/calc.py", self.read("src/calc.py") + "\n# %s\n" % text)
        self.commit_all(text)

    def run_expect(self, code):
        got, out = run_cli(["run", "--foreground"])
        self.assertEqual(got, code, out)
        return out

    def state(self):
        return State.load(self.rdir())

    def assert_last_cycle(self, out, fixes, max_fixes, round_no):
        self.assertNotIn(RUN_AGAIN, out)
        self.assertIn("fix cycles used up (%d of %d)" % (fixes, max_fixes), out)
        self.assertIn(STOP, out)
        self.assertIn("response-%d.md" % round_no, out)
        state = self.state()
        self.assertTrue(state.message.endswith(NOTE), state.message)  # AC-3
        self.assertEqual(state.message.count("next run stops"), 1, state.message)
        code, status = run_cli(["status"])
        self.assertIn(NOTE, status)

    def assert_as_before(self, out):
        self.assertIn(RUN_AGAIN, out)
        self.assertNotIn("fix cycles used up", out)
        self.assertNotIn("next run stops", self.state().message)


class ChangesRequested(LastCycleCase):
    def test_round_that_used_the_last_cycle(self):
        self.max_fixes(1)
        second = approve_response(
            verdict="CHANGES_REQUESTED", findings=[finding("F2", 5)], previous_findings=RESOLVED
        )
        self.claude(claude_entry(CR), claude_entry(second, write_tests=False))
        out = self.run_expect(EXIT_ACTION)
        self.assert_as_before(out)  # AC-4: round 1 is cycle 0 of 1
        self.fix()
        out = self.run_expect(EXIT_ACTION)
        self.assert_last_cycle(out, 1, 1, 2)  # AC-1
        self.assertIn("F2 [high correctness]", out)  # still lists what blocks
        self.assertIn("review-2.md", out)
        state = self.state()
        self.assertEqual((state.stage, state.fixes, state.fix_due), ("needs_action", 1, True))
        out = self.run_expect(EXIT_ACTION)  # AC-4: the refusal is unchanged
        self.assertIn("nothing changed since the last review", out)
        self.fix("answer")
        out = self.run_expect(EXIT_HUMAN)  # AC-4: what the message announced
        self.assertIn("2 fix cycles used (limit 1)", out)

    def test_max_fixes_zero_makes_round_one_the_last(self):
        self.max_fixes(0)
        self.claude(claude_entry(CR))
        out = self.run_expect(EXIT_ACTION)
        self.assert_last_cycle(out, 0, 0, 1)
        self.fix()
        self.run_expect(EXIT_HUMAN)

    def test_before_the_last_cycle_nothing_changes(self):
        self.claude(claude_entry(CR))
        out = self.run_expect(EXIT_ACTION)  # default max_fixes 2, cycle 0
        self.assert_as_before(out)  # AC-4
        self.assertTrue(self.state().message.endswith("review-1.md)"), self.state().message)


class ValidationFailed(LastCycleCase):
    def test_failure_after_the_last_cycle(self):
        self.max_fixes(1)
        self.runner_scenario(FAIL_R2)
        approve = approve_response(previous_findings=RESOLVED)
        self.claude(claude_entry(CR), claude_entry(approve, write_tests=False), DIAGNOSIS)
        self.run_expect(EXIT_ACTION)
        self.fix()
        out = self.run_expect(EXIT_ACTION)
        self.assertIn("FAILED", out)
        self.assert_last_cycle(out, 1, 1, 2)  # AC-2
        self.assertIn("diagnosis says the test is wrong", out)  # the rest of the message stays

    def test_failure_before_the_last_cycle(self):
        self.runner_scenario(FAIL_R2)
        approve = approve_response(previous_findings=RESOLVED)
        self.claude(claude_entry(CR), claude_entry(approve, write_tests=False), DIAGNOSIS)
        self.run_expect(EXIT_ACTION)
        self.fix()
        out = self.run_expect(EXIT_ACTION)  # cycle 1 of 2
        self.assertIn("FAILED", out)
        self.assert_as_before(out)  # AC-4


class WithStopHint(LastCycleCase):
    def test_the_hint_short_form_carries_the_stop(self):
        self.max_fixes(1)
        second = approve_response(
            verdict="CHANGES_REQUESTED",
            findings=[finding("F2", 15, sev="low", kind="convention")],
            previous_findings=RESOLVED,
        )
        self.claude(claude_entry(CR), claude_entry(second, write_tests=False))
        self.run_expect(EXIT_ACTION)
        self.fix()
        out = self.run_expect(EXIT_ACTION)
        self.assertIn("hint: the 1 low finding(s) of round 2", out)
        self.assert_last_cycle(out, 1, 1, 2)  # AC-3: one note, in the hint's short form
        message = self.state().message
        self.assertIn("hint: 1 low finding(s) in lines the last fix changed", message)
        self.assertNotIn("consider answering, not changing them", message)


if __name__ == "__main__":
    unittest.main()
