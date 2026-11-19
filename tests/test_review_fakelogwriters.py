"""AC-1, AC-3 and AC-4 of fix/fake-log-append: every writer of the test call logs
(`REVALI_FAKE_LOG`, `REVALI_FAKE_WSL_CALLS`) appends under the OS lock on `<log>.lock`, so no
line is torn when writers overlap, and what the logs hold is what it was.

Two checks, both from outside the writers:

* While another process holds the lock, no writer writes and none ends: the claude, gh, ssh,
  scp and wsl_flaky stubs, the wsl probe stand-in of tests/test_review_platformprobe.py, and
  `FakeRunner.run` on a thread of this process. Once the lock is free each one leaves one
  whole line with the keys it always had. A writer that opens its log for append on its own
  never looks at the lock, so it writes at once and fails this on Linux as well as Windows.
* Four stub processes and two fake runner threads append 150 lines each from one start time;
  every line parses and every writer's lines are all there. Appends are atomic on Linux, so
  there this second check passes with or without the lock; on Windows the writers of before
  the change tore lines in it. The first check is the one that tells the two apart everywhere.

The lock is held through the operating system (`msvcrt.locking` on Windows, `fcntl.flock`
elsewhere), not through revali.
"""

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest import mock

from revali.config import PlatformCfg
from revali.runners import FakeRunner
from tests.helpers import FAKE_BIN, RepoCase, rmtree_force
from tests.test_review_platformprobe import WSL_PROBE_STUB

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

# one writer process: a stub's own entry point, called `count` times from a common start time
# (a process per line would spend its time starting Python, not overlapping)
DRIVER = """import io, sys, time
sys.path.insert(0, sys.argv[1])
kind, tag, start, count = sys.argv[2], sys.argv[3], float(sys.argv[4]), int(sys.argv[5])
stub = __import__(kind + "_stub")
while time.time() < start:
    time.sleep(0.001)
for i in range(count):
    arg = "%s-%d-%s" % (tag, i, "x" * ((i * 131) % 1500))
    if kind == "claude":
        sys.stdin = io.StringIO("p" * ((i * 257) % 3000))
        stub.main(["-p", arg])
    elif kind == "gh":
        stub.main(["api", "user", arg])
    else:
        stub.main([arg])
"""

# how long the lock is held over the writers: far below the window a writer waits for it
# before it writes anyway (AC-5), far above what a writer that ignores the lock needs
HELD_S = 1.5
COUNT = 150

KNOBS = (
    "REVALI_FAKE_WSL_PLAN",
    "REVALI_FAKE_SSH_DOWN",
    "REVALI_FAKE_SSH_BASH_FAILS",
    "REVALI_FAKE_SSH_SCRIPT_DROP",
    "REVALI_FAKE_SSH_RM_FAILS",
    "REVIEW_FAKE_WSL_DOWN",
)

RUNNER_KEYS = {
    "exe",
    "label",
    "ref",
    "scope",
    "key",
    "steps",
    "cmds",
    "extra_files",
    "t0",
    "t1",
    "held",
    "finished",
}


def _stop(proc):
    if proc.poll() is None:
        proc.kill()
    proc.wait()
    for pipe in (proc.stdin, proc.stdout, proc.stderr):
        if pipe and not pipe.closed:
            pipe.close()


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="revali logwriters "))
        self.addCleanup(rmtree_force, self.tmp)
        self.log = os.path.join(self.tmp, "fake.log")
        self.wsl_calls = os.path.join(self.tmp, "wsl-calls.txt")
        self.runner_scenario = self.script("runner.json", '{"default": 0}')
        self.env = dict(os.environ)
        for name in KNOBS:
            self.env.pop(name, None)
        self.env.update(
            REVALI_FAKE_LOG=self.log,
            REVALI_FAKE_WSL_CALLS=self.wsl_calls,
            REVALI_FAKE_SCENARIO=self.script("scenario.json", "{}"),
            REVALI_FAKE_REMOTE=os.path.join(self.tmp, "remote"),
            REVALI_HOME=os.path.join(self.tmp, "home"),
        )
        # the fake runner reads the variable in this process
        patch = mock.patch.dict(os.environ, {"REVALI_FAKE_LOG": self.log})
        patch.start()
        self.addCleanup(patch.stop)

    def script(self, name, text):
        path = os.path.join(self.tmp, name)
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        return path

    def raw(self, path):
        if not os.path.isfile(path):
            return b""
        with open(path, "rb") as fh:
            return fh.read()

    def calls(self):
        """The call log as every RepoCase test reads it; a torn line raises here as it did in
        CI."""
        return RepoCase.fake_calls(types.SimpleNamespace(fake_log=self.log))

    def run_fake_runner(self, label, times, start, said):
        try:
            runner = FakeRunner(PlatformCfg(runner="fake"), self.runner_scenario)
            while time.time() < start:
                time.sleep(0.001)
            for i in range(times):
                runner.run(
                    self.tmp,
                    "abc123",
                    [("test", "pytest " + "y" * ((i * 97) % 1200)), ("lint", " ")],
                    {"tests/test_review_x.py": "x"},
                    os.path.join(self.tmp, "logs"),
                    label,
                )
            said.append("ok")
        except Exception as exc:  # shown by the test, not lost with the thread
            said.append(repr(exc))


