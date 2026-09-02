"""Acceptance tests for fix/reset-pending-files: `revali reset` disposes of a NEEDS_INFO
round's pending test files (and an interrupted round's leftovers) the way the next run
would have, and a leftover that cannot be deleted stays in the tolerated list instead of
being forgotten while it still blocks the tree."""
import os
import unittest
from unittest import mock

from tests.helpers import RepoCase, TEST_REVIEW_MUL, approve_response, claude_entry, git, run_cli
from revali import EXIT_ACTION, EXIT_ERROR, EXIT_OK
from revali.state import State

HERE = os.path.dirname(os.path.abspath(__file__))
PENDING = "tests/test_review_mul.py"
SECOND = "tests/test_review_zero.py"
SECOND_TEXT = TEST_REVIEW_MUL.replace("MulTests", "ZeroTests")
MINE = "tests/test_review_mine.py"      # the author's own untracked draft, not the reviewer's
HIGH = {"id": "F1", "file": "src/calc.py", "line": 3, "severity": "high", "kind": "correctness",
        "text": "wrong for negatives", "suggestion": ""}


def asking(write_tests=True):
    data = approve_response(verdict="NEEDS_INFO", questions=["Which integers?"], tests=[])
    return claude_entry(data, write_tests=write_tests)


def approving(**files):
    tests = [{"path": p, "purpose": "acceptance", "covers": ["AC-1", "AC-2"], "expected": "per AC"}
             for p in files]
    entry = claude_entry(approve_response(tests=tests), write_tests=False)
    entry["write_files"] = dict(files)
    return entry


def failing_remove(stuck_rel):
    """os.remove that refuses one relative path (as Windows does for an open file)."""
    real = os.remove
    stuck = stuck_rel.replace("\\", "/")

    def remove(path, *args, **kwargs):
        if os.path.abspath(path).replace("\\", "/").endswith("/" + stuck):
            raise PermissionError(13, "held open by another process", path)
        return real(path, *args, **kwargs)
    return remove


class PendingCase(RepoCase):
    def needs_info_round(self):
        self.claude(asking())
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ACTION, out)
        self.assertEqual(self.state().pending_test_files, [PENDING])

    def state(self):
        return State.load(self.rdir())

    def status_of_tests(self):
        return git(["status", "--porcelain", "--", "tests"], self.repo).strip()


