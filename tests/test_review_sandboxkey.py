"""Review tests for fix/sandbox-key-collision: which directory a sandbox session uses.

AC-1: two checkouts whose directories share a name, on one branch, get different directories
      (wsl and ssh), so the `rm -rf` one session starts with leaves the other's clone alone.
AC-2: branches `feature/x` and `feature__x` get different directories.
AC-3: on the ssh host, checkouts `my repo` and `my_repo` get different directories.
AC-4: the layout is <sandbox_dir>/<repo>/<branch>-<key>/<label> with an 8 hex key; one checkout
      and branch keep their directory from run to run; every level is removed when empty.
AC-5: the baseline, the smoke run, validation and the base rerun all get the key.
AC-6: a `Runner.run` call without a key keeps <repo>/<branch>/<label>.

The runners are driven through their public `run` over the repository's stand-ins for wsl.exe,
ssh and scp. Where a session ran is read from what it leaves outside: the step's own `pwd` on
the wsl runner, the scp upload target on the ssh runner.
"""

import os
import re
import shutil
import sys
import tempfile
import unittest

from revali import EXIT_OK
from revali.config import PlatformCfg
from revali.runners import SshRunner, WslRunner, sandbox_key
from revali.state import safe_branch
from tests.helpers import FAKE_BIN, RepoCase, _quote, claude_entry, git, run_cli
from tests.test_review_basererun import BaseRerunCase
from tests.test_ssh_runner import HAVE_BASH, SshCase, plat

WSL_STUB = os.path.join(FAKE_BIN, "wsl_stub.py")
BRANCH = "feature/mul"
LABEL = "validate-r1"
HEX8 = r"[0-9a-f]{8}"


def bash_form(path):
    """A host path bash accepts on both platforms (Git Bash takes D:/x/y)."""
    return path.replace("\\", "/")


def second_checkout(src, parent, name):
    """Another clone of the fixture repository at <parent>/<name>."""
    os.makedirs(parent, exist_ok=True)
    dest = os.path.join(parent, name)
    git(["clone", "-q", src, dest], parent)
    return dest


def run_kwargs(repo_root, branch, keyed):
    """What the pipeline hands a runner for this checkout and branch."""
    kw = {"scope": safe_branch(branch)}
    if keyed:
        kw["key"] = sandbox_key(repo_root, branch)
    return kw


