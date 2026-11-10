"""fix/cleanup-failures (M3.4.12): a cleanup that fails is logged and never replaces a sandbox
session's report or error (AC-1 to AC-4); `revali merge` archives with the root it found at its
start (AC-5), puts a failed `git branch -D` on one line (AC-6), and its worktree follow-up
reports git failures instead of raising them (AC-7); the docs say so (AC-8).

Runners run for real (the ssh stubs, a real local worktree) with the one cleanup call made to
fail; merges run black-box through the CLI with the fake gh and real git."""

import os
import shutil
import tempfile
import unittest
from contextlib import nullcontext as _nothing
from unittest import mock

from revali import EXIT_OK, gitops
from revali.config import PlatformCfg, history_path
from revali.procs import ExeNotFound, ProcError, ProcTimeout, Result
from revali.runners import LocalRunner, RunnerError, SshRunner, WslRunner
from revali.state import read_history
from tests.helpers import ROOT, RepoCase, git, run_cli
from tests.test_merge_pull import GitStandIn, MergeCase, PrimaryTreeCase, failed
from tests.test_ssh_runner import HAVE_BASH, SshCase, plat

LABEL = "validate-r1"


def problems(lines, needle):
    """The log lines that report a cleanup failure about `needle`."""
    return [line for line in lines if "could not" in line and needle in line]


class SshCleanupCase(SshCase):
    def session(self, steps, lines):
        logs = os.path.join(self.tmp, "logs-%d" % len(self.calls()))
        head = git(["rev-parse", "HEAD"], self.repo).strip()
        report = SshRunner(plat()).run(
            self.repo, head, steps, {"t/x.py": "x\n"}, logs, LABEL, log=lines.append
        )
        return logs, report

    def failing_remove(self, suffix, exc):
        real = os.remove

        def remove(path, *a, **kw):
            if str(path).endswith(suffix):
                raise exc
            return real(path, *a, **kw)

        return mock.patch("revali.runners.os.remove", side_effect=remove)


@unittest.skipUnless(HAVE_BASH, "needs bash")
class BundleLeft(SshCleanupCase):
    """AC-1"""

    def test_a_passing_report_is_returned(self):
        lines = []
        with self.failing_remove(".bundle", PermissionError(13, "in use by another process")):
            logs, report = self.session([("setup", "true"), ("test", "true")], lines)
        self.assertTrue(report.ok, [(s.name, s.returncode) for s in report.steps])
        bundle = os.path.join(logs, LABEL + ".bundle")
        (line,) = problems(lines, bundle)
        self.assertIn("in use by another process", line)
        self.assertIn("left for you to delete", line)

    def test_a_failing_report_is_returned(self):
        lines = []
        with self.failing_remove(".bundle", PermissionError(13, "in use")):
            _logs, report = self.session([("setup", "true"), ("test", "exit 3")], lines)
        self.assertEqual(report.failed.name, "test")
        self.assertEqual(report.failed.returncode, 3)

    def test_the_sessions_error_is_raised(self):
        os.environ["REVALI_FAKE_SSH_DOWN"] = "1"
        lines = []
        with self.failing_remove(".bundle", PermissionError(13, "in use")):
            with self.assertRaises(RunnerError) as cm:
                self.session([("test", "true")], lines)
        self.assertIn("could not reach", str(cm.exception))
        self.assertEqual(len(problems(lines, ".bundle")), 1, lines)


