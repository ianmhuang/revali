"""Test call logs stay whole under concurrent writers (fix/fake-log-append, M3.31).

Measured before the change, 2026-11-20: 4 processes appending 500 JSON lines each with
`open(path, "a")` left 169-202 unparsable lines in every one of 5 runs on Windows and none
in WSL. The Windows C runtime emulates O_APPEND as seek-to-end then write, so two writers
can land on the same offset; Linux appends are atomic, so on Linux the concurrency test
below passes with or without the lock and only Windows tells the two apart.
"""

import ast
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from revali.config import PlatformCfg
from revali.runners import FakeRunner
from revali.state import append_line
from tests.helpers import FAKE_BIN, ROOT, rmtree_force

LINES = 300
STUB_WRITERS = 4  # half claude stubs, half gh stubs

# a writer process: imports one stub and drives its own logging code LINES times from a
# common start time (one process per line would take minutes on Windows)
DRIVER = """import io, os, sys, time
sys.path.insert(0, %(fake_bin)r)
kind, tag, start = sys.argv[1], sys.argv[2], float(sys.argv[3])
if kind == "claude":
    import claude_stub
else:
    import gh_stub
while time.time() < start:
    time.sleep(0.001)
for i in range(%(lines)d):
    if kind == "claude":
        sys.stdin = io.StringIO("p" * (40 + (i * 37) %% 900))
        claude_stub.main(["-p", "%%s-%%d" %% (tag, i)])
    else:
        gh_stub.log(["%%s-%%d" %% (tag, i), "x" * ((i * 53) %% 700)])
"""

# the probe stub of tests/test_review_platformprobe.py is a string; it is checked as code
PLATFORMPROBE = os.path.join(ROOT, "tests", "test_review_platformprobe.py")

# every file that writes REVALI_FAKE_LOG or REVALI_FAKE_WSL_CALLS (AC-3)
WRITERS = [
    os.path.join(ROOT, "revali", "runners.py"),
    os.path.join(FAKE_BIN, "claude_stub.py"),
    os.path.join(FAKE_BIN, "gh_stub.py"),
    os.path.join(FAKE_BIN, "ssh_stub.py"),
    os.path.join(FAKE_BIN, "scp_stub.py"),
    os.path.join(FAKE_BIN, "wsl_flaky_stub.py"),
]


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="revali fakelog ")
        self.addCleanup(rmtree_force, self.tmp)
        self.log = os.path.join(self.tmp, "fake.log")


