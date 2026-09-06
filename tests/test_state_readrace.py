"""AC-3 of fix/issue-followups: `State.load` opening `state.json` while another process is
inside `write_json_atomic`'s rename (Windows: PermissionError) retries for the writer's window
(`[paths] write_retry_s`), returns the state once the file is released, and otherwise
re-raises the original error. Other errors are not retried. Time is faked, so the tests are
deterministic and quick."""

import builtins
import os
import tempfile
import unittest
from unittest import mock

from revali.config import load_defaults
from revali.state import State, read_json_retry, write_json_atomic
from tests.helpers import rmtree_force

DENIED = PermissionError(13, "Access is denied")


class FakeClock:
    def __init__(self):
        self.now = 1000.0
        self.slept = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


class RefusingOpen:
    """`open` that raises `error` the first `refusals` times it is asked for `path` (always
    when None); every other call goes to the real open."""

    def __init__(self, path, refusals, error=DENIED):
        self.path = os.path.abspath(path)
        self.refusals = refusals
        self.error = error
        self.calls = 0
        self.real = builtins.open

    def __call__(self, file, *args, **kwargs):
        if isinstance(file, str) and os.path.abspath(file) == self.path:
            self.calls += 1
            if self.refusals is None or self.calls <= self.refusals:
                raise self.error
        return self.real(file, *args, **kwargs)


class ReadRetryCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="revali read race ")
        self.addCleanup(rmtree_force, self.tmp)
        self.path = os.path.join(self.tmp, "state.json")
        self.clock = FakeClock()
        for name in ("monotonic", "sleep"):
            patcher = mock.patch("revali.state.time." + name, getattr(self.clock, name))
            patcher.start()
            self.addCleanup(patcher.stop)
        State(branch="feature/mul", stage="validate", message="validating").save(self.tmp)

    def refusing(self, refusals, error=DENIED):
        opener = RefusingOpen(self.path, refusals, error)
        patcher = mock.patch("revali.state.open", opener, create=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        return opener


class RetryUntilReleased(ReadRetryCase):
    def test_load_succeeds_once_the_writer_lets_go(self):
        opener = self.refusing(refusals=4)
        start = self.clock.now
        state = State.load(self.tmp)  # AC-3: no error
        self.assertEqual(opener.calls, 5)  # 4 refusals, then the read
        self.assertEqual((state.branch, state.stage), ("feature/mul", "validate"))
        self.assertLess(self.clock.now - start, load_defaults()["paths"]["write_retry_s"])
        self.assertTrue(all(0 < s <= 0.5 for s in self.clock.slept), self.clock.slept)

    def test_read_json_retry_honours_an_explicit_window(self):
        opener = self.refusing(refusals=2)
        data = read_json_retry(self.path, retry_s=1.0)
        self.assertEqual(opener.calls, 3)
        self.assertEqual(data["branch"], "feature/mul")


class GiveUp(ReadRetryCase):
    def test_original_error_propagates_after_the_window(self):
        opener = self.refusing(refusals=None)
        start = self.clock.now
        with self.assertRaises(PermissionError) as cm:
            read_json_retry(self.path, retry_s=0.5)  # AC-3: propagates
        self.assertIs(cm.exception, DENIED)  # the original error, not a wrapper
        elapsed = self.clock.now - start
        self.assertGreaterEqual(elapsed, 0.5)  # kept trying for the window
        self.assertLess(elapsed, 1.5)  # and not much longer
        self.assertGreater(opener.calls, 1)

    def test_load_uses_the_writers_window_by_default(self):
        configured = load_defaults()["paths"]["write_retry_s"]
        self.refusing(refusals=None)
        start = self.clock.now
        with self.assertRaises(PermissionError):
            State.load(self.tmp)  # AC-3: same window as write_json_atomic
        elapsed = self.clock.now - start
        self.assertGreaterEqual(elapsed, configured)
        self.assertLess(elapsed, configured + 1.0)

    def test_a_zero_window_means_a_single_attempt(self):
        opener = self.refusing(refusals=None)
        with self.assertRaises(PermissionError):
            read_json_retry(self.path, retry_s=0)
        self.assertEqual(opener.calls, 1)

    def test_other_errors_are_not_retried(self):
        lost = FileNotFoundError(2, "No such file")
        opener = self.refusing(refusals=None, error=lost)
        with self.assertRaises(FileNotFoundError) as cm:
            read_json_retry(self.path, retry_s=2.0)
        self.assertIs(cm.exception, lost)
        self.assertEqual(opener.calls, 1)  # AC-3: one attempt, no sleep
        self.assertEqual(self.clock.slept, [])


class WriterStillRetries(ReadRetryCase):
    def test_write_json_atomic_keeps_its_own_window(self):
        calls = []
        real = os.replace

        def replace(src, dst):
            calls.append(1)
            if len(calls) <= 3:
                raise DENIED
            real(src, dst)

        with mock.patch("revali.state.os.replace", replace):
            write_json_atomic(self.path, {"stage": "done"}, retry_s=2.0)
        self.assertEqual(len(calls), 4)
        self.assertEqual(read_json_retry(self.path)["stage"], "done")


if __name__ == "__main__":
    unittest.main()
