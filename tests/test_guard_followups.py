"""Guards restore from HEAD, cope with any path git reports, and the run after an
interrupted round cleans the Reviewer's leftovers (AC-1..AC-6 of fix/guard-followups)."""
import os
import unittest

from tests.helpers import ROOT, RepoCase, TEST_REVIEW_MUL, approve_response, claude_entry, git, run_cli
from revali import EXIT_ERROR, EXIT_OK
from revali.gitops import status_porcelain
from revali.preflight import preflight
from revali.review import guard_worktree, restore_protected_tests
from revali.state import State

WEAKENED = "import unittest\n\n\nclass Nothing(unittest.TestCase):\n    def test_nothing(self):\n        pass\n"
SPACED = "tests/test with space.py"
NON_ASCII = "tests/test_review_中文.py"


def changed_in(sha, repo):
    return git(["-c", "core.quotepath=false", "show", "--name-only", "--format=", sha], repo).splitlines()


class RestoreFromHeadTests(RepoCase):
    def test_protected_test_restored_in_index_and_tree(self):
        ctx = preflight(self.repo)
        original = self.read("tests/test_calc.py")
        self.write("tests/test_calc.py", WEAKENED)
        git(["add", "tests/test_calc.py"], self.repo)
        self.assertEqual(restore_protected_tests(ctx, State(), None), ["tests/test_calc.py"])
        self.assertEqual(self.read("tests/test_calc.py"), original)                        # AC-1
        self.assertEqual(git(["diff", "--cached", "--name-only"], self.repo).strip(), "")
        self.assertEqual(git(["status", "--porcelain"], self.repo).strip(), "")

    def test_outside_file_restored_in_index_and_tree(self):
        ctx = preflight(self.repo)
        original = self.read("src/calc.py")
        self.write("src/calc.py", original + "# staged\n")
        git(["add", "src/calc.py"], self.repo)
        self.assertEqual(guard_worktree(ctx, None), ["src/calc.py"])
        self.assertEqual(self.read("src/calc.py"), original)                               # AC-1
        self.assertEqual(git(["status", "--porcelain"], self.repo).strip(), "")


class MessageTests(RepoCase):
    def test_second_offence_message_names_files_without_claiming_a_retry(self):
        entry = claude_entry(approve_response())
        entry["write_files"]["tests/test_calc.py"] = WEAKENED
        self.claude(entry, entry)
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertIn("tests/test_calc.py", out)                                           # AC-2
        self.assertIn("last attempt", out)
        self.assertNotIn("after the retry", out)


class PathTests(RepoCase):
    def test_status_porcelain_unquoted(self):
        self.write(SPACED, "x = 1\n")
        self.write(NON_ASCII, "y = 2\n")
        entries = status_porcelain(self.repo)
        paths = [p for _, p in entries]
        self.assertIn(SPACED, paths)                                                       # AC-3
        self.assertIn(NON_ASCII, paths)
        self.assertEqual([c for c, _ in entries], ["??", "??"])
        self.assertFalse(any(p.startswith('"') for p in paths))

    def test_reviewer_files_with_odd_names_handled(self):
        self.write(SPACED, TEST_REVIEW_MUL)
        self.commit_all("a test file with a space in its name")
        original = self.read(SPACED)
        new_files = {"tests/test_review_my topic.py": TEST_REVIEW_MUL, NON_ASCII: TEST_REVIEW_MUL}
        answer = approve_response(tests=[{"path": p, "purpose": "p", "covers": ["AC-1", "AC-2"], "expected": "e"}
                                         for p in new_files])
        first = claude_entry(answer, write_tests=False)
        first["write_files"] = dict(new_files, **{SPACED: WEAKENED})
        second = claude_entry(answer, write_tests=False)
        second["write_files"] = dict(new_files)
        self.claude(first, second)
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertEqual(self.read(SPACED), original)                                      # AC-3, restored
        state = State.load(self.rdir())
        self.assertEqual(sorted(changed_in(state.test_commits[0], self.repo)), sorted(new_files))
        self.assertEqual(sorted(state.test_files), sorted(new_files))
        self.assertIn(SPACED, self.fake_calls("claude")[1]["prompt"])                       # named in the bounce


class InterruptedRunTests(RepoCase):
    def _previous(self, stage):
        state = State()
        state.set_stage(self.rdir(), stage, "previous run", EXIT_ERROR)

    def test_leftover_removed_after_stop(self):
        self._previous("stopped")
        self.write("tests/test_review_left.py", "# half written\n")
        self.claude(claude_entry())
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)                                               # AC-4
        self.assertIn("tests/test_review_left.py", out)
        self.assertIn("interrupted", out)
        self.assertFalse(self.exists("tests/test_review_left.py"))

    def test_leftover_removed_after_kill_mid_review(self):
        self._previous("review")
        self.write("tests/test_review_left.py", "# half written\n")
        self.claude(claude_entry())
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)                                               # AC-4
        self.assertFalse(self.exists("tests/test_review_left.py"))

    def test_other_files_untouched_and_tree_still_dirty(self):
        self._previous("stopped")
        self.write("tests/scratch.py", "# not the pattern\n")
        self.write("src/extra.py", "# outside test_dir\n")
        self.write("tests/test_calc.py", self.read("tests/test_calc.py") + "# tracked\n")
        self.claude(claude_entry())
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR, out)                                            # AC-4
        self.assertIn("not clean", out)
        self.assertTrue(self.exists("tests/scratch.py"))
        self.assertTrue(self.exists("src/extra.py"))
        self.assertTrue(self.read("tests/test_calc.py").endswith("# tracked\n"))

    def test_no_cleanup_after_a_verdict(self):
        self._previous("needs_action")
        self.write("tests/test_review_left.py", "# kept on purpose\n")
        self.claude(claude_entry())
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR, out)                                            # AC-5
        self.assertIn("not clean", out)
        self.assertTrue(self.exists("tests/test_review_left.py"))

    def test_first_run_is_not_an_interrupted_run(self):
        self.write("tests/test_review_left.py", "# the author's own file\n")
        self.claude(claude_entry())
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR, out)                                            # AC-5
        self.assertTrue(self.exists("tests/test_review_left.py"))

    def test_preflight_command_never_deletes(self):
        self._previous("stopped")
        self.write("tests/test_review_left.py", "# half written\n")
        code, out = run_cli(["preflight"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertTrue(self.exists("tests/test_review_left.py"))


class ReadmeTests(unittest.TestCase):
    def test_readme_describes_cleanup_and_restore_source(self):
        with open(os.path.join(ROOT, "README.md"), "r", encoding="utf-8") as fh:
            text = fh.read()
        section = text.split("## What revali does to your repository")[1].split("\n## ")[0]
        self.assertNotIn("do not clean up", section)                                       # AC-6
        self.assertIn("interrupted", section)
        self.assertIn("from HEAD", section)


if __name__ == "__main__":
    unittest.main()
