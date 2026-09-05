"""Reviewer acceptance tests for fix/stop-followups: the `stop` help text names both jobs
(AC-1); a state write that fails is one `ERROR:` line and exit 1 on both `stop` paths, the
process still killed and the lock still released (AC-2); every `stop` that records `stopped`
appends one history row with the state's fields, a no-op appends none, an unwritable history
file is ignored (AC-3); `stats` counts a stopped pipeline as a run without a verdict and a
later row supersedes it (AC-4); the README says so (AC-5)."""

import os
import subprocess
import sys
import unittest
from unittest import mock

from revali import EXIT_ACTION, EXIT_ERROR, EXIT_OK
from revali.config import history_path
from revali.state import State, acquire_lock, append_history, lock_path, read_history
from tests.helpers import RepoCase, run_cli

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class StopHelpText(unittest.TestCase):
    def test_top_level_help_names_both_jobs(self):
        from revali.cli import build_parser

        text = build_parser().format_help()
        line = [ln for ln in text.splitlines() if ln.strip().startswith("stop")]
        self.assertTrue(line, text)
        # the listing entry for `stop` (argparse may wrap it onto following lines)
        after = text[text.index(line[0]) :]
        self.assertIn("kill", after)  # AC-1: the live job
        self.assertIn("died without a result", after)  # AC-1: the dead-run job
        self.assertIn("stopped", after)

    def test_stop_dash_h_names_both_jobs(self):
        """`revali stop -h` is the subparser's own help, which argparse builds from
        `description=`, not from the parent's `help=` entry (round 1 F1)."""
        import argparse

        from revali.cli import build_parser

        parser = build_parser()
        actions = [a for a in parser._actions if isinstance(a, argparse._SubParsersAction)]
        self.assertEqual(len(actions), 1)
        stop_parser = actions[0].choices["stop"]
        text = " ".join(stop_parser.format_help().split())  # argparse wraps long lines
        self.assertIn("kill the running pipeline", text)  # AC-1: the live job
        self.assertIn("died without a result", text)  # AC-1: the dead-run job
        self.assertIn("as stopped", text)

    def test_stop_dash_h_through_the_cli_exits_0_with_the_text(self):
        """The same through the real entry point: `-h` prints and exits 0."""
        import io
        from contextlib import redirect_stdout

        from revali.cli import main

        buf = io.StringIO()
        with redirect_stdout(buf):
            with self.assertRaises(SystemExit) as raised:
                main(["stop", "-h"])
        self.assertEqual(raised.exception.code, 0)
        text = " ".join(buf.getvalue().split())
        self.assertIn("died without a result", text)  # AC-1
        self.assertIn("kill the running pipeline", text)  # AC-1


class StopCloseCase(RepoCase):
    def dead_state(self, stage="review", **fields):
        base = dict(
            repo="owner/repo",
            branch="feature/mul",
            base="main",
            stage=stage,
            message="reviewer round 1",
            last_exit=-1,
            pr_number=7,
            fixes=3,
            cost_usd=2.5,
            rounds=[
                {"round": 1, "verdict": "CHANGES_REQUESTED"},
                {"round": 2, "verdict": "APPROVE"},
            ],
            last_verdict="APPROVE",
            models_used=["claude-fable-5"],
        )
        base.update(fields)
        State(**base).save(self.rdir())

    def rows(self):
        return read_history(history_path())

    def live_child(self):
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(120)"], start_new_session=True
        )  # own group: kill_tree uses killpg on POSIX
        self.addCleanup(lambda: child.poll() is None and child.kill())
        return child

    @staticmethod
    def failing_write(exc):
        """The state write fails the way it does on Windows: at the file primitive, whatever
        `set_stage` does around it."""
        return mock.patch("revali.state.write_json_atomic", side_effect=exc)


