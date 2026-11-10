"""Review of fix/cleanup-failures, AC-1 to AC-4: a sandbox runner's cleanup that fails is
logged in one line and the session's report, or the error that ended it, comes back unchanged.

The runners run for real (a real git worktree, the ssh / scp stubs). The one cleanup step a
test is about is made to fail where revali meets the system: `subprocess.run` for a command
that times out, cannot start or exits non-zero, `os.remove` / `shutil.rmtree` for a file or
directory held open."""

import os
import shutil
import subprocess
import sys
import unittest
from unittest import mock

from revali.config import PlatformCfg
from revali.runners import LocalRunner, RunnerError, SshRunner, WslRunner
from tests.helpers import RepoCase, _quote, git
from tests.test_ssh_runner import HAVE_BASH, SshCase, plat

LABEL = "validate-r1"


def git_cmd(argv, *words):
    """True for a git command line that carries every one of `words`."""
    return os.path.basename(argv[0]).lower().startswith("git") and all(w in argv[1:] for w in words)


class Commands:
    """While active, `decide(argv)` sees every command started through subprocess.run and
    settles its outcome: None runs it, an exception is raised in its place, and
    (exit code, stderr) is returned as its result without running it."""

    def __init__(self, decide):
        self.decide = decide
        self.seen = []
        self._patch = None

    def __enter__(self):
        real = subprocess.run

        def run(args, *a, **kw):
            argv = [str(x) for x in args] if isinstance(args, (list, tuple)) else [str(args)]
            self.seen.append(argv)
            answer = self.decide(argv)
            if isinstance(answer, BaseException):
                raise answer
            if answer is not None:
                return subprocess.CompletedProcess(args, answer[0], stdout="", stderr=answer[1])
            return real(args, *a, **kw)

        self._patch = mock.patch.object(subprocess, "run", run)
        self._patch.start()
        return self

    def __exit__(self, *exc):
        self._patch.stop()
        return False

    def worktree(self):
        """The path the local runner gave `git worktree add`."""
        (add,) = [c for c in self.seen if git_cmd(c, "worktree", "add")]
        (path,) = [a for a in add[1:] if os.path.isabs(a)]
        return path


def held_open(suffix, message):
    """os.remove that refuses every path ending in `suffix`, as Windows does for an open file."""
    real = os.remove

    def remove(path, *a, **kw):
        if str(path).endswith(suffix):
            raise PermissionError(13, message)
        return real(path, *a, **kw)

    return mock.patch.object(os, "remove", remove)


def kept(suffix):
    """shutil.rmtree that leaves every directory ending in `suffix` where it is."""
    real = shutil.rmtree

    def rmtree(path, *a, **kw):
        if str(path).endswith(suffix):
            return None
        return real(path, *a, **kw)

    return mock.patch.object(shutil, "rmtree", rmtree)


def about(lines, *needles):
    return [line for line in lines if all(n in line for n in needles)]


