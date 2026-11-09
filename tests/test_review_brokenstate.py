"""Acceptance tests for feature/state-load-normalize, the command half (AC-1, AC-3, AC-4):
what `run`, `wait`, `status`, `merge`, `reset` and `stop` do with a state.json that holds a
null field, or that State cannot use at all."""

import os
import subprocess
import sys
from unittest import mock

from revali import EXIT_ERROR, EXIT_OK
from revali.state import State, acquire_lock, acquire_tree_lock, lock_path, tree_lock_path
from tests.helpers import RepoCase, claude_entry, run_cli

DEAD_PID = 999999999

# what the file holds -> the field its error line names ("" when no field is involved)
BROKEN = {
    '{"stage": "review", "fixes": "1"}': "fixes",
    '{"stage": "review", "pr_number": true}': "pr_number",
    '{"stage": "review", "pending_test_files": ["tests/a.py", null]}': "pending_test_files",
    "[1, 2]": "",
    '{"stage": "review",': "",
    # round 1, F1: an int no float can hold, in a float field
    '{"stage": "review", "cost_usd": 1%s}' % ("0" * 400): "cost_usd",
}


class StateCase(RepoCase):
    def put(self, text):
        os.makedirs(self.rdir(), exist_ok=True)
        with open(State.path(self.rdir()), "w", encoding="utf-8", newline="") as fh:
            fh.write(text)

    def state_text(self):
        with open(State.path(self.rdir()), "r", encoding="utf-8", newline="") as fh:
            return fh.read()

    def tree_lock(self):
        return tree_lock_path(self.repo, ".revali")

    def assert_no_locks(self):
        self.assertFalse(os.path.exists(lock_path(self.rdir())))
        self.assertFalse(os.path.exists(self.tree_lock()))

    def assert_reported(self, code, out, field=""):
        """Exit 1 and an ERROR line naming the file (and the field) with the reset hint."""
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertNotIn("Traceback", out)
        lines = [
            line
            for line in out.splitlines()
            if line.startswith("ERROR: ") and "state.json" in line and "revali reset" in line
        ]
        self.assertTrue(lines, out)
        if field:
            self.assertIn(field, lines[0])

    def live_child(self):
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(120)"], start_new_session=True
        )  # its own group: stop kills the process group on POSIX
        self.addCleanup(lambda: child.poll() is None and child.kill())
        return child


class NullFieldsRun(StateCase):
    """AC-1: a state whose lists and counters are null runs like a state without them."""

    def test_foreground_run_reaches_ready_to_merge(self):
        self.put(
            '{"pending_test_files": null, "placed_test_files": null, "rounds": null, '
            '"validations": null, "test_files": null, "fixes": null, "cost_usd": null, '
            '"reviewer_running": null, "message": null}\n'
        )
        self.claude(claude_entry())
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertNotIn("Traceback", out)
        state = State.load(self.rdir())
        self.assertEqual(state.stage, "ready_to_merge")
        self.assertEqual(state.pending_test_files, [])
        self.assertEqual(len(state.rounds), 1)

    def test_detached_run_starts(self):
        self.put('{"stage": "needs_action", "pending_test_files": null, "rounds": null}\n')
        with mock.patch("revali.pipeline.spawn_detached", return_value=DEAD_PID) as spawn:
            code, out = run_cli(["run"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertNotIn("Traceback", out)
        spawn.assert_called_once()


class CommandsReportIt(StateCase):
    """AC-3"""

    def test_wait_status_and_merge(self):
        for argv in (["wait", "--timeout", "1s"], ["status"], ["merge"]):
            for text, field in BROKEN.items():
                with self.subTest(command=argv[0], state=text):
                    self.put(text)
                    code, out = run_cli(argv)
                    self.assert_reported(code, out, field)
                    self.assertEqual(self.state_text(), text)  # reported, not repaired

    def test_detached_run_refuses_before_it_spawns(self):
        for text, field in BROKEN.items():
            with self.subTest(state=text):
                self.put(text)
                with mock.patch("revali.pipeline.spawn_detached", return_value=DEAD_PID) as spawn:
                    code, out = run_cli(["run"])
                self.assert_reported(code, out, field)
                spawn.assert_not_called()
                self.assertNotIn("started revali run", out)
                self.assert_no_locks()
                self.assertEqual(self.state_text(), text)

    def test_foreground_run_leaves_no_lock(self):
        for text, field in BROKEN.items():
            with self.subTest(state=text):
                self.put(text)
                code, out = run_cli(["run", "--foreground"])
                self.assert_reported(code, out, field)
                self.assert_no_locks()
                self.assertEqual(self.state_text(), text)


class ResetClearsIt(StateCase):
    """AC-4, reset"""

    def test_reset_removes_the_file_and_says_what_it_could_not_do(self):
        for text in BROKEN:
            with self.subTest(state=text):
                self.put(text)
                code, out = run_cli(["reset"])
                self.assertEqual(code, EXIT_OK, out)
                self.assertNotIn("Traceback", out)
                self.assertFalse(os.path.exists(State.path(self.rdir())))
                self.assertIn("test files", out)
                self.assertTrue(os.path.isfile(self.change_md()))  # the review files stay
                code, status = run_cli(["status"])
                self.assertEqual(code, EXIT_OK, status)
                self.assertIn("state: none", status)

    def test_a_file_the_broken_state_listed_is_not_touched(self):
        left = self.write("tests/test_review_left.py", "# a NEEDS_INFO round's draft\n")
        self.put('{"pending_test_files": ["tests/test_review_left.py"], "fixes": "1"}\n')
        code, out = run_cli(["reset"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertFalse(os.path.exists(State.path(self.rdir())))
        self.assertTrue(os.path.isfile(left))

    def test_the_next_run_starts_from_nothing(self):
        self.put('{"stage": "needs_action", "fixes": "1"}\n')
        self.assertEqual(run_cli(["reset"])[0], EXIT_OK)
        self.claude(claude_entry())
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertEqual(State.load(self.rdir()).stage, "ready_to_merge")


class StopStillStops(StateCase):
    """AC-4, stop"""

    def stop_live(self, text, field):
        child = self.live_child()
        self.put(text)
        acquire_lock(self.rdir(), pid=child.pid)
        acquire_tree_lock(self.tree_lock(), "feature/mul", pid=child.pid)
        code, out = run_cli(["stop"])
        self.assert_reported(code, out, field)
        self.assertIn("stopped pid %d" % child.pid, out)
        child.wait(timeout=30)  # killed: this returns instead of timing out
        self.assert_no_locks()
        self.assertEqual(self.state_text(), text)

    def test_a_field_of_the_wrong_type(self):
        self.stop_live('{"stage": "review", "fixes": "1"}', "fixes")

    def test_a_file_that_is_not_json(self):
        self.stop_live('{"stage": "review",', "")

    def test_without_a_live_run(self):
        self.put('{"stage": "review", "fixes": "1"}')
        code, out = run_cli(["stop"])
        self.assert_reported(code, out, "fixes")
        self.assert_no_locks()