class EveryWriterTakesTheLock(Case):
    """AC-3 (and the keys of AC-4)."""

    def commands(self):
        probe = self.script("wsl_probe_stub.py", WSL_PROBE_STUB)
        wsl = ["-d", "Ubuntu", "-e", "bash", "-c", "git --version"]
        return {
            "claude": [os.path.join(FAKE_BIN, "claude_stub.py"), "-p", "hello"],
            "gh": [os.path.join(FAKE_BIN, "gh_stub.py"), "auth", "status"],
            "ssh": [os.path.join(FAKE_BIN, "ssh_stub.py"), "winbox", "true"],
            "scp": [os.path.join(FAKE_BIN, "scp_stub.py"), "-q", "winbox:nowhere"],
            "wsl_flaky": [os.path.join(FAKE_BIN, "wsl_flaky_stub.py")] + wsl,
            "wsl_probe": [probe] + wsl,
        }

    def test_no_writer_writes_while_the_lock_is_held_and_each_writes_once_it_is_free(self):
        holder = subprocess.Popen(
            [sys.executable, self.script("holder.py", HOLDER)]
            + [self.log + ".lock", self.wsl_calls + ".lock"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        self.addCleanup(_stop, holder)
        self.assertEqual(holder.stdout.readline().strip(), "locked", "the holder got no lock")

        procs = {}
        for name, cmd in self.commands().items():
            procs[name] = subprocess.Popen(
                [sys.executable] + cmd,
                env=self.env,
                cwd=self.tmp,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            self.addCleanup(_stop, procs[name])
        said = []
        runner = threading.Thread(
            target=self.run_fake_runner, args=("validate-r1", 1, 0.0, said), daemon=True
        )
        runner.start()

        time.sleep(HELD_S)
        ended_early = sorted(name for name, proc in procs.items() if proc.poll() is not None)
        if not runner.is_alive():
            ended_early.append("runner")
        written_early = {
            os.path.basename(path): self.raw(path)[:200]
            for path in (self.log, self.wsl_calls)
            if self.raw(path)
        }

        holder.stdin.close()
        holder.wait(timeout=60)
        ended = {}
        for name, proc in procs.items():
            _out, err = proc.communicate(timeout=120)
            ended[name] = (proc.returncode, err.strip()[-500:])
        runner.join(timeout=120)
        self.assertFalse(runner.is_alive(), "the fake runner never came back")

        self.assertEqual(
            ended_early,
            [],
            "these ended while another process held the lock on their log: %r"
            % {name: ended.get(name, said) for name in ended_early},
        )
        self.assertEqual(written_early, {}, "written while the lock was held")

        self.assertEqual(said, ["ok"])
        for name in ("claude", "gh", "ssh", "wsl_flaky", "wsl_probe"):
            self.assertEqual(ended[name][0], 0, "%s: %s" % (name, ended[name][1]))

        # AC-4: one JSON object per line, LF, the keys of before
        data = self.raw(self.log)
        self.assertNotIn(b"\r", data)
        self.assertTrue(data.endswith(b"\n"))
        self.assertEqual(data.count(b"\n"), 6, data)
        by_exe = {}
        for call in self.calls():
            by_exe.setdefault(call["exe"], []).append(call)
        self.assertEqual(sorted(by_exe), ["claude", "gh", "runner", "scp", "ssh", "wsl"])
        self.assertEqual(
            {exe: len(calls) for exe, calls in by_exe.items()}, dict.fromkeys(by_exe, 1)
        )
        one = {exe: calls[0] for exe, calls in by_exe.items()}

        self.assertEqual(set(one["claude"]), {"exe", "argv", "prompt", "cwd"})
        self.assertEqual(one["claude"]["argv"], ["-p", "hello"])
        self.assertEqual(one["claude"]["prompt"], "")
        self.assertEqual(
            os.path.normcase(os.path.realpath(one["claude"]["cwd"])), os.path.normcase(self.tmp)
        )
        self.assertEqual(one["gh"], {"exe": "gh", "argv": ["auth", "status"]})
        self.assertEqual(one["ssh"], {"exe": "ssh", "argv": ["winbox", "true"]})
        self.assertEqual(set(one["scp"]), {"exe", "argv", "cwd"})
        self.assertEqual(one["scp"]["argv"], ["-q", "winbox:nowhere"])
        self.assertEqual(
            one["wsl"],
            {"exe": "wsl", "argv": ["-d", "Ubuntu", "-e", "bash", "-c", "git --version"]},
        )
        ran = one["runner"]
        self.assertEqual(set(ran), RUNNER_KEYS)
        self.assertEqual((ran["label"], ran["ref"]), ("validate-r1", "abc123"))
        self.assertEqual(ran["steps"], ["test"])  # the step with no command is left out
        self.assertEqual(ran["cmds"], {"test": "pytest "})
        self.assertEqual(ran["extra_files"], ["tests/test_review_x.py"])
        self.assertEqual((ran["held"], ran["finished"]), (False, True))
        self.assertLessEqual(ran["t0"], ran["t1"])

        # the wsl call record: one line per call, the kind
        self.assertEqual(self.raw(self.wsl_calls), b"probe\n")


class OverlappingWriters(Case):
    """AC-1, AC-4."""

    def test_stub_processes_and_runner_threads_leave_whole_lines_and_lose_none(self):
        driver = self.script("driver.py", DRIVER)
        start = time.time() + 2.0
        kinds = ["claude", "gh", "ssh", "scp"]
        procs = {}
        for kind in kinds:
            procs[kind] = subprocess.Popen(
                [sys.executable, driver, FAKE_BIN, kind, "w" + kind, repr(start), str(COUNT)],
                env=self.env,
                cwd=self.tmp,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            self.addCleanup(_stop, procs[kind])
        # two platforms validated at once are two fake runner threads in one process
        said = {"t0": [], "t1": []}
        threads = [
            threading.Thread(
                target=self.run_fake_runner, args=(label, COUNT, start, said[label]), daemon=True
            )
            for label in sorted(said)
        ]
        for thread in threads:
            thread.start()
        for kind, proc in procs.items():
            _out, err = proc.communicate(timeout=300)
            self.assertEqual(proc.returncode, 0, "%s writer: %s" % (kind, err.strip()[-500:]))
        for thread in threads:
            thread.join(timeout=300)
            self.assertFalse(thread.is_alive(), "a fake runner thread never came back")
        self.assertEqual(said, {"t0": ["ok"], "t1": ["ok"]})

        lines = [ln for ln in self.raw(self.log).split(b"\n") if ln.strip()]
        torn = []
        seen = {}
        for ln in lines:
            try:
                call = json.loads(ln.decode("utf-8"))
                tag = self.writer_of(call)
            except (ValueError, LookupError, TypeError, AttributeError):
                torn.append(ln[:80])
                continue
            seen[tag] = seen.get(tag, 0) + 1
        self.assertEqual(
            torn, [], "%d torn line(s) of %d, first: %r" % (len(torn), len(lines), torn[:2])
        )
        expected = dict.fromkeys(["w" + kind for kind in kinds] + ["t0", "t1"], COUNT)
        self.assertEqual(seen, expected)
        self.assertEqual(len(self.calls()), COUNT * 6)  # and RepoCase.fake_calls reads it

    @staticmethod
    def writer_of(call):
        exe = call["exe"]
        if exe == "runner":
            return call["label"]
        arg = {"claude": 1, "gh": 2, "ssh": 0, "scp": 0}[exe]
        return call["argv"][arg].split("-", 1)[0]


if __name__ == "__main__":
    unittest.main()