class LocalWorktreeCleanup(RepoCase):
    """AC-3"""

    def session(self, steps, decide, lines):
        logs = os.path.join(self.tmp, "logs")
        head = git(["rev-parse", "HEAD"], self.repo).strip()
        runner = LocalRunner(PlatformCfg(runner="local", command_timeout_min=1))
        cmds = Commands(decide)
        self.cmds = cmds
        with cmds:
            return runner.run(self.repo, head, steps, {}, logs, LABEL, log=lines.append)

    def one_line(self, lines, *needles):
        found = about(lines, *needles)
        self.assertEqual(len(found), 1, "expected one line with %r in %r" % (needles, lines))
        self.assertNotIn("\n", found[0].strip())
        return found[0]

    def test_remove_times_out_and_the_failing_report_is_returned(self):
        def decide(argv):
            if git_cmd(argv, "worktree", "remove"):
                return subprocess.TimeoutExpired(argv, 120)
            return None

        lines = []
        report = self.session([("test", "exit 3")], decide, lines)
        self.assertEqual(report.failed.name, "test")
        self.assertEqual(report.failed.returncode, 3)
        wt = self.cmds.worktree()
        self.one_line(lines, wt, "timed out")
        # the failed remove stopped neither the prune nor the removal of the directory
        self.assertTrue([c for c in self.cmds.seen if git_cmd(c, "worktree", "prune")])
        self.assertFalse(os.path.exists(wt), wt)

    def test_remove_exits_non_zero_and_gits_error_is_on_one_line(self):
        def decide(argv):
            if git_cmd(argv, "worktree", "remove"):
                return (128, "fatal: busy\nhint: twice\n")
            return None

        lines = []
        report = self.session([("test", "exit 0")], decide, lines)
        self.assertTrue(report.ok)
        wt = self.cmds.worktree()
        self.one_line(lines, wt, "fatal: busy hint: twice")
        self.assertTrue([c for c in self.cmds.seen if git_cmd(c, "worktree", "prune")])
        self.assertFalse(os.path.exists(wt), wt)

    def test_prune_cannot_be_found(self):
        def decide(argv):
            if git_cmd(argv, "worktree", "prune"):
                return FileNotFoundError(2, "No such file or directory")
            return None

        lines = []
        report = self.session([("test", "exit 0")], decide, lines)
        self.assertTrue(report.ok)
        wt = self.cmds.worktree()
        self.one_line(lines, wt, "No such file or directory")
        self.assertFalse(os.path.exists(wt), wt)

    def test_prune_cannot_be_started(self):
        def decide(argv):
            if git_cmd(argv, "worktree", "prune"):
                return PermissionError(13, "Access is denied")
            return None

        lines = []
        report = self.session([("test", "exit 3")], decide, lines)
        self.assertEqual(report.failed.returncode, 3)
        self.one_line(lines, self.cmds.worktree(), "Access is denied")

    def test_prune_times_out(self):
        def decide(argv):
            if git_cmd(argv, "worktree", "prune"):
                return subprocess.TimeoutExpired(argv, 60)
            return None

        lines = []
        report = self.session([("test", "exit 0")], decide, lines)
        self.assertTrue(report.ok)
        self.one_line(lines, self.cmds.worktree(), "timed out")

    def test_the_error_a_step_raised_is_the_one_that_comes_out(self):
        step = "exit 7"

        def decide(argv):
            if argv == [step]:
                return OSError(8, "step could not start")
            if git_cmd(argv, "worktree", "remove"):
                return subprocess.TimeoutExpired(argv, 120)
            return None

        lines = []
        with self.assertRaises(OSError) as cm:
            self.session([("test", step)], decide, lines)
        self.assertIn("step could not start", str(cm.exception))
        self.one_line(lines, self.cmds.worktree(), "timed out")


class SshCleanupCase(SshCase):
    def session(self, steps, lines):
        self.logs = os.path.join(self.tmp, "logs")
        head = git(["rev-parse", "HEAD"], self.repo).strip()
        return SshRunner(plat()).run(
            self.repo, head, steps, {"t/x.py": "x\n"}, self.logs, LABEL, log=lines.append
        )

    def remote_rm_cannot_start(self):
        def decide(argv):
            stub = any(os.path.basename(a) == "ssh_stub.py" for a in argv)
            if stub and argv[-1].startswith("rm -rf"):
                return PermissionError(13, "Access is denied")
            return None

        return Commands(decide)


class SshBundleHeldOpen(SshCleanupCase):
    """AC-1"""

    @unittest.skipUnless(HAVE_BASH, "needs bash")
    def test_the_report_is_returned_and_the_bundle_is_named_once(self):
        lines = []
        with held_open(".bundle", "in use by another process"):
            report = self.session([("setup", "true"), ("test", "true")], lines)
        self.assertEqual([s.name for s in report.steps], ["setup", "test"])
        self.assertTrue(report.ok, [(s.name, s.returncode) for s in report.steps])
        self.bundle_named_once(lines)

    def bundle_named_once(self, lines):
        # the line that carries the error; `git bundle create` names the path as well
        bundle = os.path.join(self.logs, LABEL + ".bundle")
        found = about(lines, bundle, "in use by another process")
        self.assertEqual(len(found), 1, lines)
        self.assertIn("left for you to delete", found[0])

    @unittest.skipUnless(HAVE_BASH, "needs bash")
    def test_a_failing_step_is_still_the_result(self):
        lines = []
        with held_open(".bundle", "in use by another process"):
            report = self.session([("setup", "true"), ("test", "exit 3")], lines)
        self.assertEqual(report.failed.name, "test")
        self.assertEqual(report.failed.returncode, 3)

    def test_the_sessions_own_error_is_raised(self):
        os.environ["REVALI_FAKE_SSH_DOWN"] = "1"
        lines = []
        with held_open(".bundle", "in use by another process"):
            with self.assertRaises(RunnerError) as cm:
                self.session([("test", "true")], lines)
        self.assertIn("could not reach host 'box'", str(cm.exception))
        self.bundle_named_once(lines)


