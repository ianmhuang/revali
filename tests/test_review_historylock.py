"""Reviewer acceptance tests for fix/history-append-lock.

AC-1: 4 processes x 200 `append_history` calls on one file leave 800 rows, every line JSON,
every (writer, i) once. AC-2: the lock is an OS lock on `<history file>.lock` next to the
history file, taken around the append and released after it; the history file is not locked,
so a reader is not blocked. AC-3: a holder that died, or a `.lock` file left on disk, does
not block the next writer. AC-4: a lock held past the window means the row is appended
without it, not dropped; the window is `[paths] write_retry_s` of the user file over
defaults.toml, and no repository's revali.toml sets it. AC-5: a lock file that cannot be
opened means an append as before; an error writing the row is still an OSError, which `stop`
ignores.

The lock is held from outside by a process that takes it the way AC-2 names it
(`msvcrt.locking` on Windows, `fcntl.flock` elsewhere), not through revali.
"""

import json
import os
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from revali import EXIT_OK
from revali.config import history_path, load_defaults
from revali.state import State, append_history, read_history
from tests.helpers import ROOT, RepoCase, rmtree_force, run_cli

WRITERS = 4
ROWS = 200

WRITER = """import sys, time
sys.path.insert(0, sys.argv[1])
from revali.state import append_history
path, writer, start, rows = sys.argv[2], int(sys.argv[3]), float(sys.argv[4]), int(sys.argv[5])
while time.time() < start:
    time.sleep(0.001)
for i in range(rows):
    append_history(path, {"writer": writer, "i": i, "pad": "x" * 500})
"""

# Takes the OS lock on argv[1], creates argv[2], holds for argv[3] seconds, creates argv[4]
# and only then releases; it stays alive afterwards so that "released" is never "exited".
# On Windows the first 4096 bytes are locked, which covers whichever leading byte revali locks.
HOLDER = """import os, sys, time
lock, ready, hold, released = sys.argv[1], sys.argv[2], float(sys.argv[3]), sys.argv[4]
fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o644)
if os.name == "nt":
    import msvcrt
    os.lseek(fd, 0, os.SEEK_SET)
    msvcrt.locking(fd, msvcrt.LK_NBLCK, 4096)
else:
    import fcntl
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
open(ready, "w").close()
time.sleep(hold)
open(released, "w").close()
if os.name == "nt":
    os.lseek(fd, 0, os.SEEK_SET)
    msvcrt.locking(fd, msvcrt.LK_UNLCK, 4096)
else:
    fcntl.flock(fd, fcntl.LOCK_UN)
os.close(fd)
time.sleep(600)
"""

FOREVER = 600.0


def _stop(proc):
    if proc.poll() is None:
        proc.kill()
    proc.wait()


class Holders:
    """Mixin: scripts in `self.scratch` and a process holding the lock of a history file."""

    def script(self, name, text):
        path = os.path.join(self.scratch, name)
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        return path

    def hold(self, history, seconds=FOREVER):
        """Start a holder of `<history>.lock`; returns (process, released marker path) once
        the lock is taken. The test fails when the holder cannot take the lock."""
        self.holders = getattr(self, "holders", 0) + 1
        ready = os.path.join(self.scratch, "ready-%d" % self.holders)
        released = os.path.join(self.scratch, "released-%d" % self.holders)
        proc = subprocess.Popen(
            [
                sys.executable,
                self.script("holder.py", HOLDER),
                history + ".lock",
                ready,
                str(seconds),
                released,
            ],
            stderr=subprocess.DEVNULL,
        )
        self.addCleanup(_stop, proc)
        deadline = time.monotonic() + 60
        while not os.path.exists(ready):
            self.assertIsNone(proc.poll(), "another process could not take the lock")
            self.assertLess(time.monotonic(), deadline, "the holder never took the lock")
            time.sleep(0.02)
        return proc, released


class HistoryCase(Holders, unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="revali history lock ")
        self.addCleanup(rmtree_force, self.tmp)
        self.scratch = os.path.join(self.tmp, "scratch")
        os.makedirs(self.scratch)
        self.dir = os.path.join(self.tmp, "user dir")
        os.makedirs(self.dir)
        self.path = os.path.join(self.dir, "history.jsonl")

    def values(self, key="n"):
        return [row[key] for row in read_history(self.path)]


