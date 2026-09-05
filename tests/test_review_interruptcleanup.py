"""AC-4..AC-6 of fix/guard-followups: the run after an interrupted one (`revali stop`,
Ctrl-C, a killed process) deletes only the untracked files on test_file_pattern under
test_dir, names them, and goes on; a run that ended with a verdict, a finished dry run,
a first run, `revali preflight` and `run --dry-run` never delete; the state file keeps
`reviewer_running: true` from the moment a session starts until a cleanup has run;
README describes the cleanup and the restore source."""

import json
import os
import subprocess
import sys
import time
import unittest

from revali import EXIT_ACTION, EXIT_ERROR, EXIT_HUMAN, EXIT_OK
from revali.config import paths_for
from revali.state import State
from tests.helpers import (
    CLAUDE_STUB,
    ROOT,
    TEST_REVIEW_MUL,
    RepoCase,
    _quote,
    claude_entry,
    git,
    run_cli,
)

LEFTOVER = "tests/test_review_left.py"
LEFTOVER_SPACED = "tests/test_review_my topic.py"
LEFTOVER_NON_ASCII = "tests/test_review_中文.py"

# A stand-in for `claude` that first copies the branch's state.json to a snapshot (so the
# test can see what the state file said while the session was running), then either
# behaves like the normal stub or writes a half-finished test file and hangs until killed.
WRAPPER = """import runpy, shutil, sys, time
shutil.copyfile(%(state)r, %(snapshot)r)
if %(hang)r:
    with open(%(half)r, "w", encoding="utf-8", newline="\\n") as fh:
        fh.write("# half written by the reviewer\\n")
    time.sleep(120)
sys.argv = [%(stub)r] + sys.argv[1:]
runpy.run_path(%(stub)r, run_name="__main__")
"""


def error_line(out):
    return next((line for line in out.splitlines() if line.startswith("ERROR:")), "")


