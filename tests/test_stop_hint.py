"""The stop hint after a round whose low findings all sit in the last fix, and the cost per
round (feature/stop-hint)."""

import unittest

from revali import EXIT_ACTION, EXIT_HUMAN, EXIT_OK, gitops, stophint
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
    def two_rounds(self, answer, *later, expect=EXIT_OK, first=None):
        """Round 1 answers `first` (asks for changes on mul by default), the author appends
        two lines to src/calc.py (lines 14 and 15), round 2 answers `answer`."""
        self.claude(first or claude_entry(CR), claude_entry(answer, write_tests=False), *later)
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ACTION, out)
        self.write("src/calc.py", self.read("src/calc.py") + "\n# handles negatives\n")
        self.commit_all("handle negatives")
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, expect, out)
        return out

    def message(self):
        return State.load(self.rdir()).message

    def head(self):
        return git(["rev-parse", "HEAD"], self.repo).strip()


class ReadyHint(HintCase):
    def test_low_in_the_fix_gets_the_hint(self):
        out = self.two_rounds(second([finding("F2", 15)]))
        self.assertIn("READY TO MERGE", out)  # AC-6: the outcome is the same
        self.assertIn(HINT % (1, 2), out)  # AC-2
        self.assertIn("response-2.md", out)
        self.assertIn("instead of another round", out)
        self.assertIn(SHORT % 1, self.message())  # AC-2: what wait and status print
        self.assertIn("consider stopping", self.message())
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
        # PR round 1 follow-up 3: another round runs after CHANGES_REQUESTED anyway
        self.assertIn("rather than changing those lines again; the next round still runs", out)
        self.assertNotIn("instead of another round", out)
        self.assertIn(SHORT % 1, self.message())
        self.assertNotIn("consider stopping", self.message())
        state = State.load(self.rdir())
        self.assertEqual(state.stage, "needs_action")  # AC-6
        self.assertTrue(state.fix_due)

    def test_the_last_fix_cycle_names_the_stop(self):
        """PR round 2 F4: with max_fixes 1, round 2 used the last cycle; the next run stops
        with exit 3 instead of reviewing, and the hint says so."""
        self.write(
            "revali.toml", self.read("revali.toml").replace("max_fixes = 2", "max_fixes = 1")
        )
        self.commit_all("limit")
        answer = second([finding("F2", 15)], verdict="CHANGES_REQUESTED")
        out = self.two_rounds(answer, expect=EXIT_ACTION)
        self.assertIn(HINT % (1, 2), out)
        self.assertIn("this was the last fix cycle, so the next run stops for a human", out)
        self.assertNotIn("the next round still runs", out)
        git(["commit", "-q", "--allow-empty", "-m", "answer round 2"], self.repo)
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_HUMAN, out)  # what the hint said
        self.assertIn("a human decides", out)

    def test_answering_only_reaches_the_next_round(self):
        """What skill/SKILL.md tells the author after a CHANGES_REQUESTED hint: answer in
        response-2.md, commit nothing but an empty commit, run again."""
        answer = second([finding("F2", 15)], verdict="CHANGES_REQUESTED")
        third = second([], previous=[{"id": "F2", "status": "accepted", "note": "ok"}])
        self.two_rounds(answer, claude_entry(third, write_tests=False), expect=EXIT_ACTION)
        self.write(".revali/feature__mul/response-2.md", "- F2: wontfix: cosmetic\n")
        git(["commit", "-q", "--allow-empty", "-m", "answer round 2"], self.repo)
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        state = State.load(self.rdir())
        self.assertEqual(len(state.rounds), 3)
        self.assertEqual(state.fixes, 2)

    def test_needs_info_gets_no_hint(self):
        answer = second(
            [finding("F2", 15)], verdict="NEEDS_INFO", questions=["Floats too?"], tests=[]
        )
        out = self.two_rounds(answer, expect=EXIT_ACTION)
        self.assertIn("Floats too?", out)
        self.assertNotIn("hint:", out)  # AC-3
        self.assertNotIn("hint:", self.message())

    def test_changes_requested_for_another_reason_gets_no_hint(self):
        """PR round 1 F1: an unjustified test change blocks the round, which no wontfix on
        the lows answers."""
        change = [{"file": "tests/test_calc.py", "justified": False, "reason": "weakened"}]
        answer = second([finding("F2", 15)], verdict="CHANGES_REQUESTED", test_changes=change)
        out = self.two_rounds(answer, expect=EXIT_ACTION)
        self.assertIn("changed without justification", out)
        self.assertNotIn("hint:", out)  # AC-2: only the reviewer's own request gets it
        self.assertNotIn("hint:", self.message())

    def test_an_approve_blocked_by_an_ac_gap_gets_no_hint(self):
        answer = second([finding("F2", 15)], tests=[], not_testable=[])
        out = self.two_rounds(answer, expect=EXIT_ACTION)
        self.assertIn("acceptance criteria without a test", out)
        self.assertNotIn("hint:", out)  # AC-2
        self.assertNotIn("hint:", self.message())

    def test_a_second_needs_info_gets_no_hint(self):
        """PR round 1 F1: revali turns a second NEEDS_INFO into CHANGES_REQUESTED; AC-3."""
        q = approve_response(verdict="NEEDS_INFO", questions=["Floats too?"], tests=[])
        answer = second(
            [finding("F2", 15)], previous=[], verdict="NEEDS_INFO", questions=["Ints?"], tests=[]
        )
        out = self.two_rounds(answer, expect=EXIT_ACTION, first=claude_entry(q, write_tests=False))
        self.assertIn("unanswered", out)
        self.assertNotIn("hint:", out)
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


class ChangedLineRanges(HintCase):
    def test_an_added_line_that_reads_like_a_header(self):
        """PR round 1 F2: with -U0 an added `++ x` line prints as `+++ x`; the hunks after
        it stay under the file's own name."""
        old = self.head()
        lines = self.read("src/calc.py").split("\n")
        lines.insert(1, "++ /dev/null")
        lines.insert(2, "++ b/")
        self.write("src/calc.py", "\n".join(lines) + "\n# end\n")
        self.commit_all("plus lines")
        ranges = gitops.changed_line_ranges(old, self.head(), self.repo)
        self.assertEqual(set(ranges), {"src/calc.py"})
        self.assertEqual(len(ranges["src/calc.py"]), 2, ranges)


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
