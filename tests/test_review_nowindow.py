"""Reviewer tests for feature/run-identity, AC-1: every subprocess started by `procs.run`,
`procs.run_retry` and (since round 2) `procs.run_shell` carries CREATE_NO_WINDOW on Windows
and no `creationflags` on POSIX.

`subprocess.run` is replaced with a recorder, so the tests run on any host and only look at
the keyword arguments revali hands to the interpreter; `os.name` is patched to pick the
platform branch. On the base branch no `creationflags` is ever passed, so the Windows
cases fail there; the `run_shell` cases also fail on the round-1 code."""
import os
import subprocess
import unittest
from unittest import mock

from revali import procs

CREATE_NO_WINDOW = 0x08000000


class Recorder:
    """A stand-in for subprocess.run that records every call's kwargs and answers with the
    scripted exit codes in order (the last one repeats)."""

    def __init__(self, *codes):
        self.codes = list(codes) or [0]
        self.calls = []

    def __call__(self, argv, **kw):
        self.calls.append((argv, kw))
        code = self.codes[min(len(self.calls), len(self.codes)) - 1]
        return subprocess.CompletedProcess(argv, code, "out", "err")


class NtRecorderCase(unittest.TestCase):
    """os.name says Windows and subprocess.run is the recorder."""

    def setUp(self):
        self.rec = Recorder()
        patch_name = mock.patch("os.name", "nt")
        patch_run = mock.patch("subprocess.run", self.rec)
        patch_name.start()
        patch_run.start()
        self.addCleanup(patch_name.stop)
        self.addCleanup(patch_run.stop)

    def flags(self, index=0):
        return self.rec.calls[index][1].get("creationflags", 0)


class WindowsPassesTheFlag(NtRecorderCase):
    """AC-1 on Windows: the flag is on the call, whatever else `run` is asked to do."""

    def test_run_sets_create_no_window(self):
        res = procs.run(["git", "--version"])
        self.assertTrue(res.ok)
        self.assertEqual(len(self.rec.calls), 1)
        self.assertEqual(self.flags() & CREATE_NO_WINDOW, CREATE_NO_WINDOW)

    def test_run_with_input_text_keeps_the_flag_and_the_input(self):
        procs.run(["claude", "-p"], input_text="prompt body", cwd=os.getcwd(), timeout=5)
        argv, kw = self.rec.calls[0]
        self.assertEqual(kw.get("creationflags", 0) & CREATE_NO_WINDOW, CREATE_NO_WINDOW)
        self.assertEqual(kw.get("input"), "prompt body")           # the flag did not displace anything
        self.assertEqual(kw.get("timeout"), 5)
        self.assertTrue(kw.get("capture_output"))
        self.assertEqual(kw.get("encoding"), "utf-8")
        self.assertEqual(kw.get("errors"), "replace")

    def test_run_without_input_still_closes_stdin_and_sets_the_flag(self):
        procs.run(["gh", "auth", "status"])
        argv, kw = self.rec.calls[0]
        self.assertEqual(kw.get("stdin"), subprocess.DEVNULL)
        self.assertEqual(kw.get("creationflags", 0) & CREATE_NO_WINDOW, CREATE_NO_WINDOW)

    def test_run_retry_sets_the_flag_on_every_attempt(self):
        self.rec.codes = [1, 1, 0]                                  # two failures, then success
        res = procs.run_retry(["gh", "pr", "view"], retries=2, wait=0)
        self.assertTrue(res.ok)
        self.assertEqual(len(self.rec.calls), 3)
        for i in range(3):
            self.assertEqual(self.flags(i) & CREATE_NO_WINDOW, CREATE_NO_WINDOW, "attempt %d" % (i + 1))

    def test_the_flag_is_only_the_no_window_bit(self):
        # no console must be created, but the child must not be detached from the pipes either:
        # DETACHED_PROCESS (0x8) or CREATE_NEW_CONSOLE (0x10) would defeat the capture
        procs.run(["git", "status"])
        self.assertEqual(self.flags(), CREATE_NO_WINDOW)

    def test_the_argv_is_still_passed_as_strings(self):
        procs.run(["git", "log", "-n", 3])
        argv, _ = self.rec.calls[0]
        self.assertEqual(argv, ["git", "log", "-n", "3"])


