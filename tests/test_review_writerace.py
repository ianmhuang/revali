"""AC-1 of fix/state-write-race: a state-file write whose rename is refused with
PermissionError (Windows: a reader such as `wait` holds the file open) is retried for a
bounded window, succeeds once the target is released, and otherwise re-raises the original
error. The temporary file never survives and the previous content is never clobbered. The
window is `[paths] write_retry_s`: defaults.toml, overridden by the user file, overridden by
the project's revali.toml, for the repository that holds the file. Time is faked where the
window itself is measured, so those tests are deterministic and quick."""
import json
import os
import tempfile
import unittest
from unittest import mock

from tests.helpers import RepoCase, claude_entry, rmtree_force, run_cli
from revali import EXIT_OK
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


def install_fake_clock(case):
    clock = FakeClock()
    for name in ("monotonic", "sleep"):
        patcher = mock.patch("revali.state.time." + name, getattr(clock, name))
        patcher.start()
        case.addCleanup(patcher.stop)
    return clock


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
        self.clock = install_fake_clock(self)

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


class ConfiguredWindow(RepoCase):
    """The window a real state.json write gets is the layered [paths] write_retry_s of the
    repository that holds it (round 1, F1: the user and project files must be honoured, since
    the template and README say they can set the key)."""

    def setUp(self):
        super().setUp()
        self.clock = install_fake_clock(self)
        self.default = load_defaults()["paths"]["write_retry_s"]

    def user_config(self, text):
        with open(os.path.join(self.home, "config.toml"), "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)

    def blocked_save_window(self, directory=None):
        """Fake seconds a State.save under `directory` keeps retrying before it gives up."""
        replace = RefusingReplace(refusals=None)
        start = self.clock.now
        with mock.patch("revali.state.os.replace", replace):
            with self.assertRaises(PermissionError):
                State(branch="feature/mul", stage="review").save(directory or self.rdir())
        self.assertGreater(replace.calls, 1)
        return self.clock.now - start

    def test_the_user_file_sets_the_window(self):
        elapsed = self.blocked_save_window()
        self.assertGreaterEqual(elapsed, self.default)                                # nothing set: defaults.toml
        self.assertLess(elapsed, self.default + 0.5)
        # 0.35 is off the 0.02/0.04/0.08/0.16/0.2 pause grid: a window equal to a partial sum
        # (0.3) lands a rounding error under itself on the fake clock (validation 1)
        self.user_config("[paths]\nwrite_retry_s = 0.35\n")
        elapsed = self.blocked_save_window()
        self.assertGreaterEqual(elapsed, 0.35)                                        # AC-1 window, from ~/.revali/config.toml
        self.assertLess(elapsed, 0.35 + 0.5)
        self.assertLess(elapsed, self.default)                                        # the override took effect

    def test_the_project_file_wins_over_the_user_file(self):
        self.user_config("[paths]\nwrite_retry_s = 0.3\n")
        self.write("revali.toml", self.read("revali.toml") + "\n[paths]\nwrite_retry_s = 4.0\n")
        elapsed = self.blocked_save_window()
        self.assertGreaterEqual(elapsed, 4.0)                                         # revali.toml is the most specific layer
        self.assertLess(elapsed, 4.5)

    def test_a_file_outside_any_repository_keeps_the_default(self):
        self.user_config("[paths]\nwrite_retry_s = 0.3\n")
        self.write("revali.toml", self.read("revali.toml") + "\n[paths]\nwrite_retry_s = 4.0\n")
        outside = tempfile.mkdtemp(prefix="revali outside ")
        self.addCleanup(rmtree_force, outside)
        elapsed = self.blocked_save_window(outside)
        self.assertGreaterEqual(elapsed, self.default)                                # neither layer applies out there
        self.assertLess(elapsed, self.default + 0.5)


class RefuseStateJsonBriefly:
    """os.replace that refuses the first `refusals` attempts of every state.json write and lets
    the next one through: a reader that holds the file for a moment at every stage change."""

    def __init__(self, refusals):
        self.refusals = refusals
        self.real = os.replace
        self.attempts = {}     # temp file -> attempts so far
        self.writes = 0

    def __call__(self, src, dst):
        if os.path.basename(dst) == "state.json":
            n = self.attempts.get(src, 0) + 1
            self.attempts[src] = n
            if n <= self.refusals:
                raise DENIED
            self.writes += 1
        self.real(src, dst)


class BlockedWritesInsideARun(RepoCase):
    """AC-1 end to end: every stage change of a real run meets a reader that lets go inside
    the window; the run completes as if nothing happened. Real time: each write is released
    on its third attempt (after 20 ms + 40 ms of pauses), well inside a half-second window."""

    def test_the_run_reaches_ready_to_merge_through_repeatedly_blocked_state_writes(self):
        self.write("revali.toml", self.read("revali.toml") + "\n[paths]\nwrite_retry_s = 0.5\n")
        self.commit_all("short state write window")
        self.claude(claude_entry())
        replace = RefuseStateJsonBriefly(refusals=2)
        with mock.patch("revali.state.os.replace", replace):
            code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)                                          # AC-1: the writes succeeded
        self.assertIn("READY TO MERGE", out)
        self.assertNotIn("ERROR", out)
        self.assertGreater(replace.writes, 3)                                         # several stage changes, each retried
        self.assertTrue(all(n == 3 for n in replace.attempts.values()), replace.attempts)
        state = State.load(self.rdir())
        self.assertEqual(state.stage, "ready_to_merge")
        self.assertEqual(state.last_exit, EXIT_OK)
        self.assertEqual([r["verdict"] for r in state.rounds], ["APPROVE"])
        self.assertEqual([v["result"] for v in state.validations], ["PASS"])
        self.assertEqual([f for f in os.listdir(self.rdir()) if f.startswith(".tmp-")], [])  # AC-1: no temp files left


if __name__ == "__main__":
    unittest.main()