@unittest.skipUnless(HAVE_BASH, "needs bash")
class RemoteCleanupCannotRun(SshCleanupCase):
    """AC-2"""

    def failing_rm(self, exc):
        real = SshRunner._ssh

        def ssh(runner, command, timeout, log=None):
            if command.startswith("rm -rf"):
                raise exc
            return real(runner, command, timeout, log)

        return mock.patch.object(SshRunner, "_ssh", ssh)

    def test_any_proc_or_os_error(self):
        for exc in (PermissionError(13, "Access is denied"), ProcError("cancelled: ssh")):
            with self.subTest(exc=type(exc).__name__):
                lines = []
                with self.failing_rm(exc):
                    _logs, report = self.session([("test", "true")], lines)
                self.assertTrue(report.ok)
                line = [ln for ln in lines if "could not remove" in ln and "(left for you" in ln]
                self.assertEqual(len(line), 1, lines)
                self.assertIn(str(exc), line[0])

    def test_a_lost_connection_says_the_directories_are_left(self):
        os.environ["REVALI_FAKE_SSH_SCRIPT_DROP"] = "0"
        self.addCleanup(os.environ.pop, "REVALI_FAKE_SSH_SCRIPT_DROP", None)
        with self.failing_rm(PermissionError(13, "Access is denied")):
            with self.assertRaises(RunnerError) as cm:
                self.session([("setup", "true"), ("test", "true")], [])
        self.assertIn("are left there; delete them once the script has ended", str(cm.exception))


class LocalCleanupCase(RepoCase):
    """A real worktree from self.repo; `run` answers one git subcommand from `answers`."""

    def session(self, steps, answers, lines, **patches):
        from revali import runners

        real = runners.run
        pending = dict(answers)
        self.worktrees = []
        self.subs = []  # every `git worktree` subcommand, in order

        def run(cmd, *a, **kw):
            argv = [str(c) for c in cmd]
            if "worktree" in argv:
                sub = argv[argv.index("worktree") + 1]
                self.subs.append(sub)
                if sub == "add":
                    self.worktrees.append(argv[-2])
                answer = pending.pop(sub, None)
                if callable(answer):
                    answer = answer(real, cmd, *a, **kw)
                if isinstance(answer, BaseException):
                    raise answer
                if answer is not None:
                    return answer
            return real(cmd, *a, **kw)

        logs = os.path.join(self.tmp, "logs-%d" % len(self.worktrees))
        head = git(["rev-parse", "HEAD"], self.repo).strip()
        runner = LocalRunner(PlatformCfg(runner="local", command_timeout_min=1))
        with mock.patch("revali.runners.run", side_effect=run):
            with mock.patch.multiple("revali.runners", **patches) if patches else _nothing():
                return runner.run(self.repo, head, steps, {}, logs, LABEL, log=lines.append)

    def assert_temp_dir_gone(self):
        (wt,) = self.worktrees
        self.assertFalse(os.path.exists(os.path.dirname(wt)), wt)


class LocalWorktreeLeft(LocalCleanupCase):
    """AC-3"""

    def test_a_remove_that_times_out(self):
        lines = []
        report = self.session(
            [("test", "exit 3")], {"remove": ProcTimeout("timed out after 120s: git")}, lines
        )
        self.assertEqual(report.failed.returncode, 3)
        (wt,) = self.worktrees
        (line,) = problems(lines, wt)
        self.assertIn("timed out after 120s", line)
        self.assertIn("worktree remove", line)
        self.assert_temp_dir_gone()
        # the prune still ran and dropped the worktree git could not remove
        listed = git(["worktree", "list", "--porcelain"], self.repo)
        self.assertNotIn(os.path.basename(os.path.dirname(wt)), listed)

    def test_a_remove_that_exits_non_zero_is_one_line(self):
        lines = []
        bad = Result(
            cmd=["git"], returncode=128, stdout="", stderr="fatal: busy\nhint: twice", duration=0
        )
        report = self.session([("test", "exit 0")], {"remove": bad}, lines)
        self.assertTrue(report.ok)
        (wt,) = self.worktrees
        (line,) = problems(lines, wt)
        self.assertIn("fatal: busy hint: twice", line)
        self.assert_temp_dir_gone()

    def test_a_prune_that_cannot_start(self):
        lines = []
        report = self.session(
            [("test", "exit 0")], {"prune": ExeNotFound("cannot execute git: gone")}, lines
        )
        self.assertTrue(report.ok)
        (line,) = [ln for ln in lines if "could not" in ln]
        self.assertIn("worktree prune", line)
        self.assertIn("cannot execute git: gone", line)
        self.assert_temp_dir_gone()

    def test_the_steps_error_is_raised(self):
        lines = []
        with self.assertRaises(OSError) as cm:
            self.session(
                [("test", "exit 0")],
                {"remove": ProcTimeout("timed out after 120s: git")},
                lines,
                run_shell=mock.Mock(side_effect=OSError("step could not start")),
            )
        self.assertIn("step could not start", str(cm.exception))
        self.assertEqual(len(problems(lines, self.worktrees[0])), 1, lines)

    def test_nothing_logged_when_the_cleanup_works(self):
        lines = []
        report = self.session([("test", "exit 0")], {}, lines)
        self.assertTrue(report.ok)
        self.assertEqual([ln for ln in lines if "could not" in ln], [])
        self.assert_temp_dir_gone()


