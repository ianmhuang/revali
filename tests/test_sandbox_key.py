"""Every checkout and branch gets its own sandbox directory (fix/sandbox-key-collision, M3.4.3).
Before this, the wsl and ssh runners cloned under <sandbox_dir>/<repo>/<branch>/<label> with
<repo> the checkout's directory name and <branch> the branch with `/` written as `__`, so two
clones named alike on one branch, `feature/x` and `feature__x`, or `my repo` and `my_repo` on
the ssh host shared a directory, and the script's `rm -rf` of it deleted the other's clone."""

import os
import re
import shutil
import sys
import tempfile
import unittest
from unittest import mock

from revali.config import PlatformCfg
from revali.runners import RunnerError, SshRunner, WslRunner, sandbox_key, sandbox_scope
from tests.helpers import FAKE_BIN, RepoCase, _quote, claude_entry, git, run_cli
from tests.test_review_basererun import BaseRerunCase
from tests.test_ssh_runner import HAVE_BASH, SshCase, plat

WSL_STUB = os.path.join(FAKE_BIN, "wsl_stub.py")
BRANCH = "feature/mul"


def bash_form(path):
    return path.replace("\\", "/")


def clone_as(src, parent, name):
    """A second checkout of the fixture repository at <parent>/<name>, on the same branch."""
    os.makedirs(parent, exist_ok=True)
    dest = os.path.join(parent, name)
    git(["clone", "-q", src, dest], parent)
    git(["checkout", "-q", BRANCH], dest)
    return dest


class KeyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="revali-key-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_eight_hex_characters_and_stable(self):  # AC-4
        a = sandbox_key(self.tmp, BRANCH)
        self.assertRegex(a, r"^[0-9a-f]{8}$")
        self.assertEqual(a, sandbox_key(self.tmp, BRANCH))

    def test_same_checkout_spelled_differently_gives_the_same_key(self):  # AC-4
        inner = os.path.join(self.tmp, "app")
        os.makedirs(inner)
        spelled = os.path.join(self.tmp, "app", "..", "app") + os.sep
        self.assertEqual(sandbox_key(inner, BRANCH), sandbox_key(spelled, BRANCH))

    def test_same_directory_name_elsewhere_differs(self):  # AC-1
        a = os.path.join(self.tmp, "a", "app")
        b = os.path.join(self.tmp, "b", "app")
        self.assertNotEqual(sandbox_key(a, BRANCH), sandbox_key(b, BRANCH))

    def test_branch_spellings_that_share_a_directory_name_differ(self):  # AC-2
        self.assertNotEqual(sandbox_key(self.tmp, "feature/x"), sandbox_key(self.tmp, "feature__x"))

    def test_scope_with_and_without_a_key(self):  # AC-4, AC-6
        self.assertEqual(sandbox_scope("feature__x", "0123abcd"), "feature__x-0123abcd")
        self.assertEqual(sandbox_scope("feature__x", ""), "feature__x")
        self.assertEqual(sandbox_scope("", ""), "")


class WslLayout(unittest.TestCase):
    """AC-4 / AC-6 on the path the wsl runner hands its script."""

    def sandbox_for(self, repo_root, **kw):
        seen = {}

        def capture(*a, **k):  # a[0] is the runner
            seen["sandbox"] = a[7]
            seen["scope"] = k.get("scope", "")
            raise RunnerError("captured")

        logs = tempfile.mkdtemp(prefix="revali-key-")
        self.addCleanup(shutil.rmtree, logs, True)
        r = WslRunner(PlatformCfg(runner="wsl", distro="Ubuntu", sandbox_dir="~/.revali/sandbox"))
        with (
            mock.patch.object(WslRunner, "wslpath", lambda self, p, log=None: p),
            mock.patch.object(WslRunner, "script", capture),
        ):
            with self.assertRaises(RunnerError):
                r.run(repo_root, "HEAD", [("test", "true")], {}, logs, "validate-r1", **kw)
        return seen

    def test_keyed_layout(self):
        seen = self.sandbox_for("D:/x/sample", scope="feature__mul", key="0123abcd")
        self.assertEqual(
            seen["sandbox"], "$HOME/.revali/sandbox/sample/feature__mul-0123abcd/validate-r1"
        )
        self.assertEqual(seen["scope"], "feature__mul-0123abcd")  # the script's cleanup level

    def test_without_a_key_the_layout_is_unchanged(self):  # AC-6
        seen = self.sandbox_for("D:/x/sample", scope="feature__mul")
        self.assertEqual(seen["sandbox"], "$HOME/.revali/sandbox/sample/feature__mul/validate-r1")
        self.assertEqual(seen["scope"], "feature__mul")


