"""Appends to the run history under a lock (fix/history-append-lock, M3.4.2).

Measured before the change: 4 processes appending 200 rows each to one file at the same
moment lost 91 to 226 of the 800 rows on Windows (none on Linux ext4).
"""

import errno
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from revali import gitops
from revali.procs import Result
from revali.state import append_history, read_history
from tests.helpers import ROOT, rmtree_force

WRITERS = 4
ROWS = 200

# a writer: waits for the common start time, then appends ROWS rows through revali
WRITER = """import sys, time
sys.path.insert(0, %(root)r)
from revali.state import append_history
path, writer, start = sys.argv[1], int(sys.argv[2]), float(sys.argv[3])
while time.time() < start:
    time.sleep(0.001)
for i in range(%(rows)d):
    append_history(path, {"writer": writer, "i": i, "pad": "x" * 500})
"""

# a holder: takes the OS lock on <history>.lock the way AC-2 names it, writes the ready file,
# then holds it until killed (or for `seconds`, after which it writes `<ready>.released` while it
# still holds the lock, so a test can tell a release apart from the process ending)
HOLDER = """import os, sys, time
lock, ready, seconds = sys.argv[1], sys.argv[2], float(sys.argv[3])
fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o644)
if os.name == "nt":
    import msvcrt
    os.lseek(fd, 0, os.SEEK_SET)
    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
else:
    import fcntl
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
open(ready, "w").close()
time.sleep(seconds)
open(ready + ".released", "w").close()
"""


class HistoryCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="revali history ")
        self.addCleanup(rmtree_force, self.tmp)
        self.path = os.path.join(self.tmp, "history.jsonl")

    def script(self, name, text):
        path = os.path.join(self.tmp, name)
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        return path

    def hold_lock(self, seconds=60.0):
        """A process that holds the lock; returns it once the lock is taken."""
        ready = os.path.join(self.tmp, "ready")
        holder = self.script("holder.py", HOLDER)
        proc = subprocess.Popen([sys.executable, holder, self.path + ".lock", ready, str(seconds)])
        self.addCleanup(self._stop, proc)
        deadline = time.monotonic() + 20
        while not os.path.exists(ready):
            self.assertIsNone(proc.poll(), "the holder could not take the lock")
            self.assertLess(time.monotonic(), deadline, "the holder never took the lock")
            time.sleep(0.02)
        return proc

    @staticmethod
    def _stop(proc):
        if proc.poll() is None:
            proc.kill()
        proc.wait()


class ConcurrentWriters(HistoryCase):
    """AC-1."""

    def test_no_row_lost(self):
        writer = self.script("writer.py", WRITER % {"root": ROOT, "rows": ROWS})
        start = time.time() + 2.0
        procs = [
            subprocess.Popen([sys.executable, writer, self.path, str(w), repr(start)])
            for w in range(WRITERS)
        ]
        for proc in procs:
            self.assertEqual(proc.wait(timeout=120), 0)
        with open(self.path, "r", encoding="utf-8", newline="") as fh:
            lines = fh.read().split("\n")
        self.assertEqual(lines[-1], "")  # the file ends with a newline
        rows = [json.loads(line) for line in lines[:-1]]  # every line is JSON, none empty
        self.assertEqual(len(rows), WRITERS * ROWS)
        pairs = sorted((r["writer"], r["i"]) for r in rows)
        self.assertEqual(pairs, [(w, i) for w in range(WRITERS) for i in range(ROWS)])


