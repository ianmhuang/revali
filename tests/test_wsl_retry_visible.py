"""A wsl retry shows where people look (fix/retry-visible-diagnosis-cost): a stage line in
run.log and revali.log (AC-1), a per-run record in `logs/wsl-retries.log` (AC-2), and a line
from `revali wait` while the run is going (AC-3) and when it has ended (AC-4)."""

import contextlib
import io
import os
import re
import unittest
from unittest import mock

from revali import EXIT_ACTION, EXIT_ERROR
from revali.runners import WslRunner, probe_runner
from revali.state import (
    RunLog,
    State,
    clear_wsl_retries,
    lock_path,
    write_json_atomic,
    wsl_retries_path,
)
from tests.helpers import RepoCase, claude_entry, run_cli
from tests.test_wsl_retry import TIMEOUT, FlakyWsl

EXIT_STILL_RUNNING = 4
LINE = re.compile(r"^\[\d\d:\d\d:\d\d\] wsl: Ubuntu: %s; retry 1 of 2 in 0s$" % re.escape(TIMEOUT))
OLD = "[01:02:03] wsl: Ubuntu: %s; retry 1 of 2 in 30s" % TIMEOUT


def record(rdir, *lines):
    path = wsl_retries_path(rdir, "logs")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write("".join(line + "\n" for line in lines))
    return path


def recorded(rdir):
    path = wsl_retries_path(rdir, "logs")
    if not os.path.isfile(path):
        return []
    with open(path, "r", encoding="utf-8") as fh:
        return [ln.rstrip("\n") for ln in fh if ln.strip()]


class RunnerNotice(FlakyWsl):
    """AC-1: a runner given a notice callable sends the retry there, not to its log."""

    def test_wslpath_retry_goes_to_notice(self):
        self.plan(wslpath=1)
        notices = []
        WslRunner(self.plat(), notice=notices.append).wslpath(r"C:\repo", log=self.lines.append)
        self.assertEqual(notices, ["Ubuntu: %s; retry 1 of 2 in 0s" % TIMEOUT])
        self.assertEqual(self.retry_lines(), [], self.lines)

    def test_probe_retry_goes_to_notice(self):
        self.plan(probe=1)
        notices = []
        problem = probe_runner(self.plat(), log=self.lines.append, notice=notices.append)
        self.assertEqual(problem, "")
        self.assertEqual(len(notices), 1, notices)
        self.assertEqual(self.retry_lines(), [], self.lines)

    def test_without_notice_the_log_keeps_the_line(self):
        self.plan(wslpath=1)
        WslRunner(self.plat()).wslpath(r"C:\repo", log=self.lines.append)
        self.assertEqual(len(self.retry_lines()), 1, self.lines)


class RunLogRecord(RepoCase):
    """AC-1, AC-2: RunLog.wsl_retry writes a stage line and appends it to wsl-retries.log."""

    def test_stage_line_printed_logged_and_recorded(self):
        log = RunLog(self.rdir(), logs_dir="logs")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            log.wsl_retry("Ubuntu: %s; retry 1 of 2 in 0s" % TIMEOUT)
            log.wsl_retry("Ubuntu: %s; retry 2 of 2 in 0s" % TIMEOUT)
        printed = buf.getvalue().splitlines()
        self.assertEqual(len(printed), 2)
        self.assertRegex(printed[0], LINE)
        with open(os.path.join(self.rdir(), "logs", "revali.log"), "r", encoding="utf-8") as fh:
            logged = fh.read().splitlines()
        self.assertEqual(logged, printed)
        self.assertEqual(recorded(self.rdir()), printed)

    def test_quiet_log_still_records(self):
        log = RunLog(self.rdir(), quiet=True, logs_dir="logs")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            log.wsl_retry("Ubuntu: x; retry 1 of 2 in 0s")
        self.assertEqual(buf.getvalue(), "")
        self.assertEqual(len(recorded(self.rdir())), 1)


