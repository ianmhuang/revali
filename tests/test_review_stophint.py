"""feature/stop-hint: the hint after a round whose findings are all low and all sit in what
the previous round's fix changed (AC-1, AC-2, AC-3, AC-6).

The fixture's src/calc.py has 13 lines: `return a + b` is line 5, `return a * b` line 13.
The fix between round 1 and round 2 rewrites line 5 only, so the lines within the margin
are 2 to 8."""

import json
import os
import unittest

from revali import EXIT_ACTION, EXIT_OK
from revali.state import State
from tests.helpers import RepoCase, approve_response, claude_entry, git, run_cli
from tests.test_validate import diagnosis

ADD = "    return a + b\n"
MUL = "    return a * b\n"
RESOLVED = [{"id": "F1", "status": "resolved", "note": "fixed"}]


def finding(fid, line, sev="low", kind="convention", file="src/calc.py"):
    return {
        "id": fid,
        "file": file,
        "line": line,
        "severity": sev,
        "kind": kind,
        "text": "%s wording" % fid,
        "suggestion": "",
    }


def round_one():
    blocker = finding("F1", 5, sev="high", kind="correctness")
    return approve_response(verdict="CHANGES_REQUESTED", findings=[blocker])


def answer(findings, previous=None, **kw):
    previous = RESOLVED if previous is None else previous
    return approve_response(findings=findings, previous_findings=previous, **kw)


def hint_lines(out):
    return [line.strip() for line in out.splitlines() if line.strip().startswith("hint:")]


class StopHintCase(RepoCase):
    def edit(self, old, new, message):
        text = self.read("src/calc.py")
        self.assertIn(old, text)
        self.write("src/calc.py", text.replace(old, new, 1))
        self.commit_all(message)

    def fix_add(self):
        self.edit(ADD, "    return b + a\n", "swap the operands of add")

    def fix_mul(self):
        self.edit(MUL, "    return b * a\n", "swap the operands of mul")

    def first_round(self):
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ACTION, out)

    def two_rounds(self, second, *later, expect=EXIT_OK):
        """Round 1 asks for a change at line 5 and commits its test file; the author rewrites
        line 5; round 2 answers `second`."""
        self.claude(claude_entry(round_one()), claude_entry(second, write_tests=False), *later)
        self.first_round()
        self.fix_add()
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, expect, out)
        return out

    def state(self):
        return State.load(self.rdir())

    def status(self):
        return run_cli(["status"])[1]

    def assert_hint(self, out, count, round_no):
        lines = hint_lines(out)
        self.assertEqual(len(lines), 1, out)
        self.assertRegex(lines[0], r"\b%d low\b" % count)
        self.assertRegex(lines[0], r"\bround %d\b" % round_no)
        self.assertIn("response-%d.md" % round_no, lines[0])
        self.assertIn("wontfix", lines[0])
        self.assertIn("hint:", self.state().message)
        self.assertIn("hint:", self.status())

    def assert_no_hint(self, out):
        self.assertEqual(hint_lines(out), [], out)
        self.assertNotIn("hint:", self.state().message)
        status = self.status()
        self.assertNotIn("hint:", status)
        # the run did record a round: `status` prints this branch's per-round cost line
        self.assertIn("cost per round:", status)


class ReadyToMerge(StopHintCase):
    def test_lows_in_and_three_lines_around_the_fix(self):
        out = self.two_rounds(answer([finding("F2", 2), finding("F3", 5), finding("F4", 8)]))
        self.assertIn("READY TO MERGE", out)
        self.assert_hint(out, 3, 2)  # AC-1: 5 - 3, 5, 5 + 3; AC-2
        code, waited = run_cli(["wait", "--timeout", "1s"])
        self.assertEqual(code, EXIT_OK, waited)
        self.assertIn("hint:", waited)  # AC-2: the stage message, as `wait` prints it
        state = self.state()  # AC-6: the outcome of an approved, validated second round
        self.assertEqual(state.stage, "ready_to_merge")
        self.assertEqual((len(state.rounds), state.fixes), (2, 1))
        self.assertFalse(state.fix_due)
        self.assertEqual(state.validated_sha, git(["rev-parse", "HEAD"], self.repo).strip())
        self.assertNotIn("hint:", json.dumps(self.fake_calls("gh")))  # not sent to the PR

    def test_four_lines_below_the_fix(self):
        out = self.two_rounds(answer([finding("F2", 5), finding("F3", 9)]))
        self.assertIn("READY TO MERGE", out)
        self.assert_no_hint(out)  # AC-1: one finding past the margin

    def test_four_lines_above_the_fix(self):
        out = self.two_rounds(answer([finding("F2", 1)]))
        self.assertIn("READY TO MERGE", out)
        self.assert_no_hint(out)  # AC-1

    def test_line_zero(self):
        out = self.two_rounds(answer([finding("F2", 5), finding("F3", 0)]))
        self.assert_no_hint(out)  # AC-1

    def test_no_file(self):
        out = self.two_rounds(answer([finding("F2", 5, file="")]))
        self.assert_no_hint(out)  # AC-1

    def test_file_the_fix_did_not_touch(self):
        out = self.two_rounds(answer([finding("F2", 5, file="tests/test_calc.py")]))
        self.assert_no_hint(out)  # AC-1

    def test_the_diff_starts_at_the_reviewers_test_commit(self):
        # round 1 committed tests/test_review_mul.py after the head it reviewed: measured from
        # that head the whole file would be new lines, measured from the test commit it is not
        out = self.two_rounds(answer([finding("F2", 5, file="tests/test_review_mul.py")]))
        self.assert_no_hint(out)  # AC-1

    def test_unresolved_previous_finding(self):
        previous = [{"id": "F1", "status": "unresolved", "note": "still wrong"}]
        out = self.two_rounds(answer([finding("F2", 5)], previous=previous))
        self.assert_no_hint(out)  # AC-1

    def test_medium_finding_in_the_fix(self):
        out = self.two_rounds(answer([finding("F2", 5), finding("F3", 5, sev="medium")]))
        self.assertIn("READY TO MERGE", out)  # a medium convention finding does not block
        self.assert_no_hint(out)  # AC-3

    def test_round_one(self):
        self.claude(claude_entry(approve_response(findings=[finding("F1", 13)])))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        self.assert_no_hint(out)  # AC-3


