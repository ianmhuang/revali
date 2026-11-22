"""feature/stop-hint: READY TO MERGE lists the approving round's non-blocking findings
(AC-4), and READY TO MERGE, `status` and the merge summary print the cost of each round
(AC-5)."""

import unittest

from revali import EXIT_ACTION, EXIT_OK
from revali.merge import merge_summary
from revali.state import State
from tests.helpers import RepoCase, approve_response, claude_entry, run_cli
from tests.test_validate import diagnosis


def finding(fid, line, sev, text):
    return {
        "id": fid,
        "file": "src/calc.py",
        "line": line,
        "severity": sev,
        "kind": "convention",
        "text": text,
        "suggestion": "",
    }


def ready_message(out):
    return out[out.index("READY TO MERGE") :]


class ReadyListsFindings(RepoCase):
    def test_non_blocking_findings_are_listed(self):
        findings = [
            finding("F1", 7, "low", "name the helper after what it returns"),
            finding("F2", 12, "medium", "say which rounding mul uses"),
        ]
        self.claude(claude_entry(approve_response(findings=findings)))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        ready = ready_message(out)
        for f in findings:  # AC-4: id, severity, kind, file:line, text
            rows = [row for row in ready.splitlines() if f["id"] in row and f["text"] in row]
            self.assertEqual(len(rows), 1, ready)
            self.assertIn(f["severity"], rows[0])
            self.assertIn("convention", rows[0])
            self.assertIn("src/calc.py:%d" % f["line"], rows[0])
        self.assertIn("review-1.md", ready)

    def test_nothing_listed_without_findings(self):
        self.claude(claude_entry())
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        ready = ready_message(out)
        self.assertNotIn("non-blocking", ready)
        self.assertIn("cost per round: r1 $0.50", ready)  # AC-5: one round


class CostPerRound(RepoCase):
    def status(self):
        return run_cli(["status"])[1]

    def test_ready_status_and_merge_summary(self):
        blocker = finding("F1", 13, "high", "mul adds")
        blocker["kind"] = "correctness"
        asked = approve_response(verdict="CHANGES_REQUESTED", findings=[blocker])
        resolved = [{"id": "F1", "status": "resolved", "note": "fixed"}]
        self.claude(
            claude_entry(asked, cost=1.25),
            claude_entry(
                approve_response(previous_findings=resolved), write_tests=False, cost=0.75
            ),
        )
        self.assertNotIn("cost per round", self.status())  # AC-5: no line before a round

        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ACTION, out)
        self.assertIn("cost per round: r1 $1.25", self.status().splitlines())

        self.write("src/calc.py", self.read("src/calc.py") + "\n# also for negatives\n")
        self.commit_all("say what mul covers")
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        line = "cost per round: r1 $1.25, r2 $0.75"
        ready = ready_message(out)
        self.assertIn(line, [row.strip() for row in ready.splitlines()])
        self.assertIn(line, self.status().splitlines())
        summary = merge_summary(State.load(self.rdir()), "main")
        self.assertIn(line, [row.strip() for row in summary.splitlines()])

    def test_diagnosis_follows_the_rounds(self):
        self.runner_scenario(
            {
                "default": 0,
                "results": {"validate-r1": {"new_test": 1}},
                "outputs": {"validate-r1": {"new_test": "AssertionError: 12 != 7"}},
            }
        )
        self.claude(
            claude_entry(),
            claude_entry(diagnosis(), write_tests=False, model="claude-opus-5", cost=0.2),
        )
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ACTION, out)
        self.assertIn("cost per round: r1 $0.50; diagnosis $0.20", self.status().splitlines())

    def test_other_is_what_no_round_or_diagnosis_holds(self):
        self.claude(claude_entry())
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("cost per round: r1 $0.50", self.status().splitlines())
        # a reviewer session that ended without a round adds to the run's total only
        state = State.load(self.rdir())
        state.cost_usd += 0.6
        state.save(self.rdir())
        self.assertIn("cost per round: r1 $0.50; other $0.60", self.status().splitlines())


if __name__ == "__main__":
    unittest.main()
