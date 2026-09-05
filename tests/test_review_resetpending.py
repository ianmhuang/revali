"""Reviewer tests for fix/reset-pending-files, AC-1..AC-4 and AC-6: `revali reset` disposes
of a NEEDS_INFO round's pending test files and an interrupted round's leftovers the way the
next `run` would have (untracked drafts deleted, a modified tracked file of the reviewer's
own restored from HEAD), says what it did, and then removes the state; with nothing pending
and nothing interrupted it leaves `test_dir` alone; without a loadable project it prints the
paths for the author. Black-box through the CLI, the working tree and the state file.
Round 2 adds: with pending files and no interrupted round only the pending list goes and an
author's own draft on the pattern survives (round 1 F1); a `Stop` from the HEAD restore or a
`GitError` from the status call does not escape `reset` (round 1 F2)."""
import os
import unittest
from unittest import mock

from tests.helpers import ROOT, RepoCase, TEST_REVIEW_MUL, approve_response, claude_entry, git, run_cli
from revali import EXIT_ACTION, EXIT_ERROR, EXIT_OK
from revali.state import State

MUL = "tests/test_review_mul.py"
SPACED = "tests/test_review_my topic.py"
LEFT = "tests/test_review_left.py"
MINE = "tests/test_review_mine.py"
SPACED_TEXT = TEST_REVIEW_MUL.replace("MulTests", "SpacedTests")


def needs_info(files=None):
    """A NEEDS_INFO answer writing `files` (path -> text); None: the stub's default MUL file;
    {}: nothing."""
    data = approve_response(verdict="NEEDS_INFO", questions=["Which integers?"], tests=[])
    if files is None:
        return claude_entry(data)
    entry = claude_entry(data, write_tests=False)
    if files:
        entry["write_files"] = dict(files)
    return entry


def blocking_finding():
    return {"id": "F1", "file": "src/calc.py", "line": 3, "severity": "high", "kind": "correctness",
            "text": "mul ignores negative numbers", "suggestion": "handle them"}


class ResetCase(RepoCase):
    def needs_info_round(self, files=None):
        self.claude(needs_info(files))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ACTION, out)
        return out

    def changes_requested_round(self):
        self.claude(claude_entry(approve_response(verdict="CHANGES_REQUESTED", findings=[blocking_finding()])))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ACTION, out)
        return out

    def state(self):
        return State.load(self.rdir())

    def status(self):
        return git(["status", "--porcelain"], self.repo).strip()

    def tests_status(self):
        return git(["status", "--porcelain", "--", "tests"], self.repo).strip()

    def reset(self):
        code, out = run_cli(["reset"])
        self.assertEqual(code, EXIT_OK, out)
        return out


class PendingFilesGoWithTheState(ResetCase):
    """AC-1: the untracked pending file is deleted and named, the state is removed, the tree
    under test_dir is clean and the next run is not refused."""

    def test_untracked_pending_files_are_deleted_and_named(self):
        self.needs_info_round({MUL: TEST_REVIEW_MUL, SPACED: SPACED_TEXT})
        self.assertEqual(sorted(self.state().pending_test_files), sorted([MUL, SPACED]))
        out = self.reset()
        self.assertFalse(self.exists(MUL))                                                 # AC-1: deleted
        self.assertFalse(self.exists(SPACED))
        self.assertIn(MUL, out)                                                            # AC-1: printed
        self.assertIn(SPACED, out)
        self.assertIn("state removed", out)
        self.assertIsNone(self.state())                                                    # AC-1: state gone
        self.assertEqual(self.tests_status(), "")                                          # AC-1: clean
        code, out = run_cli(["preflight"])
        self.assertEqual(code, EXIT_OK, out)                                               # AC-1: tree check passes
        self.assertNotIn("not clean", out)
        self.claude(claude_entry())
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)                                               # AC-1: next run not refused
        self.assertNotIn("not clean", out)

    def test_modified_tracked_file_of_the_reviewers_own_is_restored_from_head(self):
        self.changes_requested_round()
        self.assertEqual(self.state().test_files, [MUL])                                   # committed by round 1
        committed = self.read(MUL)
        self.write("src/calc.py", self.read("src/calc.py") + "\n# negatives handled\n")
        self.commit_all("fix: handle negatives")
        self.needs_info_round({MUL: TEST_REVIEW_MUL + "\n# updated by round 2\n", SPACED: SPACED_TEXT})
        self.assertIn(" M " + MUL, git(["status", "--porcelain"], self.repo))              # tracked, modified
        self.assertEqual(sorted(self.state().pending_test_files), sorted([MUL, SPACED]))
        out = self.reset()
        self.assertTrue(self.exists(MUL))                                                  # AC-1: not deleted
        self.assertEqual(self.read(MUL), committed)                                        # AC-1: back to HEAD
        self.assertFalse(self.exists(SPACED))                                              # AC-1: draft deleted
        self.assertIn("restored", out)                                                     # AC-1: both printed
        self.assertIn(MUL, out)
        self.assertIn(SPACED, out)
        self.assertIsNone(self.state())
        self.assertEqual(self.tests_status(), "")                                          # AC-1: clean
        code, out = run_cli(["preflight"])
        self.assertEqual(code, EXIT_OK, out)                                               # AC-1: not refused
        self.assertNotIn("not clean", out)


