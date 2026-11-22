"""fix/last-cycle-message: what the author is told after the round that used the last fix
cycle (AC-1 to AC-4), read from what `revali run` prints and from the lines `revali wait`
and `revali status` print.

The fixture's `review.max_fixes` is 2 and its src/calc.py has 13 lines: `return a + b` is
line 5, `return a * b` line 13. Every test goes through a round before the last cycle
first, so the wording that must stay (AC-4) and the new one are read on the same branch."""

import unittest

from revali import EXIT_ACTION, EXIT_HUMAN
from revali.state import State
from tests.helpers import RepoCase, approve_response, claude_entry, git, run_cli
from tests.test_validate import diagnosis

RUN_AGAIN = "commit, run again"
STOPS = "next run on a new commit stops for a human"
NOTE = r"the next run stops for a human\.?$"
ADD = "    return a + b\n"
MUL = "    return a * b\n"


def finding(fid, line, sev="high", kind="correctness"):
    return {
        "id": fid,
        "file": "src/calc.py",
        "line": line,
        "severity": sev,
        "kind": kind,
        "text": "%s wording" % fid,
        "suggestion": "",
    }


def resolved(*ids):
    return [{"id": fid, "status": "resolved", "note": "fixed"} for fid in ids]


def changes(findings, *previous):
    return approve_response(
        verdict="CHANGES_REQUESTED", findings=findings, previous_findings=resolved(*previous)
    )


def hint_lines(out):
    return [line.strip() for line in out.splitlines() if line.strip().startswith("hint:")]


class LastCycleCase(RepoCase):
    def limit(self, n):
        toml = self.read("revali.toml")
        self.assertIn("max_fixes = 2", toml)
        self.write("revali.toml", toml.replace("max_fixes = 2", "max_fixes = %d" % n))
        self.commit_all("max_fixes = %d" % n)

    def edit(self, old, new, message):
        text = self.read("src/calc.py")
        self.assertIn(old, text)
        self.write("src/calc.py", text.replace(old, new, 1))
        self.commit_all(message)

    def fix_add(self):
        self.edit(ADD, "    return b + a\n", "swap the operands of add")

    def fix_mul(self):
        self.edit(MUL, "    return b * a\n", "swap the operands of mul")

    def run_expect(self, code):
        got, out = run_cli(["run", "--foreground"])
        self.assertEqual(got, code, out)
        return out

    def state(self):
        return State.load(self.rdir())

    def wait_line(self):
        code, out = run_cli(["wait", "--timeout", "1s"])
        self.assertEqual(code, EXIT_ACTION, out)
        line = out.strip().splitlines()[-1]
        self.assertTrue(line.startswith("needs_action: "), out)
        return line.rstrip()

    def status_line(self):
        out = run_cli(["status"])[1]
        lines = [line.rstrip() for line in out.splitlines() if line.startswith("message: ")]
        self.assertEqual(len(lines), 1, out)
        return lines[0]

    def assert_before(self, out):
        """AC-4: a round before the last fix cycle reads as it did."""
        self.assertIn(RUN_AGAIN, out)
        self.assertNotIn("used up", out)
        self.assertNotIn("stops for a human", out)
        for line in (self.wait_line(), self.status_line()):
            self.assertNotIn("next run stops", line)
            self.assertNotIn("last fix cycle", line)

    def assert_last(self, out, fixes, limit, round_no):
        """AC-1 / AC-2: the exit 2 message; AC-3: the stage message of `wait` and `status`."""
        self.assertIn("ACTION NEEDED", out)
        self.assertNotIn(RUN_AGAIN, out)
        self.assertRegex(out, r"used up \(%d of %d\)" % (fixes, limit))
        self.assertIn(STOPS, out)
        self.assertIn("response-%d.md" % round_no, out)
        for line in (self.wait_line(), self.status_line()):
            self.assertRegex(line, NOTE)
            self.assertIn("last fix cycle", line)  # what skill/SKILL.md looks for
            self.assertEqual(line.count("next run stops"), 1, line)
        state = self.state()
        self.assertEqual(state.stage, "needs_action")  # AC-4: the outcome is the same
        self.assertEqual(state.last_exit, EXIT_ACTION)
        self.assertEqual((len(state.rounds), state.fixes), (round_no, fixes))
        self.assertTrue(state.fix_due)


