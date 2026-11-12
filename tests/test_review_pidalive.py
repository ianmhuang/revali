"""Acceptance tests for fix/windows-pid-alive: on Windows `procs.pid_alive` answers from the
process handle (alive until it is signalled), not from the exit code, which is set while the
process is still exiting.

The Windows branch is driven through a model of kernel32 installed as `ctypes.windll`, the
boundary the function calls on every revision, so the cases run on every platform and a
revision that reads the exit code gives a wrong answer here instead of an error."""

import ctypes  # imported before os.name is patched: ctypes reads os.name on import
import os
import subprocess
import sys
import time
import types
import unittest
from unittest import mock

from revali import procs

SYNCHRONIZE = 0x00100000
QUERY_LIMITED = 0x1000
WAIT_OBJECT_0 = 0x0
WAIT_TIMEOUT = 0x102
WAIT_FAILED = 0xFFFFFFFF
STILL_ACTIVE = 259
PID = 4242
NO_SUCH_PID = 2**31 - 1  # above the pid range of Windows and Linux


class ModelKernel32:
    """One process as kernel32 shows it. `signalled` is whether the process object is
    signalled (the process has fully exited); `exit_code` is what GetExitCodeProcess reports,
    which is no longer 259 once the process has started to exit. `grants` is the access mask
    OpenProcess allows. A handle only serves the calls its access covers, as on Windows."""

    def __init__(self, signalled, exit_code, grants=SYNCHRONIZE | QUERY_LIMITED, exists=True):
        self.signalled = signalled
        self.exit_code = exit_code
        self.grants = grants
        self.exists = exists
        self.handles = {}  # open handle -> access
        self.next_handle = 4
        self.opened_pids = []
        self.wait_timeouts = []

    def OpenProcess(self, access, inherit, pid):
        self.opened_pids.append(pid)
        if not self.exists or access & ~self.grants:
            return 0
        handle = self.next_handle
        self.next_handle += 4
        self.handles[handle] = access
        return handle

    def WaitForSingleObject(self, handle, ms):
        self.wait_timeouts.append(ms)
        if not self.handles.get(handle, 0) & SYNCHRONIZE:
            return WAIT_FAILED
        return WAIT_OBJECT_0 if self.signalled else WAIT_TIMEOUT

    def GetExitCodeProcess(self, handle, ref):
        if not self.handles.get(handle, 0) & QUERY_LIMITED:
            return 0
        target = getattr(ref, "_obj", None)  # byref(...)
        if target is None:
            target = ref.contents  # pointer(...)
        target.value = self.exit_code
        return 1

    def CloseHandle(self, handle):
        return 1 if self.handles.pop(handle, None) is not None else 0

    def GetLastError(self):
        return 5  # ERROR_ACCESS_DENIED


def ask(k32, pid=PID):
    """pid_alive(pid) as the Windows branch answers it with `k32` as kernel32."""
    windll = types.SimpleNamespace(kernel32=k32)
    with (
        mock.patch("os.name", "nt"),
        mock.patch.object(ctypes, "windll", windll, create=True),
    ):
        return procs.pid_alive(pid)


class TheHandleDecidesOnWindows(unittest.TestCase):
    """AC-1: alive while the process object is not signalled, dead once it is."""

    def test_an_exiting_process_is_alive_although_its_exit_code_is_set(self):
        # the window the fix closes: killed, exit code already 1 or 0, object not signalled
        for code in (1, 0):
            k32 = ModelKernel32(signalled=False, exit_code=code)
            self.assertTrue(ask(k32), "exit code %d, handle not signalled" % code)

    def test_a_running_process_is_alive(self):
        self.assertTrue(ask(ModelKernel32(signalled=False, exit_code=STILL_ACTIVE)))

    def test_an_exited_process_is_dead(self):
        self.assertFalse(ask(ModelKernel32(signalled=True, exit_code=1)))
        self.assertFalse(ask(ModelKernel32(signalled=True, exit_code=0)))

    def test_a_process_that_exited_with_259_is_dead(self):
        # 259 is STILL_ACTIVE, and also a code a process may exit with; the handle knows
        self.assertFalse(ask(ModelKernel32(signalled=True, exit_code=STILL_ACTIVE)))

    def test_the_wait_has_a_zero_timeout_and_is_on_the_pid_asked_about(self):
        for signalled in (False, True):
            k32 = ModelKernel32(signalled=signalled, exit_code=1)
            ask(k32)
            self.assertEqual(k32.wait_timeouts, [0])  # one wait, never blocking
            self.assertEqual(set(k32.opened_pids), {PID})

    def test_no_handle_is_left_open(self):
        for signalled, code in ((False, STILL_ACTIVE), (False, 1), (True, 1)):
            k32 = ModelKernel32(signalled=signalled, exit_code=code)
            ask(k32)
            self.assertEqual(k32.handles, {})