class ConcurrentWriters(HistoryCase):
    """AC-1."""

    def test_four_processes_of_200_rows_leave_800_rows(self):
        writer = self.script("writer.py", WRITER)
        start = time.time() + 3.0
        procs = [
            subprocess.Popen(
                [sys.executable, writer, ROOT, self.path, str(w), repr(start), str(ROWS)]
            )
            for w in range(WRITERS)
        ]
        for proc in procs:
            self.addCleanup(_stop, proc)
        for proc in procs:
            self.assertEqual(proc.wait(timeout=300), 0)
        with open(self.path, "rb") as fh:
            lines = fh.read().split(b"\n")
        self.assertEqual(lines[-1], b"")  # the last row ends with a newline
        rows = [json.loads(line.decode("utf-8")) for line in lines[:-1]]  # no empty line
        self.assertEqual(len(rows), WRITERS * ROWS)
        pairs = sorted((row["writer"], row["i"]) for row in rows)
        self.assertEqual(pairs, [(w, i) for w in range(WRITERS) for i in range(ROWS)])


class LockFile(HistoryCase):
    """AC-2."""

    def test_the_lock_is_a_separate_file_next_to_the_history_file(self):
        append_history(self.path, {"n": 1})
        self.assertTrue(os.path.isfile(self.path + ".lock"))
        self.assertEqual(sorted(os.listdir(self.dir)), ["history.jsonl", "history.jsonl.lock"])
        with open(self.path, "rb") as fh:
            lines = fh.read().split(b"\n")
        self.assertEqual(len(lines), 2)  # one row and the final newline, nothing else
        self.assertEqual(json.loads(lines[0].decode("utf-8"))["n"], 1)

    def test_the_lock_file_follows_a_history_file_of_another_name(self):
        other = os.path.join(self.tmp, "elsewhere", "runs.log")
        append_history(other, {"n": 1})
        self.assertTrue(os.path.isfile(other + ".lock"))
        self.assertFalse(os.path.exists(self.path + ".lock"))

    def test_a_writer_waits_until_the_holder_releases(self):
        _, released = self.hold(self.path, seconds=1.5)
        started = time.monotonic()
        append_history(self.path, {"n": 1}, retry_s=60)
        took = time.monotonic() - started
        # the holder creates the marker before it unlocks: a row written under the lock
        # cannot be there before the marker
        self.assertTrue(os.path.exists(released), "appended while the lock was still held")
        self.assertLess(took, 30)
        self.assertEqual(self.values(), [1])

    def test_the_lock_is_released_after_the_append(self):
        append_history(self.path, {"n": 1})
        self.hold(self.path)  # fails when this process still holds the lock
        self.assertEqual(self.values(), [1])

    def test_a_reader_is_not_blocked_while_the_lock_is_held(self):
        append_history(self.path, {"n": 1})
        append_history(self.path, {"n": 2})
        holder, _ = self.hold(self.path)
        started = time.monotonic()
        self.assertEqual(self.values(), [1, 2])
        self.assertLess(time.monotonic() - started, 5)
        self.assertIsNone(holder.poll())  # it held the lock the whole time


class DeadHolder(HistoryCase):
    """AC-3."""

    def test_a_killed_holder_does_not_block_the_next_writer(self):
        holder, _ = self.hold(self.path)
        holder.kill()
        holder.wait()
        self.assertTrue(os.path.isfile(self.path + ".lock"))  # the file is still on disk
        started = time.monotonic()
        append_history(self.path, {"n": 1}, retry_s=60)
        self.assertLess(time.monotonic() - started, 15)  # far below the 60 s window
        self.assertEqual(self.values(), [1])

    def test_a_lock_file_left_on_disk_is_not_a_held_lock(self):
        with open(self.path + ".lock", "w", encoding="utf-8", newline="\n") as fh:
            fh.write("pid 4242\n")  # whatever an earlier process left there
        started = time.monotonic()
        append_history(self.path, {"n": 1}, retry_s=60)
        self.assertLess(time.monotonic() - started, 15)
        self.assertEqual(self.values(), [1])


