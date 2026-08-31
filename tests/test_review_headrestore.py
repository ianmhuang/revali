"""AC-1..AC-3 of fix/guard-followups: the guards restore from HEAD (index and working
tree alike), the second-offence message names the files and the round without claiming
a retry, and `git status` paths with spaces or non-ASCII characters arrive unquoted so
every guard handles them."""
import unittest

from tests.helpers import RepoCase, TEST_REVIEW_MUL, approve_response, claude_entry, git, run_cli
from revali import EXIT_ERROR, EXIT_OK
from revali.gitops import dirty_paths, status_porcelain
from revali.preflight import preflight
from revali.review import guard_worktree, new_test_files, restore_protected_tests
from revali.state import State

HOLLOW = "import unittest\n\n\nclass Hollow(unittest.TestCase):\n    def test_nothing(self):\n        pass\n"
TRACKED_SPACED = "tests/test_calc extra.py"      # tracked, not the reviewer's, not on test_file_pattern
TRACKED_NON_ASCII = "tests/test_計算.py"
NEW_SPACED = "tests/test_review_my topic.py"
NEW_NON_ASCII = "tests/test_review_乘法.py"


def files_in_commit(sha, repo):
    out = git(["-c", "core.quotepath=false", "show", "--name-only", "--format=", sha], repo)
    return sorted(l for l in out.splitlines() if l.strip())


def error_line(out):
    return next((l for l in out.splitlines() if l.startswith("ERROR:")), "")


def status(repo):
    return git(["-c", "core.quotepath=false", "status", "--porcelain", "--untracked-files=all"], repo).strip()


class RestoreFromHead(RepoCase):
    def test_staged_modification_of_a_protected_test_is_undone_in_index_and_tree(self):
        ctx = preflight(self.repo)
        original = self.read("tests/test_calc.py")
        self.write("tests/test_calc.py", HOLLOW)
        git(["add", "tests/test_calc.py"], self.repo)
        self.assertEqual(restore_protected_tests(ctx, State(), None), ["tests/test_calc.py"])
        self.assertEqual(self.read("tests/test_calc.py"), original)                        # AC-1: tree
        self.assertEqual(git(["diff", "--cached", "--name-only"], self.repo).strip(), "")  # AC-1: index
        self.assertEqual(status(self.repo), "")

    def test_staged_deletion_of_a_protected_test_is_undone(self):
        ctx = preflight(self.repo)
        original = self.read("tests/test_calc.py")
        git(["rm", "-q", "tests/test_calc.py"], self.repo)
        self.assertEqual(restore_protected_tests(ctx, State(), None), ["tests/test_calc.py"])
        self.assertEqual(self.read("tests/test_calc.py"), original)                        # AC-1
        self.assertEqual(status(self.repo), "")

    def test_staged_change_outside_test_dir_is_undone_in_index_and_tree(self):
        ctx = preflight(self.repo)
        original = self.read("src/calc.py")
        self.write("src/calc.py", original + "\n# staged by the reviewer\n")
        git(["add", "src/calc.py"], self.repo)
        self.assertEqual(guard_worktree(ctx, None), ["src/calc.py"])
        self.assertEqual(self.read("src/calc.py"), original)                               # AC-1
        self.assertEqual(git(["diff", "--cached", "--name-only"], self.repo).strip(), "")
        self.assertEqual(status(self.repo), "")

    def test_staged_deletion_outside_test_dir_is_undone(self):
        ctx = preflight(self.repo)
        git(["rm", "-q", "src/__init__.py"], self.repo)
        self.assertEqual(guard_worktree(ctx, None), ["src/__init__.py"])
        self.assertTrue(self.exists("src/__init__.py"))                                    # AC-1
        self.assertEqual(status(self.repo), "")

    def test_staged_addition_outside_test_dir_leaves_index_and_tree_at_head(self):
        # Round 1, F3: a path absent from HEAD cannot be restored from it; the guard must
        # unstage and delete it instead of reporting a revert that did not happen.
        ctx = preflight(self.repo)
        self.write("src/new module.py", "x = 1\n")
        git(["add", "src/new module.py"], self.repo)
        self.assertEqual(guard_worktree(ctx, None), ["src/new module.py"])                 # AC-1
        self.assertFalse(self.exists("src/new module.py"))
        self.assertEqual(git(["diff", "--cached", "--name-only"], self.repo).strip(), "")
        self.assertEqual(status(self.repo), "")

    def test_staged_addition_under_test_dir_is_the_reviewers_own_file(self):
        ctx = preflight(self.repo)
        self.write(NEW_SPACED, "a = 1\n")
        git(["add", NEW_SPACED], self.repo)
        self.assertEqual(restore_protected_tests(ctx, State(), None), [])                  # AC-1: not a protected file
        self.assertTrue(self.exists(NEW_SPACED))
        self.assertEqual(new_test_files(ctx), [NEW_SPACED])
        self.assertEqual(status(self.repo), "A  " + NEW_SPACED)


class SecondOffenceMessage(RepoCase):
    def test_exit1_names_the_files_and_round_without_claiming_a_retry(self):
        entry = claude_entry(approve_response())
        entry["write_files"]["tests/test_calc.py"] = HOLLOW
        self.claude(entry, entry)
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR, out)
        line = error_line(out)
        self.assertIn("tests/test_calc.py", line)                                          # AC-2
        self.assertIn("round 1", line)
        self.assertIn("last attempt", line)
        self.assertNotIn("after the retry", line)
        self.assertNotIn("retry allowed", line)
        self.assertEqual(len(self.fake_calls("claude")), 2)
        self.assertEqual(status(self.repo), "")                                            # restored, nothing committed
        state = State.load(self.rdir())
        self.assertEqual(state.stage, "error")
        self.assertIn("tests/test_calc.py", state.message)
        self.assertNotIn("after the retry", state.message)


