"""`revali stop` follow-ups: the help text, a state write that fails, and the history row."""
import os
import subprocess
import sys
import unittest
from unittest import mock

from tests.helpers import RepoCase, run_cli
from revali import EXIT_ERROR, EXIT_OK
from revali.state import State, acquire_lock, lock_path, read_history
from revali.stats import summarise


class StopHelp(unittest.TestCase):
    def test_help_names_both_jobs(self):                                                  # AC-1
        from revali.cli import build_parser
        text = build_parser().format_help()
        self.assertIn("stop", text)
        self.assertIn("died without a result", text)
        self.assertIn("kill the running pipeline", text)


class StopClosesTheRun(RepoCase):
    def _history(self):
        return read_history(os.path.join(self.home, "history.jsonl"))

    def _dead_state(self, **fields):
        st = State(repo="owner/repo", branch="feature/mul", base="main", stage="review",
                   message="reviewer round 1", last_exit=-1, pr_number=7, cost_usd=1.5, fixes=1,
                   rounds=[{"round": 1, "verdict": "CHANGES_REQUESTED"}], **fields)
        st.save(self.rdir())
        return st

    def _live_child(self):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"],
                                 start_new_session=True)   # own group: kill_tree uses killpg on POSIX
        self.addCleanup(lambda: child.poll() is None and child.kill())
        return child

    # --- AC-2: the state write fails ------------------------------------------------

    def test_dead_path_write_failure_is_one_error_line(self):
        self._dead_state()
        with mock.patch.object(State, "set_stage", side_effect=PermissionError("state.json is busy")):
            code, out = run_cli(["stop"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertIn("ERROR: the state file could not be updated (state.json is busy)", out)
        self.assertIn("report the run as dead", out)
        self.assertNotIn("Traceback", out)
        self.assertEqual(out.count("ERROR:"), 1)
        state = State.load(self.rdir())
        self.assertEqual(state.stage, "review")   # untouched: still reads as dead
        self.assertEqual(state.last_exit, -1)
        self.assertEqual(self._history(), [])

    def test_live_path_write_failure_still_kills_and_unlocks(self):
        child = self._live_child()
        self._dead_state()
        acquire_lock(self.rdir(), pid=child.pid)
        with mock.patch.object(State, "set_stage", side_effect=PermissionError("state.json is busy")):
            code, out = run_cli(["stop"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertIn("ERROR: the state file could not be updated", out)
        self.assertIn("stopped pid %d" % child.pid, out)
        self.assertNotIn("Traceback", out)
        self.assertIsNotNone(child.poll())                     # killed
        self.assertFalse(os.path.exists(lock_path(self.rdir())))   # lock released
        self.assertEqual(self._history(), [])
        code, out = run_cli(["wait", "--timeout", "1s"])
        self.assertEqual(code, EXIT_ERROR)
        self.assertIn("died at stage 'review'", out)

    def test_a_second_stop_after_the_failure_closes_the_run(self):
        self._dead_state()
        with mock.patch.object(State, "set_stage", side_effect=PermissionError("busy")):
            run_cli(["stop"])
        code, out = run_cli(["stop"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertEqual(State.load(self.rdir()).stage, "stopped")
        self.assertEqual([r["stage"] for r in self._history()], ["stopped"])

    # --- AC-3: the history row ------------------------------------------------------

    def test_dead_path_appends_one_row(self):
        self._dead_state()
        code, out = run_cli(["stop"])
        self.assertEqual(code, EXIT_OK, out)
        rows = self._history()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["stage"], "stopped")
        self.assertEqual(row["exit"], EXIT_ERROR)
        self.assertEqual(row["repo"], "owner/repo")
        self.assertEqual(row["branch"], "feature/mul")
        self.assertEqual(row["pr"], 7)
        self.assertEqual(row["rounds"], 1)
        self.assertEqual(row["fixes"], 1)
        self.assertAlmostEqual(row["cost_usd"], 1.5)

    def test_live_path_appends_one_row(self):
        child = self._live_child()
        self._dead_state()
        acquire_lock(self.rdir(), pid=child.pid)
        code, out = run_cli(["stop"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("stopped pid", out)
        rows = self._history()
        self.assertEqual([(r["stage"], r["exit"], r["branch"]) for r in rows],
                         [("stopped", EXIT_ERROR, "feature/mul")])

    def test_nothing_to_stop_appends_nothing(self):
        code, out = run_cli(["stop"])                       # no state at all
        self.assertEqual(code, EXIT_OK, out)
        st = State(branch="feature/mul", base="main", stage="needs_action", last_exit=2)
        st.save(self.rdir())
        code, out = run_cli(["stop"])                       # a result is recorded
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("no run in progress", out)
        self.assertEqual(self._history(), [])

    def test_an_unwritable_history_file_is_ignored(self):
        self._dead_state()
        os.makedirs(os.path.join(self.home, "history.jsonl"))   # a directory: append raises OSError
        code, out = run_cli(["stop"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertNotIn("Traceback", out)
        self.assertEqual(State.load(self.rdir()).stage, "stopped")


class StatsWithStoppedRuns(unittest.TestCase):                                            # AC-4
    def _row(self, stage, exit_code, **kw):
        row = {"repo": "owner/repo", "branch": "feature/mul", "base": "main", "stage": stage,
               "exit": exit_code, "rounds": 1, "fixes": 0, "last_verdict": "APPROVE",
               "cost_usd": 1.0, "models": ["m"], "fallback": False, "pr": 7}
        row.update(kw)
        return row

    def _table_row(self, text):
        return [line for line in text.splitlines() if line.startswith("| owner/repo ")][0]

    def test_a_stopped_pipeline_counts_as_a_run_without_a_verdict(self):
        text = summarise([self._row("stopped", 1)])
        self.assertIn("pipelines: 1", text)
        cells = [c.strip() for c in self._table_row(text).strip("|").split("|")]
        # repo | runs | reached verdict | first-try pass | merged | needs human | fallback | mean rounds | cost
        self.assertEqual(cells[1:6], ["1", "0", "-", "0", "0"])

    def test_a_later_row_supersedes_the_stop(self):
        text = summarise([self._row("stopped", 1), self._row("ready_to_merge", 0, cost_usd=2.0)])
        self.assertIn("pipelines: 1", text)
        cells = [c.strip() for c in self._table_row(text).strip("|").split("|")]
        self.assertEqual(cells[1:6], ["1", "1", "1/1", "0", "0"])
        self.assertEqual(cells[8], "$2.00")

    def test_a_stop_on_another_branch_is_its_own_pipeline(self):
        text = summarise([self._row("merged", 0), self._row("stopped", 1, branch="feature/div", pr=8)])
        self.assertIn("pipelines: 2", text)
        cells = [c.strip() for c in self._table_row(text).strip("|").split("|")]
        self.assertEqual(cells[1:6], ["2", "1", "1/1", "1", "0"])


if __name__ == "__main__":
    unittest.main()