class WslStubCase(RepoCase):
    """The wsl runner end to end: the script runs under the host's bash through the stub."""

    runner = "wsl"

    def setUp(self):
        super().setUp()
        self.use_real_local_runner()
        os.environ["REVALI_WSL_CMD"] = "%s %s" % (_quote(sys.executable), _quote(WSL_STUB))
        self.sand = tempfile.mkdtemp(prefix="revali-sand-")
        self.addCleanup(shutil.rmtree, self.sand, True)

    def runner_(self):
        return WslRunner(
            PlatformCfg(
                runner="wsl",
                distro="Ubuntu",
                command_timeout_min=1,
                sandbox_dir=bash_form(self.sand),
            )
        )

    def run_in(self, repo_root, key):
        logs = os.path.join(self.tmp, "logs-" + key)
        marker = os.path.join(logs, "where.txt")
        head = git(["rev-parse", "HEAD"], repo_root).strip()
        report = self.runner_().run(
            repo_root,
            head,
            [("test", 'pwd > "%s"' % bash_form(marker))],
            {},
            logs,
            "validate-r1",
            scope="feature__mul",
            key=key,
        )
        self.assertTrue(report.ok, [(s.name, s.returncode, s.stdout[-300:]) for s in report.steps])
        with open(marker, encoding="utf-8") as fh:
            return fh.read().strip().replace("\\", "/")

    def busy(self, *parts):
        path = os.path.join(self.sand, *parts, "repo")
        os.makedirs(path)
        with open(os.path.join(path, "busy"), "w", encoding="utf-8", newline="") as fh:
            fh.write("x")
        return os.path.join(path, "busy")


class WslTwoCheckouts(WslStubCase):
    """AC-1, AC-4."""

    @unittest.skipUnless(HAVE_BASH, "needs bash")
    def test_two_checkouts_named_alike_on_one_branch_do_not_share(self):  # AC-1
        other = clone_as(self.repo, os.path.join(self.tmp, "elsewhere"), "sample")
        self.assertEqual(os.path.basename(other), os.path.basename(self.repo))
        mine, theirs = sandbox_key(self.repo, BRANCH), sandbox_key(other, BRANCH)
        self.assertNotEqual(mine, theirs)
        # the other checkout is mid-session: its clone must survive ours
        busy = self.busy("sample", "feature__mul-" + theirs, "validate-r1")
        where = self.run_in(self.repo, mine)
        self.assertTrue(where.endswith("/sample/feature__mul-%s/validate-r1/repo" % mine), where)
        self.assertTrue(os.path.isfile(busy))
        # ours is gone: <label>, then <branch>-<key>; <repo> stays while the other is there
        self.assertFalse(os.path.exists(os.path.join(self.sand, "sample", "feature__mul-" + mine)))
        self.assertTrue(os.path.isdir(os.path.join(self.sand, "sample")))

    @unittest.skipUnless(HAVE_BASH, "needs bash")
    def test_all_levels_go_when_empty(self):  # AC-4
        self.run_in(self.repo, sandbox_key(self.repo, BRANCH))
        self.assertEqual(os.listdir(self.sand), [])

    @unittest.skipUnless(HAVE_BASH, "needs bash")
    def test_the_old_layout_was_shared(self):
        # the defect itself, kept as a control: without a key both checkouts use one directory
        busy = self.busy("sample", "feature__mul", "validate-r1")
        logs = os.path.join(self.tmp, "logs-plain")
        head = git(["rev-parse", "HEAD"], self.repo).strip()
        self.runner_().run(
            self.repo, head, [("test", "true")], {}, logs, "validate-r1", scope="feature__mul"
        )
        self.assertFalse(os.path.exists(busy))


