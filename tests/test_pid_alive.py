"""procs.pid_alive on Windows counts a process as alive until its handle is signalled
(fix/windows-pid-alive). The exit code is set before the process object is signalled, so a
process that is still exiting used to read as dead, and `revali stop` returned while the run
it killed was still exiting."""

import ctypes  # noqa: F401  imported before os.name is patched: ctypes reads it on import
import os
import subprocess
import sys
import time
import unittest
from unittest import mock

from revali import procs

SYNCHRONIZE = 0x00100000
QUERY_LIMITED = 0x1000
WAIT_OBJECT_0 = 0x0
WAIT_TIMEOUT = 0x102
WAIT_FAILED = 0xFFFFFFFF
STILL_ACTIVE = 259


def dead_pid():
    """A pid no process has; not a just-exited child's pid, which Windows reuses at once."""
    return 2**31 - 1  # above any pid range on Windows and Linux


class FakeKernel32:
    """The four kernel32 calls pid_alive makes. `opens` maps an access mask to the handle
    OpenProcess returns for it (0 = refused)."""

    def __init__(self, opens, wait=WAIT_TIMEOUT, code=STILL_ACTIVE, code_ok=True):
        self.opens = opens
        self.wait = wait
        self.code = code
        self.code_ok = code_ok
        self.calls = []
        self.closed = []

    def OpenProcess(self, access, inherit, pid):
        self.calls.append(("OpenProcess", access, pid))
        return self.opens.get(access, 0)

    def WaitForSingleObject(self, handle, ms):
        self.calls.append(("WaitForSingleObject", handle, ms))
        return self.wait

    def GetExitCodeProcess(self, handle, ref):
        self.calls.append(("GetExitCodeProcess", handle))
        ref._obj.value = self.code
        return 1 if self.code_ok else 0

    def CloseHandle(self, handle):
        self.closed.append(handle)
        return 1

    def names(self):
        return [c[0] for c in self.calls]


class WithFakeKernel32(unittest.TestCase):
    """The Windows branch with kernel32 replaced, so it runs on every platform."""

    def alive(self, k32, pid=4242):
        with mock.patch("os.name", "nt"), mock.patch.object(procs, "_kernel32", lambda: k32):
            return procs.pid_alive(pid)

    def test_an_unsignalled_handle_is_alive_whatever_the_exit_code(self):
        k32 = FakeKernel32({SYNCHRONIZE: 7}, wait=WAIT_TIMEOUT, code=0)
        self.assertTrue(self.alive(k32))
        self.assertEqual(k32.calls[0], ("OpenProcess", SYNCHRONIZE, 4242))
        self.assertEqual(k32.calls[1], ("WaitForSingleObject", 7, 0))
        self.assertNotIn("GetExitCodeProcess", k32.names())
        self.assertEqual(k32.closed, [7])

    def test_a_signalled_handle_is_dead_even_if_the_exit_code_said_active(self):
        # an exit code of 259 is also a code a process may exit with
        k32 = FakeKernel32({SYNCHRONIZE: 7}, wait=WAIT_OBJECT_0, code=STILL_ACTIVE)
        self.assertFalse(self.alive(k32))
        self.assertEqual(k32.closed, [7])

    def test_a_failed_wait_is_dead(self):
        k32 = FakeKernel32({SYNCHRONIZE: 7}, wait=WAIT_FAILED)
        self.assertFalse(self.alive(k32))
        self.assertEqual(k32.closed, [7])

    def test_without_synchronize_access_the_exit_code_decides_as_before(self):
        k32 = FakeKernel32({QUERY_LIMITED: 9}, code=STILL_ACTIVE)
        self.assertTrue(self.alive(k32))
        self.assertEqual(k32.names(), ["OpenProcess", "OpenProcess", "GetExitCodeProcess"])
        self.assertEqual(k32.closed, [9])
        k32 = FakeKernel32({QUERY_LIMITED: 9}, code=1)
        self.assertFalse(self.alive(k32))
        self.assertEqual(k32.closed, [9])
        k32 = FakeKernel32({QUERY_LIMITED: 9}, code_ok=False)
        self.assertFalse(self.alive(k32))
        self.assertEqual(k32.closed, [9])

    def test_a_process_that_cannot_be_opened_is_dead(self):
        k32 = FakeKernel32({})
        self.assertFalse(self.alive(k32))
        self.assertEqual(k32.names(), ["OpenProcess", "OpenProcess"])
        self.assertEqual(k32.closed, [])

    def test_a_pid_of_zero_or_below_is_dead_without_a_call(self):
        for pid in (0, -1):
            k32 = FakeKernel32({SYNCHRONIZE: 7})
            self.assertFalse(self.alive(k32, pid))
            self.assertEqual(k32.calls, [])


@unittest.skipUnless(os.name == "nt", "the race is in the Windows branch")
class OnTheWindowsHost(unittest.TestCase):
    def test_the_current_process_is_alive(self):
        self.assertTrue(procs.pid_alive(os.getpid()))

    def test_a_pid_no_process_has_is_dead(self):
        self.assertFalse(procs.pid_alive(dead_pid()))

    def test_a_live_child_is_alive(self):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        self.addCleanup(lambda: child.poll() is None and child.kill())
        self.assertTrue(procs.pid_alive(child.pid))

    def test_dead_by_pid_alive_means_exited_by_the_handle(self):
        # revali stop's sequence: kill_tree, then pid_alive until False
        for i in range(100):
            child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
            self.addCleanup(lambda c=child: c.poll() is None and c.kill())
            time.sleep(0.02)
            procs.kill_tree(child.pid)
            for _ in range(50):
                if not procs.pid_alive(child.pid):
                    break
                time.sleep(0.1)
            else:
                self.fail("kill %d: still alive 5 s after kill_tree" % i)
            self.assertIsNotNone(child.poll(), "kill %d: pid_alive False, handle not signalled" % i)


if __name__ == "__main__":
    unittest.main()
