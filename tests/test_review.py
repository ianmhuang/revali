import json
import os
import unittest

from tests.helpers import RepoCase, ROOT, approve_response
from revali.preflight import preflight
from revali.review import (APPROVE, CHANGES_REQUESTED, NEEDS_INFO, ac_gaps, assemble_checklist,
                           build_prompt, compute_verdict,
                           render_review_md, validate_shape)
from revali.state import State


def finding(sev, kind, fid="F1"):
    return {"id": fid, "file": "src/calc.py", "line": 3, "severity": sev, "kind": kind, "text": "t", "suggestion": ""}


class VerdictTests(unittest.TestCase):
    def test_clean_approve(self):
        self.assertEqual(compute_verdict(approve_response(), [], True), (APPROVE, []))

    def test_high_correctness_blocks(self):
        v, reasons = compute_verdict(approve_response(findings=[finding("high", "correctness")]), [], True)
        self.assertEqual(v, CHANGES_REQUESTED)
        self.assertIn("F1", reasons[0])

    def test_medium_correctness_blocks_medium_convention_does_not(self):
        v, _ = compute_verdict(approve_response(findings=[finding("medium", "correctness")]), [], True)
        self.assertEqual(v, CHANGES_REQUESTED)
        v, _ = compute_verdict(approve_response(findings=[finding("medium", "convention")]), [], True)
        self.assertEqual(v, APPROVE)
        v, _ = compute_verdict(approve_response(findings=[finding("high", "convention")]), [], True)
        self.assertEqual(v, CHANGES_REQUESTED)

    def test_low_never_blocks(self):
        v, _ = compute_verdict(approve_response(findings=[finding("low", "security")]), [], True)
        self.assertEqual(v, APPROVE)

    def test_unjustified_test_change_blocks(self):
        data = approve_response(test_changes=[{"file": "tests/test_calc.py", "justified": False, "reason": "assert removed"}])
        v, reasons = compute_verdict(data, [], True)
        self.assertEqual(v, CHANGES_REQUESTED)
        self.assertIn("without justification", reasons[0])

    def test_unjustified_dependency_blocks(self):
        data = approve_response(dependencies_changed=[{"file": "requirements.txt", "justified": False, "reason": "no reason given"}])
        self.assertEqual(compute_verdict(data, [], True)[0], CHANGES_REQUESTED)

    def test_gaps_block(self):
        v, reasons = compute_verdict(approve_response(), ["AC-2"], True)
        self.assertEqual(v, CHANGES_REQUESTED)
        self.assertIn("AC-2", reasons[0])

    def test_needs_info_once(self):
        data = approve_response(verdict=NEEDS_INFO, questions=["Which rounding?"])
        v, reasons = compute_verdict(data, [], True)
        self.assertEqual(v, NEEDS_INFO)
        self.assertIn("Which rounding?", reasons[0])
        v, reasons = compute_verdict(data, [], False)
        self.assertEqual(v, CHANGES_REQUESTED)
        self.assertIn("unanswered", reasons[0])

    def test_reviewer_stricter_verdict_respected(self):
        data = approve_response(verdict=CHANGES_REQUESTED, findings=[finding("low", "convention")])
        v, reasons = compute_verdict(data, [], True)
        self.assertEqual(v, CHANGES_REQUESTED)
        self.assertIn("reviewer requested changes", reasons[0])