class WslFromAWorktree(WslStubCase):
    """AC-9: a linked worktree's `.git` file names its git directory by a host path, which
    git inside WSL cannot resolve on Windows (`/mnt/e/.../E:/...`); the runner clones from the
    common git directory and resolves `HEAD` on the host. The stub's bash can read host paths,
    so the WSL failure itself is not reproduced here (the PR's own round 1 hit it); these tests
    pin what the fix does."""

    def setUp(self):
        super().setUp()
        self.wt = os.path.join(self.tmp, "wt")
        git(["worktree", "add", "-q", "-b", "feature/other", self.wt], self.repo)
        with open(os.path.join(self.wt, "other.txt"), "w", encoding="utf-8", newline="") as fh:
            fh.write("only in the worktree\n")
        git(["add", "other.txt"], self.wt)
        git(["commit", "-q", "-m", "worktree commit"], self.wt)

    def head_seen(self, ref):
        logs = os.path.join(self.tmp, "logs-wt")
        marker = os.path.join(logs, "head.txt")
        report = self.runner_().run(
            self.wt,
            ref,
            [("test", 'git rev-parse HEAD > "%s"' % bash_form(marker))],
            {},
            logs,
            "validate-r1",
            scope="feature__other",
            key=sandbox_key(self.wt, "feature/other"),
        )
        self.assertTrue(report.ok, [(s.name, s.returncode, s.stdout[-300:]) for s in report.steps])
        with open(marker, encoding="utf-8") as fh:
            head = fh.read().strip()
        with open(os.path.join(logs, "validate-r1.sh"), encoding="utf-8") as fh:
            host = [ln for ln in fh.read().splitlines() if ln.startswith("HOST=")][0]
        return head, host

    @unittest.skipUnless(HAVE_BASH, "needs bash")
    def test_head_is_the_worktrees_commit(self):
        wt_head = git(["rev-parse", "HEAD"], self.wt).strip()
        self.assertNotEqual(wt_head, git(["rev-parse", "HEAD"], self.repo).strip())
        head, _ = self.head_seen("HEAD")
        self.assertEqual(head, wt_head)

    @unittest.skipUnless(HAVE_BASH, "needs bash")
    def test_the_clone_source_is_the_common_git_directory(self):
        _, host = self.head_seen("HEAD")
        self.assertTrue(host.rstrip('"').endswith("/.git"), host)
        self.assertNotIn("/wt", host)

    @unittest.skipUnless(HAVE_BASH, "needs bash")
    def test_an_unknown_ref_still_fails_in_the_script(self):
        # unresolved on the host, the ref goes to the script as before, whose checkout fails
        logs = os.path.join(self.tmp, "logs-bad")
        with self.assertRaises(RunnerError) as ctx:
            self.runner_().run(self.wt, "no-such-ref", [("test", "true")], {}, logs, "validate-r1")
        self.assertIn("clone/checkout failed", str(ctx.exception))
        with open(os.path.join(logs, "validate-r1.sh"), encoding="utf-8") as fh:
            self.assertIn('REF="no-such-ref"', fh.read())


class SshNames(SshCase):
    """AC-1, AC-3 on the ssh runner: `my repo` and `my_repo` reduce to one remote name."""

    def run_in(self, repo_root, key):
        logs = os.path.join(self.tmp, "logs-" + key)
        head = git(["rev-parse", "HEAD"], repo_root).strip()
        report = SshRunner(plat()).run(
            repo_root,
            head,
            [("test", "true")],
            {},
            logs,
            "validate-r1",
            scope="feature__mul",
            key=key,
        )
        self.assertTrue(report.ok, [(s.name, s.returncode) for s in report.steps])
        return [c for c in self.calls() if c[1][-1].startswith("mkdir -p ")][-1][1][-1]

    @unittest.skipUnless(HAVE_BASH, "needs bash")
    def test_names_that_reduce_alike_get_different_directories(self):  # AC-3
        a = clone_as(self.repo, os.path.join(self.tmp, "one"), "my repo")
        b = clone_as(self.repo, os.path.join(self.tmp, "two"), "my_repo")
        ka, kb = sandbox_key(a, BRANCH), sandbox_key(b, BRANCH)
        self.assertNotEqual(ka, kb)
        # the other checkout is mid-session on the host
        busy = os.path.join(
            self.remote, ".revali", "sandbox", "my_repo", "feature__mul-" + kb, "validate-r1", "x"
        )
        os.makedirs(os.path.dirname(busy))
        with open(busy, "w", encoding="utf-8", newline="") as fh:
            fh.write("x")
        mkdir = self.run_in(a, ka)
        self.assertIn("sandbox/my_repo/feature__mul-%s/validate-r1-in" % ka, mkdir)
        self.assertTrue(os.path.isfile(busy))
        self.assertFalse(
            os.path.exists(
                os.path.join(self.remote, ".revali", "sandbox", "my_repo", "feature__mul-" + ka)
            )
        )

    @unittest.skipUnless(HAVE_BASH, "needs bash")
    def test_all_levels_go_when_empty(self):  # AC-4
        key = sandbox_key(self.repo, BRANCH)
        self.run_in(self.repo, key)
        self.assertEqual(os.listdir(os.path.join(self.remote, ".revali", "sandbox")), [])