class ResetWithPendingFiles(PendingCase):
    def test_untracked_pending_file_is_deleted_and_named(self):
        self.needs_info_round()
        code, out = run_cli(["reset"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn(PENDING, out)                                                        # AC-1: printed
        self.assertIn("removed", out)
        self.assertFalse(self.exists(PENDING))                                             # AC-1: deleted
        self.assertEqual(self.status_of_tests(), "")
        self.assertIsNone(self.state())                                                    # state gone
        self.assertIn("state removed", out)
        self.claude(claude_entry())
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)                                               # AC-1: not refused

    def test_modified_own_tracked_pending_file_goes_back_to_head(self):
        self.claude(claude_entry(approve_response(verdict="CHANGES_REQUESTED", findings=[HIGH])))
        self.assertEqual(run_cli(["run", "--foreground"])[0], EXIT_ACTION)
        committed = self.read(PENDING)
        self.write("src/calc.py", self.read("src/calc.py") + "\n# negatives handled\n")
        self.commit_all("fix")
        entry = asking(write_tests=False)
        entry["write_files"] = {PENDING: TEST_REVIEW_MUL + "\n# updated by round 2\n", SECOND: SECOND_TEXT}
        self.claude(entry)
        self.assertEqual(run_cli(["run", "--foreground"])[0], EXIT_ACTION)
        self.assertEqual(sorted(self.state().pending_test_files), [PENDING, SECOND])
        code, out = run_cli(["reset"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertEqual(self.read(PENDING), committed)                                    # AC-1: restored
        self.assertFalse(self.exists(SECOND))                                              # AC-1: deleted
        self.assertIn("restored", out)
        self.assertIn(PENDING, out)
        self.assertIn(SECOND, out)
        self.assertEqual(self.status_of_tests(), "")


class ResetAfterInterruption(PendingCase):
    def test_leftovers_of_a_killed_session_are_removed(self):
        self.needs_info_round()
        state = self.state()
        state.reviewer_running = True          # what a session killed mid-round leaves behind
        state.set_stage(self.rdir(), "review", "killed", EXIT_ERROR)
        self.write(SECOND, "# half written\n")
        code, out = run_cli(["reset"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("removed 2 unfinished test file", out)                               # AC-2
        self.assertFalse(self.exists(PENDING))
        self.assertFalse(self.exists(SECOND))
        self.assertEqual(self.status_of_tests(), "")


class ResetLeavesACleanStateAlone(PendingCase):
    def test_authors_untracked_test_file_survives(self):
        self.claude(claude_entry(approve_response(verdict="CHANGES_REQUESTED", findings=[HIGH])))
        self.assertEqual(run_cli(["run", "--foreground"])[0], EXIT_ACTION)
        self.write(MINE, "# the author's draft\n")
        code, out = run_cli(["reset"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertTrue(self.exists(MINE))                                                 # AC-3
        self.assertNotIn("removed", out.replace("state removed", ""))
        self.assertIn("state removed", out)

    def test_no_state_at_all_touches_nothing(self):
        self.write(MINE, "# the author's draft\n")
        code, out = run_cli(["reset"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("no state to remove", out)
        self.assertTrue(self.exists(MINE))


class ResetWithoutAUsableProject(PendingCase):
    def test_pending_paths_are_printed_for_the_author(self):
        self.needs_info_round()
        os.remove(self.change_md())                       # locate() cannot build a context now
        code, out = run_cli(["reset"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIsNone(self.state())                                                    # AC-4: state gone
        self.assertIn("by hand", out)
        self.assertIn(PENDING, out)
        self.assertTrue(self.exists(PENDING))                                              # AC-4: untouched


class UndeletableLeftoverStaysTolerated(PendingCase):
    def test_stop_path_keeps_the_stuck_file_in_the_list(self):
        self.needs_info_round()
        self.claude(claude_entry(is_error=True))
        with mock.patch("os.remove", failing_remove(PENDING)):
            code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertTrue(self.exists(PENDING))
        self.assertIn("could not remove", out)                                             # AC-5: named
        self.assertIn("tolerate", out)
        self.assertEqual(self.state().pending_test_files, [PENDING])                       # AC-5: kept
        code, out = run_cli(["preflight"])
        self.assertEqual(code, EXIT_OK, out)                                               # AC-5: not refused
        self.claude(approving(**{PENDING: TEST_REVIEW_MUL}))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        prompt = self.fake_calls("claude")[-1]["prompt"]
        self.assertIn("not committed yet", prompt)                                         # AC-5: listed
        self.assertIn("- " + PENDING, prompt)
        self.assertEqual(self.state().pending_test_files, [])
        self.assertEqual(self.status_of_tests(), "")

    def test_start_of_run_cleanup_adds_the_stuck_file_to_the_list(self):
        self.needs_info_round()
        state = self.state()
        state.reviewer_running = True
        state.set_stage(self.rdir(), "review", "killed", EXIT_ERROR)
        self.write(SECOND, "# half written\n")            # not pending: the killed session's own
        self.claude(approving(**{SECOND: SECOND_TEXT}))
        with mock.patch("os.remove", failing_remove(SECOND)):
            code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)                                               # AC-5: not refused
        self.assertFalse(self.exists(PENDING))            # the deletable one went as before
        self.assertIn("could not remove", out)
        self.assertIn(SECOND, out)
        prompt = self.fake_calls("claude")[-1]["prompt"]
        self.assertIn("- " + SECOND, prompt.split("not committed yet", 1)[1])              # AC-5: listed
        state = self.state()
        self.assertEqual(state.pending_test_files, [])
        self.assertEqual(state.test_files, [SECOND])
        self.assertEqual(self.status_of_tests(), "")


class ReadmeDescribesIt(unittest.TestCase):
    def test_reset_and_stuck_files_are_documented(self):
        with open(os.path.join(os.path.dirname(HERE), "README.md"), "r", encoding="utf-8") as fh:
            text = fh.read()
        part = text.split("## What revali does to your repository", 1)[1].split("\n## ", 1)[0]
        self.assertIn("`revali reset`", part)                                              # AC-6
        self.assertIn("cannot be deleted", part)


if __name__ == "__main__":
    unittest.main()
