"""Acceptance tests for fix/retry-visible-diagnosis-cost, AC-1 to AC-4: a wsl retry is a
`wsl:` stage line (output, run.log, revali.log), every run keeps its own retries in
`.revali/<branch>/logs/wsl-retries.log`, and `revali wait` reports them.

Black box through `revali run` and `revali wait`. The retries come from the flaky wsl.exe
stand-in the wsl retry tests already use; a run "in progress" for `wait` is a lock carrying
this process's pid, a finished one a state file with a result.
"""

import os
import re
import unittest

from revali import EXIT_ACTION, EXIT_ERROR, EXIT_OK
from revali.state import State, lock_path, write_json_atomic
from tests.helpers import RepoCase, claude_entry, run_cli
from tests.test_wsl_retry import TIMEOUT, FlakyWsl

STILL_RUNNING = 4
RECORD = "wsl-retries.log"
STAGE_LINE = re.compile(
    r"^\[\d\d:\d\d:\d\d\] wsl: Ubuntu: %s; retry (\d) of 2 in 0s$" % re.escape(TIMEOUT)
)
OLD = "[01:02:03] wsl: Ubuntu: %s; retry 1 of 2 in 30s" % TIMEOUT
LATEST = "[01:02:40] wsl: Ubuntu: %s; retry 2 of 2 in 30s" % TIMEOUT


def record_path(rdir):
    return os.path.join(rdir, "logs", RECORD)


def seed(rdir, text):
    """Leave `text` in the record, as an earlier run (or the one in progress) would have."""
    path = record_path(rdir)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)


def recorded(rdir):
    path = record_path(rdir)
    if not os.path.isfile(path):
        return []
    with open(path, "r", encoding="utf-8") as fh:
        return [ln.rstrip("\r\n") for ln in fh if ln.strip()]


def file_lines(path):
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        return fh.read().splitlines()


def names_path(text, path):
    """`text` carries `path`, whichever way its separators and its drive letter are written."""

    def flat(s):
        return os.path.normcase(s).replace("\\", "/")

    return flat(path) in flat(text)


class RetriesOfARun(FlakyWsl):
    """AC-1, AC-2: through a run whose wsl calls fail with the transient error."""

    def setUp(self):
        super().setUp()
        cfg = self.read("revali.toml")
        quick = cfg.replace('distro = "Ubuntu"\n', 'distro = "Ubuntu"\nwsl_retry_wait_s = 0\n', 1)
        self.assertNotEqual(cfg, quick, "fixture distro line not found")
        self.write("revali.toml", quick)
        self.commit_all("no wait between wsl retries")

    def retry_lines(self, lines):
        return [ln for ln in lines if STAGE_LINE.match(ln)]

    def numbers(self, lines):
        """The retry number of each line; a line that is no retry stage line stays as it is."""
        found = [(STAGE_LINE.match(ln), ln) for ln in lines]
        return [m.group(1) if m else ln for m, ln in found]

    def test_foreground_run_prints_logs_and_records_each_retry(self):
        seed(self.rdir(), OLD + "\n")
        self.plan(probe=9)  # the probe never gets through: two retries, then exit 1
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR, out)
        printed = self.retry_lines(out.splitlines())
        self.assertEqual(self.numbers(printed), ["1", "2"], out)
        # revali.log has them as stage lines: the very same lines, not indented detail
        logged = file_lines(os.path.join(self.rdir(), "logs", "revali.log"))
        self.assertEqual(self.retry_lines(logged), printed, logged)
        # the record holds this run's retries and nothing of the earlier run
        self.assertEqual(recorded(self.rdir()), printed)

    def test_baseline_inside_preflight(self):
        self.sequential_baseline()
        # the probe fails once; every wslpath fails, so the baseline retries twice and gives up
        self.plan(probe=1, wslpath=99)
        self.claude(claude_entry())
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR, out)
        lines = recorded(self.rdir())
        self.assertEqual(len(lines), 3, lines)
        self.assertEqual(self.numbers(lines), ["1", "1", "2"])
        self.assertEqual(self.retry_lines(out.splitlines()), lines, out)

    def test_baseline_on_its_own_thread(self):
        seed(self.rdir(), OLD + "\n")
        self.plan(probe=1, wslpath=99)
        self.claude(claude_entry())
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR, out)
        lines = recorded(self.rdir())
        self.assertNotIn(OLD, lines)
        # one from the probe, two from the baseline's wslpath; a smoke run that got as far as
        # its own wslpath before the baseline failed adds more of the same
        self.assertGreaterEqual(len(lines), 3, lines)
        printed = out.splitlines()
        logged = file_lines(os.path.join(self.rdir(), "logs", "revali.log"))
        for line in lines:
            self.assertRegex(line, STAGE_LINE)
            self.assertIn(line, printed)
            self.assertIn(line, logged)

    def test_detached_run_has_them_in_run_log_and_wait_names_the_record(self):  # AC-1, AC-4
        seed(self.rdir(), OLD + "\n")
        self.plan(probe=9)
        code, out = run_cli(["run"])
        self.assertEqual(code, EXIT_OK, out)
        code, out = run_cli(["wait", "--timeout", "120s"])
        self.assertEqual(code, EXIT_ERROR, out)
        lines = recorded(self.rdir())
        self.assertEqual(self.numbers(lines), ["1", "2"], lines)
        run_log = file_lines(os.path.join(self.rdir(), "logs", "run.log"))
        self.assertEqual(self.retry_lines(run_log), lines, run_log)
        shown = out.splitlines()
        notes = [i for i, ln in enumerate(shown) if RECORD in ln]
        self.assertEqual(len(notes), 1, out)
        note = shown[notes[0]]
        self.assertRegex(note, r"^\D*2\D")  # the number of retries, ahead of the path
        self.assertTrue(names_path(note, record_path(self.rdir())), note)
        self.assertTrue(shown[notes[0] + 1].startswith("error: "), out)  # the result follows