class ChangesRequested(LastCycleCase):
    def test_round_three_of_the_default_limit(self):
        self.claude(
            claude_entry(changes([finding("F1", 5)])),
            claude_entry(changes([finding("F2", 13)], "F1"), write_tests=False),
            claude_entry(changes([finding("F3", 9)], "F2"), write_tests=False),
        )
        out = self.run_expect(EXIT_ACTION)
        self.assertIn("fix cycle 0 of 2", out)
        self.assert_before(out)  # AC-4
        self.fix_add()
        out = self.run_expect(EXIT_ACTION)
        self.assertIn("fix cycle 1 of 2", out)
        self.assert_before(out)  # AC-4: one cycle is left
        self.fix_mul()
        out = self.run_expect(EXIT_ACTION)
        self.assertIn("fix cycle 2 of 2", out)
        self.assert_last(out, 2, 2, 3)  # AC-1, AC-3
        self.assertIn("review-3.md", out)
        blocking = [line for line in out.splitlines() if line.strip().startswith("- F3")]
        self.assertEqual(len(blocking), 1, out)  # AC-1: what blocks is still listed
        self.assertIn("high", blocking[0])
        # AC-4: the refusal on the same HEAD, the counter and the exit 3 stop are as before
        out = self.run_expect(EXIT_ACTION)
        self.assertIn("nothing changed since the last review", out)
        self.assertEqual(self.state().fixes, 2)
        git(["commit", "-q", "--allow-empty", "-m", "answer round 3"], self.repo)
        self.run_expect(EXIT_HUMAN)  # what the message announced
        state = self.state()
        self.assertEqual(state.stage, "needs_human")
        self.assertEqual(len(state.rounds), 3)
        self.assertEqual(len(self.fake_calls("claude")), 3)  # no fourth review

    def test_a_round_revali_blocks_for_an_ac_without_a_test(self):
        """The reviewer approves round 2, revali asks for changes: the same exit 2 message."""
        self.limit(1)
        gap = approve_response(previous_findings=resolved("F1"), tests=[], not_testable=[])
        self.claude(claude_entry(changes([finding("F1", 5)])), claude_entry(gap, write_tests=False))
        out = self.run_expect(EXIT_ACTION)
        self.assert_before(out)  # AC-4: cycle 0 of 1
        self.fix_add()
        out = self.run_expect(EXIT_ACTION)
        self.assertEqual(self.state().last_verdict, "CHANGES_REQUESTED")
        self.assert_last(out, 1, 1, 2)  # AC-1, AC-3
        self.assertIn("AC-1", out)  # what blocks
        self.assertIn("review-2.md", out)
        self.fix_mul()
        self.run_expect(EXIT_HUMAN)


class ValidationFailed(LastCycleCase):
    def test_second_failure_uses_the_last_cycle(self):
        self.limit(1)
        failed = {"new_test": 1}
        said = {"new_test": "AssertionError: 12 != 7"}
        self.runner_scenario(
            {
                "default": 0,
                "results": {"validate-r1": failed, "validate-r2": failed},
                "outputs": {"validate-r1": said, "validate-r2": said},
            }
        )
        diagnosed = claude_entry(diagnosis(), write_tests=False, model="claude-opus-5", cost=0.2)
        second = approve_response(previous_findings=[])
        self.claude(claude_entry(), diagnosed, claude_entry(second, write_tests=False), diagnosed)
        out = self.run_expect(EXIT_ACTION)
        self.assertIn("validation 1 FAILED", out)
        self.assert_before(out)  # AC-4: cycle 0 of 1
        self.fix_mul()
        out = self.run_expect(EXIT_ACTION)
        self.assertIn("validation 2 FAILED", out)
        self.assert_last(out, 1, 1, 2)  # AC-2, AC-3
        self.assertIn("return a * b", out)  # the diagnosis is still printed
        self.assertIn("tests.md", out)
        self.assertEqual(self.state().last_verdict, "FAIL")
        self.fix_add()
        self.run_expect(EXIT_HUMAN)  # AC-4: what the message announced
        state = self.state()
        self.assertEqual((len(state.rounds), len(state.validations)), (2, 2))


class WithStopHint(LastCycleCase):
    def test_short_form_before_and_at_the_last_cycle(self):
        low = {"sev": "low", "kind": "convention"}
        self.claude(
            claude_entry(changes([finding("F1", 5)])),
            claude_entry(changes([finding("F2", 5, **low)], "F1"), write_tests=False),
            claude_entry(changes([finding("F3", 13, **low)], "F2"), write_tests=False),
        )
        self.run_expect(EXIT_ACTION)
        self.fix_add()  # line 5
        out = self.run_expect(EXIT_ACTION)
        self.assertEqual(len(hint_lines(out)), 1, out)
        self.assert_before(out)  # AC-4: cycle 1 of 2
        line = self.wait_line()
        self.assertIn("hint:", line)
        self.assertTrue(line.endswith("consider answering, not changing them"), line)  # AC-4
        self.fix_mul()  # line 13
        out = self.run_expect(EXIT_ACTION)
        hints = hint_lines(out)
        self.assertEqual(len(hints), 1, out)
        self.assertIn("human", hints[0])
        self.assert_last(out, 2, 2, 3)  # AC-1; AC-3: the note once, at the end
        for line in (self.wait_line(), self.status_line()):
            self.assertIn("hint:", line)  # AC-3: the hint's short form carries the note
            self.assertRegex(line, r"\b1 low\b")
            self.assertNotIn("not changing them", line)


if __name__ == "__main__":
    unittest.main()
