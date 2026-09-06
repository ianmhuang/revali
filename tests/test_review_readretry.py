"""AC-3 of fix/issue-followups: `State.load` opening `state.json` while a writer is inside its
rename (PermissionError on Windows) keeps trying for the writer's window, `[paths]
write_retry_s` of the repository holding the file (defaults.toml outside one), returns the
parsed state once the file is released, and re-raises the original error after the window.
Other errors are not retried; a zero window is one attempt. Time is faked."""

import builtins
import os
import tempfile
import unittest
from unittest import mock

from revali.config import load_defaults
from revali.state import State, write_json_atomic
from tests.helpers import RepoCase, rmtree_force

DENIED = PermissionError(13, "Access is denied")


class FakeClock:
    def __init__(self):
        self.now = 500.0
        self.slept = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


def fake_clock(case):
    clock = FakeClock()
    for name in ("monotonic", "sleep"):
        patcher = mock.patch("revali.state.time." + name, getattr(clock, name))
        patcher.start()
        case.addCleanup(patcher.stop)
    return clock


class Refusing:
    """`open` that raises `error` for `path` the first `refusals` times (always when None) and
    delegates everything else to the real open."""

    def __init__(self, path, refusals, error=DENIED):
        self.path = os.path.normcase(os.path.abspath(path))
        self.refusals = refusals
        self.error = error
        self.calls = 0
        self.real = builtins.open

    def __call__(self, file, *args, **kwargs):
        if isinstance(file, str) and os.path.normcase(os.path.abspath(file)) == self.path:
            self.calls += 1
            if self.refusals is None or self.calls <= self.refusals:
                raise self.error
        return self.real(file, *args, **kwargs)


def refuse(case, path, refusals, error=DENIED):
    opener = Refusing(path, refusals, error)
    patcher = mock.patch("revali.state.open", opener, create=True)
    patcher.start()
    case.addCleanup(patcher.stop)
    return opener


class OutsideARepository(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="revali read retry ")
        self.addCleanup(rmtree_force, self.tmp)
        self.path = State.path(self.tmp)
        self.clock = fake_clock(self)
        self.default = load_defaults()["paths"]["write_retry_s"]
        State(branch="feature/mul", stage="validate", round=2, message="m").save(self.tmp)

    def test_load_returns_the_state_once_the_writer_lets_go(self):
        opener = refuse(self, self.path, refusals=3)
        start = self.clock.now
        state = State.load(self.tmp)  # AC-3: no error
        self.assertIsNotNone(state)
        self.assertEqual((state.branch, state.stage, state.round), ("feature/mul", "validate", 2))
        self.assertEqual(opener.calls, 4)  # three refusals, then the read
        self.assertLess(self.clock.now - start, self.default)
        self.assertEqual(len(self.clock.slept), 3)
        self.assertTrue(all(0 < s <= 0.5 for s in self.clock.slept), self.clock.slept)

    def test_after_the_window_the_original_error_propagates(self):
        opener = refuse(self, self.path, refusals=None)
        start = self.clock.now
        with self.assertRaises(PermissionError) as cm:
            State.load(self.tmp)
        self.assertIs(cm.exception, DENIED)  # AC-3: the original error
        elapsed = self.clock.now - start
        self.assertGreaterEqual(elapsed, self.default)  # AC-3: the defaults.toml window
        self.assertLess(elapsed, self.default + 1.0)
        self.assertGreater(opener.calls, 1)

    def test_other_errors_are_not_retried(self):
        gone = OSError(22, "Invalid argument")
        opener = refuse(self, self.path, refusals=None, error=gone)
        with self.assertRaises(OSError) as cm:
            State.load(self.tmp)
        self.assertIs(cm.exception, gone)  # AC-3: not retried, not wrapped
        self.assertEqual(opener.calls, 1)
        self.assertEqual(self.clock.slept, [])

    def test_a_missing_file_is_still_none(self):
        refuse(self, self.path, refusals=None)
        self.assertIsNone(State.load(os.path.join(self.tmp, "nowhere")))
        self.assertEqual(self.clock.slept, [])

    def test_the_writer_keeps_its_window_too(self):
        attempts = []
        real = os.replace

        def replace(src, dst):
            attempts.append(dst)
            if len(attempts) <= 2:
                raise DENIED
            real(src, dst)

        with mock.patch("revali.state.os.replace", replace):
            write_json_atomic(self.path, {"stage": "done"}, retry_s=1.0)
        self.assertEqual(len(attempts), 3)
        state = State.load(self.tmp)
        self.assertEqual(state.stage, "done")


class InsideARepository(RepoCase):
    """The window is the layered `[paths] write_retry_s` of the repository holding the file:
    the same lookup the writer uses."""

    def setUp(self):
        super().setUp()
        self.clock = fake_clock(self)
        self.default = load_defaults()["paths"]["write_retry_s"]
        State(branch="feature/mul", stage="review").save(self.rdir())
        self.path = State.path(self.rdir())
        self.toml = self.read("revali.toml")

    def set_window(self, window):
        self.write("revali.toml", self.toml + "\n[paths]\nwrite_retry_s = %s\n" % window)

    def blocked_load_window(self):
        opener = refuse(self, self.path, refusals=None)
        start = self.clock.now
        with self.assertRaises(PermissionError):
            State.load(self.rdir())
        self.assertGreater(opener.calls, 1)
        return self.clock.now - start

    def test_nothing_configured_means_the_default(self):
        elapsed = self.blocked_load_window()
        self.assertGreaterEqual(elapsed, self.default)
        self.assertLess(elapsed, self.default + 0.5)

    def test_a_longer_project_window_is_honoured(self):
        self.set_window("4.0")  # well above the default
        elapsed = self.blocked_load_window()
        self.assertGreaterEqual(elapsed, 4.0)  # AC-3: the configured window
        self.assertLess(elapsed, 4.5)

    def test_a_shorter_project_window_is_honoured(self):
        self.set_window("0.35")  # below the default and off the 0.02/0.04/... pause grid
        elapsed = self.blocked_load_window()
        self.assertGreaterEqual(elapsed, 0.35)  # AC-3
        self.assertLess(elapsed, 0.85)
        self.assertLess(elapsed, self.default)

    def test_a_zero_window_is_a_single_attempt(self):
        self.set_window("0.0")
        opener = refuse(self, self.path, refusals=None)
        with self.assertRaises(PermissionError):
            State.load(self.rdir())
        self.assertEqual(opener.calls, 1)  # AC-3
        self.assertEqual(self.clock.slept, [])

    def test_released_inside_the_configured_window(self):
        self.set_window("0.35")
        opener = refuse(self, self.path, refusals=2)
        state = State.load(self.rdir())
        self.assertEqual(state.stage, "review")
        self.assertEqual(opener.calls, 3)


if __name__ == "__main__":
    unittest.main()
