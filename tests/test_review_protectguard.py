"""Acceptance tests for AC-1..AC-4 of fix/test-guard-exit2: the Reviewer may add test
files and update the ones it wrote in earlier rounds; every other tracked file under
test_dir is restored before any commit, the session is sent back once, a second offence
ends the run with exit 1 and no commit, and the prompt names the files it must not use."""

import os
import unittest

from revali import EXIT_ERROR, EXIT_OK, PROMPT_VERSION
from revali.state import State
from tests.helpers import TEST_REVIEW_MUL, RepoCase, approve_response, claude_entry, git, run_cli

HOLLOW = (
    "import unittest\n\n\n"
    "class Hollow(unittest.TestCase):\n"
    "    def test_nothing(self):\n"
    "        pass\n"
)
OLD_PR_TESTS = TEST_REVIEW_MUL.replace("MulTests", "OldTests")
EXTRA_TESTS = TEST_REVIEW_MUL.replace("MulTests", "ExtraTests")
HIGH = {
    "id": "F1",
    "file": "src/calc.py",
    "line": 3,
    "severity": "high",
    "kind": "correctness",
    "text": "wrong for negatives",
    "suggestion": "",
}


def files_in_commit(sha, repo):
    return sorted(git(["show", "--name-only", "--format=", sha], repo).split())


def head(repo):
    return git(["rev-parse", "HEAD"], repo).strip()


def error_line(out):
    return next((line for line in out.splitlines() if line.startswith("ERROR:")), "")


class ProtectedCase(RepoCase):
    """The fixture plus `tests/test_review_old.py` committed on main (a reviewer file from
    an earlier PR) with the feature branch rebased on it, so the file is not in the diff."""

    def setUp(self):
        super().setUp()
        git(["checkout", "-q", "main"], self.repo)
        self.write("tests/test_review_old.py", OLD_PR_TESTS)
        self.commit_all("test: review tests from an earlier PR")
        git(["push", "-q", "origin", "main"], self.repo)
        git(["checkout", "-q", "feature/mul"], self.repo)
        git(["rebase", "-q", "main"], self.repo)


class RestoreAndBounce(ProtectedCase):
    def test_tampered_files_restored_and_session_sent_back_once(self):
        old = self.read("tests/test_review_old.py")
        calc = self.read("tests/test_calc.py")
        first = claude_entry(approve_response())
        first["write_files"]["tests/test_review_old.py"] = HOLLOW  # an earlier PR's reviewer file
        first["write_files"]["tests/test_calc.py"] = HOLLOW  # the project's own suite
        self.claude(first, claude_entry(approve_response()))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertEqual(self.read("tests/test_review_old.py"), old)  # AC-1
        self.assertEqual(self.read("tests/test_calc.py"), calc)
        state = State.load(self.rdir())
        self.assertEqual(len(state.test_commits), 1)
        self.assertEqual(
            files_in_commit(state.test_commits[0], self.repo), ["tests/test_review_mul.py"]
        )
        self.assertEqual(state.test_files, ["tests/test_review_mul.py"])
        self.assertEqual(git(["status", "--porcelain", "--", "tests"], self.repo).strip(), "")
        prompts = [c["prompt"] for c in self.fake_calls("claude")]
        self.assertEqual(len(prompts), 2)  # AC-2: one bounce
        self.assertIn("Corrections required", prompts[1])
        note = prompts[1].split("Corrections required", 1)[1]
        self.assertIn("tests/test_review_old.py", note)  # restored, and a name already taken
        self.assertIn("tests/test_calc.py", note)  # restored

    def test_bounce_shares_the_single_retry_with_ac_gaps(self):
        partial = approve_response(
            tests=[
                {
                    "path": "tests/test_review_mul.py",
                    "purpose": "p",
                    "covers": ["AC-1"],
                    "expected": "e",
                }
            ]
        )
        first = claude_entry(partial)
        first["write_files"]["tests/test_review_old.py"] = HOLLOW
        self.claude(first, claude_entry(approve_response()))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        prompts = [c["prompt"] for c in self.fake_calls("claude")]
        self.assertEqual(len(prompts), 2)  # AC-2: still one bounce
        note = prompts[1].split("Corrections required", 1)[1]
        self.assertIn("tests/test_review_old.py", note)
        self.assertIn("AC-2", note)  # the gap rides along
        self.assertEqual(self.read("tests/test_review_old.py"), OLD_PR_TESTS)

    def test_second_offence_is_exit1_without_a_commit(self):
        before = head(self.repo)
        entry = claude_entry(approve_response())
        entry["write_files"]["tests/test_review_old.py"] = HOLLOW
        self.claude(entry, entry)
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR, out)  # AC-2
        self.assertIn("tests/test_review_old.py", error_line(out))
        self.assertEqual(len(self.fake_calls("claude")), 2)
        self.assertEqual(self.read("tests/test_review_old.py"), OLD_PR_TESTS)
        self.assertEqual(head(self.repo), before)  # no test commit
        self.assertEqual(git(["status", "--porcelain", "--", "tests"], self.repo).strip(), "")
        state = State.load(self.rdir())
        self.assertEqual(state.stage, "error")
        self.assertEqual(state.test_commits, [])
        self.assertEqual(state.test_files, [])
        self.assertIn("tests/test_review_old.py", state.message)