class RecordFailures(RepoCase):
    """Round 1 F1, F2: the record never stops a run, and a failure to write or empty it is
    noted in revali.log."""

    def revali_log(self):
        with open(os.path.join(self.rdir(), "logs", "revali.log"), "r", encoding="utf-8") as fh:
            return fh.read()

    def test_failed_append_is_noted_and_the_retry_goes_on(self):
        log = RunLog(self.rdir(), quiet=True, logs_dir="logs")
        os.makedirs(wsl_retries_path(self.rdir(), "logs"))  # a directory: open() fails
        log.wsl_retry("Ubuntu: x; retry 1 of 2 in 0s")  # must not raise
        text = self.revali_log()
        self.assertRegex(text, r"(?m)^\[\d\d:\d\d:\d\d\] wsl: Ubuntu: x; retry 1 of 2 in 0s$")
        self.assertIn("could not add the retry to %s" % wsl_retries_path(self.rdir(), "logs"), text)

    def test_failed_clear_is_returned(self):
        path = record(self.rdir(), OLD)
        with mock.patch("revali.state.open", side_effect=PermissionError("held"), create=True):
            problem = clear_wsl_retries(self.rdir(), "logs")
        self.assertIn("could not empty %s" % path, problem)
        self.assertIn("held", problem)
        self.assertEqual(recorded(self.rdir()), [OLD])
        self.assertEqual(clear_wsl_retries(self.rdir(), "logs"), "")
        self.assertEqual(recorded(self.rdir()), [])
        self.assertEqual(clear_wsl_retries(self.rdir(), "logs"), "")  # nothing to empty

    def test_foreground_run_logs_a_failed_clear(self):
        self.claude(claude_entry())
        with mock.patch("revali.pipeline.clear_wsl_retries", return_value="could not empty X"):
            run_cli(["run", "--foreground"])
        self.assertRegex(self.revali_log(), r"(?m)^\[\d\d:\d\d:\d\d\]   could not empty X$")

    def test_a_failing_log_write_there_still_releases_the_locks(self):
        # round 2 follow-up: the clear and its log line run under the locks' finally
        real = RunLog.detail

        def detail(log, msg):
            if msg == "could not empty X":
                raise OSError("disk full")
            real(log, msg)

        self.claude(claude_entry())
        with mock.patch("revali.pipeline.clear_wsl_retries", return_value="could not empty X"):
            with mock.patch.object(RunLog, "detail", detail):
                code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertFalse(os.path.exists(lock_path(self.rdir())))
        self.assertFalse(os.path.exists(os.path.join(self.repo, ".revali", "tree.lock")))
        state = State.load(self.rdir())
        self.assertEqual(state.stage, "error")
        self.assertIn("OSError: disk full", state.message)


class RunRecordsItsRetries(FlakyWsl):
    """AC-1, AC-2 through `revali run`: the probe's and the baseline thread's retries reach
    the output, revali.log as stage lines, and a record that has nothing from earlier runs."""

    def test_probe_and_baseline_retries(self):
        cfg = self.read("revali.toml").replace(
            'distro = "Ubuntu"\n', 'distro = "Ubuntu"\nwsl_retry_wait_s = 0\n', 1
        )
        self.write("revali.toml", cfg)
        self.commit_all("no wait between wsl retries")
        record(self.rdir(), OLD)
        # the probe fails once; every wslpath fails, so the baseline (on its thread) retries
        # twice and gives up
        self.plan(probe=1, wslpath=99)
        self.claude(claude_entry())
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR, out)
        lines = recorded(self.rdir())
        self.assertNotIn(OLD, lines)
        self.assertEqual(len(lines), 3, lines)
        for line in lines:
            self.assertRegex(line, r"^\[\d\d:\d\d:\d\d\] wsl: Ubuntu: .*; retry [12] of 2 in 0s$")
            self.assertIn(line, out.splitlines())
        with open(os.path.join(self.rdir(), "logs", "revali.log"), "r", encoding="utf-8") as fh:
            logged = fh.read().splitlines()
        for line in lines:
            self.assertIn(line, logged)


