"""Reviewer acceptance tests for feature/revali-version-in-logs, AC-6 (PR #61 round 3
follow-up): `write_retry_s(path)`, the state-file window, gives the defaults.toml value when
the configured `[paths] write_retry_s` is `nan`, `inf` or negative, as `history_retry_s()`
does. Seen from the caller too: a state file that stays refused is given up after the default
window. Time is faked; a retry loop that would never end fails the test instead of hanging it.
"""

import builtins
import os
from unittest import mock

from revali.config import load_defaults
from revali.state import read_json_retry, write_retry_s
from tests.helpers import RepoCase

UNUSABLE = ("nan", "inf", "-inf", "-1", "-0.25")
RUNAWAY_S = 60.0


class Refused:
    """`open` that always raises PermissionError for one path."""

    def __init__(self, path):
        self.path = os.path.normcase(os.path.abspath(path))
        self.calls = 0
        self.real = builtins.open

    def __call__(self, file, *args, **kwargs):
        if isinstance(file, str) and os.path.normcase(os.path.abspath(file)) == self.path:
            self.calls += 1
            raise PermissionError(13, "Access is denied")
        return self.real(file, *args, **kwargs)


class Clock:
    def __init__(self, limit):
        self.now = 500.0
        self.limit = self.now + limit

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds
        if self.now > self.limit:
            raise AssertionError("still retrying %.1fs after the default window" % RUNAWAY_S)


class StateFileWindow(RepoCase):
    def setUp(self):
        super().setUp()
        self.default = float(load_defaults()["paths"]["write_retry_s"])
        self.assertGreater(self.default, 0.0)
        self.state_path = os.path.join(self.rdir(), "state.json")

    def user_window(self, value):
        path = os.path.join(self.home, "config.toml")
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("[paths]\nwrite_retry_s = %s\n" % value)

    def test_unusable_values_give_the_default(self):
        for value in UNUSABLE:
            with self.subTest(value=value):
                self.user_window(value)
                self.assertEqual(write_retry_s(self.state_path), self.default)
        # a usable value is still the window, zero included
        for value, window in (("0.4", 0.4), ("3", 3.0), ("0", 0.0)):
            with self.subTest(value=value):
                self.user_window(value)
                self.assertEqual(write_retry_s(self.state_path), window)

    def test_a_refused_state_file_is_given_up_after_the_default_window(self):
        for value in UNUSABLE:
            with self.subTest(value=value):
                self.user_window(value)
                clock = Clock(self.default + RUNAWAY_S)
                start = clock.now
                opener = Refused(self.state_path)
                with (
                    mock.patch("revali.state.open", opener, create=True),
                    mock.patch("revali.state.time.monotonic", clock.monotonic),
                    mock.patch("revali.state.time.sleep", clock.sleep),
                ):
                    with self.assertRaises(PermissionError):
                        read_json_retry(self.state_path)
                self.assertGreater(opener.calls, 1)  # it was retried
                self.assertGreaterEqual(clock.now - start, self.default)
                self.assertLess(clock.now - start, self.default + 1.0)