class InterruptedCase(RepoCase):
    def previous_run(self, stage, exit_code=EXIT_ERROR):
        """Leave the state file a previous run would have left: `revali stop` writes
        `stopped`; Ctrl-C or a kill leaves the stage the run was in, with no lock.
        A session that was cut short also leaves `reviewer_running` set (round 1, F1)."""
        state = State()
        state.reviewer_running = stage in ("stopped", "review")
        state.set_stage(self.rdir(), stage, "previous run at %s" % stage, exit_code)

    def log_text(self):
        path = os.path.join(self.rdir(), paths_for(self.repo).logs_dir, "revali.log")
        with open(path, "r", encoding="utf-8", newline="") as fh:
            return fh.read()

    def state_json(self):
        with open(State.path(self.rdir()), "r", encoding="utf-8") as fh:
            return json.load(fh)

    def wrap_claude(self, hang=False):
        """Point REVALI_CLAUDE_CMD at WRAPPER; returns the snapshot path."""
        snapshot = os.path.join(self.tmp, "state-during-session.json")
        script = os.path.join(self.tmp, "claude wrapper.py")
        with open(script, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(
                WRAPPER
                % {
                    "state": State.path(self.rdir()),
                    "snapshot": snapshot,
                    "hang": hang,
                    "half": os.path.join(self.repo, LEFTOVER),
                    "stub": CLAUDE_STUB,
                }
            )
        os.environ["REVALI_CLAUDE_CMD"] = "%s %s" % (_quote(sys.executable), _quote(script))
        return snapshot

    def wait_for(self, relpath, seconds=60):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if self.exists(relpath):
                return
            time.sleep(0.1)
        self.fail("%s did not appear within %ds" % (relpath, seconds))


class CleanupAfterInterruption(InterruptedCase):
    def test_after_stop_leftovers_are_removed_named_and_the_run_proceeds(self):
        self.previous_run("stopped")
        self.write(LEFTOVER, "# half written\n")
        self.write(LEFTOVER_SPACED, "# half written\n")
        self.write(LEFTOVER_NON_ASCII, "# half written\n")
        self.claude(claude_entry())
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)  # AC-4: proceeds
        for path in (LEFTOVER, LEFTOVER_SPACED, LEFTOVER_NON_ASCII):
            self.assertFalse(self.exists(path), path)  # AC-4: deleted
            self.assertIn(path, out)  # AC-4: named in output
            self.assertIn(path, self.log_text())  # AC-4: named in the log
        self.assertNotIn("not clean", out)
        # Round 2, F2: the cleanup runs during preflight of a run that has not reached the
        # review stage, so its line carries the `run` label and precedes every `review:` line.
        log = self.log_text()
        cleanup = [line for line in log.splitlines() if "removed 3 unfinished test file(s)" in line]
        self.assertEqual(len(cleanup), 1, log)
        self.assertIn("] run: removed", cleanup[0])
        self.assertIn("the interrupted run left behind", cleanup[0])
        self.assertNotIn("] review: removed", log)
        self.assertLess(log.index(cleanup[0]), log.index("] review:"))
        state = State.load(self.rdir())
        self.assertEqual(state.stage, "ready_to_merge")
        self.assertEqual(state.test_files, ["tests/test_review_mul.py"])

    def test_after_a_kill_in_the_review_stage(self):
        self.previous_run("review")
        self.write(LEFTOVER, "# half written\n")
        self.claude(claude_entry())
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)  # AC-4
        self.assertFalse(self.exists(LEFTOVER))
        self.assertIn(LEFTOVER, out)

    def test_after_a_kill_in_the_pr_stage(self):
        # No session had started, so nothing can be a leftover: the file is the author's
        # and the tree is simply dirty (round 1, F1: the rule follows the flag, not the stage).
        self.previous_run("pr")
        self.write(LEFTOVER, "# half written\n")
        self.claude(claude_entry())
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR, out)  # AC-5
        self.assertIn("not clean", out)
        self.assertTrue(self.exists(LEFTOVER))

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
        self.assertEqual(code, EXIT_ERROR, out)  # AC-4: still dirty
        self.assertIn("not clean", error_line(out))
        self.assertFalse(self.exists(LEFTOVER))  # the leftover went
        self.assertTrue(self.exists("tests/scratch.py"))  # everything else stays
        self.assertTrue(self.exists("tests/helper_review_x.py"))
        self.assertTrue(self.exists("src/test_review_outside.py"))
        self.assertTrue(self.read("tests/test_calc.py").endswith("# tracked, modified\n"))
        self.assertEqual(
            self.read("tests/test_review_old.py"), "# tracked, on the pattern, modified\n"
        )
        for kept in (
            "tests/scratch.py",
            "tests/helper_review_x.py",
            "src/test_review_outside.py",
            "tests/test_calc.py",
            "tests/test_review_old.py",
        ):
            self.assertIn(kept, error_line(out) + out.split("not clean", 1)[1])
        self.assertEqual(self.fake_calls("claude"), [])  # never reached the reviewer

    def test_untouched_when_the_tree_was_dirty_only_outside_test_dir(self):
        self.previous_run("stopped")
        self.write("src/extra.py", "# outside test_dir\n")
        self.claude(claude_entry())
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR, out)  # AC-4
        self.assertIn("not clean", error_line(out))
        self.assertTrue(self.exists("src/extra.py"))
        self.assertNotIn("removed", out)


class NoCleanupWithoutInterruption(InterruptedCase):
    def test_a_run_that_ended_with_a_verdict_keeps_the_file(self):
        for stage, exit_code in (
            ("needs_action", EXIT_ACTION),
            ("ready_to_merge", EXIT_OK),
            ("error", EXIT_ERROR),
            ("needs_human", EXIT_HUMAN),
        ):
            with self.subTest(stage=stage):
                self.previous_run(stage, exit_code)
                self.write(LEFTOVER, "# kept on purpose\n")
                self.claude(claude_entry())
                code, out = run_cli(["run", "--foreground"])
                self.assertEqual(code, EXIT_ERROR, out)  # AC-5
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
        self.assertEqual(code, EXIT_ERROR, out)  # AC-5
        self.assertIn("not clean", error_line(out))
        self.assertTrue(self.exists(LEFTOVER))

    def test_preflight_command_never_deletes(self):
        self.previous_run("stopped")
        self.write(LEFTOVER, "# half written\n")
        code, out = run_cli(["preflight"])
        self.assertEqual(code, EXIT_ERROR, out)  # AC-4 / README
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