class DeletionRestore(ProtectedCase):
    def test_deleted_tracked_files_are_restored_and_new_files_kept(self):
        # The claude stub cannot delete, so the deletion case of AC-1 is exercised on the
        # function that runs after every session.
        from revali.preflight import preflight
        from revali.review import restore_protected_tests

        ctx = preflight(self.repo)
        os.remove(os.path.join(self.repo, "tests", "test_review_old.py"))
        os.remove(os.path.join(self.repo, "tests", "__init__.py"))
        self.write("tests/test_review_mul.py", TEST_REVIEW_MUL)
        restored = restore_protected_tests(ctx, State(), None)
        self.assertEqual(
            sorted(restored), ["tests/__init__.py", "tests/test_review_old.py"]
        )  # AC-1
        self.assertEqual(self.read("tests/test_review_old.py"), OLD_PR_TESTS)
        self.assertTrue(self.exists("tests/__init__.py"))
        self.assertTrue(self.exists("tests/test_review_mul.py"))  # AC-3: new file untouched
        self.assertEqual(
            git(["status", "--porcelain", "--", "tests"], self.repo).strip(),
            "?? tests/test_review_mul.py",
        )


class OwnFilesAccepted(ProtectedCase):
    def test_round_two_updates_its_own_file_and_adds_another(self):
        cr = approve_response(verdict="CHANGES_REQUESTED", findings=[HIGH])
        ok = approve_response(
            previous_findings=[{"id": "F1", "status": "resolved", "note": "fixed"}],
            tests=[
                {
                    "path": "tests/test_review_mul.py",
                    "purpose": "p",
                    "covers": ["AC-1"],
                    "expected": "e",
                },
                {
                    "path": "tests/test_review_extra.py",
                    "purpose": "p",
                    "covers": ["AC-2"],
                    "expected": "e",
                },
            ],
        )
        second = claude_entry(ok, write_tests=False)
        second["write_files"] = {
            "tests/test_review_mul.py": TEST_REVIEW_MUL + "\n# updated in round 2\n",
            "tests/test_review_extra.py": EXTRA_TESTS,
        }
        self.claude(claude_entry(cr), second)
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, 2, out)
        self.write("src/calc.py", self.read("src/calc.py") + "\n# negatives handled\n")
        self.commit_all("fix negatives")
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)  # AC-3
        self.assertEqual(len(self.fake_calls("claude")), 2)  # no bounce in either round
        state = State.load(self.rdir())
        self.assertEqual(len(state.test_commits), 2)
        self.assertEqual(
            files_in_commit(state.test_commits[1], self.repo),
            ["tests/test_review_extra.py", "tests/test_review_mul.py"],
        )
        self.assertTrue(self.read("tests/test_review_mul.py").endswith("# updated in round 2\n"))
        self.assertEqual(
            sorted(state.test_files), ["tests/test_review_extra.py", "tests/test_review_mul.py"]
        )
        self.assertEqual(self.read("tests/test_review_old.py"), OLD_PR_TESTS)


class PromptNamesTakenFiles(ProtectedCase):
    def test_earlier_pr_file_is_listed_and_the_project_suite_is_not(self):
        self.claude(claude_entry(approve_response()))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        prompt = self.fake_calls("claude")[0]["prompt"]
        self.assertIn("\n- tests/test_review_old.py\n", prompt)  # AC-4
        self.assertNotIn("test_calc.py", prompt)  # only names matching test_file_pattern

    def test_own_round_one_file_is_not_listed_as_taken_in_round_two(self):
        cr = approve_response(verdict="CHANGES_REQUESTED", findings=[HIGH])
        self.claude(claude_entry(cr), claude_entry(write_tests=False))
        run_cli(["run", "--foreground"])
        self.write("src/calc.py", self.read("src/calc.py") + "\n# fix\n")
        self.commit_all("fix")
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        prompt = self.fake_calls("claude")[1]["prompt"]
        self.assertIn("\n- tests/test_review_old.py\n", prompt)
        # once, in the list of files it wrote itself; not a second time as a taken name
        self.assertEqual(prompt.count("\n- tests/test_review_mul.py\n"), 1)


class PromptWithoutTakenFiles(RepoCase):
    def test_section_absent_when_no_file_is_taken(self):
        self.claude(claude_entry(approve_response()))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        prompt = self.fake_calls("claude")[0]["prompt"]
        self.assertNotIn("\n- tests/", prompt)  # AC-4: absent
        self.assertNotIn("test_calc.py", prompt)

    def test_prompt_version_is_4(self):
        self.assertGreaterEqual(int(PROMPT_VERSION), 4)  # AC-4


if __name__ == "__main__":
    unittest.main()