class WslDirectories(RepoCase):
    runner = "wsl"

    def setUp(self):
        super().setUp()
        self.use_real_local_runner()
        os.environ["REVALI_WSL_CMD"] = "%s %s" % (_quote(sys.executable), _quote(WSL_STUB))
        # a sandbox root of our own, without whitespace, so what is left on disk can be listed
        self.sand = tempfile.mkdtemp(prefix="revali-sand-")
        self.addCleanup(shutil.rmtree, self.sand, True)
        self.count = 0

    def session(self, repo_root, branch, keyed=True):
        """Run one session; the directory its step ran in, relative to the sandbox root."""
        self.count += 1
        logs = os.path.join(self.tmp, "wsl-logs-%d" % self.count)
        marker = os.path.join(logs, "where.txt")
        head = git(["rev-parse", "HEAD"], repo_root).strip()
        runner = WslRunner(
            PlatformCfg(
                runner="wsl",
                distro="Ubuntu",
                command_timeout_min=1,
                sandbox_dir=bash_form(self.sand),
            )
        )
        report = runner.run(
            repo_root,
            head,
            [("test", 'pwd > "%s"' % bash_form(marker))],
            {},
            logs,
            LABEL,
            **run_kwargs(repo_root, branch, keyed),
        )
        self.assertTrue(report.ok, [(s.name, s.returncode, s.stdout[-300:]) for s in report.steps])
        with open(marker, encoding="utf-8") as fh:
            where = fh.read().strip().replace("\\", "/")
        root = "/%s/" % os.path.basename(self.sand)
        self.assertIn(root, where)
        return where.split(root, 1)[1]

    def occupy(self, where):
        """Put a file where a session of another checkout would be working right now."""
        busy = os.path.join(self.sand, *where.split("/"), "busy")
        os.makedirs(os.path.dirname(busy))
        with open(busy, "w", encoding="utf-8", newline="") as fh:
            fh.write("x")
        return busy

    @unittest.skipUnless(HAVE_BASH, "needs bash")
    def test_checkouts_named_alike_on_one_branch_get_different_directories(self):  # AC-1, AC-4
        one = second_checkout(self.repo, os.path.join(self.tmp, "one"), "app")
        two = second_checkout(self.repo, os.path.join(self.tmp, "two"), "app")
        first, second = self.session(one, BRANCH), self.session(two, BRANCH)
        self.assertNotEqual(first, second)
        for where in (first, second):
            self.assertRegex(where, r"^app/feature__mul-%s/validate-r1(/|$)" % HEX8)

    @unittest.skipUnless(HAVE_BASH, "needs bash")
    def test_a_session_leaves_the_other_checkouts_clone_alone(self):  # AC-1
        one = second_checkout(self.repo, os.path.join(self.tmp, "one"), "app")
        two = second_checkout(self.repo, os.path.join(self.tmp, "two"), "app")
        busy = self.occupy(self.session(two, BRANCH))  # the other checkout is mid-session
        mine = self.session(one, BRANCH)
        self.assertTrue(os.path.isfile(busy), "the other checkout's clone was removed")
        # ours is gone up to its <branch>-<key> level; <repo> stays while the other is there
        self.assertFalse(os.path.exists(os.path.join(self.sand, *mine.split("/")[:2])))
        self.assertTrue(os.path.isdir(os.path.join(self.sand, "app")))

    @unittest.skipUnless(HAVE_BASH, "needs bash")
    def test_branches_written_alike_get_different_directories(self):  # AC-2
        slash = self.session(self.repo, "feature/x")
        underscores = self.session(self.repo, "feature__x")
        self.assertNotEqual(slash, underscores)
        for where in (slash, underscores):
            self.assertRegex(where, r"^sample/feature__x-%s/validate-r1(/|$)" % HEX8)

    @unittest.skipUnless(HAVE_BASH, "needs bash")
    def test_one_checkout_and_branch_keep_their_directory(self):  # AC-4
        first = self.session(self.repo, BRANCH)
        again = self.session(self.repo, BRANCH)
        # the same checkout by another spelling of its path: the key follows the real path
        spelled = os.path.join(self.repo, os.pardir, os.path.basename(self.repo))
        other_spelling = self.session(spelled, BRANCH)
        self.assertEqual(first, again)
        self.assertEqual(first, other_spelling)
        self.assertRegex(first, r"^sample/feature__mul-%s/validate-r1(/|$)" % HEX8)

    @unittest.skipUnless(HAVE_BASH, "needs bash")
    def test_every_level_is_removed_when_empty(self):  # AC-4
        self.session(self.repo, BRANCH)
        self.assertEqual(os.listdir(self.sand), [])

    @unittest.skipUnless(HAVE_BASH, "needs bash")
    def test_without_a_key_the_layout_is_unchanged(self):  # AC-6
        where = self.session(self.repo, BRANCH, keyed=False)
        self.assertRegex(where, r"^sample/feature__mul/validate-r1(/|$)")
        self.assertEqual(os.listdir(self.sand), [])


