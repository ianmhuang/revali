"""Review of fix/cleanup-failures, AC-1 to AC-4: a sandbox runner's cleanup that fails is
logged in one line and the session's report, or the error that ended it, comes back unchanged.

The runners run for real (a real git worktree, the ssh / scp stubs). The one cleanup step a
test is about is made to fail where revali meets the system: `subprocess.run` for a command
that times out, cannot start or exits non-zero, `os.remove` / `shutil.rmtree` for a file or
directory held open.

Round 2 adds the two cleanup paths of round 1's F1 (the local runner's temp directory after
a failed `git worktree add`, the `<label>-extra` directory when staging fails) and F2 (the
line for a directory says why it is still there).

Round 3 adds the two follow-ups of round 2: a worktree that `git worktree add` registered
before it stopped is dropped from the repository's list, and an earlier session's
`<label>-extra` directory that cannot be removed before staging is named in the log."""

import os
import shutil
import subprocess
import sys
import unittest
from unittest import mock

from revali.config import PlatformCfg
from revali.procs import ProcError
from revali.runners import LocalRunner, RunnerError, SshRunner, WslRunner
from tests.helpers import RepoCase, _quote, git
from tests.test_ssh_runner import HAVE_BASH, SshCase, plat

LABEL = "validate-r1"


def git_cmd(argv, *words):
    """True for a git command line that carries every one of `words`."""
    return os.path.basename(argv[0]).lower().startswith("git") and all(w in argv[1:] for w in words)


class Commands:
    """While active, `decide(argv)` sees every command started through subprocess.run and
    settles its outcome: None runs it, an exception is raised in its place,
    (exit code, stderr) is returned as its result without running it, and a function is
    called with a `really()` that runs the command, to answer for it afterwards."""

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
            if callable(answer):
                return answer(lambda: real(args, *a, **kw))
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


def kept(which):
    """shutil.rmtree that leaves a directory where it is: one whose path ends in `which` (a
    string), or for which `which(path)` holds."""
    real = shutil.rmtree
    test = which if callable(which) else lambda path: path.endswith(which)

    def rmtree(path, *a, **kw):
        if test(str(path)):
            return None
        return real(path, *a, **kw)

    return mock.patch.object(shutil, "rmtree", rmtree)


def refused(suffix, message, times=None):
    """shutil.rmtree that cannot remove a directory ending in `suffix` and reports it the way
    the real one does: to the `onexc` / `onerror` handler it was given, by raising without
    one, and not at all with `ignore_errors`. With `times`, only that many removals are
    refused and the later ones happen."""
    real = shutil.rmtree
    left = [times]

    def rmtree(path, *a, **kw):
        if not str(path).endswith(suffix) or left[0] == 0:
            return real(path, *a, **kw)
        if left[0] is not None:
            left[0] -= 1
        exc = PermissionError(13, message)
        if kw.get("onexc"):
            kw["onexc"](os.rmdir, path, exc)
        elif kw.get("onerror"):
            kw["onerror"](os.rmdir, path, (PermissionError, exc, None))
        elif not (kw.get("ignore_errors") or (a and a[0])):
            raise exc
        return None

    return mock.patch.object(shutil, "rmtree", rmtree)