class RecordStartsEmpty(RepoCase):
    """AC-2, AC-3, AC-4: a run without retries leaves no earlier run's lines behind, and
    `wait` then prints what it printed before."""

    def test_foreground_run_clears_the_record(self):
        record(self.rdir(), OLD)
        self.claude(claude_entry())
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(recorded(self.rdir()), [], out)
        code, out = run_cli(["wait", "--timeout", "1s"])
        self.assertNotIn("wsl retries", out)

    def test_detached_run_refused_for_a_dirty_tree_clears_the_record(self):
        record(self.rdir(), OLD)
        self.write("src/calc.py", self.read("src/calc.py") + "\n# not committed\n")
        code, out = run_cli(["run"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertEqual(recorded(self.rdir()), [])
        code, out = run_cli(["wait", "--timeout", "1s"])
        self.assertNotIn("wsl retries", out)


class WaitShowsRetries(RepoCase):
    """AC-3, AC-4: `wait` reads the record of the run it reports on."""

    def live_lock(self):
        os.makedirs(self.rdir(), exist_ok=True)
        write_json_atomic(lock_path(self.rdir()), {"pid": os.getpid(), "since": "x"})
        self.addCleanup(
            lambda: os.path.isfile(lock_path(self.rdir())) and os.remove(lock_path(self.rdir()))
        )

    def state(self, stage, message, last_exit):
        State(
            branch="feature/mul", base="main", stage=stage, message=message, last_exit=last_exit
        ).save(self.rdir())

    LATEST = "[01:02:40] wsl: Ubuntu: %s; retry 2 of 2 in 30s" % TIMEOUT

    def test_still_running_adds_count_and_latest(self):
        self.state("review", "reviewer round 1", -1)
        self.live_lock()
        record(self.rdir(), OLD, "", self.LATEST)
        code, out = run_cli(["wait", "--timeout", "1s"])
        self.assertEqual(code, EXIT_STILL_RUNNING, out)
        lines = out.splitlines()
        self.assertEqual(len(lines), 3, out)
        self.assertTrue(lines[1].startswith("still running (pid %d), stage review" % os.getpid()))
        self.assertEqual(lines[2], "wsl retries in this run: 2; latest: %s" % self.LATEST)

    def test_still_running_without_retries_is_unchanged(self):
        self.state("review", "reviewer round 1", -1)
        self.live_lock()
        code, out = run_cli(["wait", "--timeout", "1s"])
        self.assertEqual(code, EXIT_STILL_RUNNING, out)
        self.assertEqual(len(out.splitlines()), 2, out)

    def test_finished_run_names_the_record_before_the_result(self):
        self.state("needs_action", "changes requested in round 1", EXIT_ACTION)
        path = record(self.rdir(), OLD, self.LATEST)
        code, out = run_cli(["wait", "--timeout", "1s"])
        self.assertEqual(code, EXIT_ACTION, out)
        lines = out.splitlines()
        self.assertEqual(lines[-1], "needs_action: changes requested in round 1")
        self.assertEqual(lines[-2], "wsl retries in this run: 2; see %s" % path)

    def test_died_run_names_the_record_before_the_error(self):
        self.state("validate", "", -1)
        path = record(self.rdir(), self.LATEST)
        code, out = run_cli(["wait", "--timeout", "1s"])
        self.assertEqual(code, EXIT_ERROR, out)
        lines = out.splitlines()
        self.assertTrue(lines[-1].startswith("error: the run died at stage 'validate'"), out)
        self.assertEqual(lines[-2], "wsl retries in this run: 1; see %s" % path)

    def test_finished_without_retries_is_unchanged(self):
        self.state("needs_action", "changes requested in round 1", EXIT_ACTION)
        code, out = run_cli(["wait", "--timeout", "1s"])
        self.assertEqual(out.splitlines()[1:], ["needs_action: changes requested in round 1"])


class Docs(unittest.TestCase):
    """AC-7"""

    def read(self, rel):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, rel), "r", encoding="utf-8") as fh:
            return fh.read()

    def test_files_md_lists_the_record(self):
        self.assertIn("wsl-retries.log", self.read(os.path.join("docs", "files.md")))

    def test_sandbox_md_says_where_retries_show(self):
        text = " ".join(self.read(os.path.join("docs", "sandbox.md")).split())
        para = text.split("Every `wsl.exe` call the runner makes", 1)[1].split("A wsl or ssh", 1)[0]
        for word in ("run.log", "`revali wait`", "wsl-retries.log"):
            self.assertIn(word, para)


if __name__ == "__main__":
    unittest.main()