class LockFile(HistoryCase):
    """AC-2, AC-3."""

    def test_lock_file_next_to_the_history(self):
        append_history(self.path, {"n": 1})
        self.assertTrue(os.path.isfile(self.path + ".lock"))
        self.assertEqual([r["n"] for r in read_history(self.path)], [1])

    def test_readers_are_not_blocked_by_a_writer(self):
        append_history(self.path, {"n": 1})
        self.hold_lock()
        started = time.monotonic()
        self.assertEqual([r["n"] for r in read_history(self.path)], [1])
        self.assertLess(time.monotonic() - started, 1.0)

    def test_a_writer_waits_for_the_holder(self):
        self.hold_lock(seconds=1.0)
        started = time.monotonic()
        self.assertEqual(append_history(self.path, {"n": 1}, retry_s=30), "")
        took = time.monotonic() - started
        # PR #61 F6: the holder writes the marker before it lets go, so it exists here
        self.assertTrue(
            os.path.exists(os.path.join(self.tmp, "ready.released")),
            "appended while the holder still held the lock",
        )
        self.assertLess(took, 20)
        self.assertEqual([r["n"] for r in read_history(self.path)], [1])

    def test_a_killed_holder_leaves_no_lock(self):
        holder = self.hold_lock()
        holder.kill()
        holder.wait()
        self.assertTrue(os.path.isfile(self.path + ".lock"))  # the file stays on disk
        started = time.monotonic()
        append_history(self.path, {"n": 1}, retry_s=30)
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertEqual([r["n"] for r in read_history(self.path)], [1])


