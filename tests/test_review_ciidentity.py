"""feature/ci AC-5: the suite passes on a runner with no global git identity.

A GitHub-hosted runner has no `user.name` / `user.email`. The workflow must not configure
one, and every repository the suite commits into must carry its own: the fixture repo sets a
repo-local identity, and a test that builds a repository by other means sets its own.
"""

import glob
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from tests.fixtures import make_sample_repo
from tests.helpers import ROOT

WORKFLOW = os.path.join(ROOT, ".github", "workflows", "test.yml")
IDENTITY_ENV = (
    "GIT_AUTHOR_NAME",
    "GIT_AUTHOR_EMAIL",
    "GIT_COMMITTER_NAME",
    "GIT_COMMITTER_EMAIL",
    "EMAIL",
)


def git(args, cwd):
    return subprocess.run(
        ["git"] + args, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace"
    )


class WorkflowConfiguresNoIdentity(unittest.TestCase):
    def test_no_git_config_step(self):
        # AC-5: the workflow leaves the runner's git as it finds it
        with open(WORKFLOW, "r", encoding="utf-8", newline="") as fh:
            text = fh.read()
        code = "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))
        for marker in ("user.name", "user.email", "git config"):
            with self.subTest(marker=marker):
                self.assertNotIn(marker, code)


class NoGlobalIdentity(unittest.TestCase):
    """git sees an empty global config, no system config and no identity variables."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="revali identity ")
        self.addCleanup(self._cleanup)
        empty = os.path.join(self.tmp, "gitconfig")
        with open(empty, "w", encoding="utf-8", newline="") as fh:
            fh.write("")
        env = {"GIT_CONFIG_GLOBAL": empty, "GIT_CONFIG_NOSYSTEM": "1"}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in IDENTITY_ENV:
            os.environ.pop(name, None)
        # Only meaningful where git really is left without an identity: a bare `git init`
        # repository must then refuse the commit, as it does on a fresh runner.
        plain = os.path.join(self.tmp, "plain")
        os.makedirs(plain)
        git(["init", "-q"], plain)
        probe = git(["commit", "-q", "--allow-empty", "-m", "probe"], plain)
        if probe.returncode == 0:
            self.skipTest("git still finds an identity here; the runner's state cannot be set up")

    def _cleanup(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_fixture_repo_commits_with_its_own_identity(self):
        # AC-5: the repository every RepoCase test builds carries a repo-local identity
        info = make_sample_repo.create(
            os.path.join(self.tmp, "sample"), with_remote=True, with_branch=True
        )
        repo = info["repo"]
        res = git(["commit", "-q", "--allow-empty", "-m", "on the runner"], repo)
        self.assertEqual(res.returncode, 0, res.stderr)
        who = git(["log", "-1", "--format=%an|%ae"], repo).stdout.strip()
        name, email = who.split("|", 1)
        self.assertTrue(name and email, who)
        self.assertEqual(git(["rev-list", "--count", "HEAD"], repo).stdout.strip(), "3")

    def test_worktree_of_the_fixture_shares_the_identity(self):
        # AC-5: tests that work in a linked worktree commit there without configuring anything
        info = make_sample_repo.create(
            os.path.join(self.tmp, "sample"), with_remote=False, with_branch=True
        )
        wt = os.path.join(self.tmp, "wt")
        added = git(["worktree", "add", "--quiet", wt, "main"], info["repo"])
        self.assertEqual(added.returncode, 0, added.stderr)
        res = git(["commit", "-q", "--allow-empty", "-m", "in the worktree"], wt)
        self.assertEqual(res.returncode, 0, res.stderr)


class TestsOutsideTheFixture(unittest.TestCase):
    def test_a_test_that_inits_its_own_repository_sets_an_identity(self):
        # AC-5: a non-bare `git init` outside make_sample_repo comes with user.email. This
        # module's probe repository never commits, so the module itself is left out.
        init = re.compile(r"""["']init["']""")

        def canon(path):
            return os.path.normcase(os.path.abspath(path))

        skip = {canon(__file__), canon(make_sample_repo.__file__)}
        offenders = []
        for path in sorted(glob.glob(os.path.join(ROOT, "tests", "**", "*.py"), recursive=True)):
            if canon(path) in skip:
                continue
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                source = fh.read()
            lines = [ln for ln in source.splitlines() if init.search(ln) and "git" in ln.lower()]
            lines = [ln for ln in lines if "--bare" not in ln]
            if lines and "user.email" not in source:
                offenders.append(os.path.relpath(path, ROOT))
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
