"""Reviewer tests for fix/reset-pending-files, AC-5: a leftover the OS refuses to delete
(an open file on Windows) is kept in, or added to, `pending_test_files` by the round's stop
path and by the cleanup at the start of the next run; the log names it and says the next
run tolerates it; that run's tree check passes and its prompt lists the file for the
reviewer. `os.remove` is patched in-process to refuse one path, which is the only way to
produce an undeletable file portably.
Round 3 adds: `reset` on a stuck file says by hand and never "tolerate" (round 2 F1), the
next preflight is then refused, and the stop path still says "tolerate"."""
import os
import unittest
from unittest import mock

from tests.helpers import RepoCase, TEST_REVIEW_MUL, approve_response, claude_entry, git, run_cli
from revali import EXIT_ACTION, EXIT_ERROR, EXIT_OK
from revali.config import paths_for
from revali.state import State

MUL = "tests/test_review_mul.py"
LEFT = "tests/test_review_left.py"
LEFT_TEXT = TEST_REVIEW_MUL.replace("MulTests", "LeftTests")


def needs_info(files=None):
    data = approve_response(verdict="NEEDS_INFO", questions=["Which integers?"], tests=[])
    if files is None:
        return claude_entry(data)
    entry = claude_entry(data, write_tests=False)
    if files:
        entry["write_files"] = dict(files)
    return entry


def approving(files):
    tests = [{"path": p, "purpose": "acceptance", "covers": ["AC-1", "AC-2"], "expected": "per AC"}
             for p in files]
    entry = claude_entry(approve_response(tests=tests), write_tests=False)
    entry["write_files"] = dict(files)
    return entry


def refusing(relpath):
    """An os.remove that refuses one repository-relative path the way Windows refuses an
    open file, and deletes everything else as usual."""
    real = os.remove
    tail = "/" + relpath.replace("\\", "/")

    def remove(path, *args, **kwargs):
        if os.path.abspath(path).replace("\\", "/").endswith(tail):
            raise PermissionError(13, "The process cannot access the file because it is being used "
                                      "by another process", path)
        return real(path, *args, **kwargs)
    return remove


class StuckCase(RepoCase):
    def needs_info_round(self, files=None):
        self.claude(needs_info(files))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ACTION, out)
        return out

    def state(self):
        return State.load(self.rdir())

    def status(self):
        return git(["status", "--porcelain"], self.repo).strip()

    def log_text(self):
        path = os.path.join(self.rdir(), paths_for(self.repo).logs_dir, "revali.log")
        with open(path, "r", encoding="utf-8", newline="") as fh:
            return fh.read()

    def last_prompt(self):
        return self.fake_calls("claude")[-1]["prompt"]

    def pending_section(self, prompt):
        self.assertIn("not committed yet", prompt)
        return prompt.split("not committed yet", 1)[1]


class StopPathKeepsTheStuckFile(StuckCase):
    def test_pending_file_that_cannot_be_deleted_stays_tolerated(self):
        self.needs_info_round()
        self.assertEqual(self.state().pending_test_files, [MUL])
        self.claude(claude_entry(is_error=True, exit=1))          # the round stops before its commit
        with mock.patch("os.remove", refusing(MUL)):
            code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertTrue(self.exists(MUL))                                                  # still there
        self.assertIn("could not remove", out)                                             # AC-5: named
        self.assertIn(MUL, out)
        self.assertIn("tolerate", out)                                                     # AC-5: says so
        self.assertNotIn("by hand", out)                                                   # round 1 F3: no contradiction
        log = self.log_text()
        self.assertIn(MUL, log)
        self.assertIn("tolerate", log)
        self.assertNotIn("by hand", log)
        state = self.state()
        self.assertEqual(state.pending_test_files, [MUL])                                  # AC-5: kept
        self.assertFalse(state.reviewer_running)
        code, out = run_cli(["preflight"])
        self.assertEqual(code, EXIT_OK, out)                                               # AC-5: tree check passes
        self.assertNotIn("not clean", out)
        self.claude(approving({MUL: TEST_REVIEW_MUL}))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)                                               # AC-5: not refused
        self.assertNotIn("not clean", out)
        self.assertIn("- " + MUL, self.pending_section(self.last_prompt()))                # AC-5: listed
        state = self.state()
        self.assertEqual(state.pending_test_files, [])                                     # committed by this round
        self.assertEqual(state.test_files, [MUL])
        self.assertEqual(self.status(), "")

    def test_the_rounds_own_new_file_that_cannot_be_deleted_joins_the_list(self):
        self.needs_info_round({})                       # nothing pending from round 1
        self.assertEqual(self.state().pending_test_files, [])
        self.claude(claude_entry(is_error=True, exit=1))          # writes MUL, then fails
        with mock.patch("os.remove", refusing(MUL)):
            code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertTrue(self.exists(MUL))
        self.assertIn(MUL, out)
        self.assertIn("tolerate", out)
        self.assertEqual(self.state().pending_test_files, [MUL])                           # AC-5: added
        code, out = run_cli(["preflight"])
        self.assertEqual(code, EXIT_OK, out)                                               # AC-5: tolerated
        self.claude(approving({MUL: TEST_REVIEW_MUL}))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("- " + MUL, self.pending_section(self.last_prompt()))                # AC-5: listed
        self.assertEqual(self.state().test_files, [MUL])
        self.assertEqual(self.status(), "")

    def test_a_deletable_pending_file_is_still_dropped_next_to_a_stuck_one(self):
        self.needs_info_round({MUL: TEST_REVIEW_MUL, LEFT: LEFT_TEXT})
        self.claude(claude_entry(is_error=True, exit=1, write_tests=False))
        with mock.patch("os.remove", refusing(LEFT)):
            code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertFalse(self.exists(MUL))                                                 # deleted as before
        self.assertTrue(self.exists(LEFT))
        self.assertEqual(self.state().pending_test_files, [LEFT])                          # AC-5: exactly the stuck one
        code, out = run_cli(["preflight"])
        self.assertEqual(code, EXIT_OK, out)