class HeldPastTheWindow(HistoryCase):
    """AC-4."""

    def test_the_row_is_appended_without_the_lock_once_the_window_is_over(self):
        holder, released = self.hold(self.path)
        started = time.monotonic()
        append_history(self.path, {"n": 1}, retry_s=0.5)
        took = time.monotonic() - started
        self.assertGreaterEqual(took, 0.45)  # it waited for the lock
        self.assertLess(took, 5)  # and stopped waiting
        self.assertIsNone(holder.poll())
        self.assertFalse(os.path.exists(released))  # the lock was held throughout
        self.assertEqual(self.values(), [1])  # not dropped

    def test_a_zero_window_appends_at_once(self):
        self.hold(self.path)
        started = time.monotonic()
        append_history(self.path, {"n": 1}, retry_s=0)
        self.assertLess(time.monotonic() - started, 3)
        self.assertEqual(self.values(), [1])

    def user_home(self, config_text=None):
        """A private user directory, with `config_text` as its config.toml when given."""
        home = os.path.join(self.tmp, "user home")
        os.makedirs(home)
        if config_text is not None:
            user_file = os.path.join(home, "config.toml")
            with open(user_file, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(config_text)
        patcher = mock.patch.dict(os.environ, {"REVALI_HOME": home})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_window_defaults_to_write_retry_s(self):
        configured = float(load_defaults()["paths"]["write_retry_s"])
        self.user_home()  # no user file: the shipped default
        self.hold(self.path)
        started = time.monotonic()
        append_history(self.path, {"n": 1})
        took = time.monotonic() - started
        self.assertGreaterEqual(took, configured * 0.9)
        self.assertLess(took, configured + 5)
        self.assertEqual(self.values(), [1])

    def test_the_user_file_raises_the_window(self):
        # round 1 F1: `[paths] write_retry_s` of the user file is the window; a value above
        # the shipped one shows as a wait longer than the default would give
        configured = float(load_defaults()["paths"]["write_retry_s"])
        raised = configured + 1.5
        self.user_home("[paths]\nwrite_retry_s = %s\n" % raised)
        holder, released = self.hold(self.path)
        started = time.monotonic()
        append_history(self.path, {"n": 1})
        took = time.monotonic() - started
        self.assertGreaterEqual(took, raised * 0.95)
        self.assertLess(took, raised + 10)
        self.assertIsNone(holder.poll())
        self.assertFalse(os.path.exists(released))
        self.assertEqual(self.values(), [1])

    def test_the_user_file_lowers_the_window(self):
        # AC-4: never slower than the window, so a lowered key ends the wait before the
        # shipped default would
        configured = float(load_defaults()["paths"]["write_retry_s"])
        lowered = configured / 4
        self.user_home("[paths]\nwrite_retry_s = %s\n" % lowered)
        self.hold(self.path)
        started = time.monotonic()
        append_history(self.path, {"n": 1})
        took = time.monotonic() - started
        self.assertGreaterEqual(took, lowered * 0.9)
        self.assertLess(took, configured * 0.9)
        self.assertEqual(self.values(), [1])

    def test_a_user_file_that_does_not_load_keeps_the_row(self):
        self.user_home("[paths\nwrite_retry_s = 0.2\n")  # not TOML
        self.hold(self.path)
        started = time.monotonic()
        append_history(self.path, {"n": 1})
        self.assertLess(time.monotonic() - started, 30)
        self.assertEqual(self.values(), [1])

    def endless(self, value):
        """AC-4 with a `write_retry_s` that would never expire: the wait still ends, after
        the shipped window. The holder lets go on its own long after that, so a writer that
        waits for ever ends this test as a failure and not as a hang."""
        configured = float(load_defaults()["paths"]["write_retry_s"])
        self.user_home("[paths]\nwrite_retry_s = %s\n" % value)
        holder, released = self.hold(self.path, seconds=configured + 25)
        started = time.monotonic()
        append_history(self.path, {"n": 1})
        took = time.monotonic() - started
        self.assertFalse(os.path.exists(released), "waited until the holder let go")
        self.assertGreaterEqual(took, configured * 0.9)  # it did wait for the lock
        self.assertLess(took, configured + 10)
        self.assertIsNone(holder.poll())
        self.assertEqual(self.values(), [1])  # appended without the lock, not dropped

    def test_an_infinite_window_in_the_user_file_still_ends(self):
        self.endless("inf")

    def test_a_nan_window_in_the_user_file_still_ends(self):
        self.endless("nan")


class NoLockFile(HistoryCase):
    """AC-5."""

    def test_a_directory_where_the_lock_file_would_be(self):
        os.makedirs(self.path + ".lock")
        started = time.monotonic()
        append_history(self.path, {"n": 1})
        append_history(self.path, {"n": 2})
        self.assertLess(time.monotonic() - started, 3)  # nothing to wait for
        self.assertEqual(self.values(), [1, 2])
        self.assertTrue(os.path.isdir(self.path + ".lock"))  # left alone

    @unittest.skipIf(os.name == "nt", "directory permissions do not stop a create on Windows")
    def test_a_read_only_directory(self):
        if os.geteuid() == 0:
            self.skipTest("root creates files in a read-only directory")
        with open(self.path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write('{"n": 1}\n')
        os.chmod(self.dir, stat.S_IRUSR | stat.S_IXUSR)
        self.addCleanup(os.chmod, self.dir, stat.S_IRWXU)
        append_history(self.path, {"n": 2})
        self.assertEqual(self.values(), [1, 2])
        self.assertFalse(os.path.exists(self.path + ".lock"))

    def test_an_error_writing_the_row_is_an_oserror_and_the_lock_is_released(self):
        os.makedirs(self.path)  # the history path is a directory
        with self.assertRaises(OSError):
            append_history(self.path, {"n": 1})
        self.hold(self.path)  # fails when the failed append left the lock held


class ThroughTheCommands(Holders, RepoCase):
    """AC-2, AC-4, AC-5 through `revali stop` and `revali stats` with a private user
    directory: `stop` on a run that died appends one history row."""

    def setUp(self):
        super().setUp()
        self.scratch = os.path.join(self.tmp, "scratch")
        os.makedirs(self.scratch)

    def dead_run(self):
        State(
            repo="owner/repo",
            branch="feature/mul",
            base="main",
            stage="review",
            message="reviewer round 1",
            last_exit=-1,
            pr_number=7,
        ).save(self.rdir())

    def stop(self):
        self.dead_run()
        started = time.monotonic()
        code, out = run_cli(["stop"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertNotIn("Traceback", out)
        return time.monotonic() - started

    def stages(self, path=None):
        return [row["stage"] for row in read_history(path or history_path())]

    def test_stop_leaves_the_lock_file_next_to_the_history_file(self):
        self.stop()
        history = os.path.join(self.home, "history.jsonl")
        self.assertEqual(self.stages(history), ["stopped"])
        self.assertTrue(os.path.isfile(history + ".lock"))

    def test_the_lock_file_follows_history_path(self):
        moved = os.path.join(self.tmp, "moved", "runs.jsonl")
        config = os.path.join(self.home, "config.toml")
        with open(config, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("history_path = '%s'\n" % moved)
        self.stop()
        self.assertEqual(self.stages(moved), ["stopped"])
        self.assertTrue(os.path.isfile(moved + ".lock"))
        self.assertFalse(os.path.exists(os.path.join(self.home, "history.jsonl.lock")))

    def test_stats_reads_while_another_process_holds_the_lock(self):
        self.stop()
        holder, _ = self.hold(history_path())
        started = time.monotonic()
        code, out = run_cli(["stats"])
        self.assertEqual(code, 0, out)
        self.assertLess(time.monotonic() - started, 10)
        self.assertIn("history rows: 1", out)
        self.assertIsNone(holder.poll())

    def test_stop_keeps_its_row_when_the_lock_is_held_past_the_window(self):
        holder, released = self.hold(history_path())
        took = self.stop()
        self.assertLess(took, float(load_defaults()["paths"]["write_retry_s"]) + 20)
        self.assertIsNone(holder.poll())
        self.assertFalse(os.path.exists(released))
        self.assertEqual(self.stages(), ["stopped"])  # AC-4: appended, not dropped

    def test_a_project_file_does_not_set_the_window(self):
        # round 1 F1: the history file is per user, so the revali.toml of a repository that
        # happens to hold it has no say; the user file's window applies
        self.write("revali.toml", self.read("revali.toml") + "\n[paths]\nwrite_retry_s = 120.0\n")
        user_file = os.path.join(self.home, "config.toml")
        with open(user_file, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("[paths]\nwrite_retry_s = 0.5\n")
        history = os.path.join(self.repo, "kept in the checkout", "history.jsonl")
        os.makedirs(os.path.dirname(history))
        holder, released = self.hold(history)
        started = time.monotonic()
        append_history(history, {"n": 1})
        took = time.monotonic() - started
        self.assertGreaterEqual(took, 0.45)
        self.assertLess(took, 60)  # half the project file's 120 s
        self.assertIsNone(holder.poll())
        self.assertFalse(os.path.exists(released))
        self.assertEqual([row["n"] for row in read_history(history)], [1])

    def test_stop_keeps_its_row_when_the_lock_file_cannot_be_opened(self):
        os.makedirs(history_path() + ".lock")
        self.stop()
        self.assertEqual(self.stages(), ["stopped"])  # AC-5

    def test_stop_ignores_a_history_file_that_cannot_be_written(self):
        os.makedirs(history_path())  # AC-5: the OSError is ignored as before
        self.stop()
        self.assertEqual(State.load(self.rdir()).stage, "stopped")


if __name__ == "__main__":
    unittest.main()