class ConcurrentWriters(Case):
    """AC-1, AC-4."""

    def test_every_line_whole_and_none_lost(self):
        scenario = os.path.join(self.tmp, "scenario.json")
        with open(scenario, "w", encoding="utf-8") as fh:
            fh.write("{}")
        driver = os.path.join(self.tmp, "driver.py")
        with open(driver, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(DRIVER % {"fake_bin": FAKE_BIN, "lines": LINES})
        env = dict(os.environ, REVALI_FAKE_LOG=self.log, REVALI_FAKE_SCENARIO=scenario)
        start = time.time() + 3.0
        kinds = ["claude" if w % 2 == 0 else "gh" for w in range(STUB_WRITERS)]
        procs = [
            subprocess.Popen(
                [sys.executable, driver, kind, "w%d" % w, repr(start)],
                env=env,
                cwd=self.tmp,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
            )
            for w, kind in enumerate(kinds)
        ]
        runner_error = []
        thread = threading.Thread(target=self.run_fake_runner, args=(start, runner_error))
        old = os.environ.get("REVALI_FAKE_LOG")
        os.environ["REVALI_FAKE_LOG"] = self.log  # FakeRunner reads it from its own process
        try:
            thread.start()
            for proc in procs:
                self.assertEqual(proc.wait(timeout=300), 0)
            thread.join(timeout=300)
        finally:
            if old is None:
                os.environ.pop("REVALI_FAKE_LOG", None)
            else:
                os.environ["REVALI_FAKE_LOG"] = old
        self.assertEqual(runner_error, [])

        with open(self.log, "r", encoding="utf-8", newline="") as fh:
            lines = [ln for ln in fh.read().split("\n") if ln]
        bad = []
        calls = []
        for ln in lines:
            try:
                calls.append(json.loads(ln))
            except ValueError:
                bad.append(ln[:80])
        self.assertEqual(
            len(bad), 0, "%d torn line(s) of %d, first: %r" % (len(bad), len(lines), bad[:2])
        )

        seen = {}
        for c in calls:
            if c["exe"] == "runner":
                tag = "runner"
                self.assertEqual(c["steps"], ["test"])  # AC-4: the runner's keys as before
            elif c["exe"] == "claude":
                tag = c["argv"][1].rsplit("-", 1)[0]
                self.assertIn("prompt", c)
            else:
                self.assertEqual(c["exe"], "gh")
                tag = c["argv"][0].rsplit("-", 1)[0]
            seen[tag] = seen.get(tag, 0) + 1
        expected = {"w%d" % w: LINES for w in range(STUB_WRITERS)}
        expected["runner"] = LINES
        self.assertEqual(seen, expected)

    def run_fake_runner(self, start, errors):
        try:
            scenario = os.path.join(self.tmp, "runner.json")
            with open(scenario, "w", encoding="utf-8") as fh:
                fh.write('{"default": 0}')
            runner = FakeRunner(PlatformCfg(runner="fake"), scenario)
            logs = os.path.join(self.tmp, "logs")
            while time.time() < start:
                time.sleep(0.001)
            for i in range(LINES):
                runner.run(self.tmp, "abc", [("test", "true " + "y" * (i % 400))], {}, logs, "r")
        except Exception as exc:  # reported by the test, not lost in the thread
            errors.append(repr(exc))


def append_mode_opens(source):
    """(line, call) of every `open(...)` call in `source` whose mode is append."""
    found = []
    for node in ast.walk(ast.parse(source)):
        if not (isinstance(node, ast.Call) and getattr(node.func, "id", None) == "open"):
            continue
        mode = node.args[1] if len(node.args) > 1 else None
        for kw in node.keywords:
            if kw.arg == "mode":
                mode = kw.value
        if isinstance(mode, ast.Constant) and isinstance(mode.value, str) and "a" in mode.value:
            found.append((node.lineno, ast.unparse(node)))
    return found


class OneWayToAppend(unittest.TestCase):
    """AC-3."""

    def test_no_writer_opens_its_log_for_append_itself(self):
        for path in WRITERS:
            with self.subTest(path=os.path.relpath(path, ROOT)):
                with open(path, "r", encoding="utf-8") as fh:
                    self.assertEqual(append_mode_opens(fh.read()), [])

    def test_the_platformprobe_stub_neither(self):
        from tests.test_review_platformprobe import WSL_PROBE_STUB

        self.assertEqual(append_mode_opens(WSL_PROBE_STUB), [])
        self.assertTrue(os.path.isfile(PLATFORMPROBE))

    def test_the_scan_sees_an_append(self):
        # the check above would pass on anything if it could not see an append
        self.assertEqual(len(append_mode_opens('open(p, "a")\nopen(p, mode="ab")\n')), 2)
        self.assertEqual(append_mode_opens('open(p, "w")\nopen(p)\n'), [])


HOLDER = """import os, sys, time
lock, ready = sys.argv[1], sys.argv[2]
fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o644)
if os.name == "nt":
    import msvcrt
    os.lseek(fd, 0, os.SEEK_SET)
    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
else:
    import fcntl
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
open(ready, "w").close()
time.sleep(60)
"""


class AppendLine(Case):
    """AC-2, AC-5."""

    def read(self):
        with open(self.log, "r", encoding="utf-8", newline="") as fh:
            return fh.read()

    def test_one_line_lf_and_a_lock_file(self):
        self.assertEqual(append_line(self.log, '{"n": 1}', retry_s=1), "")
        self.assertEqual(append_line(self.log, "plain", retry_s=1), "")
        self.assertEqual(self.read(), '{"n": 1}\nplain\n')
        self.assertTrue(os.path.isfile(self.log + ".lock"))

    def test_a_held_lock_still_writes_the_line(self):
        holder = os.path.join(self.tmp, "holder.py")
        with open(holder, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(HOLDER)
        ready = os.path.join(self.tmp, "ready")
        proc = subprocess.Popen([sys.executable, holder, self.log + ".lock", ready])
        self.addCleanup(proc.wait)
        self.addCleanup(proc.kill)
        deadline = time.monotonic() + 20
        while not os.path.exists(ready):
            self.assertIsNone(proc.poll(), "the holder could not take the lock")
            self.assertLess(time.monotonic(), deadline, "the holder never took the lock")
            time.sleep(0.02)
        started = time.monotonic()
        why = append_line(self.log, "x", retry_s=0.3)
        took = time.monotonic() - started
        self.assertIn("longer than 0.3s", why)
        self.assertGreaterEqual(took, 0.3)
        self.assertLess(took, 5)
        self.assertEqual(self.read(), "x\n")


if __name__ == "__main__":
    unittest.main()