class LeftoversOfAnInterruptedRoundGoToo(ResetCase):
    """AC-2: with `reviewer_running` set, the untracked files on test_file_pattern under
    test_dir go, as the next run would have removed them."""

    def test_killed_round_after_needs_info_loses_pending_and_half_written_files(self):
        self.needs_info_round()
        state = self.state()
        state.reviewer_running = True                  # what a kill mid-session leaves on disk
        state.set_stage(self.rdir(), "review", "killed", EXIT_ERROR)
        self.write(LEFT, "# half written by the killed session\n")
        out = self.reset()
        self.assertFalse(self.exists(MUL))                                                 # AC-2
        self.assertFalse(self.exists(LEFT))
        self.assertIn("removed 2 unfinished test file", out)
        self.assertIn(MUL, out)
        self.assertIn(LEFT, out)
        self.assertIsNone(self.state())
        self.assertEqual(self.tests_status(), "")
        code, out = run_cli(["preflight"])
        self.assertEqual(code, EXIT_OK, out)

    def test_interrupted_round_with_no_pending_list_is_still_cleaned(self):
        state = State()                                # `revali stop` during round 1: no pending list
        state.reviewer_running = True
        state.set_stage(self.rdir(), "stopped", "stopped by user", EXIT_ERROR)
        self.write(LEFT, "# half written\n")
        self.write(SPACED, "# half written\n")
        out = self.reset()
        self.assertFalse(self.exists(LEFT))                                                # AC-2
        self.assertFalse(self.exists(SPACED))
        self.assertIn(LEFT, out)
        self.assertIn(SPACED, out)
        self.assertIn("state removed", out)
        self.assertIsNone(self.state())
        self.assertEqual(self.tests_status(), "")


class NothingToCleanTouchesNothing(ResetCase):
    """AC-3: no pending files, no interrupted round: an untracked test file of the author's
    survives, and the output does not claim a removal."""

    def test_after_a_changes_requested_round(self):
        self.changes_requested_round()
        self.write(MINE, "# the author's draft\n")
        out = self.reset()
        self.assertTrue(self.exists(MINE))                                                 # AC-3
        self.assertEqual(self.read(MINE), "# the author's draft\n")
        self.assertNotIn("unfinished test file", out)
        self.assertNotIn("restored", out)
        self.assertIn("state removed", out)
        self.assertIsNone(self.state())

    def test_after_a_needs_info_round_that_wrote_nothing(self):
        self.needs_info_round({})
        self.assertEqual(self.state().pending_test_files, [])
        self.write(MINE, "# the author's draft\n")
        out = self.reset()
        self.assertTrue(self.exists(MINE))                                                 # AC-3
        self.assertNotIn("unfinished test file", out)
        self.assertIn("state removed", out)

    def test_without_any_state(self):
        self.assertIsNone(self.state())
        self.write(MINE, "# the author's draft\n")
        out = self.reset()
        self.assertTrue(self.exists(MINE))                                                 # AC-3
        self.assertIn("no state to remove", out)
        self.assertNotIn("unfinished test file", out)