class RecordStartsEmpty(RepoCase):
    """AC-2: an earlier run's retries are gone once the next run has started."""

    def test_run_without_retries_empties_the_record(self):
        seed(self.rdir(), OLD + "\n" + LATEST + "\n")
        self.claude(claude_entry())
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertEqual(recorded(self.rdir()), [])
        self.assertNotIn(" wsl: ", out)
        code, out = run_cli(["wait", "--timeout", "1s"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertNotIn("retries", out)
        self.assertNotIn(OLD, out)

    def test_refused_detached_run_does_not_report_the_earlier_runs_retries(self):
        seed(self.rdir(), OLD + "\n")
        self.write("src/calc.py", self.read("src/calc.py") + "\n# not committed\n")
        code, out = run_cli(["run"])
        self.assertEqual(code, EXIT_ERROR, out)
        code, out = run_cli(["wait", "--timeout", "1s"])
        self.assertEqual(code, EXIT_ERROR, out)  # the refusal is the result now
        self.assertNotIn("retries", out)
        self.assertNotIn(OLD, out)


class WaitReportsRetries(RepoCase):
    """AC-3, AC-4: what `wait` prints for a run with and without recorded retries."""

    def running(self, stage):
        self.result(stage, "", -1)
        write_json_atomic(lock_path(self.rdir()), {"pid": os.getpid(), "since": "x"})
        self.addCleanup(self.drop_lock)

    def drop_lock(self):
        if os.path.isfile(lock_path(self.rdir())):
            os.remove(lock_path(self.rdir()))

    def result(self, stage, message, last_exit):
        State(
            branch="feature/mul", base="main", stage=stage, message=message, last_exit=last_exit
        ).save(self.rdir())

    def still_running_line(self, stage):
        return "still running (pid %d), stage %s; call `revali wait` again" % (os.getpid(), stage)

    def test_in_progress_adds_count_and_latest_line(self):  # AC-3
        # stage `review`: the retries are the baseline thread's
        self.running("review")
        seed(self.rdir(), OLD + "\n" + LATEST + "\n")
        code, out = run_cli(["wait", "--timeout", "1s"])
        self.assertEqual(code, STILL_RUNNING, out)
        lines = out.splitlines()
        self.assertEqual(len(lines), 3, out)
        self.assertEqual(lines[1], self.still_running_line("review"))
        self.assertIn("2", lines[2].replace(LATEST, ""))  # the number of retries
        self.assertIn(LATEST, lines[2])  # the latest one, with its time
        self.assertNotIn(OLD, lines[2])

    def test_in_progress_with_one_retry(self):  # AC-3
        self.running("validate")
        seed(self.rdir(), OLD + "\n")
        code, out = run_cli(["wait", "--timeout", "1s"])
        self.assertEqual(code, STILL_RUNNING, out)
        lines = out.splitlines()
        self.assertEqual(len(lines), 3, out)
        self.assertEqual(lines[1], self.still_running_line("validate"))
        self.assertIn("1", lines[2].replace(OLD, ""))
        self.assertIn(OLD, lines[2])

    def test_in_progress_without_retries_is_as_before(self):  # AC-3
        self.running("review")
        code, out = run_cli(["wait", "--timeout", "1s"])
        self.assertEqual(code, STILL_RUNNING, out)
        self.assertEqual(out.splitlines()[1:], [self.still_running_line("review")])
        seed(self.rdir(), "")  # the record as a run leaves it when it starts
        code, out = run_cli(["wait", "--timeout", "1s"])
        self.assertEqual(code, STILL_RUNNING, out)
        self.assertEqual(out.splitlines()[1:], [self.still_running_line("review")])

    def test_finished_run_names_count_and_file_before_the_result(self):  # AC-4
        self.result("needs_action", "changes requested in round 1", EXIT_ACTION)
        seed(self.rdir(), OLD + "\n" + LATEST + "\n")
        code, out = run_cli(["wait", "--timeout", "1s"])
        self.assertEqual(code, EXIT_ACTION, out)
        lines = out.splitlines()
        self.assertEqual(len(lines), 3, out)
        self.assertEqual(lines[-1], "needs_action: changes requested in round 1")
        note = lines[-2]
        self.assertRegex(note, r"^\D*2\D")  # the number of retries, ahead of the path
        self.assertTrue(names_path(note, record_path(self.rdir())), note)

    def test_finished_run_without_retries_is_as_before(self):  # AC-4
        self.result("needs_action", "changes requested in round 1", EXIT_ACTION)
        code, out = run_cli(["wait", "--timeout", "1s"])
        self.assertEqual(code, EXIT_ACTION, out)
        self.assertEqual(out.splitlines()[1:], ["needs_action: changes requested in round 1"])


if __name__ == "__main__":
    unittest.main()