class ActionNeeded(StopHintCase):
    def test_changes_requested_with_lows_in_the_fix(self):
        second = answer([finding("F2", 5), finding("F3", 6)], verdict="CHANGES_REQUESTED")
        done = [
            {"id": "F2", "status": "resolved", "note": "done"},
            {"id": "F3", "status": "resolved", "note": "done"},
        ]
        third = approve_response(previous_findings=done)
        out = self.two_rounds(second, claude_entry(third, write_tests=False), expect=EXIT_ACTION)
        self.assertIn("ACTION NEEDED", out)
        self.assert_hint(out, 2, 2)  # AC-2
        state = self.state()  # AC-6: a round that asked for changes, as without the hint
        self.assertEqual(state.stage, "needs_action")
        self.assertTrue(state.fix_due)
        self.assertEqual(state.fixes, 1)
        self.assertEqual(state.validated_sha, "")
        code, again = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ACTION, again)
        self.assertIn("nothing changed since the last review", again)
        self.fix_mul()
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("READY TO MERGE", out)
        state = self.state()
        self.assertEqual((len(state.rounds), state.fixes), (3, 2))  # the fix counter went on

    def test_needs_info(self):
        second = answer(
            [finding("F2", 5)], verdict="NEEDS_INFO", questions=["Are floats in scope?"], tests=[]
        )
        out = self.two_rounds(second, expect=EXIT_ACTION)
        self.assertIn("Are floats in scope?", out)
        self.assert_no_hint(out)  # AC-3

    def test_failed_validation(self):
        self.runner_scenario(
            {
                "default": 0,
                "results": {"validate-r2": {"new_test": 1}},
                "outputs": {"validate-r2": {"new_test": "AssertionError: 12 != 7"}},
            }
        )
        out = self.two_rounds(
            answer([finding("F2", 5)]),
            claude_entry(diagnosis(), write_tests=False, model="claude-opus-5", cost=0.2),
            expect=EXIT_ACTION,
        )
        self.assertIn("FAILED", out)
        self.assert_no_hint(out)  # AC-3


class ThirdRound(StopHintCase):
    """AC-1: round 3 is measured against round 2's tip (the head round 2 reviewed, since it
    committed no tests), not against round 1's."""

    def three_rounds(self, findings):
        blocker = finding("F2", 13, sev="high", kind="correctness")
        second = answer([blocker], verdict="CHANGES_REQUESTED")
        third = approve_response(
            findings=findings,
            previous_findings=[{"id": "F2", "status": "resolved", "note": "fixed"}],
        )
        out = self.two_rounds(second, claude_entry(third, write_tests=False), expect=EXIT_ACTION)
        self.assertEqual(hint_lines(out), [], out)  # AC-3: round 2 has a high finding
        self.fix_mul()  # line 13
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        return out

    def test_low_in_the_last_fix(self):
        out = self.three_rounds([finding("F3", 13)])
        self.assert_hint(out, 1, 3)

    def test_low_in_the_fix_before_the_last(self):
        out = self.three_rounds([finding("F3", 5)])  # what round 1's fix changed
        self.assert_no_hint(out)


class PreviousTipGone(StopHintCase):
    def test_not_an_ancestor_gives_no_hint_and_one_log_line(self):
        self.claude(
            claude_entry(round_one()),
            claude_entry(answer([finding("F2", 5)]), write_tests=False),
        )
        self.first_round()
        # what a rewritten history leaves behind: the recorded tip of round 1 is a commit
        # that is not an ancestor of the head round 2 reviews
        stray = git(["commit-tree", "HEAD^{tree}", "-m", "not on the branch"], self.repo).strip()
        state = self.state()
        state.rounds[-1]["test_commit"] = stray
        state.save(self.rdir())
        self.fix_add()
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("READY TO MERGE", out)
        self.assert_no_hint(out)  # AC-1
        path = os.path.join(self.rdir(), "logs", "revali.log")
        with open(path, "r", encoding="utf-8", newline="") as fh:
            said = [line for line in fh.read().splitlines() if "stop hint" in line]
        self.assertEqual(len(said), 1, said)  # AC-1: one line saying why


if __name__ == "__main__":
    unittest.main()
