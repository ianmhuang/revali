"""Every exit 2 names the findings that did not block, so none is missed
(AC-5..AC-7 of fix/test-guard-exit2)."""

import unittest

from revali import EXIT_ACTION, EXIT_OK
from revali.review import non_blocking_findings
from revali.state import State
from tests.helpers import RepoCase, approve_response, claude_entry, run_cli
from tests.test_validate import diagnosis


def finding(fid, sev, kind, line=3):
    return {
        "id": fid,
        "file": "src/calc.py",
        "line": line,
        "severity": sev,
        "kind": kind,
        "text": "%s text" % fid,
        "suggestion": "",
    }


F_HIGH = finding("F1", "high", "correctness")
F_LOW = finding("F2", "low", "convention", line=7)
F_MEDCONV = finding("F3", "medium", "convention", line=9)


class SplitTests(unittest.TestCase):
    def test_non_blocking_split(self):
        data = approve_response(
            findings=[
                F_HIGH,
                F_LOW,
                F_MEDCONV,
                finding("F4", "medium", "security"),
                finding("F5", "high", "convention"),
            ]
        )
        self.assertEqual([f["id"] for f in non_blocking_findings(data)], ["F2", "F3"])


class ChangesRequestedTests(RepoCase):
    def test_non_blocking_listed(self):
        cr = approve_response(verdict="CHANGES_REQUESTED", findings=[F_HIGH, F_LOW, F_MEDCONV])
        self.claude(claude_entry(cr))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ACTION, out)
        self.assertIn("2 non-blocking finding(s)", out)  # AC-5
        self.assertIn("F2 [low convention] src/calc.py:7 F2 text", out)
        self.assertIn("F3 [medium convention] src/calc.py:9", out)
        self.assertIn("review-1.md", out)
        message = State.load(self.rdir()).message
        self.assertIn("1 blocking, 2 non-blocking", message)  # AC-6
        self.assertIn("review-1.md", message)
        code, out = run_cli(["wait", "--timeout", "1s"])
        self.assertEqual(code, EXIT_ACTION)
        self.assertIn("2 non-blocking", out)

    def test_only_blocking_findings_no_list(self):
        self.claude(claude_entry(approve_response(verdict="CHANGES_REQUESTED", findings=[F_HIGH])))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ACTION, out)
        self.assertNotIn("non-blocking finding(s)", out)
        self.assertIn("1 blocking, 0 non-blocking", State.load(self.rdir()).message)

    def test_nothing_changed_message_unchanged(self):
        self.claude(
            claude_entry(approve_response(verdict="CHANGES_REQUESTED", findings=[F_HIGH, F_LOW]))
        )
        run_cli(["run", "--foreground"])
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ACTION, out)  # AC-7
        self.assertIn("nothing changed", out)
        self.assertNotIn("non-blocking", out)
        self.assertNotIn("non-blocking", State.load(self.rdir()).message)


class NeedsInfoTests(RepoCase):
    def test_non_blocking_listed_with_questions(self):
        q = approve_response(
            verdict="NEEDS_INFO",
            questions=["Should mul accept floats?"],
            tests=[],
            findings=[F_LOW],
        )
        self.claude(claude_entry(q, write_tests=False))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ACTION, out)
        self.assertIn("accept floats", out)
        self.assertIn("1 non-blocking finding(s)", out)  # AC-5
        self.assertIn("F2 [low convention]", out)
        self.assertIn("0 blocking, 1 non-blocking", State.load(self.rdir()).message)  # AC-6


class ValidationFailTests(RepoCase):
    def test_findings_of_the_approving_round_listed(self):
        self.runner_scenario(
            {
                "default": 0,
                "results": {"validate-r1": {"new_test": 1}},
                "outputs": {"validate-r1": {"new_test": "AssertionError: 12 != 7"}},
            }
        )
        self.claude(
            claude_entry(approve_response(findings=[F_LOW])),
            claude_entry(diagnosis(), write_tests=False, model="claude-opus-5", cost=0.2),
        )
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ACTION, out)
        self.assertIn("validation 1 FAILED", out)
        self.assertIn("1 non-blocking finding(s)", out)  # AC-5, FAIL
        self.assertIn("F2 [low convention]", out)
        self.assertIn("review-1.md", out)
        message = State.load(self.rdir()).message
        self.assertIn("validation 1 failed", message)
        self.assertIn("0 blocking, 1 non-blocking", message)  # AC-6

    def test_ready_to_merge_unchanged(self):
        self.claude(claude_entry(approve_response(findings=[F_LOW])))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)  # AC-7
        self.assertIn("READY TO MERGE", out)
        self.assertNotIn("non-blocking", out)
        self.assertEqual(State.load(self.rdir()).message, "validation 1 passed")


if __name__ == "__main__":
    unittest.main()
