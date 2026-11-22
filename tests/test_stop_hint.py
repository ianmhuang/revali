"""The stop hint after a round whose low findings all sit in the last fix, and the cost per
round (feature/stop-hint)."""

import os
import unittest

from revali import EXIT_ACTION, EXIT_OK, stophint
from revali.merge import merge_summary
from revali.state import State, cost_per_round
from tests.helpers import RepoCase, approve_response, claude_entry, git, run_cli
from tests.test_validate import diagnosis

HINT = "hint: the %d low finding(s) of round %d all sit in lines the last fix changed"
SHORT = "hint: %d low finding(s) in lines the last fix changed"


def finding(fid, line, sev="low", kind="convention", file="src/calc.py"):
    return {
        "id": fid,
        "file": file,
        "line": line,
        "severity": sev,
        "kind": kind,
        "text": "%s text" % fid,
        "suggestion": "",
    }


F_HIGH = finding("F1", 13, sev="high", kind="correctness")
CR = approve_response(verdict="CHANGES_REQUESTED", findings=[F_HIGH])
RESOLVED = [{"id": "F1", "status": "resolved", "note": "fixed"}]


def second(findings, previous=RESOLVED, **kw):
    return approve_response(findings=findings, previous_findings=previous, **kw)


class HintCase(RepoCase):
    def two_rounds(self, answer, *later, expect=EXIT_OK):
        """Round 1 asks for changes on mul, the author appends two lines to src/calc.py
        (lines 14 and 15), round 2 answers `answer`."""
        self.claude(claude_entry(CR), claude_entry(answer, write_tests=False), *later)
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ACTION, out)
        self.write("src/calc.py", self.read("src/calc.py") + "\n# handles negatives\n")
        self.commit_all("handle negatives")
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, expect, out)
        return out

    def message(self):
        return State.load(self.rdir()).message

    def revali_log(self):
        with open(os.path.join(self.rdir(), "logs", "revali.log"), encoding="utf-8") as fh:
            return fh.read()


class ReadyHint(HintCase):
    def test_low_in_the_fix_gets_the_hint(self):
        out = self.two_rounds(second([finding("F2", 15)]))
        self.assertIn("READY TO MERGE", out)  # AC-6: the outcome is the same
        self.assertIn(HINT % (1, 2), out)  # AC-2
        self.assertIn("response-2.md", out)
        self.assertIn(SHORT % 1, self.message())  # AC-2: what wait and status print
        code, wait_out = run_cli(["wait", "--timeout", "1s"])
        self.assertEqual(code, EXIT_OK)
        self.assertIn(SHORT % 1, wait_out)
        self.assertEqual(State.load(self.rdir()).stage, "ready_to_merge")

    def test_within_three_lines_of_the_fix(self):
        out = self.two_rounds(second([finding("F2", 11), finding("F3", 18)]))
        self.assertIn(HINT % (2, 2), out)  # AC-1: 14 - 3 and 15 + 3

    def test_low_outside_the_fix_gets_no_hint(self):
        out = self.two_rounds(second([finding("F2", 15), finding("F3", 5)]))
        self.assertIn("READY TO MERGE", out)
        self.assertNotIn("hint:", out)  # AC-1: one finding outside
        self.assertNotIn("hint:", self.message())

    def test_line_zero_gets_no_hint(self):
        out = self.two_rounds(second([finding("F2", 0)]))
        self.assertNotIn("hint:", out)  # AC-1

    def test_no_file_gets_no_hint(self):
        out = self.two_rounds(second([finding("F2", 15, file="")]))
        self.assertNotIn("hint:", out)  # AC-1

    def test_a_file_the_fix_did_not_touch_gets_no_hint(self):
        out = self.two_rounds(second([finding("F2", 3, file="tests/test_calc.py")]))
        self.assertNotIn("hint:", out)  # AC-1

    def test_the_reviewers_own_test_commit_is_not_the_fix(self):
        out = self.two_rounds(second([finding("F2", 5, file="tests/test_review_mul.py")]))
        self.assertNotIn("hint:", out)  # AC-1: the diff starts at the round 1 test commit

    def test_an_unresolved_previous_finding_gets_no_hint(self):
        unresolved = [{"id": "F1", "status": "unresolved", "note": "still there"}]
        out = self.two_rounds(second([finding("F2", 15)], previous=unresolved))
        self.assertNotIn("hint:", out)  # AC-1

    def test_a_finding_other_than_low_gets_no_hint(self):
        out = self.two_rounds(second([finding("F2", 15), finding("F3", 15, sev="medium")]))
        self.assertIn("READY TO MERGE", out)
        self.assertNotIn("hint:", out)  # AC-3: a medium convention does not block

    def test_round_one_gets_no_hint(self):
        self.claude(claude_entry(approve_response(findings=[finding("F1", 13)])))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertNotIn("hint:", out)  # AC-3
        self.assertEqual(self.message(), "validation 1 passed")


class ReadyListsFindings(HintCase):
    def test_ready_lists_the_non_blocking_findings(self):
        self.claude(claude_entry(approve_response(findings=[finding("F1", 7)])))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("1 non-blocking finding(s)", out)  # AC-4
        self.assertIn("F1 [low convention] src/calc.py:7 F1 text", out)
        self.assertIn("review-1.md", out)

    def test_ready_without_findings_lists_nothing(self):
        self.claude(claude_entry())
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertNotIn("non-blocking", out)