def rmtree_keeping(keep):
    """shutil.rmtree that leaves any directory whose path ends with `keep` (a string) or for
    which `keep(path)` holds."""
    real = shutil.rmtree
    test = keep if callable(keep) else lambda path: path.endswith(keep)

    def rmtree(path, *a, **kw):
        if test(str(path)):
            return None
        return real(path, *a, **kw)

    return mock.patch("revali.runners.shutil.rmtree", side_effect=rmtree)


class LocalTempDir(LocalCleanupCase):
    """PR #66 round 1 F1 (1) and follow-up 1: the temp dir of a `git worktree add` that
    failed or did not finish."""

    def test_a_temp_dir_left_after_a_failed_add_is_logged(self):
        lines = []
        bad = Result(
            cmd=["git"], returncode=128, stdout="", stderr="fatal: invalid ref", duration=0
        )

        def is_temp(path):
            return os.path.basename(path).startswith("revali-%s-" % LABEL)

        with rmtree_keeping(is_temp):
            with self.assertRaises(RunnerError) as cm:
                self.session([("test", "exit 0")], {"add": bad}, lines)
        self.assertIn("git worktree add failed: fatal: invalid ref", str(cm.exception))
        tmp = os.path.dirname(self.worktrees[0])
        self.addCleanup(shutil.rmtree, tmp, True)
        self.assertEqual(len(problems(lines, tmp)), 1, lines)

    def test_an_add_that_raises_still_removes_the_temp_dir(self):
        for exc in (ProcTimeout("timed out after 300s: git"), ExeNotFound("no git")):
            with self.subTest(exc=type(exc).__name__):
                lines = []
                with self.assertRaises(type(exc)):
                    self.session([("test", "exit 0")], {"add": exc}, lines)
                self.assert_temp_dir_gone()
                self.assertEqual([ln for ln in lines if "could not" in ln], [])
                self.assertEqual(self.subs, ["add", "prune"])

    def test_a_worktree_registered_before_the_add_timed_out_is_pruned(self):
        # PR #66 round 2 follow-up: git can register the worktree and then not finish
        def add_then_time_out(real, cmd, *a, **kw):
            res = real(cmd, *a, **kw)
            self.assertTrue(res.ok, res.text)
            return ProcTimeout("timed out after 300s: git")

        with self.assertRaises(ProcTimeout):
            self.session([("test", "exit 0")], {"add": add_then_time_out}, [])
        self.assert_temp_dir_gone()
        listed = git(["worktree", "list", "--porcelain"], self.repo)
        self.assertNotIn(os.path.basename(os.path.dirname(self.worktrees[0])), listed)

    def test_no_prune_without_git(self):
        lines = []
        with self.assertRaises(ExeNotFound):
            self.session(
                [("test", "exit 0")],
                {},
                lines,
                resolve=mock.Mock(side_effect=ExeNotFound("executable not found: git")),
            )
        self.assertEqual(self.subs, [])
        self.assertEqual([ln for ln in lines if "could not" in ln], [])