class NoLock(HistoryCase):
    """AC-4, AC-5."""

    def test_held_past_the_window_appends_anyway(self):
        self.hold_lock()
        started = time.monotonic()
        append_history(self.path, {"n": 1}, retry_s=0.5)
        took = time.monotonic() - started
        self.assertGreaterEqual(took, 0.5)
        self.assertLess(took, 5)
        self.assertEqual([r["n"] for r in read_history(self.path)], [1])

    def user_home(self, config_text=None):
        home = os.path.join(self.tmp, "home")
        os.makedirs(home, exist_ok=True)
        if config_text is not None:
            with open(os.path.join(home, "config.toml"), "w", encoding="utf-8") as fh:
                fh.write(config_text)
        patcher = mock.patch.dict(os.environ, {"REVALI_HOME": home})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_window_from_the_user_file(self):
        # PR #61 F1: the user layer's [paths] write_retry_s, looked up for real (no mock);
        # 0.4 s is well under the shipped 2 s, so the default would show
        self.user_home("[paths]\nwrite_retry_s = 0.4\n")
        self.hold_lock()
        started = time.monotonic()
        why = append_history(self.path, {"n": 1})
        took = time.monotonic() - started
        self.assertGreaterEqual(took, 0.4)
        self.assertLess(took, 1.5)
        self.assertIn("longer than 0.4s", why)
        self.assertEqual([r["n"] for r in read_history(self.path)], [1])

    def test_window_from_a_user_file_that_does_not_load_is_the_default(self):
        from revali.config import load_defaults
        from revali.state import history_retry_s

        self.user_home('[paths]\nwrite_retry_s = "slow"\n')
        self.assertEqual(history_retry_s(), float(load_defaults()["paths"]["write_retry_s"]))

    def test_a_window_that_never_ends_is_the_default(self):
        # PR #61 round 2 follow-up: `nan` never compares past a deadline, `inf` never comes
        from revali.config import load_defaults
        from revali.state import history_retry_s

        default = float(load_defaults()["paths"]["write_retry_s"])
        for value in ("nan", "inf", "-1.0"):
            with self.subTest(value=value):
                self.user_home("[paths]\nwrite_retry_s = %s\n" % value)
                self.assertEqual(history_retry_s(), default)

    def test_uncontended_append_does_not_look_up_the_window(self):
        with mock.patch("revali.state.history_retry_s", return_value=0.3) as window:
            self.assertEqual(append_history(self.path, {"n": 1}), "")
        window.assert_not_called()

    def test_the_wait_ends_on_the_deadline(self):
        # PR #61 F2: on a fake clock the pauses add up to the window exactly, none past it
        clock = [100.0]
        slept = []

        def sleep(seconds):
            slept.append(seconds)
            clock[0] += seconds

        with (
            mock.patch("revali.state._try_os_lock", return_value=False),
            mock.patch("revali.state.time.monotonic", side_effect=lambda: clock[0]),
            mock.patch("revali.state.time.sleep", side_effect=sleep),
        ):
            why = append_history(self.path, {"n": 1}, retry_s=0.5)
        self.assertAlmostEqual(sum(slept), 0.5, places=9)
        self.assertLessEqual(max(slept), 0.1)
        self.assertIn("longer than", why)
        self.assertEqual([r["n"] for r in read_history(self.path)], [1])

    def test_locking_unsupported_appends_at_once(self):
        # PR #61 F3: an error that is not contention does not wait out the window
        with (
            mock.patch("revali.state._try_os_lock", return_value=None),
            mock.patch("revali.state.history_retry_s", return_value=30) as window,
        ):
            started = time.monotonic()
            why = append_history(self.path, {"n": 1})
        self.assertLess(time.monotonic() - started, 1.0)
        window.assert_not_called()
        self.assertIn("cannot be locked", why)
        self.assertEqual([r["n"] for r in read_history(self.path)], [1])

    def test_try_lock_tells_contention_from_other_errors(self):
        from revali import state

        lock = os.path.join(self.tmp, "x.lock")
        fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o644)
        self.addCleanup(os.close, fd)
        target = "msvcrt.locking" if os.name == "nt" else "fcntl.flock"
        __import__(target.split(".")[0])
        for exc, expected in (
            (BlockingIOError(errno.EWOULDBLOCK, "busy"), False),
            (PermissionError(errno.EACCES, "held"), False),
            (OSError(errno.ENOLCK, "no locks"), None),
            (OSError(errno.EOPNOTSUPP, "not supported"), None),
        ):
            with self.subTest(exc=exc), mock.patch(target, side_effect=exc):
                self.assertIs(state._try_os_lock(fd), expected)

    def test_a_failing_window_lookup_still_appends_and_closes_the_lock(self):
        # PR #61 F4
        with (
            mock.patch("revali.state._try_os_lock", return_value=False),
            mock.patch("revali.state.history_retry_s", side_effect=RuntimeError("broken defaults")),
        ):
            why = append_history(self.path, {"n": 1})
        self.assertIn("no lock window", why)
        self.assertEqual([r["n"] for r in read_history(self.path)], [1])
        os.remove(self.path + ".lock")  # Windows refuses this while a descriptor is open

    def test_lock_file_that_cannot_be_opened(self):
        os.makedirs(self.path + ".lock")  # a directory where the lock file would be
        why = append_history(self.path, {"n": 1})
        self.assertIn("cannot be opened", why)
        self.assertEqual([r["n"] for r in read_history(self.path)], [1])

    def test_an_unlocked_row_is_noted_in_the_branch_log(self):
        # PR #61 follow-up 1: `_record_history` says so in revali.log
        from revali.pipeline import _record_history
        from revali.state import RunLog, State

        self.user_home()
        os.makedirs(os.path.join(self.tmp, "home", "history.jsonl.lock"))
        rdir = os.path.join(self.tmp, "rdir")
        log = RunLog(rdir, quiet=True, logs_dir="logs")
        _record_history(State(repo="o/r", branch="b", base="main", stage="error"), 1, log)
        with open(os.path.join(rdir, "logs", "revali.log"), encoding="utf-8") as fh:
            self.assertIn("history row appended without its lock", fh.read())
        rows = read_history(os.path.join(self.tmp, "home", "history.jsonl"))
        self.assertEqual([r["branch"] for r in rows], ["b"])

    def test_an_unwritable_history_still_raises(self):
        os.makedirs(self.path)  # the history path is a directory
        with self.assertRaises(OSError):
            append_history(self.path, {"n": 1})


class OpenIssuesUrl(unittest.TestCase):
    """AC-6, the code half (PR #60 round 2 follow-up)."""

    def listing(self, items):
        res = Result(cmd=["gh"], returncode=0, stdout=json.dumps(items), stderr="", duration=0)
        with mock.patch("revali.gitops._gh", return_value=res):
            return gitops.gh_open_issues(".")

    def test_null_or_missing_url_is_empty(self):
        got = self.listing(
            [
                {"number": 1, "title": "a", "url": None},
                {"number": 2, "title": "b"},
                {"number": 3, "title": "c", "url": "https://x/3"},
            ]
        )
        self.assertEqual([i["url"] for i in got], ["", "", "https://x/3"])


class Docs(unittest.TestCase):
    """AC-7."""

    def test_files_md(self):
        with open(os.path.join(ROOT, "docs", "files.md"), "r", encoding="utf-8") as fh:
            self.assertIn("history.jsonl.lock", fh.read())


if __name__ == "__main__":
    unittest.main()