def session_temp_dir(path):
    """The local runner's temp directory for LABEL (it holds the worktree)."""
    return os.path.basename(path).startswith("revali-%s-" % LABEL)


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

    def test_the_temp_dir_left_after_a_failed_add_is_named(self):
        # round 1 F1 (1): the cleanup before the `finally`, when `git worktree add` fails
        def decide(argv):
            if git_cmd(argv, "worktree", "add"):
                return (128, "fatal: invalid reference: nope\n")
            return None

        lines = []
        with kept(session_temp_dir):
            with self.assertRaises(RunnerError) as cm:
                self.session([("test", "exit 0")], decide, lines)
        tmp = os.path.dirname(self.cmds.worktree())
        self.addCleanup(shutil.rmtree, tmp, True)
        self.assertIn("git worktree add failed: fatal: invalid reference: nope", str(cm.exception))
        self.one_line(lines, tmp, "left for you to delete")

    def test_an_add_that_times_out_leaves_no_temp_dir(self):
        def decide(argv):
            if git_cmd(argv, "worktree", "add"):
                return subprocess.TimeoutExpired(argv, 300)
            return None

        lines = []
        with self.assertRaises(ProcError) as cm:
            self.session([("test", "exit 0")], decide, lines)
        tmp = os.path.dirname(self.cmds.worktree())
        self.addCleanup(shutil.rmtree, tmp, True)
        self.assertIn("timed out", str(cm.exception))
        self.assertFalse(os.path.exists(tmp), tmp)
        self.assertEqual(about(lines, "left for you to delete"), [])

    def add_registers_and_then(self, outcome):
        """A session whose `git worktree add` really runs, so the worktree is registered, and
        then ends as `outcome(argv)` says: an exception to raise or (exit code, stderr)."""

        def decide(argv):
            if not git_cmd(argv, "worktree", "add"):
                return None

            def answer(really):
                done = really()
                if done.returncode != 0:
                    raise AssertionError("git worktree add: %s" % done.stderr)
                end = outcome(argv)
                if isinstance(end, BaseException):
                    raise end
                return subprocess.CompletedProcess(argv, end[0], stdout="", stderr=end[1])

            return answer

        lines = []
        with self.assertRaises((ProcError, RunnerError)) as cm:
            self.session([("test", "exit 0")], decide, lines)
        tmp = os.path.dirname(self.cmds.worktree())
        self.addCleanup(shutil.rmtree, tmp, True)
        # neither the directory nor its entry in the repository's worktree list is left
        self.assertFalse(os.path.exists(tmp), tmp)
        listed = git(["worktree", "list", "--porcelain"], self.repo)
        self.assertNotIn(os.path.basename(tmp), listed)
        self.assertEqual(about(lines, "could not"), [])
        return str(cm.exception)

    def test_a_worktree_registered_before_the_add_timed_out_is_dropped(self):
        # round 2 follow-up
        message = self.add_registers_and_then(lambda argv: subprocess.TimeoutExpired(argv, 300))
        self.assertIn("timed out", message)

    def test_a_worktree_registered_before_the_add_failed_is_dropped(self):
        # round 2 follow-up
        message = self.add_registers_and_then(lambda argv: (128, "fatal: stopped midway\n"))
        self.assertIn("git worktree add failed: fatal: stopped midway", message)


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

    def test_the_line_says_why_the_directory_is_still_there(self):
        # round 1 F2
        os.environ["REVALI_FAKE_SSH_DOWN"] = "1"
        lines = []
        with refused(LABEL + "-extra", "in use by another\nprocess"):
            with self.assertRaises(RunnerError) as cm:
                self.session([("test", "true")], lines)
        self.assertIn("could not reach host 'box'", str(cm.exception))
        found = about(lines, os.path.join(self.logs, LABEL + "-extra"))
        self.assertEqual(len(found), 1, lines)
        self.assertIn("in use by another process", found[0])  # on one line
        self.assertIn("left for you to delete", found[0])


class StagingFails(SshCleanupCase):
    """Round 1 F1 (2): staging the inputs fails before the runner's own `finally` exists, and
    the `<label>-extra` directory it made cannot be removed."""

    def test_the_staging_error_is_raised_and_the_directory_is_named_once(self):
        # a directory where the step's command file goes: writing the file fails on any host
        blocked = os.path.join(self.tmp, "logs", "%s-test.cmd" % LABEL)
        os.makedirs(blocked)
        lines = []
        with kept(LABEL + "-extra"):
            with self.assertRaises(OSError) as cm:
                self.session([("test", "true")], lines)
        self.assertIn("%s-test.cmd" % LABEL, str(cm.exception))
        extra = os.path.join(self.logs, LABEL + "-extra")
        self.assertTrue(os.path.isdir(extra))
        found = about(lines, extra)
        self.assertEqual(len(found), 1, lines)
        self.assertIn("left for you to delete", found[0])


class StaleExtraDirBeforeStaging(SshCleanupCase):
    """Round 2 follow-up: the `<label>-extra` directory an earlier session left cannot be
    removed before staging, so its files travel with this session's and the log says so."""

    def test_named_once_and_the_sessions_error_is_raised(self):
        os.environ["REVALI_FAKE_SSH_DOWN"] = "1"
        extra = os.path.join(self.tmp, "logs", LABEL + "-extra")
        os.makedirs(os.path.join(extra, "t"))
        with open(os.path.join(extra, "t", "old.py"), "w", encoding="utf-8") as fh:
            fh.write("old\n")
        lines = []
        # held while the inputs are staged, free again when the session ends
        with refused(LABEL + "-extra", "in use by another process", times=1):
            with self.assertRaises(RunnerError) as cm:
                self.session([("test", "true")], lines)
        self.assertIn("could not reach host 'box'", str(cm.exception))
        found = about(lines, extra)
        self.assertEqual(len(found), 1, lines)
        self.assertIn("could not remove", found[0])
        self.assertIn("in use by another process", found[0])
        self.assertFalse(os.path.exists(extra), extra)


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
