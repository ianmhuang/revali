"""AC-6 of fix/leftover-lows: on Windows a real child process holds `state.json` open with a
plain `open()` for a fraction of a second; `write_json_atomic` with the default window waits,
succeeds once the child closes the file, the file holds the new content and no `.tmp-` file
is left. Elsewhere `open()` does not block a rename, so the test is skipped with a reason."""

import os
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from revali.state import State, write_json_atomic
from tests.helpers import rmtree_force

HOLD_S = 0.5
HOLDER = (
    "import sys, time\n"
    "fh = open(sys.argv[1], 'r')\n"
    "print('holding', flush=True)\n"
    "time.sleep(float(sys.argv[2]))\n"
    "fh.close()\n"
    "print('released', flush=True)\n"
)


@unittest.skipUnless(sys.platform == "win32", "only Windows refuses a rename over an open file")
class RealReaderOnWindows(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="revali real reader ")
        self.addCleanup(rmtree_force, self.tmp)
        self.path = os.path.join(self.tmp, "state.json")
        write_json_atomic(self.path, {"stage": "review"})

    def hold(self, seconds):
        child = subprocess.Popen(
            [sys.executable, "-c", HOLDER, self.path, str(seconds)],
            stdout=subprocess.PIPE,
            text=True,
        )
        self.addCleanup(child.stdout.close)
        self.addCleanup(child.wait)  # cleanups run last-added first: wait, then close the pipe
        self.assertEqual(child.stdout.readline().strip(), "holding")
        return child

    def test_the_write_lands_once_the_child_lets_go(self):  # AC-6
        self.hold(HOLD_S)
        started = time.monotonic()
        with mock.patch("revali.state.os.replace", wraps=os.replace) as spy:
            write_json_atomic(self.path, {"stage": "validate"})  # the default window
        elapsed = time.monotonic() - started
        self.assertGreater(spy.call_count, 1, "the child's open() never blocked the rename")
        self.assertGreater(elapsed, HOLD_S / 4, "the write did not wait for the child")
        self.assertLess(elapsed, 2.0, "the default window is 2 s; the child let go before")
        self.assertEqual(State.load(self.tmp).stage, "validate")
        self.assertEqual([f for f in os.listdir(self.tmp) if f.startswith(".tmp-")], [])

    def test_a_reader_that_outlasts_the_window_gets_the_original_error(self):  # AC-6: bound
        child = self.hold(2.0)
        with self.assertRaises(PermissionError):
            write_json_atomic(self.path, {"stage": "validate"}, retry_s=0.3)
        self.assertEqual([f for f in os.listdir(self.tmp) if f.startswith(".tmp-")], [])
        child.wait()
        self.assertEqual(State.load(self.tmp).stage, "review")  # the old content is intact


if __name__ == "__main__":
    unittest.main()