class SshRemoteCleanupCannotRun(SshCleanupCase):
    """AC-2: the cleanup call fails with an OSError that is neither a timeout nor a missing
    ssh (the spawn is refused)."""

    @unittest.skipUnless(HAVE_BASH, "needs bash")
    def test_the_report_is_returned_and_the_line_is_logged(self):
        lines = []
        with self.remote_rm_cannot_start():
            report = self.session([("test", "true")], lines)
        self.assertTrue(report.ok)
        found = about(lines, "could not remove", "left for you to delete", "Access is denied")
        self.assertEqual(len(found), 1, lines)

    def test_the_sessions_own_error_is_raised(self):
        os.environ["REVALI_FAKE_SSH_BASH_FAILS"] = "1"
        lines = []
        with self.remote_rm_cannot_start():
            with self.assertRaises(RunnerError) as cm:
                self.session([("test", "true")], lines)
        self.assertIn("could not start (exit 127)", str(cm.exception))
        found = about(lines, "could not remove", "left for you to delete", "Access is denied")
        self.assertEqual(len(found), 1, lines)

    @unittest.skipUnless(HAVE_BASH, "needs bash")
    def test_a_lost_connection_says_the_directories_are_left(self):
        os.environ["REVALI_FAKE_SSH_SCRIPT_DROP"] = "0"
        with self.remote_rm_cannot_start():
            with self.assertRaises(RunnerError) as cm:
                self.session([("setup", "true"), ("test", "true")], [])
        message = str(cm.exception)
        self.assertIn("are left there", message)
        self.assertNotIn("revali removed this session's directories", message)


class SshExtraDirLeft(SshCleanupCase):
    """AC-4, ssh runner"""

    def test_named_once_and_the_error_is_raised(self):
        os.environ["REVALI_FAKE_SSH_DOWN"] = "1"
        lines = []
        with kept(LABEL + "-extra"):
            with self.assertRaises(RunnerError) as cm:
                self.session([("test", "true")], lines)
        self.assertIn("could not reach host 'box'", str(cm.exception))
        self.assertEqual(len(about(lines, os.path.join(self.logs, LABEL + "-extra"))), 1, lines)

    @unittest.skipUnless(HAVE_BASH, "needs bash")
    def test_named_once_and_the_report_is_returned(self):
        lines = []
        with kept(LABEL + "-extra"):
            report = self.session([("test", "true")], lines)
        self.assertTrue(report.ok)
        self.assertEqual(len(about(lines, os.path.join(self.logs, LABEL + "-extra"))), 1, lines)


class WslExtraDirLeft(RepoCase):
    """AC-4, wsl runner: `wsl` is a command that always fails, so the session ends with the
    runner's own error on any host."""

    def test_named_once_and_the_error_is_raised(self):
        missing = os.path.join(self.tmp, "no-wsl.py")
        os.environ["REVALI_WSL_CMD"] = "%s %s" % (_quote(sys.executable), _quote(missing))
        logs = os.path.join(self.tmp, "logs")
        head = git(["rev-parse", "HEAD"], self.repo).strip()
        runner = WslRunner(PlatformCfg(runner="wsl", distro="Ubuntu", command_timeout_min=1))
        lines = []
        with kept(LABEL + "-extra"):
            with self.assertRaises(RunnerError) as cm:
                runner.run(
                    self.repo,
                    head,
                    [("test", "true")],
                    {"t/x.py": "x\n"},
                    logs,
                    LABEL,
                    log=lines.append,
                )
        self.assertIn("wslpath failed", str(cm.exception))
        self.assertEqual(len(about(lines, os.path.join(logs, LABEL + "-extra"))), 1, lines)


if __name__ == "__main__":
    unittest.main()