class SshDropped(SshCase):
    """AC-7: an ssh call that ends with exit 255 after the script started."""

    def drop_after(self, rows):
        os.environ["REVALI_FAKE_SSH_SCRIPT_DROP"] = str(rows)
        self.addCleanup(os.environ.pop, "REVALI_FAKE_SSH_SCRIPT_DROP", None)

    def refused(self):
        logs = os.path.join(self.tmp, "logs-drop")
        head = git(["rev-parse", "HEAD"], self.repo).strip()
        with self.assertRaises(RunnerError) as ctx:
            SshRunner(plat()).run(
                self.repo,
                head,
                [("setup", "true"), ("test", "true")],
                {},
                logs,
                "validate-r1",
                scope="feature__mul",
                key="0123abcd",
            )
        return str(ctx.exception)

    @unittest.skipUnless(HAVE_BASH, "needs bash")
    def test_rows_lost_with_the_connection(self):
        self.drop_after(1)
        text = self.refused()
        self.assertIn("ended before `test` finished", text)
        self.assertIn("may still be running on box", text)
        self.assertIn("exit 255", text)

    @unittest.skipUnless(HAVE_BASH, "needs bash")
    def test_no_row_with_the_connection(self):
        self.drop_after(0)
        text = self.refused()
        self.assertIn("ended before `setup` finished", text)
        self.assertIn("may still be running on box", text)

    @unittest.skipUnless(HAVE_BASH, "needs bash")
    def test_a_complete_session_with_exit_255_is_still_read(self):
        # every row arrived: the result stands, whatever ssh said afterwards
        self.drop_after(99)
        logs = os.path.join(self.tmp, "logs-full")
        head = git(["rev-parse", "HEAD"], self.repo).strip()
        report = SshRunner(plat()).run(
            self.repo, head, [("setup", "true"), ("test", "true")], {}, logs, "validate-r1"
        )
        self.assertTrue(report.ok)

    @unittest.skipUnless(HAVE_BASH, "needs bash")
    def test_another_exit_code_adds_nothing(self):
        os.environ["REVALI_FAKE_SSH_BASH_FAILS"] = "1"
        self.addCleanup(os.environ.pop, "REVALI_FAKE_SSH_BASH_FAILS", None)
        self.assertNotIn("may still be running", self.refused())


class PipelineKey(RepoCase):
    """AC-5: every runner call of a run carries the key of this checkout and branch."""

    def test_baseline_smoke_and_validation(self):
        self.claude(claude_entry())
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, 0, out)
        calls = self.fake_calls("runner")
        self.assertEqual(
            {"baseline", "smoke-r1-1", "validate-r1"} - {c["label"] for c in calls}, set()
        )
        want = sandbox_key(self.repo, BRANCH)
        for c in calls:
            self.assertEqual((c["scope"], c.get("key")), ("feature__mul", want), c)


class PipelineKeyBaseRerun(BaseRerunCase):
    def test_base_rerun(self):  # AC-5
        self.run_failing()
        (call,) = self.runner_calls("base-r1")
        self.assertEqual(call.get("key"), sandbox_key(self.repo, BRANCH))


class Docs(unittest.TestCase):
    def test_sandbox_md(self):  # AC-8
        path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "docs", "sandbox.md")
        with open(path, encoding="utf-8") as fh:
            text = re.sub(r"\s+", " ", fh.read())
        self.assertIn("<repo>/<branch>-<key>/<label>", text)
        self.assertIn("old layout", text)
        self.assertIn("by hand", text)
        self.assertIn("common git directory", text)  # AC-9
        self.assertIn("linked worktree added on Windows", text)


if __name__ == "__main__":
    unittest.main()