class CoverageAndShapeTests(unittest.TestCase):
    def test_ac_gaps(self):
        self.assertEqual(ac_gaps(approve_response(), ["AC-1", "AC-2", "AC-3"]), ["AC-3"])
        data = approve_response(not_testable=[{"ac": "AC-3", "reason": "needs hardware"}])
        self.assertEqual(ac_gaps(data, ["AC-1", "AC-2", "AC-3"]), [])
        data = approve_response(not_testable=[{"ac": "AC-3", "reason": ""}])
        self.assertEqual(ac_gaps(data, ["AC-3"]), ["AC-3"])

    def test_validate_shape(self):
        self.assertEqual(validate_shape(approve_response()), [])
        self.assertTrue(validate_shape({}))
        self.assertTrue(validate_shape("nope"))
        bad = approve_response(findings=[{"id": "F1", "severity": "urgent", "kind": "correctness"}])
        self.assertTrue(any("severity" in p for p in validate_shape(bad)))
        bad = approve_response(tests=[{"purpose": "x"}])
        self.assertTrue(any("path" in p for p in validate_shape(bad)))

    def test_render_review_md(self):
        data = approve_response(findings=[finding("high", "correctness")], suggestions=["rename x"])
        md = render_review_md(data, CHANGES_REQUESTED, ["F1 ..."], {"round": 1, "model_actual": "m"}, ["AC-1", "AC-2", "AC-3"])
        self.assertIn("# Review round 1: CHANGES_REQUESTED", md)
        self.assertIn("## Blocking", md)
        self.assertIn("**F1**", md)
        self.assertIn("AC-3: **uncovered**", md)
        self.assertIn("Reviewer said APPROVE", md)
        self.assertIn("rename x", md)


class PromptTests(RepoCase):
    def test_prompt_contents(self):
        ctx = preflight(self.repo)
        state = State()
        prompt = build_prompt(ctx, state, self.rdir(), 1)
        self.assertIn("add a mul(a, b) function", prompt)          # request verbatim
        self.assertIn("- AC-1:", prompt)
        self.assertIn("def mul(a, b):", prompt)                    # the diff
        self.assertIn("Built-in", prompt)                          # built-in checklist
        self.assertIn("Project conventions", prompt)               # project layer
        self.assertIn("test_review_{topic}.py", prompt)
        self.assertNotIn("$diff", prompt)
        self.assertIn("required for kind feature", prompt)
        self.assertNotIn("Previous round", prompt)

    def test_user_checklist_layer(self):
        path = os.path.join(self.home, "review-checklist.md")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("# Mine\n- indent 4 spaces\n")
        with open(os.path.join(self.home, "config.toml"), "w", encoding="utf-8") as fh:
            fh.write('checklist = "review-checklist.md"\n')
        ctx = preflight(self.repo)
        text = assemble_checklist(ctx)
        self.assertIn("### User", text)
        self.assertIn("indent 4 spaces", text)
        self.assertLess(text.index("Built-in"), text.index("### User"))
        self.assertLess(text.index("### User"), text.index("### Project"))

    def test_round_two_includes_prior_findings_and_response(self):
        ctx = preflight(self.repo)
        os.makedirs(self.rdir(), exist_ok=True)
        prev = {"data": approve_response(verdict=CHANGES_REQUESTED, findings=[finding("high", "correctness")])}
        with open(os.path.join(self.rdir(), "review-1.json"), "w", encoding="utf-8") as fh:
            json.dump(prev, fh)
        with open(os.path.join(self.rdir(), "response-1.md"), "w", encoding="utf-8") as fh:
            fh.write("- F1: wontfix: the AC allows it\n")
        state = State()
        state.rounds.append({"round": 1})
        state.test_files.append("tests/test_review_mul.py")
        prompt = build_prompt(ctx, state, self.rdir(), 2)
        self.assertIn("Previous round (1)", prompt)
        self.assertIn("F1 [high correctness]", prompt)
        self.assertIn("wontfix: the AC allows it", prompt)
        self.assertIn("earlier rounds", prompt)
        self.assertIn("re-review", prompt)

    def test_modified_existing_tests_listed(self):
        self.write("tests/test_calc.py", self.read("tests/test_calc.py").replace("5)", "6)"))
        self.commit_all("weaken")
        ctx = preflight(self.repo)
        prompt = build_prompt(ctx, State(), self.rdir(), 1)
        self.assertIn("modifies or deletes: tests/test_calc.py", prompt)

    def test_bounce_section(self):
        ctx = preflight(self.repo)
        prompt = build_prompt(ctx, State(), self.rdir(), 1, bounce_notes="cover AC-2")
        self.assertIn("Corrections required", prompt)
        self.assertIn("cover AC-2", prompt)


if __name__ == "__main__":
    unittest.main()