class ActionHint(HintCase):
    def test_changes_requested_with_lows_in_the_fix(self):
        answer = second([finding("F2", 15)], verdict="CHANGES_REQUESTED")
        out = self.two_rounds(answer, expect=EXIT_ACTION)
        self.assertIn("ACTION NEEDED: changes requested (round 2", out)
        self.assertIn(HINT % (1, 2), out)  # AC-2
        self.assertIn("response-2.md", out)
        self.assertIn(SHORT % 1, self.message())
        state = State.load(self.rdir())
        self.assertEqual(state.stage, "needs_action")  # AC-6
        self.assertTrue(state.fix_due)

    def test_needs_info_gets_no_hint(self):
        answer = second(
            [finding("F2", 15)], verdict="NEEDS_INFO", questions=["Floats too?"], tests=[]
        )
        out = self.two_rounds(answer, expect=EXIT_ACTION)
        self.assertIn("Floats too?", out)
        self.assertNotIn("hint:", out)  # AC-3
        self.assertNotIn("hint:", self.message())

    def test_failed_validation_gets_no_hint(self):
        self.runner_scenario(
            {
                "default": 0,
                "results": {"validate-r2": {"new_test": 1}},
                "outputs": {"validate-r2": {"new_test": "AssertionError: 12 != 7"}},
            }
        )
        out = self.two_rounds(
            second([finding("F2", 15)]),
            claude_entry(diagnosis(), write_tests=False, model="claude-opus-5", cost=0.2),
            expect=EXIT_ACTION,
        )
        self.assertIn("FAILED", out)
        self.assertNotIn("hint:", out)  # AC-3
        self.assertNotIn("hint:", self.message())


class DerivedLows(HintCase):
    """AC-1 on stophint.derived_lows directly: a rebase and a git failure."""

    def state_with(self, prev_tip, head, findings):
        state = State()
        state.rounds = [
            {"round": 1, "head_sha": prev_tip, "test_commit": "", "data": {"findings": []}},
            {
                "round": 2,
                "head_sha": head,
                "test_commit": "",
                "data": {"findings": findings, "previous_findings": []},
            },
        ]
        return state

    def head(self):
        return git(["rev-parse", "HEAD"], self.repo).strip()

    def test_in_the_fix(self):
        old = self.head()
        self.write("src/calc.py", self.read("src/calc.py") + "\n# fix\n")
        self.commit_all("fix")
        notes = []
        got = stophint.derived_lows(
            self.state_with(old, self.head(), [finding("F2", 15)]), self.repo, notes.append
        )
        self.assertEqual([f["id"] for f in got], ["F2"])
        self.assertEqual(notes, [])

    def test_not_an_ancestor(self):
        git(["checkout", "-q", "-b", "side"], self.repo)
        self.write("src/calc.py", self.read("src/calc.py") + "\n# side\n")
        self.commit_all("side")
        side = self.head()
        git(["checkout", "-q", "feature/mul"], self.repo)
        self.write("src/calc.py", self.read("src/calc.py") + "\n# fix\n")
        self.commit_all("fix")
        notes = []
        got = stophint.derived_lows(
            self.state_with(side, self.head(), [finding("F2", 15)]), self.repo, notes.append
        )
        self.assertEqual(got, [])
        self.assertEqual(len(notes), 1)
        self.assertIn("not an ancestor", notes[0])

    def test_git_fails(self):
        notes = []
        got = stophint.derived_lows(
            self.state_with("0" * 40, self.head(), [finding("F2", 15)]), self.repo, notes.append
        )
        self.assertEqual(got, [])
        self.assertEqual(len(notes), 1)
        self.assertIn("no stop hint", notes[0])

    def test_a_deleted_block_counts_where_it_was(self):
        old = self.head()
        text = self.read("src/calc.py")
        self.write("src/calc.py", text.replace("def sub(a, b):\n    return a - b\n", ""))
        self.commit_all("drop sub")
        got = stophint.derived_lows(
            self.state_with(old, self.head(), [finding("F2", 8)]), self.repo, None
        )
        self.assertEqual([f["id"] for f in got], ["F2"])


class CostPerRound(HintCase):
    def test_ready_status_and_merge_print_it(self):
        out = self.two_rounds(second([]))
        self.assertIn("cost per round: r1 $0.50, r2 $0.50", out)  # AC-5
        self.assertNotIn("diagnosis $", out)
        code, status = run_cli(["status"])
        self.assertEqual(code, EXIT_OK)
        self.assertIn("cost per round: r1 $0.50, r2 $0.50", status)
        self.assertIn(
            "cost per round: r1 $0.50, r2 $0.50", merge_summary(State.load(self.rdir()), "main")
        )

    def test_diagnosis_and_other(self):
        state = State()
        state.rounds = [{"cost_usd": 1.25}, {"cost_usd": 0.75}]
        state.validations = [{"cost_usd": 0.4}, {"cost_usd": 0.0}]
        state.cost_usd = 3.0
        self.assertEqual(
            cost_per_round(state),
            "cost per round: r1 $1.25, r2 $0.75; diagnosis $0.40; other $0.60",
        )
        state.cost_usd = 2.4
        self.assertEqual(
            cost_per_round(state), "cost per round: r1 $1.25, r2 $0.75; diagnosis $0.40"
        )

    def test_no_round_no_line(self):
        state = State()
        state.cost_usd = 0.3
        self.assertEqual(cost_per_round(state), "")
        self.assertNotIn("cost per round", merge_summary(state, "main"))


if __name__ == "__main__":
    unittest.main()