class FlagFollowsTheSession(InterruptedCase):
    """Round 2: the state file, not the stage, says whether a session was cut short. The flag
    goes on disk before the session starts, stays through `stop`, a dry run and a failed
    preflight, and is cleared by any round that finished or discarded its own files."""

    def test_state_file_says_reviewer_running_during_the_session_and_not_after(self):
        snapshot = self.wrap_claude()
        self.claude(claude_entry())
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        with open(snapshot, "r", encoding="utf-8") as fh:
            during = json.load(fh)
        self.assertIs(during.get("reviewer_running"), True)  # AC-4: set before the session
        self.assertEqual(during.get("stage"), "review")
        self.assertIs(
            self.state_json().get("reviewer_running"), False
        )  # AC-5: cleared by the verdict
        self.assertFalse(State.load(self.rdir()).reviewer_running)

    def test_stop_during_a_session_then_the_next_run_removes_what_it_left(self):
        self.wrap_claude(hang=True)
        self.claude(claude_entry())
        extra = {"start_new_session": True} if os.name != "nt" else {}
        child = subprocess.Popen(
            [sys.executable, os.path.join(ROOT, "revali.py"), "run", "--foreground"],
            cwd=self.repo,
            env=dict(os.environ),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **extra,
        )
        self.addCleanup(lambda: child.poll() is None and child.kill())
        self.wait_for(LEFTOVER)  # the session is running and has written its half file
        code, out = run_cli(["stop"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("stopped pid", out)
        child.wait(timeout=60)
        loaded = State.load(self.rdir())
        self.assertEqual(loaded.stage, "stopped")
        self.assertTrue(loaded.reviewer_running)  # AC-4: survives `stop`
        self.assertTrue(self.exists(LEFTOVER))  # `stop` itself deletes nothing
        os.environ["REVALI_CLAUDE_CMD"] = "%s %s" % (_quote(sys.executable), _quote(CLAUDE_STUB))
        self.claude(claude_entry())
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)  # AC-4: cleaned, then proceeds
        self.assertFalse(self.exists(LEFTOVER))
        self.assertIn(LEFTOVER, out)
        self.assertIn("interrupted", out)
        self.assertFalse(State.load(self.rdir()).reviewer_running)
        self.assertEqual(State.load(self.rdir()).stage, "ready_to_merge")

    def test_dry_run_after_an_interruption_neither_deletes_nor_forgets(self):
        self.previous_run("stopped")
        self.write(LEFTOVER, "# half written\n")
        self.claude(claude_entry())
        code, out = run_cli(["run", "--dry-run"])
        self.assertEqual(code, EXIT_ERROR, out)  # AC-5: a dry run never deletes
        self.assertIn("not clean", error_line(out))
        self.assertTrue(self.exists(LEFTOVER))
        self.assertNotIn("removed", out)
        self.assertTrue(State.load(self.rdir()).reviewer_running)  # AC-4: the flag is kept
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)  # AC-4: the next real run cleans
        self.assertFalse(self.exists(LEFTOVER))
        self.assertIn(LEFTOVER, out)

    def test_a_finished_dry_run_is_not_an_interrupted_run(self):
        self.claude(claude_entry())
        code, out = run_cli(["run", "--dry-run"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertFalse(State.load(self.rdir()).reviewer_running)
        self.write("tests/test_review_mine.py", "# the author's own file\n")
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR, out)  # AC-5 (round 1, F1)
        self.assertIn("not clean", error_line(out))
        self.assertTrue(self.exists("tests/test_review_mine.py"))
        self.assertNotIn("removed", out)
        self.assertEqual(self.fake_calls("claude"), [])

    def test_a_preflight_failure_before_the_tree_check_keeps_the_pending_cleanup(self):
        self.previous_run("review")
        self.write(LEFTOVER, "# half written\n")
        with open(self.change_md(), "r", encoding="utf-8", newline="") as fh:
            doc = fh.read()
        with open(self.change_md(), "w", encoding="utf-8", newline="\n") as fh:
            fh.write(
                doc.replace("author_model: fixture\n", "author_model: fixture\nstatus: draft\n", 1)
            )
        self.claude(claude_entry())
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertIn("draft", out)
        self.assertTrue(self.exists(LEFTOVER))  # stopped before the tree check
        loaded = State.load(self.rdir())
        self.assertEqual(loaded.stage, "error")
        self.assertTrue(loaded.reviewer_running)  # AC-4: whatever the stage says
        with open(self.change_md(), "w", encoding="utf-8", newline="\n") as fh:
            fh.write(doc)
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)  # AC-4: still cleaned
        self.assertFalse(self.exists(LEFTOVER))
        self.assertIn(LEFTOVER, out)

    def test_a_session_failure_that_discarded_its_files_clears_the_flag(self):
        entry = claude_entry()
        entry["is_error"] = True
        entry["exit"] = 1
        self.claude(entry)
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertFalse(self.exists("tests/test_review_mul.py"))  # discarded by the round
        self.assertFalse(State.load(self.rdir()).reviewer_running)  # AC-5
        self.write("tests/test_review_mine.py", "# the author's own file\n")
        self.claude(claude_entry())
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR, out)  # AC-5: nothing to clean
        self.assertIn("not clean", error_line(out))
        self.assertTrue(self.exists("tests/test_review_mine.py"))
        self.assertNotIn("removed", out)

    def test_a_needs_info_round_keeps_its_files_and_clears_the_flag(self):
        asking = claude_entry(
            {
                "verdict": "NEEDS_INFO",
                "summary": "unclear",
                "questions": ["Which integers?"],
                "findings": [],
                "previous_findings": [],
                "scope_mismatch": [],
                "dependencies_changed": [],
                "test_changes": [],
                "tests": [],
                "not_testable": [],
                "suggestions": [],
            }
        )
        self.claude(asking)
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ACTION, out)
        self.assertTrue(self.exists("tests/test_review_mul.py"))  # kept on purpose
        self.assertFalse(State.load(self.rdir()).reviewer_running)  # AC-5: the round finished
        self.claude(claude_entry())
        code, out = run_cli(["run", "--foreground"])
        # Since fix/needs-info-files the pending file is tolerated and committed by this round
        # instead of stopping it; still not deleted by the interruption cleanup.
        self.assertEqual(code, EXIT_OK, out)  # AC-5: not deleted
        self.assertTrue(self.exists("tests/test_review_mul.py"))
        self.assertNotIn("removed", out)


class ReadmeDescribesIt(unittest.TestCase):
    def test_section_mentions_restore_source_and_interrupted_cleanup(self):
        with open(os.path.join(ROOT, "docs", "side-effects.md"), "r", encoding="utf-8") as fh:
            text = fh.read()
        section = text.split("# What revali does to your repository", 1)[1].split("\n## ", 1)[0]
        self.assertNotIn("do not clean up", section)  # AC-6
        self.assertNotIn("may leave such files for you to delete", section)
        self.assertIn("restored from HEAD", section)
        self.assertIn("git checkout HEAD", section)
        self.assertIn("interrupted", section)
        self.assertIn("revali stop", section)
        self.assertIn("Ctrl-C", section)
        self.assertIn("revali preflight", section)
        self.assertIn("run --dry-run", section)  # AC-5 as documented


if __name__ == "__main__":
    unittest.main()