class WithoutALoadableProject(ResetCase):
    """AC-4: the cleanup cannot run (config or change.md does not load); reset still removes
    the state, prints the pending paths and tells the author to delete them by hand."""

    def test_draft_change_md(self):
        self.needs_info_round()
        doc = self.read(self.change_md())
        self.write(self.change_md(), doc.replace("author_model: fixture\n", "author_model: fixture\nstatus: draft\n", 1))
        out = self.reset()
        self.assertIsNone(self.state())                                                    # AC-4: state gone
        self.assertIn("by hand", out)                                                      # AC-4: told
        self.assertIn(MUL, out)                                                            # AC-4: path printed
        self.assertTrue(self.exists(MUL))                                                  # not touched
        self.assertEqual(self.read(MUL), TEST_REVIEW_MUL)

    def test_missing_change_md(self):
        self.needs_info_round({MUL: TEST_REVIEW_MUL, SPACED: SPACED_TEXT})
        os.remove(self.change_md())
        out = self.reset()
        self.assertIsNone(self.state())                                                    # AC-4
        self.assertIn("by hand", out)
        self.assertIn(MUL, out)
        self.assertIn(SPACED, out)
        self.assertTrue(self.exists(MUL))
        self.assertTrue(self.exists(SPACED))

    def test_broken_project_config(self):
        self.needs_info_round()
        self.write("revali.toml", self.read("revali.toml") + "\n[project\nbroken = \n")
        out = self.reset()
        self.assertIsNone(self.state())                                                    # AC-4
        self.assertIn("by hand", out)
        self.assertIn(MUL, out)
        self.assertTrue(self.exists(MUL))


class OnlyThePendingListGoesWhenNothingWasInterrupted(ResetCase):
    """Round 1 F1 / AC-1: with pending files and no interrupted round the reviewer's files
    are known exactly, so `reset` deletes 'the untracked ones' of the pending list and an
    author's own untracked file on test_file_pattern survives, unlogged."""

    def test_authors_draft_next_to_a_pending_file_survives(self):
        self.needs_info_round({MUL: TEST_REVIEW_MUL, SPACED: SPACED_TEXT})
        self.write(MINE, "# the author's draft\n")
        out = self.reset()
        self.assertFalse(self.exists(MUL))                                                 # pending: deleted
        self.assertFalse(self.exists(SPACED))
        self.assertTrue(self.exists(MINE))                                                 # F1: not pending, kept
        self.assertEqual(self.read(MINE), "# the author's draft\n")
        self.assertNotIn(MINE, out)
        self.assertIn("removed 2 unfinished test file", out)
        self.assertIsNone(self.state())
        self.assertEqual(self.tests_status(), "?? " + MINE)                                # only the author's file left

    def test_authors_draft_next_to_a_restored_tracked_pending_file_survives(self):
        self.changes_requested_round()
        committed = self.read(MUL)
        self.write("src/calc.py", self.read("src/calc.py") + "\n# negatives handled\n")
        self.commit_all("fix: handle negatives")
        self.needs_info_round({MUL: TEST_REVIEW_MUL + "\n# updated by round 2\n"})
        self.assertEqual(self.state().pending_test_files, [MUL])
        self.write(MINE, "# the author's draft\n")
        out = self.reset()
        self.assertEqual(self.read(MUL), committed)                                        # restored
        self.assertTrue(self.exists(MINE))                                                 # F1: kept
        self.assertNotIn("unfinished test file", out)                                      # nothing deleted
        self.assertIn("restored", out)
        self.assertIsNone(self.state())

    def test_interrupted_round_still_sweeps_the_whole_pattern(self):
        """AC-2 keeps its documented behaviour: after a kill the session's files are unknown,
        so every untracked file on the pattern goes, as the next `run` would have done."""
        self.needs_info_round()
        state = self.state()
        state.reviewer_running = True
        state.set_stage(self.rdir(), "review", "killed", EXIT_ERROR)
        self.write(LEFT, "# half written by the killed session\n")
        out = self.reset()
        self.assertFalse(self.exists(MUL))
        self.assertFalse(self.exists(LEFT))                                                # AC-2: not pending, swept
        self.assertIn(LEFT, out)
        self.assertEqual(self.tests_status(), "")


