"""Review tests for fix/wsl-transient-retry, on one sandbox session of the wsl runner
(`WslRunner.run`, the Runner interface the pipeline calls), with the planned wsl.exe
stand-in of tests/test_review_wslflaky.py. The step appends a line to a marker file, so
the file's line count says how often the sandbox script really ran.

AC-1: `wslpath` and the script each fail once with the service timeout, the session
      passes, the steps ran once, and the log has one line per retry.
AC-2: a script that never starts, or a `wslpath` that never answers, is tried three
      times; the error has no NUL and says what `wsl --shutdown` does.
AC-4: a failing step, a `bash` that exits 1 and another WSL error get one call.
AC-5: a script that left its results is not run again, whatever wsl.exe reported.
AC-6: the pattern is a substring of the output, UTF-16 or not.
"""

import os
import unittest

from revali.config import PlatformCfg
from revali.runners import RunnerError, WslRunner
from tests.helpers import git
from tests.test_review_wslflaky import HAVE_BASH, NOT_FOUND_TEXT, TIMEOUT, TIMEOUT_TEXT, FlakyCase


class SessionCase(FlakyCase):
    def setUp(self):
        super().setUp()
        self.lines = []
        self.logs = os.path.join(self.rdir(), "logs")
        self.marker = os.path.join(self.tmp, "ran.txt")
        self.mark = 'echo ran >> "%s"' % self.marker.replace("\\", "/")

    def session(self, steps=None, label="validate-r1"):
        plat = PlatformCfg(
            name="linux",
            runner="wsl",
            distro="Ubuntu",
            command_timeout_min=1,
            sandbox_dir="~/.revali/sandbox",
            wsl_retries=2,
            wsl_retry_wait_s=0,
            wsl_transient_errors=[TIMEOUT],
        )
        head = git(["rev-parse", "HEAD"], self.repo).strip()
        steps = steps or [("test", self.mark)]
        return WslRunner(plat).run(
            self.repo, head, steps, {}, self.logs, label, log=self.lines.append
        )

    def times_ran(self):
        if not os.path.isfile(self.marker):
            return 0
        with open(self.marker, "r", encoding="utf-8") as fh:
            return len([ln for ln in fh if ln.strip()])

    def retry_lines(self):
        # the command lines the runner logs carry paths, never the error code
        return [ln for ln in self.lines if TIMEOUT in ln]

    def assert_gave_up_with_the_hint(self, message):
        self.assertNotIn("\x00", message)
        self.assertIn(TIMEOUT, message)
        self.assertIn("wsl --shutdown", message)
        self.assertRegex(message, r"(?i)stop\w*\s+every\s+running\s+distro")


class Recovers(SessionCase):
    """AC-1"""

    @unittest.skipUnless(HAVE_BASH, "needs bash")
    def test_wslpath_and_script_each_fail_once_and_the_steps_run_once(self):
        self.plan(before={"wslpath": 1, "script": 1})
        report = self.session()
        self.assertTrue(report.ok, [(s.name, s.returncode) for s in report.steps])
        self.assertEqual(self.times_ran(), 1)
        self.assertEqual(len(self.calls("script")), 2)
        lines = self.retry_lines()
        self.assertEqual(len(lines), 2, self.lines)
        for line in lines:
            self.assertNotIn("\x00", line)


class GivesUp(SessionCase):
    """AC-2"""

    def test_script_that_never_starts_is_tried_three_times(self):
        self.plan(before={"script": 99})
        with self.assertRaises(RunnerError) as cm:
            self.session()
        self.assertEqual(len(self.calls("script")), 3)
        self.assertEqual(self.times_ran(), 0)
        self.assertEqual(len(self.retry_lines()), 2, self.lines)
        self.assert_gave_up_with_the_hint(str(cm.exception))

    def test_wslpath_that_never_answers_is_tried_three_times(self):
        self.plan(before={"wslpath": 99})
        with self.assertRaises(RunnerError) as cm:
            self.session()
        self.assertEqual(len(self.calls("wslpath")), 3)
        self.assertEqual(self.calls("script"), [])
        self.assertEqual(len(self.retry_lines()), 2, self.lines)
        self.assert_gave_up_with_the_hint(str(cm.exception))


class NotTransient(SessionCase):
    """AC-4"""

    @unittest.skipUnless(HAVE_BASH, "needs bash")
    def test_failing_step_is_reported_from_one_call(self):
        report = self.session([("setup", self.mark), ("test", "exit 3")], "validate-r2")
        self.assertEqual(report.failed.name, "test")
        self.assertEqual(report.failed.returncode, 3)
        self.assertEqual(len(self.calls("script")), 1)
        self.assertEqual(self.times_ran(), 1)
        self.assertEqual(self.retry_lines(), [])

    def test_bash_that_exits_1_is_one_call_and_todays_message(self):
        self.plan(
            before={"script": 99},
            default={"text": "bash: validate-r1.sh: No such file\n", "exit": 1, "utf16": False},
        )
        with self.assertRaises(RunnerError) as cm:
            self.session()
        self.assertEqual(len(self.calls("script")), 1)
        self.assertIn("No such file", str(cm.exception))
        self.assertNotIn("wsl --shutdown", str(cm.exception))

    def test_another_wsl_error_on_wslpath_is_one_call(self):
        self.plan(
            before={"wslpath": 99}, default={"text": NOT_FOUND_TEXT, "exit": -1, "utf16": True}
        )
        with self.assertRaises(RunnerError) as cm:
            self.session()
        self.assertEqual(len(self.calls("wslpath")), 1)
        self.assertIn("wslpath", str(cm.exception))
        self.assertNotIn("wsl --shutdown", str(cm.exception))
        self.assertEqual(self.retry_lines(), [])


class ScriptThatRan(SessionCase):
    """AC-5: the script ran (its results file exists), then wsl.exe reported the timeout."""

    @unittest.skipUnless(HAVE_BASH, "needs bash")
    def test_passing_script_is_not_run_again(self):
        self.plan(after={"script": 99})
        report = self.session()
        self.assertTrue(os.path.isfile(os.path.join(self.logs, "validate-r1.results")))
        self.assertEqual(len(self.calls("script")), 1)
        self.assertEqual(self.times_ran(), 1)
        self.assertTrue(report.ok, [(s.name, s.returncode) for s in report.steps])
        self.assertEqual(self.retry_lines(), [])

    @unittest.skipUnless(HAVE_BASH, "needs bash")
    def test_script_with_a_failed_step_is_not_run_again(self):
        self.plan(after={"script": 99})
        report = self.session([("setup", self.mark), ("test", "exit 3")], "validate-r2")
        self.assertEqual(len(self.calls("script")), 1)
        self.assertEqual(self.times_ran(), 1)
        self.assertEqual(report.failed.name, "test")
        self.assertEqual(report.failed.returncode, 3)


class Patterns(SessionCase):
    """AC-6: a substring match on the output, whether or not it came in UTF-16."""

    def test_pattern_in_plain_output_counts_too(self):
        self.plan(before={"wslpath": 99}, default={"text": TIMEOUT_TEXT, "exit": 1, "utf16": False})
        with self.assertRaises(RunnerError) as cm:
            self.session()
        self.assertEqual(len(self.calls("wslpath")), 3)
        self.assertIn("wsl --shutdown", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