class SshDirectories(SshCase):
    def session(self, repo_root, branch, keyed=True):
        """Run one session; the scp target its inputs went to (`box:<dir>/<label>-in/`)."""
        seen = len(self.calls())
        logs = os.path.join(self.tmp, "ssh-logs-%d" % seen)
        head = git(["rev-parse", "HEAD"], repo_root).strip()
        report = SshRunner(plat()).run(
            repo_root,
            head,
            [("test", "true")],
            {},
            logs,
            LABEL,
            **run_kwargs(repo_root, branch, keyed),
        )
        self.assertTrue(report.ok, [(s.name, s.returncode, s.stdout[-300:]) for s in report.steps])
        uploads = [
            c[1][-1] for c in self.calls()[seen:] if c[0] == "scp" and c[1][-1].startswith("box:")
        ]
        self.assertEqual(len(uploads), 1, uploads)
        return uploads[0]

    def branch_level(self, upload):
        """The <repo>/<branch>[-<key>] directory of an upload target, on the fake host."""
        # fix/ssh-session-dirs AC-1: the target is `<label>-<session id>-in/` since that change
        match = re.fullmatch(r"box:(.+)/%s-%s-in/" % (LABEL, HEX8), upload)
        self.assertIsNotNone(match, upload)
        return os.path.join(self.remote, *match.group(1).split("/"))

    def sandbox_root(self):
        return os.path.join(self.remote, ".revali", "sandbox")

    @unittest.skipUnless(HAVE_BASH, "needs bash")
    def test_names_that_reduce_alike_get_different_directories(self):  # AC-3
        spaced = second_checkout(self.repo, os.path.join(self.tmp, "one"), "my repo")
        plain = second_checkout(self.repo, os.path.join(self.tmp, "two"), "my_repo")
        first, second = self.session(spaced, BRANCH), self.session(plain, BRANCH)
        self.assertNotEqual(first, second)
        for upload in (first, second):
            self.assertRegex(
                upload,
                r"^box:\.revali/sandbox/my_repo/feature__mul-%s/validate-r1-%s-in/$" % (HEX8, HEX8),
            )

    @unittest.skipUnless(HAVE_BASH, "needs bash")
    def test_a_session_leaves_the_other_checkouts_clone_alone(self):  # AC-3
        spaced = second_checkout(self.repo, os.path.join(self.tmp, "one"), "my repo")
        plain = second_checkout(self.repo, os.path.join(self.tmp, "two"), "my_repo")
        # `my_repo` is mid-session on the host under the same label
        busy = os.path.join(self.branch_level(self.session(plain, BRANCH)), LABEL, "repo", "busy")
        os.makedirs(os.path.dirname(busy))
        with open(busy, "w", encoding="utf-8", newline="") as fh:
            fh.write("x")
        mine = self.branch_level(self.session(spaced, BRANCH))
        self.assertTrue(os.path.isfile(busy), "the other checkout's clone was removed")
        self.assertFalse(os.path.exists(mine))
        self.assertTrue(os.path.isdir(os.path.join(self.sandbox_root(), "my_repo")))

    @unittest.skipUnless(HAVE_BASH, "needs bash")
    def test_checkouts_named_alike_on_one_branch_get_different_directories(self):  # AC-1
        one = second_checkout(self.repo, os.path.join(self.tmp, "one"), "app")
        two = second_checkout(self.repo, os.path.join(self.tmp, "two"), "app")
        first, second = self.session(one, BRANCH), self.session(two, BRANCH)
        self.assertNotEqual(first, second)
        for upload in (first, second):
            self.assertRegex(
                upload,
                r"^box:\.revali/sandbox/app/feature__mul-%s/validate-r1-%s-in/$" % (HEX8, HEX8),
            )

    @unittest.skipUnless(HAVE_BASH, "needs bash")
    def test_branches_written_alike_get_different_directories(self):  # AC-2
        slash = self.session(self.repo, "feature/x")
        underscores = self.session(self.repo, "feature__x")
        self.assertNotEqual(slash, underscores)
        for upload in (slash, underscores):
            self.assertRegex(
                upload,
                r"^box:\.revali/sandbox/sample/feature__x-%s/validate-r1-%s-in/$" % (HEX8, HEX8),
            )

    @unittest.skipUnless(HAVE_BASH, "needs bash")
    def test_one_checkout_and_branch_keep_their_directory_and_leave_nothing(self):  # AC-4
        first = self.session(self.repo, BRANCH)
        self.assertEqual(os.listdir(self.sandbox_root()), [])  # <label>, <branch>-<key>, <repo>
        # fix/ssh-session-dirs AC-1: the <branch>-<key> level is kept; the session's own
        # directories below it are new every time
        again = self.session(self.repo, BRANCH)
        self.assertEqual(self.branch_level(first), self.branch_level(again))
        self.assertNotEqual(first, again)
        self.assertEqual(os.listdir(self.sandbox_root()), [])

    @unittest.skipUnless(HAVE_BASH, "needs bash")
    def test_without_a_key_the_layout_is_unchanged(self):  # AC-6
        upload = self.session(self.repo, BRANCH, keyed=False)
        self.assertRegex(
            upload, r"^box:\.revali/sandbox/sample/feature__mul/validate-r1-%s-in/$" % HEX8
        )
        self.assertEqual(os.listdir(self.sandbox_root()), [])


class PipelineSessions(RepoCase):
    """AC-5: what `revali run` hands the runner, recorded by the fake runner."""

    def test_baseline_smoke_run_and_validation_carry_the_key(self):
        self.claude(claude_entry())
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        by_label = {c["label"]: c for c in self.fake_calls("runner")}
        want = sandbox_key(self.repo, BRANCH)
        self.assertRegex(want, "^%s$" % HEX8)
        for label in ("baseline", "smoke-r1-1", LABEL):
            self.assertIn(label, by_label)
            self.assertEqual(by_label[label].get("key"), want, by_label[label])
            self.assertEqual(by_label[label].get("scope"), "feature__mul", by_label[label])


class PipelineBaseRerun(BaseRerunCase):
    """AC-5: the base rerun is a session of this checkout and branch too."""

    def test_the_base_rerun_carries_the_key(self):
        self.run_failing()
        calls = self.runner_calls("base-r1")
        self.assertEqual(len(calls), 1, self.fake_calls("runner"))
        self.assertEqual(calls[0].get("key"), sandbox_key(self.repo, BRANCH), calls[0])
        self.assertEqual(calls[0].get("scope"), "feature__mul", calls[0])


if __name__ == "__main__":
    unittest.main()