class AFailingCleanupDoesNotEscapeReset(ResetCase):
    """Round 1 F2 / AC-4: a `Stop` from the HEAD restore or a `GitError` from the status call
    is reported like an unloadable project: the pending paths are printed with the by-hand
    advice, no traceback, and the state is still removed."""

    def _tracked_pending_file(self):
        self.changes_requested_round()
        self.write("src/calc.py", self.read("src/calc.py") + "\n# negatives handled\n")
        self.commit_all("fix: handle negatives")
        self.needs_info_round({MUL: TEST_REVIEW_MUL + "\n# updated by round 2\n", SPACED: SPACED_TEXT})
        self.assertEqual(sorted(self.state().pending_test_files), sorted([MUL, SPACED]))

    def test_stop_from_the_head_restore(self):
        from revali.preflight import Stop
        self._tracked_pending_file()
        boom = Stop(EXIT_ERROR, "could not restore %s from HEAD: git checkout failed" % MUL)
        with mock.patch("revali.review._restore_from_head", side_effect=boom):
            code, out = run_cli(["reset"])
        self.assertEqual(code, EXIT_OK, out)                                               # F2: reset finishes
        self.assertNotIn("Traceback", out)
        self.assertIn("by hand", out)                                                      # AC-4 wording
        self.assertIn("git checkout failed", out)                                          # the reason
        self.assertIn(MUL, out)                                                            # both pending paths
        self.assertIn(SPACED, out)
        self.assertIn("state removed", out)
        self.assertIsNone(self.state())                                                    # state gone
        self.assertIn("updated by round 2", self.read(MUL))                                # left as it was

    def test_git_error_from_the_status_call(self):
        from revali.gitops import GitError
        self.needs_info_round({MUL: TEST_REVIEW_MUL, SPACED: SPACED_TEXT})
        with mock.patch("revali.gitops.dirty_paths", side_effect=GitError("git status: exit 128")):
            code, out = run_cli(["reset"])
        self.assertEqual(code, EXIT_OK, out)                                               # F2
        self.assertNotIn("Traceback", out)
        self.assertIn("by hand", out)
        self.assertIn("git status: exit 128", out)
        self.assertIn(MUL, out)
        self.assertIn(SPACED, out)
        self.assertIsNone(self.state())
        self.assertTrue(self.exists(MUL))                                                  # nothing deleted
        self.assertTrue(self.exists(SPACED))

    def test_interrupted_round_with_no_pending_list_names_the_pattern(self):
        from revali.gitops import GitError
        state = State()
        state.reviewer_running = True
        state.set_stage(self.rdir(), "stopped", "stopped by user", EXIT_ERROR)
        self.write(LEFT, "# half written\n")
        with mock.patch("revali.gitops.dirty_paths", side_effect=GitError("git status: exit 128")):
            code, out = run_cli(["reset"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("by hand", out)
        self.assertIn("test_file_pattern", out)                                            # no paths to name
        self.assertIsNone(self.state())
        self.assertTrue(self.exists(LEFT))


class ReadmeDescribesIt(unittest.TestCase):
    def test_reset_and_undeletable_leftovers_are_documented(self):
        with open(os.path.join(ROOT, "docs", "side-effects.md"), "r", encoding="utf-8") as fh:
            text = fh.read()
        section = text.split("# What revali does to your repository", 1)[1].split("\n## ", 1)[0]
        self.assertIn("`revali reset`", section)                                           # AC-6: reset
        self.assertIn("cannot be deleted", section)                                        # AC-6: stuck leftover
        self.assertIn("tolerated", section)
        self.assertIn("by hand", section)


if __name__ == "__main__":
    unittest.main()