class AFailedStateWriteIsContained(StopCloseCase):
    def test_dead_path_prints_one_error_line_and_returns_1(self):
        self.dead_state(stage="review")
        with self.failing_write(PermissionError("state.json is in use")):
            code, out = run_cli(["stop"])
        self.assertEqual(code, EXIT_ERROR, out)  # AC-2: returns 1
        self.assertNotIn("Traceback", out)  # AC-2: no traceback
        error_lines = [ln for ln in out.splitlines() if ln.startswith("ERROR:")]
        self.assertEqual(len(error_lines), 1, out)  # AC-2: one ERROR: line
        self.assertIn("state.json is in use", error_lines[0])  # AC-2: names the error
        self.assertTrue(
            "wait" in error_lines[0] and "status" in error_lines[0], error_lines[0]
        )  # AC-2: what they show
        self.assertIn("dead", error_lines[0])
        self.assertNotIn("now recorded as stopped", out)  # not claimed
        state = State.load(self.rdir())
        self.assertEqual(state.stage, "review")  # nothing written
        self.assertEqual(state.last_exit, -1)
        self.assertEqual(self.rows(), [])  # AC-3: nothing changed, no row

    def test_any_oserror_is_contained_not_only_permission_errors(self):
        for exc in (OSError("disk full"), FileNotFoundError("state dir vanished")):
            with self.subTest(exc=type(exc).__name__):
                self.dead_state(stage="validate")
                with self.failing_write(exc):
                    code, out = run_cli(["stop"])
                self.assertEqual(code, EXIT_ERROR, out)  # AC-2: an OSError
                self.assertNotIn("Traceback", out)
                self.assertEqual(out.count("ERROR:"), 1, out)
                self.assertIn(str(exc), out)

    def test_wait_and_status_then_still_report_a_death(self):
        self.dead_state(stage="review")
        with self.failing_write(PermissionError("busy")):
            run_cli(["stop"])
        code, out = run_cli(["wait", "--timeout", "1s"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertIn("died at stage 'review' without a result", out)  # AC-2: what wait shows
        code, out = run_cli(["status"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("without a result", out)  # AC-2: what status shows

    def test_live_path_still_kills_and_unlocks_before_the_write(self):
        child = self.live_child()
        self.dead_state(stage="review")
        acquire_lock(self.rdir(), pid=child.pid)
        with self.failing_write(PermissionError("state.json is in use")):
            code, out = run_cli(["stop"])
        self.assertEqual(code, EXIT_ERROR, out)  # AC-2: returns 1
        self.assertNotIn("Traceback", out)
        self.assertEqual(out.count("ERROR:"), 1, out)  # AC-2: one line
        self.assertIn("state.json is in use", out)
        self.assertIsNotNone(child.poll())  # AC-2: still killed
        self.assertFalse(os.path.exists(lock_path(self.rdir())))  # AC-2: lock released
        self.assertEqual(State.load(self.rdir()).stage, "review")  # write never landed
        self.assertEqual(self.rows(), [])  # AC-3: no row without the mark

    def test_a_second_stop_once_the_file_is_free_closes_the_run(self):
        self.dead_state(stage="review")
        with self.failing_write(PermissionError("busy")):
            run_cli(["stop"])
        code, out = run_cli(["stop"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertEqual(State.load(self.rdir()).stage, "stopped")
        self.assertEqual([r["stage"] for r in self.rows()], ["stopped"])  # AC-3: exactly one row


class EveryStopThatMarksStoppedWritesOneRow(StopCloseCase):
    def assert_row_matches_state(self, row):
        self.assertEqual(row["stage"], "stopped")  # AC-3
        self.assertEqual(row["exit"], EXIT_ERROR)  # AC-3
        self.assertEqual(row["repo"], "owner/repo")
        self.assertEqual(row["branch"], "feature/mul")
        self.assertEqual(row["base"], "main")
        self.assertEqual(row["pr"], 7)
        self.assertEqual(row["rounds"], 2)
        self.assertEqual(row["fixes"], 3)
        self.assertAlmostEqual(row["cost_usd"], 2.5)
        self.assertEqual(row["last_verdict"], "APPROVE")
        self.assertEqual(row["models"], ["claude-fable-5"])

    def test_dead_path_appends_one_row_from_the_state(self):
        self.dead_state(stage="review")
        code, out = run_cli(["stop"])
        self.assertEqual(code, EXIT_OK, out)
        rows = self.rows()
        self.assertEqual(len(rows), 1, rows)  # AC-3: one row
        self.assert_row_matches_state(rows[0])

    def test_live_path_appends_one_row_from_the_state(self):
        child = self.live_child()
        self.dead_state(stage="validate")
        acquire_lock(self.rdir(), pid=child.pid)
        code, out = run_cli(["stop"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("stopped pid %d" % child.pid, out)
        rows = self.rows()
        self.assertEqual(len(rows), 1, rows)  # AC-3: one row
        self.assert_row_matches_state(rows[0])
        self.assertEqual(State.load(self.rdir()).stage, "stopped")

    def test_a_stop_that_changes_nothing_appends_no_row(self):
        code, out = run_cli(["stop"])  # no state at all
        self.assertEqual(code, EXIT_OK, out)
        self.assertEqual(self.rows(), [])  # AC-3
        for stage, exit_code in (
            ("needs_action", EXIT_ACTION),
            ("ready_to_merge", EXIT_OK),
            ("error", EXIT_ERROR),
            ("stopped", EXIT_ERROR),
        ):
            with self.subTest(stage=stage):
                self.dead_state(stage=stage, last_exit=exit_code)  # a result is recorded
                code, out = run_cli(["stop"])
                self.assertEqual(code, EXIT_OK, out)
                self.assertIn("no run in progress", out)
                self.assertEqual(self.rows(), [])  # AC-3: none

    def test_a_second_stop_after_the_mark_appends_no_second_row(self):
        self.dead_state(stage="review")
        run_cli(["stop"])
        code, out = run_cli(["stop"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("no run in progress", out)
        self.assertEqual(len(self.rows()), 1)  # AC-3: still one

    def test_an_unwritable_history_file_is_ignored(self):
        self.dead_state(stage="review")
        os.makedirs(history_path())  # a directory: append fails
        code, out = run_cli(["stop"])
        self.assertEqual(code, EXIT_OK, out)  # AC-3: ignored
        self.assertNotIn("Traceback", out)
        self.assertNotIn("ERROR:", out)
        self.assertEqual(State.load(self.rdir()).stage, "stopped")  # the mark still landed
        self.assertEqual(State.load(self.rdir()).last_exit, EXIT_ERROR)


class StatsCountsTheStoppedEpisode(StopCloseCase):
    def table_row(self, text):
        rows = [ln for ln in text.splitlines() if ln.startswith("| owner/repo ")]
        self.assertEqual(len(rows), 1, text)
        return [c.strip() for c in rows[0].strip("|").split("|")]

    def test_a_stopped_pipeline_is_a_run_without_a_verdict(self):
        self.dead_state(stage="review")
        run_cli(["stop"])
        code, out = run_cli(["stats"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("pipelines: 1", out)  # AC-4: counted
        cells = self.table_row(out)
        # repo | runs | reached verdict | first-try pass | merged | needs human | fallback | mean
        # rounds | cost
        self.assertEqual(cells[1], "1")  # AC-4: under runs
        self.assertEqual(cells[2], "0")  # AC-4: no verdict reached
        self.assertEqual(cells[3], "-")  # AC-4: no first-try pass
        self.assertEqual(cells[4], "0")  # AC-4: not merged
        self.assertEqual(cells[5], "0")  # AC-4: not needs human
        self.assertEqual(cells[8], "$2.50")  # the state's cost

    def test_a_later_row_for_the_same_pipeline_supersedes_the_stop(self):
        self.dead_state(stage="review")
        run_cli(["stop"])
        append_history(
            history_path(),
            {
                "repo": "owner/repo",
                "branch": "feature/mul",
                "base": "main",
                "stage": "merged",
                "exit": EXIT_OK,
                "rounds": 3,
                "fixes": 3,
                "last_verdict": "PASS",
                "cost_usd": 4.0,
                "models": ["claude-fable-5"],
                "fallback": False,
                "pr": 7,
            },
        )
        code, out = run_cli(["stats"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("history rows: 2, pipelines: 1", out)  # AC-4: one pipeline
        cells = self.table_row(out)
        self.assertEqual(cells[1:6], ["1", "1", "0/1", "1", "0"])  # AC-4: the later row wins
        self.assertEqual(cells[8], "$4.00")  # not summed with the stop

    def test_a_stop_on_another_pr_is_its_own_pipeline(self):
        append_history(
            history_path(),
            {
                "repo": "owner/repo",
                "branch": "feature/div",
                "base": "main",
                "stage": "merged",
                "exit": EXIT_OK,
                "rounds": 1,
                "fixes": 0,
                "last_verdict": "PASS",
                "cost_usd": 1.0,
                "models": ["claude-fable-5"],
                "fallback": False,
                "pr": 8,
            },
        )
        self.dead_state(stage="review")
        run_cli(["stop"])
        code, out = run_cli(["stats"])
        self.assertIn("pipelines: 2", out)  # AC-4: not collapsed
        cells = self.table_row(out)
        self.assertEqual(cells[1:6], ["2", "1", "1/1", "1", "0"])


class ReadmeSaysSo(unittest.TestCase):
    def test_the_stop_paragraph_mentions_the_history_row_and_the_failed_write(self):
        with open(
            os.path.join(ROOT, "docs", "workflow.md"), "r", encoding="utf-8", newline=""
        ) as fh:
            readme = fh.read()
        self.assertIn("`revali stop`", readme)
        paragraph = readme.split("`revali stop` acknowledges", 1)[1][:1200]
        self.assertIn("history row", paragraph)  # AC-5: a row is written
        self.assertIn("`stats`", paragraph)
        self.assertIn("cannot be written", paragraph)  # AC-5: the failed write
        self.assertIn("`ERROR:`", paragraph)
        self.assertIn("returns 1", paragraph)  # AC-5: with exit 1


if __name__ == "__main__":
    unittest.main()
