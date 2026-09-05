"""Acceptance tests for AC-5..AC-7 of fix/test-guard-exit2: every exit 2 that follows a
review round names the findings that did not block (and where the full review is), the
needs_action stage message carries the counts, and the other exits are unchanged."""

import os
import unittest

from revali import EXIT_ACTION, EXIT_ERROR, EXIT_HUMAN, EXIT_OK
from revali.state import State
from tests.helpers import RepoCase, approve_response, claude_entry, run_cli


def finding(fid, sev, kind, line):
    return {
        "id": fid,
        "file": "src/calc.py",
        "line": line,
        "severity": sev,
        "kind": kind,
        "text": "see line %d" % line,
        "suggestion": "",
    }


F1 = finding("F1", "high", "correctness", 3)
F2 = finding("F2", "low", "correctness", 5)
F3 = finding("F3", "medium", "convention", 8)
F4 = finding("F4", "low", "security", 11)
DIAGNOSIS = {
    "summary": "mul returns the wrong product.",
    "cause": "code",
    "failures": [
        {
            "test": "tests/test_review_mul.py::MulTests::test_product",
            "cause": "code",
            "note": "expected 12, got 7",
        }
    ],
    "recommendation": "return a * b",
}


def action_message(out):
    """The ACTION NEEDED text: from its first line to the end of the output."""
    idx = out.find("ACTION NEEDED")
    return out[idx:] if idx >= 0 else ""


def line_with(text, token):
    return next((line for line in text.splitlines() if token in line), "")


class ChangesRequested(RepoCase):
    def test_non_blocking_findings_listed_once_each(self):
        self.claude(
            claude_entry(approve_response(verdict="CHANGES_REQUESTED", findings=[F1, F2, F3, F4]))
        )
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ACTION, out)
        msg = action_message(out)
        self.assertIn("3 non-blocking", msg)  # AC-5: count
        for fid, sev, kind, line in (
            ("F2", "low", "correctness", 5),
            ("F3", "medium", "convention", 8),
            ("F4", "low", "security", 11),
        ):
            entry = line_with(msg, fid)
            self.assertTrue(entry, "%s missing from:\n%s" % (fid, msg))
            self.assertIn(sev, entry)
            self.assertIn(kind, entry)
            self.assertIn("src/calc.py:%d" % line, entry)
        self.assertEqual(msg.count("F1"), 1)  # the blocking one stays in its own list
        self.assertIn(os.path.join(self.rdir(), "review-1.md"), msg)
        state = State.load(self.rdir())
        self.assertEqual(state.stage, "needs_action")
        self.assertIn("1 blocking", state.message)  # AC-6
        self.assertIn("3 non-blocking", state.message)
        self.assertIn("review-1.md", state.message)
        code, out = run_cli(["wait", "--timeout", "1s"])
        self.assertEqual(code, EXIT_ACTION)
        self.assertIn("3 non-blocking", out)
        self.assertIn("review-1.md", out)

    def test_no_list_when_every_finding_blocks(self):
        self.claude(claude_entry(approve_response(verdict="CHANGES_REQUESTED", findings=[F1])))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ACTION, out)
        msg = action_message(out)
        self.assertEqual(msg.count("src/calc.py:"), 1)  # AC-5: list absent
        self.assertEqual(msg.count("F1"), 1)
        message = State.load(self.rdir()).message
        self.assertIn("1 blocking", message)  # AC-6
        self.assertIn("0 non-blocking", message)


class NeedsInfo(RepoCase):
    def test_questions_then_non_blocking_findings(self):
        data = approve_response(
            verdict="NEEDS_INFO", questions=["Are floats in scope?"], tests=[], findings=[F3, F2]
        )
        self.claude(claude_entry(data, write_tests=False))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ACTION, out)
        msg = action_message(out)
        self.assertIn("Are floats in scope?", msg)
        self.assertIn("2 non-blocking", msg)  # AC-5
        self.assertIn("src/calc.py:8", line_with(msg, "F3"))
        self.assertIn("src/calc.py:5", line_with(msg, "F2"))
        self.assertIn(os.path.join(self.rdir(), "review-1.md"), msg)
        message = State.load(self.rdir()).message
        self.assertIn("0 blocking", message)  # AC-6
        self.assertIn("2 non-blocking", message)
        self.assertIn("review-1.md", message)


