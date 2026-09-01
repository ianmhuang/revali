"""The Reviewer may add test files and update its own; every other tracked file under
test_dir is restored after the session (AC-1..AC-4 of fix/test-guard-exit2)."""
import os
import unittest

from tests.helpers import RepoCase, TEST_REVIEW_MUL, approve_response, claude_entry, git, run_cli
from revali import EXIT_ERROR, EXIT_OK, PROMPT_VERSION
from revali.preflight import preflight
from revali.review import build_prompt, existing_test_names, restore_protected_tests
from revali.state import State

WEAKENED = "import unittest\n\n\nclass Nothing(unittest.TestCase):\n    def test_nothing(self):\n        pass\n"
TEST_REVIEW_ZERO = TEST_REVIEW_MUL.replace("MulTests", "ZeroTests")


def changed_in(sha, repo):
    return git(["show", "--name-only", "--format=", sha], repo).split()


class RestoreTests(RepoCase):
    def test_modified_existing_test_is_restored_and_reviewer_bounced(self):
        original = self.read("tests/test_calc.py")
        first = claude_entry(approve_response())
        first["write_files"]["tests/test_calc.py"] = WEAKENED
        self.claude(first, claude_entry(approve_response()))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertEqual(self.read("tests/test_calc.py"), original)          # AC-1
        state = State.load(self.rdir())
        self.assertEqual(len(state.test_commits), 1)
        self.assertEqual(changed_in(state.test_commits[0], self.repo), ["tests/test_review_mul.py"])
        prompts = [c["prompt"] for c in self.fake_calls("claude")]
        self.assertEqual(len(prompts), 2)                                     # AC-2: one bounce
        self.assertIn("Corrections required", prompts[1])
        self.assertIn("tests/test_calc.py", prompts[1])
        self.assertIn("restored", prompts[1])
        self.assertIn("restored", out)

    def test_second_offence_is_a_pipeline_error(self):
        original = self.read("tests/test_calc.py")
        entry = claude_entry(approve_response())
        entry["write_files"]["tests/test_calc.py"] = WEAKENED
        self.claude(entry, entry)
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR, out)                              # AC-2
        self.assertIn("tests/test_calc.py", out)
        self.assertEqual(self.read("tests/test_calc.py"), original)
        state = State.load(self.rdir())
        self.assertEqual(state.test_commits, [])
        self.assertFalse(self.exists("tests/test_review_mul.py"))            # unfinished tests discarded
        self.assertEqual(git(["status", "--porcelain", "--", "tests"], self.repo).strip(), "")

    def test_deleted_existing_test_is_restored(self):
        ctx = preflight(self.repo)
        os.remove(os.path.join(self.repo, "tests", "test_calc.py"))
        restored = restore_protected_tests(ctx, State(), None)
        self.assertEqual(restored, ["tests/test_calc.py"])                   # AC-1, deletion
        self.assertTrue(self.exists("tests/test_calc.py"))
        self.assertIn("def test_add", self.read("tests/test_calc.py"))

    def test_own_earlier_round_files_and_new_files_are_committed(self):
        cr = approve_response(verdict="CHANGES_REQUESTED",
                              findings=[{"id": "F1", "file": "src/calc.py", "line": 3, "severity": "high",
                                         "kind": "correctness", "text": "negatives", "suggestion": ""}])
        ok = approve_response(previous_findings=[{"id": "F1", "status": "resolved", "note": "fixed"}],
                              tests=[{"path": "tests/test_review_mul.py", "purpose": "p", "covers": ["AC-1"],
                                      "expected": "e"},
                                     {"path": "tests/test_review_zero.py", "purpose": "p", "covers": ["AC-2"],
                                      "expected": "e"}])
        second = claude_entry(ok, write_tests=False)
        second["write_files"] = {"tests/test_review_mul.py": TEST_REVIEW_MUL + "\n# round 2\n",
                                 "tests/test_review_zero.py": TEST_REVIEW_ZERO}
        self.claude(claude_entry(cr), second)
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, 2, out)
        self.write("src/calc.py", self.read("src/calc.py") + "\n# handles negatives\n")
        self.commit_all("fix negatives")
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)                                 # AC-3
        state = State.load(self.rdir())
        self.assertEqual(len(state.test_commits), 2)
        self.assertEqual(sorted(changed_in(state.test_commits[1], self.repo)),
                         ["tests/test_review_mul.py", "tests/test_review_zero.py"])
        self.assertTrue(self.read("tests/test_review_mul.py").endswith("# round 2\n"))
        self.assertEqual(sorted(state.test_files), ["tests/test_review_mul.py", "tests/test_review_zero.py"])
        self.assertNotIn("Corrections required", self.fake_calls("claude")[1]["prompt"])


class PromptTests(RepoCase):
    def test_existing_reviewer_style_files_are_listed(self):
        self.write("tests/test_review_old.py", TEST_REVIEW_MUL)
        self.commit_all("tests from an earlier PR")
        ctx = preflight(self.repo)
        self.assertEqual(existing_test_names(ctx, State()), ["tests/test_review_old.py"])
        prompt = build_prompt(ctx, State(), self.rdir(), 1)
        self.assertIn("already exist", prompt)                               # AC-4
        self.assertIn("tests/test_review_old.py", prompt)
        self.assertNotIn("tests/test_calc.py", prompt.split("already exist")[1].split("##")[0])

    def test_own_files_are_not_listed_as_taken(self):
        self.write("tests/test_review_mul.py", TEST_REVIEW_MUL)
        self.commit_all("round 1 tests")
        ctx = preflight(self.repo)
        state = State()
        state.test_files.append("tests/test_review_mul.py")
        self.assertEqual(existing_test_names(ctx, state), [])
        self.assertNotIn("already exist", build_prompt(ctx, state, self.rdir(), 1))

    def test_section_absent_without_such_files(self):
        ctx = preflight(self.repo)
        self.assertNotIn("already exist", build_prompt(ctx, State(), self.rdir(), 1))

    def test_prompt_version_bumped(self):
        self.assertGreaterEqual(int(PROMPT_VERSION), 4)


if __name__ == "__main__":
    unittest.main()