class StaleExtraDir(unittest.TestCase):
    """PR #66 round 2 follow-up: an earlier session's `<label>-extra` that cannot be removed
    before staging is named, since its files would travel with this session's."""

    def setUp(self):
        self.logs = tempfile.mkdtemp(prefix="revali-stale-")
        self.addCleanup(shutil.rmtree, self.logs, True)
        self.extra = os.path.join(self.logs, LABEL + "-extra")
        os.makedirs(os.path.join(self.extra, "t"))
        with open(os.path.join(self.extra, "t", "old.py"), "w", encoding="utf-8") as fh:
            fh.write("old\n")

    def stage(self):
        from revali.runners import stage_inputs

        lines = []
        steps, extra = stage_inputs(
            self.logs, LABEL, [("test", "true")], {"t/x.py": "x\n"}, lines.append
        )
        self.assertEqual(extra, self.extra)
        self.assertEqual(steps, [("test", "true")])
        return lines

    def test_named_when_it_stays(self):
        with rmtree_keeping(LABEL + "-extra"):
            lines = self.stage()
        self.assertEqual(len(problems(lines, self.extra)), 1, lines)

    def test_removed_quietly_otherwise(self):
        lines = self.stage()
        self.assertEqual(lines, [])
        self.assertFalse(os.path.exists(os.path.join(self.extra, "t", "old.py")))


