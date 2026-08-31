"""AC-4..AC-6 of fix/guard-followups: the run after an interrupted one (`revali stop`,
Ctrl-C, a killed process) deletes only the untracked files on test_file_pattern under
test_dir, names them, and goes on; a run that ended with a verdict, a first run, and
`revali preflight` never delete; README describes the cleanup and the restore source."""
import os
import unittest

from tests.helpers import ROOT, RepoCase, TEST_REVIEW_MUL, claude_entry, git, run_cli
from revali import EXIT_ACTION, EXIT_ERROR, EXIT_HUMAN, EXIT_OK
from revali.config import paths_for
from revali.state import State

LEFTOVER = "tests/test_review_left.py"
LEFTOVER_SPACED = "tests/test_review_my topic.py"
LEFTOVER_NON_ASCII = "tests/test_review_中文.py"


def error_line(out):
    return next((l for l in out.splitlines() if l.startswith("ERROR:")), "")


class InterruptedCase(RepoCase):
    def previous_run(self, stage, exit_code=EXIT_ERROR):
        """Leave the state file a previous run would have left: `revali stop` writes
        `stopped`; Ctrl-C or a kill leaves the stage the run was in, with no lock."""
        State().set_stage(self.rdir(), stage, "previous run at %s" % stage, exit_code)

    def log_text(self):
        path = os.path.join(self.rdir(), paths_for(self.repo).logs_dir, "revali.log")
        with open(path, "r", encoding="utf-8", newline="") as fh:
            return fh.read()


class CleanupAfterInterruption(InterruptedCase):
    def test_after_stop_leftovers_are_removed_named_and_the_run_proceeds(self):
        self.previous_run("stopped")
        self.write(LEFTOVER, "# half written\n")
        self.write(LEFTOVER_SPACED, "# half written\n")
        self.write(LEFTOVER_NON_ASCII, "# half written\n")
        self.claude(claude_entry())
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)                                               # AC-4: proceeds
        for path in (LEFTOVER, LEFTOVER_SPACED, LEFTOVER_NON_ASCII):
            self.assertFalse(self.exists(path), path)                                      # AC-4: deleted
            self.assertIn(path, out)                                                       # AC-4: named in output
            self.assertIn(path, self.log_text())                                           # AC-4: named in the log
        self.assertNotIn("not clean", out)
        state = State.load(self.rdir())
        self.assertEqual(state.stage, "ready_to_merge")
        self.assertEqual(state.test_files, ["tests/test_review_mul.py"])

    def test_after_a_kill_in_the_review_stage(self):
        self.previous_run("review")
        self.write(LEFTOVER, "# half written\n")
        self.claude(claude_entry())
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)                                               # AC-4
        self.assertFalse(self.exists(LEFTOVER))
        self.assertIn(LEFTOVER, out)

    def test_after_a_kill_in_the_pr_stage(self):
        self.previous_run("pr")
        self.write(LEFTOVER, "# half written\n")
        self.claude(claude_entry())
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)                                               # AC-4
        self.assertFalse(self.exists(LEFTOVER))

    def test_only_pattern_files_under_test_dir_go_and_the_rest_still_stops_the_run(self):
        self.write("tests/test_review_old.py", TEST_REVIEW_MUL.replace("MulTests", "OldTests"))
        self.commit_all("a tracked file on the pattern")
        self.previous_run("stopped")
        self.write(LEFTOVER, "# half written\n")
        self.write("tests/scratch.py", "# untracked, not on the pattern\n")
        self.write("tests/helper_review_x.py", "# untracked, not on the pattern either\n")
        self.write("src/test_review_outside.py", "# on the pattern, outside test_dir\n")
        self.write("tests/test_calc.py", self.read("tests/test_calc.py") + "# tracked, modified\n")
        self.write("tests/test_review_old.py", "# tracked, on the pattern, modified\n")
        self.claude(claude_entry())
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR, out)                                            # AC-4: still dirty
        self.assertIn("not clean", error_line(out))
        self.assertFalse(self.exists(LEFTOVER))                                            # the leftover went
        self.assertTrue(self.exists("tests/scratch.py"))                                   # everything else stays
        self.assertTrue(self.exists("tests/helper_review_x.py"))
        self.assertTrue(self.exists("src/test_review_outside.py"))
        self.assertTrue(self.read("tests/test_calc.py").endswith("# tracked, modified\n"))
        self.assertEqual(self.read("tests/test_review_old.py"), "# tracked, on the pattern, modified\n")
        for kept in ("tests/scratch.py", "tests/helper_review_x.py", "src/test_review_outside.py",
                     "tests/test_calc.py", "tests/test_review_old.py"):
            self.assertIn(kept, error_line(out) + out.split("not clean", 1)[1])
        self.assertEqual(self.fake_calls("claude"), [])                                    # never reached the reviewer

    def test_untouched_when_the_tree_was_dirty_only_outside_test_dir(self):
        self.previous_run("stopped")
        self.write("src/extra.py", "# outside test_dir\n")
        self.claude(claude_entry())
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR, out)                                            # AC-4
        self.assertIn("not clean", error_line(out))
        self.assertTrue(self.exists("src/extra.py"))
        self.assertNotIn("removed", out)