class StartOfRunCleanupKeepsTheStuckFile(StuckCase):
    def test_leftover_of_a_killed_round_that_cannot_be_deleted_is_handed_to_the_reviewer(self):
        self.needs_info_round()
        state = self.state()
        state.reviewer_running = True                  # what a kill mid-session leaves on disk
        state.set_stage(self.rdir(), "review", "killed", EXIT_ERROR)
        self.write(LEFT, "# half written by the killed session\n")
        self.claude(approving({LEFT: LEFT_TEXT}))
        with mock.patch("os.remove", refusing(LEFT)):
            code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)                                               # AC-5: tree check passes
        self.assertNotIn("not clean", out)
        self.assertFalse(self.exists(MUL))                                                 # the deletable one went
        self.assertIn("could not remove", out)                                             # AC-5: named
        self.assertIn(LEFT, out)
        self.assertIn("tolerate", out)
        self.assertNotIn("by hand", out)                                                   # round 1 F3
        self.assertIn("] run:", out)
        self.assertIn("- " + LEFT, self.pending_section(self.last_prompt()))               # AC-5: listed
        self.assertNotIn("- " + MUL, self.last_prompt())
        state = self.state()
        self.assertFalse(state.reviewer_running)
        self.assertEqual(state.pending_test_files, [])                                     # committed by this round
        self.assertEqual(state.test_files, [LEFT])
        self.assertEqual(self.status(), "")

    def test_a_dry_run_after_the_stuck_file_was_recorded_is_not_refused(self):
        self.needs_info_round()
        self.claude(claude_entry(is_error=True, exit=1))
        with mock.patch("os.remove", refusing(MUL)):
            code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertEqual(self.state().pending_test_files, [MUL])
        code, out = run_cli(["run", "--dry-run"])
        self.assertEqual(code, EXIT_OK, out)                                               # AC-5: tolerated
        self.assertTrue(self.exists(MUL))                                                  # a dry run deletes nothing
        self.assertEqual(self.state().pending_test_files, [MUL])


class ResetWithAStuckFile(StuckCase):
    """AC-1 with AC-5's failure: `reset` drops the state, so a file it could not delete has
    no list left to tolerate it; reset names it for the author to delete by hand and still
    removes the state and the deletable pending file."""

    def test_stuck_pending_file_is_named_for_the_author_and_the_state_goes(self):
        self.needs_info_round({MUL: TEST_REVIEW_MUL, LEFT: LEFT_TEXT})
        with mock.patch("os.remove", refusing(LEFT)):
            code, out = run_cli(["reset"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertNotIn("Traceback", out)
        self.assertFalse(self.exists(MUL))                                                 # deletable one went
        self.assertTrue(self.exists(LEFT))
        self.assertIn("could not remove", out)                                             # named
        self.assertIn("by hand", out)                                                      # told what to do
        self.assertIn(LEFT, out.split("by hand", 1)[1])
        self.assertNotIn("tolerate", out)                                                  # round 2 F1: no false promise
        self.assertNotIn(MUL, out.split("by hand", 1)[1])                                  # only the stuck one
        self.assertIn("state removed", out)
        self.assertIsNone(self.state())
        code, out = run_cli(["preflight"])
        self.assertEqual(code, EXIT_ERROR, out)                                            # nothing tolerates it now
        self.assertIn("not clean", out)

    def test_run_path_still_promises_tolerance_after_reset_changed_nothing_there(self):
        """The round 2 fix must not silence the line on the stop path: there the state
        survives and the promise is true."""
        self.needs_info_round()
        self.claude(claude_entry(is_error=True, exit=1))
        with mock.patch("os.remove", refusing(MUL)):
            code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertIn("tolerate", out)                                                     # AC-5: still said here
        self.assertNotIn("by hand", out)
        self.assertEqual(self.state().pending_test_files, [MUL])


if __name__ == "__main__":
    unittest.main()