class PorcelainPaths(RepoCase):
    def test_untracked_and_modified_paths_arrive_unquoted(self):
        self.write(TRACKED_SPACED, "x = 1\n")
        self.write(TRACKED_NON_ASCII, "y = 2\n")
        self.commit_all("tracked files with odd names")
        self.write(TRACKED_SPACED, "x = 2\n")
        self.write(TRACKED_NON_ASCII, "y = 3\n")
        self.write(NEW_SPACED, "a = 1\n")
        self.write(NEW_NON_ASCII, "b = 1\n")
        entries = {path: code for code, path in status_porcelain(self.repo)}
        self.assertEqual(entries, {TRACKED_SPACED: " M", TRACKED_NON_ASCII: " M",           # AC-3
                                   NEW_SPACED: "??", NEW_NON_ASCII: "??"})
        self.assertFalse(any('"' in p or "\\" in p for p in entries))
        self.assertEqual(sorted(dirty_paths(self.repo, (".revali/",))),
                         sorted(["M " + TRACKED_SPACED, "M " + TRACKED_NON_ASCII,
                                 "?? " + NEW_SPACED, "?? " + NEW_NON_ASCII]))

    def test_rename_entry_carries_only_the_new_path(self):
        git(["mv", "tests/test_calc.py", "tests/test calc.py"], self.repo)
        entries = status_porcelain(self.repo)
        self.assertEqual(entries, [("R ", "tests/test calc.py")])                          # AC-3
        self.assertEqual(dirty_paths(self.repo, (".revali/",)), ["R tests/test calc.py"])

    def test_new_test_files_reports_odd_names_unquoted(self):
        ctx = preflight(self.repo)
        self.write(NEW_SPACED, "a = 1\n")
        self.write(NEW_NON_ASCII, "b = 1\n")
        self.assertEqual(new_test_files(ctx), sorted([NEW_SPACED, NEW_NON_ASCII]))         # AC-3

    def test_guards_restore_odd_names(self):
        self.write(TRACKED_SPACED, "x = 1\n")
        self.write(TRACKED_NON_ASCII, "y = 2\n")
        self.write("src/mod ule.py", "z = 1\n")
        self.commit_all("tracked files with odd names")
        ctx = preflight(self.repo)
        self.write(TRACKED_SPACED, HOLLOW)
        self.write(TRACKED_NON_ASCII, HOLLOW)
        self.write("src/mod ule.py", "z = 2\n")
        self.write("src/new 檔案.py", "n = 1\n")
        self.assertEqual(sorted(guard_worktree(ctx, None)), ["src/mod ule.py", "src/new 檔案.py"])
        self.assertEqual(self.read("src/mod ule.py"), "z = 1\n")                           # AC-3
        self.assertFalse(self.exists("src/new 檔案.py"))
        self.assertEqual(sorted(restore_protected_tests(ctx, State(), None)),
                         sorted([TRACKED_SPACED, TRACKED_NON_ASCII]))
        self.assertEqual(self.read(TRACKED_SPACED), "x = 1\n")
        self.assertEqual(self.read(TRACKED_NON_ASCII), "y = 2\n")
        self.assertEqual(status(self.repo), "")


class PipelineWithOddNames(RepoCase):
    def test_tracked_spaced_file_restored_and_new_odd_files_committed(self):
        self.write(TRACKED_SPACED, TEST_REVIEW_MUL.replace("MulTests", "SpacedTests"))
        self.write(TRACKED_NON_ASCII, TEST_REVIEW_MUL.replace("MulTests", "NonAsciiTests"))
        self.commit_all("tracked test files with odd names")
        spaced_original = self.read(TRACKED_SPACED)
        non_ascii_original = self.read(TRACKED_NON_ASCII)
        new_files = {NEW_SPACED: TEST_REVIEW_MUL, NEW_NON_ASCII: TEST_REVIEW_MUL.replace("MulTests", "T2")}
        answer = approve_response(tests=[{"path": p, "purpose": "p", "covers": ["AC-1", "AC-2"], "expected": "e"}
                                         for p in new_files])
        first = claude_entry(answer, write_tests=False)
        first["write_files"] = dict(new_files, **{TRACKED_SPACED: HOLLOW, TRACKED_NON_ASCII: HOLLOW})
        second = claude_entry(answer, write_tests=False)
        second["write_files"] = dict(new_files)
        self.claude(first, second)
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertEqual(self.read(TRACKED_SPACED), spaced_original)                       # AC-3: restored
        self.assertEqual(self.read(TRACKED_NON_ASCII), non_ascii_original)
        self.assertEqual(status(self.repo), "")
        state = State.load(self.rdir())
        self.assertEqual(len(state.test_commits), 1)
        self.assertEqual(files_in_commit(state.test_commits[0], self.repo), sorted(new_files))   # AC-3: committed
        self.assertEqual(sorted(state.test_files), sorted(new_files))
        prompts = [c["prompt"] for c in self.fake_calls("claude")]
        self.assertEqual(len(prompts), 2)
        note = prompts[1].split("Corrections required", 1)[1]
        self.assertIn(TRACKED_SPACED, note)                                                # named unquoted
        self.assertIn(TRACKED_NON_ASCII, note)
        self.assertNotIn('"' + TRACKED_SPACED + '"', note)


if __name__ == "__main__":
    unittest.main()