class NoCleanupWithoutInterruption(InterruptedCase):
    def test_a_run_that_ended_with_a_verdict_keeps_the_file(self):
        for stage, exit_code in (("needs_action", EXIT_ACTION), ("ready_to_merge", EXIT_OK),
                                 ("error", EXIT_ERROR), ("needs_human", EXIT_HUMAN)):
            with self.subTest(stage=stage):
                self.previous_run(stage, exit_code)
                self.write(LEFTOVER, "# kept on purpose\n")
                self.claude(claude_entry())
                code, out = run_cli(["run", "--foreground"])
                self.assertEqual(code, EXIT_ERROR, out)                                    # AC-5
                self.assertIn("not clean", error_line(out))
                self.assertIn(LEFTOVER, out)
                self.assertTrue(self.exists(LEFTOVER))
                self.assertNotIn("removed", out)
                self.assertEqual(self.fake_calls("claude"), [])

    def test_first_run_on_a_branch_keeps_the_file(self):
        self.assertFalse(os.path.isfile(State.path(self.rdir())))
        self.write(LEFTOVER, "# the author's own file\n")
        self.claude(claude_entry())
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR, out)                                            # AC-5
        self.assertIn("not clean", error_line(out))
        self.assertTrue(self.exists(LEFTOVER))

    def test_preflight_command_never_deletes(self):
        self.previous_run("stopped")
        self.write(LEFTOVER, "# half written\n")
        code, out = run_cli(["preflight"])
        self.assertEqual(code, EXIT_ERROR, out)                                            # AC-4 / README
        self.assertIn("not clean", error_line(out))
        self.assertTrue(self.exists(LEFTOVER))
        self.assertNotIn("removed", out)

    def test_stop_then_leftover_kept_when_the_reviewer_file_was_committed(self):
        # A run interrupted after its tests were committed leaves nothing untracked; a
        # tracked file on the pattern is not a leftover and must survive untouched.
        self.write("tests/test_review_mul.py", TEST_REVIEW_MUL)
        self.commit_all("test: review tests (round 1)")
        self.previous_run("validate")
        self.claude(claude_entry(write_tests=False))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertTrue(self.exists("tests/test_review_mul.py"))
        self.assertNotIn("removed", out)
        self.assertEqual(git(["status", "--porcelain"], self.repo).strip(), "")


class ReadmeDescribesIt(unittest.TestCase):
    def test_section_mentions_restore_source_and_interrupted_cleanup(self):
        with open(os.path.join(ROOT, "README.md"), "r", encoding="utf-8") as fh:
            text = fh.read()
        section = text.split("## What revali does to your repository", 1)[1].split("\n## ", 1)[0]
        self.assertNotIn("do not clean up", section)                                       # AC-6
        self.assertNotIn("may leave such files for you to delete", section)
        self.assertIn("restored from HEAD", section)
        self.assertIn("git checkout HEAD", section)
        self.assertIn("interrupted", section)
        self.assertIn("revali stop", section)
        self.assertIn("Ctrl-C", section)
        self.assertIn("revali preflight", section)


if __name__ == "__main__":
    unittest.main()