class ValidationFail(RepoCase):
    def test_findings_of_the_approving_round_not_of_the_earlier_one(self):
        self.runner_scenario(
            {
                "default": 0,
                "results": {"validate-r2": {"new_test": 1}},
                "outputs": {"validate-r2": {"new_test": "AssertionError: 12 != 7"}},
            }
        )
        self.claude(claude_entry(approve_response(verdict="CHANGES_REQUESTED", findings=[F1, F2])))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ACTION, out)
        self.write("src/calc.py", self.read("src/calc.py") + "\n# F1 fixed\n")
        self.commit_all("fix F1")
        ok = approve_response(
            previous_findings=[
                {"id": "F1", "status": "resolved", "note": "fixed"},
                {"id": "F2", "status": "resolved", "note": "fixed"},
            ],
            findings=[finding("F5", "low", "convention", 13)],
        )
        self.claude(
            claude_entry(ok, write_tests=False),
            claude_entry(DIAGNOSIS, write_tests=False, model="claude-opus-5", cost=0.2),
        )
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ACTION, out)
        msg = action_message(out)
        self.assertIn("FAILED", msg)
        self.assertIn("1 non-blocking", msg)  # AC-5, FAIL
        self.assertIn("src/calc.py:13", line_with(msg, "F5"))
        self.assertNotIn("F2", msg)  # round 1's findings are not the latest
        self.assertIn(os.path.join(self.rdir(), "review-2.md"), msg)
        message = State.load(self.rdir()).message
        self.assertIn("0 blocking", message)  # AC-6
        self.assertIn("1 non-blocking", message)
        self.assertIn("review-2.md", message)


class OtherExitsUnchanged(RepoCase):
    def test_nothing_changed_rerun(self):
        self.claude(claude_entry(approve_response(verdict="CHANGES_REQUESTED", findings=[F1, F2])))
        run_cli(["run", "--foreground"])
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ACTION, out)  # AC-7
        msg = action_message(out)
        self.assertIn("nothing changed", msg)
        self.assertNotIn("non-blocking", msg)
        self.assertNotIn("F2", msg)
        message = State.load(self.rdir()).message
        self.assertTrue(message.startswith("nothing changed since the last review"), message)
        self.assertNotIn("non-blocking", message)

    def test_ready_to_merge(self):
        self.claude(claude_entry(approve_response(findings=[F2, F3])))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)  # AC-7
        self.assertIn("READY TO MERGE", out)
        self.assertNotIn("non-blocking", out)
        self.assertEqual(State.load(self.rdir()).message, "validation 1 passed")

    def test_pipeline_error(self):
        entry = claude_entry(approve_response(findings=[F2]))
        entry["write_files"]["src/calc.py"] = "def mul(a, b):\n    return a * b\n"
        self.claude(entry)
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR, out)  # AC-7
        self.assertNotIn("non-blocking", out)
        self.assertEqual(
            State.load(self.rdir()).message,
            "the reviewer modified files outside tests (reverted): src/calc.py",
        )

    def test_fix_limit_needs_a_human(self):
        cr = approve_response(verdict="CHANGES_REQUESTED", findings=[F1, F2])
        self.claude(
            claude_entry(cr),
            claude_entry(cr, write_tests=False),
            claude_entry(cr, write_tests=False),
        )
        for n in range(3):
            code, out = run_cli(["run", "--foreground"])
            self.assertEqual(code, EXIT_ACTION, out)
            self.write("src/calc.py", self.read("src/calc.py") + "\n# attempt %d\n" % n)
            self.commit_all("attempt %d" % n)
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_HUMAN, out)  # AC-7
        self.assertNotIn("non-blocking", out)
        message = State.load(self.rdir()).message
        self.assertTrue(message.startswith("3 fix cycles used (limit 2)"), message)
        self.assertNotIn("non-blocking", message)


if __name__ == "__main__":
    unittest.main()
