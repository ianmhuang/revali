"""AC-1 of fix/state-write-race: a state-file write whose rename is refused with
PermissionError (Windows: a reader such as `wait` holds the file open) is retried for a
bounded window taken from defaults.toml, succeeds once the target is released, and
otherwise re-raises the original error. The temporary file never survives and the
previous content is never clobbered. Time is faked so the tests are deterministic."""
import json
import os
import tempfile
import unittest
from unittest import mock

from tests.helpers import rmtree_force
from revali.config import load_defaults
from revali.state import State, write_json_atomic

DENIED = PermissionError(13, "Access is denied")


class FakeClock:
    """Replacement for time.monotonic / time.sleep: sleeping advances the clock, nobody waits."""

    def __init__(self):
        self.now = 1000.0
        self.slept = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


class RefusingReplace:
    """os.replace that raises PermissionError the first `refusals` times (always when None)."""

    def __init__(self, refusals):
        self.refusals = refusals
        self.calls = 0
        self.real = os.replace

    def __call__(self, src, dst):
        self.calls += 1
        if self.refusals is None or self.calls <= self.refusals:
            raise DENIED
        self.real(src, dst)


class WriteRetryCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="revali write race ")
        self.addCleanup(rmtree_force, self.tmp)
        self.path = os.path.join(self.tmp, "state.json")
        self.clock = FakeClock()
        for name in ("monotonic", "sleep"):
            patcher = mock.patch("revali.state.time." + name, getattr(self.clock, name))
            patcher.start()
            self.addCleanup(patcher.stop)

    def leftovers(self):
        return sorted(f for f in os.listdir(self.tmp) if f != "state.json")

    def contents(self):
        with open(self.path, "r", encoding="utf-8") as fh:
            return json.load(fh)


class RetryUntilReleased(WriteRetryCase):
    def test_write_succeeds_once_the_reader_lets_go(self):
        replace = RefusingReplace(refusals=4)
        start = self.clock.now
        with mock.patch("revali.state.os.replace", replace):
            write_json_atomic(self.path, {"stage": "validate"}, retry_s=2.0)          # AC-1: succeeds
        self.assertEqual(replace.calls, 5)                                            # 4 refusals, then the rename
        self.assertEqual(self.contents(), {"stage": "validate"})
        self.assertEqual(self.leftovers(), [])                                        # AC-1: temp file gone
        self.assertLess(self.clock.now - start, 2.0)                                  # released inside the window
        self.assertTrue(all(0 < s <= 0.5 for s in self.clock.slept), self.clock.slept)  # short sleeps

    def test_state_save_survives_a_reader(self):
        State(branch="feature/mul", stage="review", message="reviewer round 1").save(self.tmp)
        replace = RefusingReplace(refusals=3)
        with mock.patch("revali.state.os.replace", replace):
            state = State.load(self.tmp)
            state.set_stage(self.tmp, "validate", "review approved in round 1; validating")
        self.assertEqual(replace.calls, 4)
        loaded = State.load(self.tmp)
        self.assertEqual(loaded.stage, "validate")                                    # AC-1: the stage change landed
        self.assertEqual(loaded.branch, "feature/mul")
        self.assertEqual(self.leftovers(), [])


class GiveUp(WriteRetryCase):
    def test_original_error_propagates_after_the_window_and_the_old_file_is_intact(self):
        write_json_atomic(self.path, {"stage": "review"})
        replace = RefusingReplace(refusals=None)
        start = self.clock.now
        with mock.patch("revali.state.os.replace", replace):
            with self.assertRaises(PermissionError) as cm:
                write_json_atomic(self.path, {"stage": "validate"}, retry_s=0.5)      # AC-1: propagates
        self.assertIs(cm.exception, DENIED)                                           # the original error, not a wrapper
        elapsed = self.clock.now - start
        self.assertGreaterEqual(elapsed, 0.5)                                         # kept trying for the window
        self.assertLess(elapsed, 1.5)                                                 # and not much longer (bounded)
        self.assertGreater(replace.calls, 1)
        self.assertEqual(self.contents(), {"stage": "review"})                        # never clobbered
        self.assertEqual(self.leftovers(), [])                                        # AC-1: temp file gone

    def test_window_defaults_to_the_constant_in_defaults_toml(self):
        configured = load_defaults()["paths"]["write_retry_s"]
        self.assertGreaterEqual(configured, 0.5)                                      # AC-1: "about 2 seconds"
        self.assertLessEqual(configured, 10)
        replace = RefusingReplace(refusals=None)
        start = self.clock.now
        with mock.patch("revali.state.os.replace", replace):
            with self.assertRaises(PermissionError):
                write_json_atomic(self.path, {"stage": "validate"})                   # no retry_s: the default
        elapsed = self.clock.now - start
        self.assertGreaterEqual(elapsed, configured)
        self.assertLess(elapsed, configured + 1.0)
        self.assertFalse(os.path.exists(self.path))
        self.assertEqual(self.leftovers(), [])

    def test_a_zero_window_means_a_single_attempt(self):
        replace = RefusingReplace(refusals=None)
        with mock.patch("revali.state.os.replace", replace):
            with self.assertRaises(PermissionError):
                write_json_atomic(self.path, {"a": 1}, retry_s=0)
        self.assertEqual(replace.calls, 1)
        self.assertEqual(self.leftovers(), [])


class OtherErrors(WriteRetryCase):
    def test_other_os_errors_are_not_retried(self):
        write_json_atomic(self.path, {"stage": "review"})
        invalid = OSError(22, "Invalid argument")                                     # plain OSError, not a PermissionError
        with mock.patch("revali.state.os.replace", side_effect=invalid) as replace:
            with self.assertRaises(OSError) as cm:
                write_json_atomic(self.path, {"stage": "validate"}, retry_s=2.0)
        self.assertIs(cm.exception, invalid)
        self.assertEqual(replace.call_count, 1)                                       # AC-1: only PermissionError retries
        self.assertEqual(self.clock.slept, [])
        self.assertEqual(self.contents(), {"stage": "review"})
        self.assertEqual(self.leftovers(), [])


if __name__ == "__main__":
    unittest.main()