class RemoveDirReason(unittest.TestCase):
    """PR #66 round 1 F2: the line for a directory left behind carries the first error."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="revali-keep-")
        self.addCleanup(shutil.rmtree, self.dir, True)

    def left(self, rmtree):
        from revali.runners import remove_dir

        lines = []
        with mock.patch("revali.runners.shutil.rmtree", side_effect=rmtree):
            remove_dir(self.dir, LABEL, lines.append)
        (line,) = lines
        self.assertIn(self.dir, line)
        self.assertIn("left for you to delete", line)
        return line

    def test_onexc(self):
        def rmtree(path, onexc=None, **kw):
            onexc(os.unlink, path, PermissionError(13, "held\nopen"))
            onexc(os.rmdir, path, OSError(39, "second"))

        line = self.left(rmtree)
        self.assertIn("[Errno 13] held open", line)
        self.assertNotIn("second", line)

    def test_onerror_on_python_3_11(self):
        def rmtree(path, onerror=None, **kw):
            if "onexc" in kw:
                raise TypeError("rmtree() got an unexpected keyword argument 'onexc'")
            exc = PermissionError(13, "held open")
            onerror(os.unlink, path, (type(exc), exc, None))

        self.assertIn("[Errno 13] held open", self.left(rmtree))

    def test_nothing_for_a_directory_that_is_gone(self):
        from revali.runners import remove_dir

        lines = []
        shutil.rmtree(self.dir)
        remove_dir(self.dir, LABEL, lines.append)
        remove_dir(self.dir + "-never", LABEL, lines.append)
        self.assertEqual(lines, [])


class WslExtraLeft(unittest.TestCase):
    """AC-4, wsl runner"""

    def setUp(self):
        self.logs = tempfile.mkdtemp(prefix="revali-extra-")
        self.addCleanup(shutil.rmtree, self.logs, True)

    def test_named_once_and_the_error_is_raised(self):
        runner = WslRunner(PlatformCfg(runner="wsl", distro="Ubuntu", command_timeout_min=1))
        lines = []

        def wslpath(_self, path, log=None):
            raise RunnerError("wslpath failed for %s" % path)

        with (
            mock.patch("revali.runners.clone_source", return_value=("D:/x/repo", "abc")),
            mock.patch.object(WslRunner, "wslpath", wslpath),
            rmtree_keeping(LABEL + "-extra"),
        ):
            with self.assertRaises(RunnerError) as cm:
                runner.run(
                    "D:/x/repo",
                    "abc",
                    [("test", "true")],
                    {"t/x.py": "x\n"},
                    self.logs,
                    LABEL,
                    log=lines.append,
                )
        self.assertIn("wslpath failed", str(cm.exception))
        extra = os.path.join(self.logs, LABEL + "-extra")
        self.assertEqual(len(problems(lines, extra)), 1, lines)

    def test_staging_that_fails_names_the_extra_dir_it_left(self):
        # PR #66 round 1 F1 (2): stage_inputs' own removal, before the runner's `finally`
        from revali.state import write_text

        def write(path, text):
            if path.endswith("x.py"):
                raise OSError("disk full")
            return write_text(path, text)

        runner = WslRunner(PlatformCfg(runner="wsl", distro="Ubuntu", command_timeout_min=1))
        lines = []
        with (
            mock.patch("revali.runners.clone_source", return_value=("D:/x/repo", "abc")),
            mock.patch("revali.runners.write_text", side_effect=write),
            rmtree_keeping(LABEL + "-extra"),
        ):
            with self.assertRaises(OSError) as cm:
                runner.run(
                    "D:/x/repo",
                    "abc",
                    [("test", "true")],
                    {"t/x.py": "x\n"},
                    self.logs,
                    LABEL,
                    log=lines.append,
                )
        self.assertIn("disk full", str(cm.exception))
        extra = os.path.join(self.logs, LABEL + "-extra")
        self.assertEqual(len(problems(lines, extra)), 1, lines)

    def test_nothing_logged_when_it_is_gone(self):
        runner = WslRunner(PlatformCfg(runner="wsl", distro="Ubuntu", command_timeout_min=1))
        lines = []
        with (
            mock.patch("revali.runners.clone_source", return_value=("D:/x/repo", "abc")),
            mock.patch.object(
                WslRunner, "wslpath", mock.Mock(side_effect=RunnerError("wslpath failed"))
            ),
        ):
            with self.assertRaises(RunnerError):
                runner.run(
                    "D:/x/repo", "abc", [("test", "true")], {}, self.logs, LABEL, log=lines.append
                )
        self.assertEqual([ln for ln in lines if "could not" in ln], [])


@unittest.skipUnless(HAVE_BASH, "needs bash")
class SshExtraLeft(SshCleanupCase):
    """AC-4, ssh runner"""

    def test_named_once_and_the_report_is_returned(self):
        lines = []
        with rmtree_keeping(LABEL + "-extra"):
            logs, report = self.session([("test", "true")], lines)
        self.assertTrue(report.ok)
        self.assertEqual(len(problems(lines, os.path.join(logs, LABEL + "-extra"))), 1, lines)

    def test_nothing_logged_when_every_cleanup_works(self):
        lines = []
        logs, report = self.session([("test", "true")], lines)
        self.assertTrue(report.ok)
        self.assertEqual([ln for ln in lines if "could not" in ln], [])
        self.assertNotIn(LABEL + ".bundle", os.listdir(logs))
        self.assertNotIn(LABEL + "-extra", os.listdir(logs))


class ArchiveWithTheFirstRoot(PrimaryTreeCase):
    """AC-5"""

    def test_git_gone_after_the_merge_still_archives(self):
        real = gitops.repo_root

        def repo_root(cwd):
            if self.gh_calls("pr", "merge"):
                return None
            return real(cwd)

        with mock.patch("revali.gitops.repo_root", side_effect=repo_root):
            out = self.merged()  # asserts the state directory is gone
        self.line_with(out, "archived to")


class BranchErrorOneLine(MergeCase):
    """AC-6"""

    ERROR = "error: cannot delete branch\nhint: it is used by another worktree"

    def check(self, out, prefix):
        line = self.line_with(out, prefix)
        self.assertIn(
            "git branch -D failed: error: cannot delete branch hint: it is used by another",
            line,
        )
        self.assertFalse([ln for ln in out.splitlines() if ln.startswith("hint:")], out)

    def test_primary_tree(self):
        self.ready(self.repo)
        GitStandIn(self, "branch", [failed("branch", self.ERROR)])
        code, out = run_cli(["merge"])
        self.assertEqual(code, EXIT_OK, out)
        self.check(out, "local: on main")

    def test_worktree_mode(self):
        git(["checkout", "-q", "main"], self.repo)
        wt = self.add_worktree("feature/mul", "wt-feature")
        os.chdir(wt)
        self.ready(wt)
        GitStandIn(self, "branch", [failed("branch", self.ERROR)])
        code, out = run_cli(["merge"])
        self.assertEqual(code, EXIT_OK, out)
        self.check(out, "worktree:")

    def test_cut_to_200_characters(self):
        self.ready(self.repo)
        GitStandIn(self, "branch", [failed("branch", "x" * 300)])
        code, out = run_cli(["merge"])
        self.assertEqual(code, EXIT_OK, out)
        line = self.line_with(out, "local: on main")
        self.assertIn("x" * 200, line)
        self.assertNotIn("x" * 201, line)


class WorktreeFollowUp(MergeCase):
    """AC-7: git failing in the linked worktree's follow-up after the PR merged."""

    def merged(self, sub, answers):
        git(["checkout", "-q", "main"], self.repo)
        wt = self.add_worktree("feature/mul", "wt-feature")
        os.chdir(wt)
        rdir = self.ready(wt)
        git_ = GitStandIn(self, sub, answers)
        code, out = run_cli(["merge"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertNotIn("Traceback", out)
        self.assertIn("MERGED: PR #7 into main", out)
        self.assertFalse(os.path.isdir(rdir), "state directory not archived")
        self.assertTrue(any(r.get("stage") == "merged" for r in read_history(history_path())))
        return out, git_

    def test_push_delete_times_out(self):
        out, _ = self.merged("push", [ProcTimeout("timed out after 300s: git")])
        line = self.line_with(out, "note: could not delete origin/feature/mul")
        self.assertIn("timed out after 300s", line)
        self.line_with(out, "worktree: detached at the merged main")

    def test_fetch_cannot_start(self):
        out, git_ = self.merged("fetch", [ExeNotFound("cannot execute git: gone")] * 2)
        self.assertIn("cannot execute git", self.line_with(out, "note: git fetch failed"))
        # no checkout without a fetched base: the worktree stays on the branch, which is kept
        self.assertIn("still on feature/mul", self.line_with(out, "worktree:"))
        self.assertIn("local branch feature/mul kept", self.line_with(out, "worktree:"))

    def test_checkout_detach_spawn_error(self):
        out, _ = self.merged("checkout", [PermissionError(13, "Access is denied")])
        line = self.line_with(out, "note: git checkout --detach failed")
        self.assertIn("Access is denied", line)

    def test_branch_check_fails(self):
        real = gitops.current_branch

        def current_branch(cwd):
            if self.gh_calls("pr", "merge"):
                raise gitops.GitError("git rev-parse --abbrev-ref HEAD failed: index\nbroken")
            return real(cwd)

        with mock.patch("revali.gitops.current_branch", side_effect=current_branch):
            out, _ = self.merged("push", [])
        line = self.line_with(out, "worktree:")
        self.assertIn("could not tell whether local branch feature/mul was removed", line)
        self.assertIn("could not tell where this worktree is", line)
        self.assertIn("index broken", line)
        self.assertIn("git worktree remove", line)

    def test_branch_deletion_cannot_run(self):
        out, _ = self.merged("branch", [ProcTimeout("timed out after 120s: git")])
        line = self.line_with(out, "worktree: detached at the merged main")
        self.assertIn("could not tell whether local branch feature/mul was removed", line)
        self.assertIn("timed out after 120s", line)


class Docs(unittest.TestCase):
    """AC-8"""

    def read(self, name):
        with open(os.path.join(ROOT, "docs", name), encoding="utf-8") as fh:
            return " ".join(fh.read().split())

    def test_sandbox_md(self):
        text = self.read("sandbox.md")
        self.assertIn("A cleanup that fails is logged", text)
        self.assertIn("never changes the session's result", text)

    def test_side_effects_md(self):
        text = self.read("side-effects.md")
        self.assertIn("each failure in the worktree follow-up is reported", text)


if __name__ == "__main__":
    unittest.main()