class WindowsRunShellPassesTheFlag(NtRecorderCase):
    """AC-1 on Windows for `run_shell`, the path of the `lint` line (preflight) and of every
    `local` runner step (runners.LocalRunner). Round 2, F1."""

    def test_run_shell_sets_create_no_window(self):
        res = procs.run_shell("python --version")
        self.assertTrue(res.ok)
        self.assertEqual(len(self.rec.calls), 1)
        self.assertEqual(self.flags() & CREATE_NO_WINDOW, CREATE_NO_WINDOW)

    def test_run_shell_keeps_the_shell_and_the_capture(self):
        procs.run_shell("make test", cwd=os.getcwd(), timeout=7)
        argv, kw = self.rec.calls[0]
        self.assertEqual(argv, "make test")                          # the string goes to the shell as is
        self.assertTrue(kw.get("shell"))                             # the flag did not displace shell=True
        self.assertEqual(kw.get("cwd"), os.getcwd())
        self.assertEqual(kw.get("timeout"), 7)
        self.assertTrue(kw.get("capture_output"))
        self.assertEqual(kw.get("encoding"), "utf-8")
        self.assertEqual(kw.get("errors"), "replace")

    def test_run_shell_flag_is_only_the_no_window_bit(self):
        procs.run_shell("echo lint")
        self.assertEqual(self.flags(), CREATE_NO_WINDOW)

    def test_run_shell_non_zero_exit_is_still_a_result_not_an_exception(self):
        self.rec.codes = [3]
        res = procs.run_shell("false")
        self.assertFalse(res.ok)
        self.assertEqual(res.returncode, 3)
        self.assertEqual(self.flags() & CREATE_NO_WINDOW, CREATE_NO_WINDOW)


class PosixPassesNothing(unittest.TestCase):
    """AC-1 elsewhere: `creationflags` is a Windows-only argument and must not appear."""

    def setUp(self):
        self.rec = Recorder()
        patch_name = mock.patch("os.name", "posix")
        patch_run = mock.patch("subprocess.run", self.rec)
        patch_name.start()
        patch_run.start()
        self.addCleanup(patch_name.stop)
        self.addCleanup(patch_run.stop)

    def test_run_passes_no_creationflags(self):
        procs.run(["git", "--version"])
        self.assertEqual(len(self.rec.calls), 1)
        self.assertNotIn("creationflags", self.rec.calls[0][1])

    def test_run_retry_passes_no_creationflags(self):
        self.rec.codes = [1, 0]
        procs.run_retry(["gh", "auth", "status"], retries=1, wait=0)
        self.assertEqual(len(self.rec.calls), 2)
        for _, kw in self.rec.calls:
            self.assertNotIn("creationflags", kw)

    def test_run_shell_passes_no_creationflags(self):
        procs.run_shell("echo lint")
        self.assertEqual(len(self.rec.calls), 1)
        argv, kw = self.rec.calls[0]
        self.assertTrue(kw.get("shell"))
        self.assertNotIn("creationflags", kw)


class ErrorsStillSurface(unittest.TestCase):
    """AC-1 must not swallow the failure paths `run` already had."""

    def test_timeout_is_still_reported_with_the_flag_on(self):
        def slow(argv, **kw):
            raise subprocess.TimeoutExpired(argv, kw.get("timeout"))

        with mock.patch("os.name", "nt"), mock.patch("subprocess.run", slow):
            with self.assertRaises(procs.ProcTimeout):
                procs.run(["git", "fetch"], timeout=1)

    def test_missing_executable_is_still_reported_with_the_flag_on(self):
        def missing(argv, **kw):
            raise FileNotFoundError(argv[0])

        with mock.patch("os.name", "nt"), mock.patch("subprocess.run", missing):
            with self.assertRaises(procs.ExeNotFound):
                procs.run(["no-such-exe"])

    def test_run_shell_timeout_is_still_reported_with_the_flag_on(self):
        def slow(cmd, **kw):
            raise subprocess.TimeoutExpired(cmd, kw.get("timeout"))

        with mock.patch("os.name", "nt"), mock.patch("subprocess.run", slow):
            with self.assertRaises(procs.ProcTimeout):
                procs.run_shell("sleep 100", timeout=1)


if __name__ == "__main__":
    unittest.main()
