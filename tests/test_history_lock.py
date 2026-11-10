"""Appends to the run history under a lock (fix/history-append-lock, M3.4.2).

Measured before the change: 4 processes appending 200 rows each to one file at the same
moment lost 91 to 226 of the 800 rows on Windows (none on Linux ext4).
"""

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
# then holds it until killed (or for `seconds`)
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
        holder = self.hold_lock(seconds=1.0)
        started = time.monotonic()
        append_history(self.path, {"n": 1}, retry_s=30)
        took = time.monotonic() - started
        self.assertIsNotNone(holder.poll(), "appended while the holder still held the lock")
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

    def test_default_window_is_write_retry_s(self):
        self.hold_lock()
        with mock.patch("revali.state.write_retry_s", return_value=0.3) as window:
            append_history(self.path, {"n": 1})
        window.assert_called()
        self.assertEqual([r["n"] for r in read_history(self.path)], [1])

    def test_uncontended_append_does_not_look_up_the_window(self):
        with mock.patch("revali.state.write_retry_s", return_value=0.3) as window:
            append_history(self.path, {"n": 1})
        window.assert_not_called()

    def test_lock_file_that_cannot_be_opened(self):
        os.makedirs(self.path + ".lock")  # a directory where the lock file would be
        append_history(self.path, {"n": 1})
        self.assertEqual([r["n"] for r in read_history(self.path)], [1])

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