class TheOtherAnswersOnWindows(unittest.TestCase):
    """AC-2: what pid_alive answered before for the cases that are not about exiting."""

    def test_a_pid_of_zero_or_below_is_dead_and_opens_nothing(self):
        for pid in (0, -1):
            k32 = ModelKernel32(signalled=False, exit_code=STILL_ACTIVE)
            self.assertFalse(ask(k32, pid))
            self.assertEqual(k32.opened_pids, [])

    def test_a_pid_no_process_has_is_dead(self):
        k32 = ModelKernel32(signalled=False, exit_code=STILL_ACTIVE, exists=False)
        self.assertFalse(ask(k32))
        self.assertEqual(k32.handles, {})

    def test_a_process_it_may_not_open_is_dead(self):
        k32 = ModelKernel32(signalled=False, exit_code=STILL_ACTIVE, grants=0)
        self.assertFalse(ask(k32))
        self.assertEqual(k32.handles, {})

    def test_a_process_that_only_grants_query_access_keeps_its_old_answer(self):
        k32 = ModelKernel32(signalled=False, exit_code=STILL_ACTIVE, grants=QUERY_LIMITED)
        self.assertTrue(ask(k32))
        self.assertEqual(k32.handles, {})
        k32 = ModelKernel32(signalled=True, exit_code=1, grants=QUERY_LIMITED)
        self.assertFalse(ask(k32))
        self.assertEqual(k32.handles, {})


def sleeper():
    """A child that has started Python and sleeps; returned once it has said so."""
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; print('up', flush=True); time.sleep(120)"],
        stdout=subprocess.PIPE,
    )
    child.stdout.readline()
    return child


def put_down(child):
    if child.poll() is None:
        child.kill()
        child.wait(timeout=30)
    child.stdout.close()


@unittest.skipUnless(os.name == "nt", "the real kernel32")
class OnTheWindowsHost(unittest.TestCase):
    def test_the_current_process_is_alive(self):  # AC-2
        self.assertTrue(procs.pid_alive(os.getpid()))

    def test_a_pid_no_process_has_is_dead(self):  # AC-2
        self.assertFalse(procs.pid_alive(NO_SUCH_PID))

    def test_a_child_is_alive_until_it_has_exited(self):  # AC-1, AC-2
        child = sleeper()
        self.addCleanup(put_down, child)
        self.assertTrue(procs.pid_alive(child.pid))
        child.kill()
        child.wait(timeout=30)
        # the Popen object still holds a handle, so the pid still names this process
        self.assertFalse(procs.pid_alive(child.pid))

    def test_once_pid_alive_is_false_the_killed_child_has_exited(self):  # AC-1
        # revali stop's sequence: kill_tree, then pid_alive every 0.1 s until False
        for i in range(20):
            child = sleeper()
            self.addCleanup(put_down, child)
            procs.kill_tree(child.pid)
            deadline = time.monotonic() + 30
            while procs.pid_alive(child.pid):
                self.assertLess(time.monotonic(), deadline, "kill %d: never reported dead" % i)
                time.sleep(0.1)
            self.assertIsNotNone(child.poll(), "kill %d: reported dead while still exiting" % i)


@unittest.skipIf(os.name == "nt", "the POSIX branch")
class OnPosix(unittest.TestCase):
    """AC-3: the POSIX answers are the ones from before, and kernel32 plays no part."""

    def test_the_current_process_is_alive(self):
        self.assertTrue(procs.pid_alive(os.getpid()))

    def test_a_pid_of_zero_or_below_is_dead(self):
        self.assertFalse(procs.pid_alive(0))
        self.assertFalse(procs.pid_alive(-1))

    def test_a_pid_no_process_has_is_dead(self):
        self.assertFalse(procs.pid_alive(NO_SUCH_PID))

    def test_a_child_is_alive_until_it_is_reaped(self):
        child = sleeper()
        self.addCleanup(put_down, child)
        self.assertTrue(procs.pid_alive(child.pid))
        child.kill()
        self.assertTrue(procs.pid_alive(child.pid))  # a zombie still has its pid
        child.wait(timeout=30)
        self.assertFalse(procs.pid_alive(child.pid))

    def test_kernel32_is_not_consulted(self):
        k32 = mock.Mock()
        windll = types.SimpleNamespace(kernel32=k32)
        with mock.patch.object(ctypes, "windll", windll, create=True):
            self.assertTrue(procs.pid_alive(os.getpid()))
            self.assertFalse(procs.pid_alive(NO_SUCH_PID))
        self.assertEqual(k32.mock_calls, [])


if __name__ == "__main__":
    unittest.main()
