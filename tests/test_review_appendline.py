"""AC-2 and AC-5 of fix/fake-log-append: `revali.state.append_line` appends one LF-ended line
while it holds the OS lock on `<file>.lock`, `append_history` writes its row through it, and
a lock that cannot be had within the window does not cost the line.

The lock is held here by another process through the operating system (`msvcrt.locking` on
Windows, `fcntl.flock` elsewhere), the lock `append_history` has always taken, not through
revali. The holder says "locked" on its standard output and lets go when its standard input
is closed, so no test waits on a sleep to know where the holder is.
"""

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from revali import state
from tests.helpers import rmtree_force

HOLDER = """import os, sys
kept = []
for lock in sys.argv[1:]:
    fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o644)
    if os.name == "nt":
        import msvcrt
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    kept.append(fd)
sys.stdout.write("locked\\n")
sys.stdout.flush()
sys.stdin.read()
"""


def _stop(proc):
    if proc.poll() is None:
        proc.kill()
    proc.wait()
    for pipe in (proc.stdin, proc.stdout):
        if pipe and not pipe.closed:
            pipe.close()


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="revali appendline "))
        self.addCleanup(rmtree_force, self.tmp)
        self.path = os.path.join(self.tmp, "calls.log")

    def raw(self, path=None):
        with open(path or self.path, "rb") as fh:
            return fh.read()

    def hold(self, *locks):
        """A process holding the OS lock on every file of `locks`; returns once it has them."""
        script = os.path.join(self.tmp, "holder.py")
        with open(script, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(HOLDER)
        proc = subprocess.Popen(
            [sys.executable, script] + list(locks),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        self.addCleanup(_stop, proc)
        self.assertEqual(proc.stdout.readline().strip(), "locked", "the holder got no lock")
        return proc

    def release(self, proc):
        proc.stdin.close()
        proc.wait(timeout=60)


class OneLineUnderTheLock(Case):
    """AC-2: one function, one line, the lock on `<file>.lock`."""

    def test_the_line_and_one_lf_are_appended(self):
        self.assertEqual(state.append_line(self.path, '{"n": 1}', retry_s=1), "")
        self.assertEqual(state.append_line(self.path, "café 中", retry_s=1), "")
        # bytes, so a CRLF the platform would add in text mode shows
        self.assertEqual(self.raw(), b'{"n": 1}\n' + "café 中\n".encode("utf-8"))

    def test_the_lock_file_is_next_to_the_file_and_free_afterwards(self):
        self.assertEqual(state.append_line(self.path, "one", retry_s=1), "")
        self.assertTrue(os.path.isfile(self.path + ".lock"))
        # another process can take it at once: the append let go of it
        self.release(self.hold(self.path + ".lock"))
        self.assertEqual(self.raw(), b"one\n")

    def test_a_writer_waits_while_another_process_holds_the_lock(self):
        holder = self.hold(self.path + ".lock")
        got = []
        writer = threading.Thread(
            target=lambda: got.append(state.append_line(self.path, "late", retry_s=60)),
            daemon=True,
        )
        writer.start()
        writer.join(timeout=1.0)
        waiting = writer.is_alive()
        written_early = os.path.isfile(self.path) and self.raw() != b""
        self.release(holder)
        writer.join(timeout=60)
        self.assertFalse(writer.is_alive(), "the writer never came back")
        self.assertTrue(waiting, "the line was written while the lock was held")
        self.assertFalse(written_early)
        self.assertEqual(got, [""])  # written under the lock once it was free
        self.assertEqual(self.raw(), b"late\n")


class TheLineIsNeverLost(Case):
    """AC-5: no lock within the window, the line is written all the same."""

    def test_a_lock_held_past_the_window(self):
        self.hold(self.path + ".lock")
        started = time.monotonic()
        why = state.append_line(self.path, "kept", retry_s=0.3)
        took = time.monotonic() - started
        self.assertTrue(why, "an append without the lock must say why")
        self.assertIsInstance(why, str)
        self.assertGreaterEqual(took, 0.25, "it did not wait for the window")
        self.assertLess(took, 30, "it hung on the lock")
        self.assertEqual(self.raw(), b"kept\n")

    def test_a_zero_window_writes_at_once(self):
        self.hold(self.path + ".lock")
        started = time.monotonic()
        why = state.append_line(self.path, "now", retry_s=0)
        self.assertTrue(why)
        self.assertLess(time.monotonic() - started, 30)
        self.assertEqual(self.raw(), b"now\n")

    def test_a_lock_file_that_cannot_be_opened(self):
        os.mkdir(self.path + ".lock")  # a directory where the lock file would be
        why = state.append_line(self.path, "still", retry_s=0.2)
        self.assertTrue(why)
        self.assertEqual(self.raw(), b"still\n")


class HistoryGoesThroughIt(Case):
    """AC-2: `append_history` appends through `append_line`, and its row is what it was."""

    def test_append_history_hands_its_row_to_append_line(self):
        history = os.path.join(self.tmp, "history.jsonl")
        with mock.patch.object(state, "append_line", return_value="why not") as appended:
            got = state.append_history(history, {"command": "run", "result": "ready"}, 0.5)
        self.assertEqual(got, "why not")  # the reason an append went unlocked is passed on
        self.assertEqual(appended.call_count, 1)
        args, kwargs = appended.call_args
        names = ("path", "line", "retry_s")  # positional or by keyword, either is fine
        call = {names[i]: value for i, value in enumerate(args)}
        call.update(kwargs)
        self.assertEqual(call["path"], history)
        self.assertEqual(call.get("retry_s"), 0.5)
        row = json.loads(call["line"])
        self.assertEqual((row["command"], row["result"]), ("run", "ready"))
        for key in ("at", "revali_version", "prompt_version"):
            self.assertIn(key, row)
        self.assertFalse(os.path.exists(history), "append_history wrote the row itself as well")

    def test_the_row_on_disk_is_unchanged(self):
        history = os.path.join(self.tmp, "not yet", "history.jsonl")  # the directory is made
        self.assertEqual(state.append_history(history, {"command": "run", "note": "café"}), "")
        self.assertEqual(state.append_history(history, {"command": "merge"}, retry_s=1), "")
        data = self.raw(history)
        self.assertNotIn(b"\r", data)
        self.assertTrue(data.endswith(b"\n"))
        self.assertIn("café".encode("utf-8"), data)  # not escaped: ensure_ascii is off
        rows = [json.loads(ln) for ln in data.decode("utf-8").split("\n") if ln]
        self.assertEqual([r["command"] for r in rows], ["run", "merge"])
        for row in rows:
            for key in ("at", "revali_version", "prompt_version"):
                self.assertIn(key, row)
        self.assertTrue(os.path.isfile(history + ".lock"))
        self.assertEqual(state.read_history(history), rows)


if __name__ == "__main__":
    unittest.main()
