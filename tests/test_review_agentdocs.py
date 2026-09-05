"""Review of feature/worktree-docs: the documentation (AC-1, AC-2, AC-10), the ssh stub's
rmdir and the on-host cleanup it makes visible (AC-6), and the sandbox script's cleanup on
a failed clone or checkout (AC-7)."""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

from tests.helpers import FAKE_BIN, RepoCase, _quote, git
from tests.test_ssh_runner import HAVE_BASH, SshCase, plat
from revali import VERSION
from revali.config import PlatformCfg
from revali.runners import RunnerError, SshRunner, WslRunner

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SSH_STUB = os.path.join(FAKE_BIN, "ssh_stub.py")
WSL_STUB = os.path.join(FAKE_BIN, "wsl_stub.py")


def read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


def section(text, heading):
    """The body of one `## heading` up to the next `## `."""
    marker = "\n## %s\n" % heading
    assert marker in text, heading
    return text.split(marker, 1)[1].split("\n## ", 1)[0]


def unwrap(text):
    return " ".join(text.split())


class ReadmeAgents(unittest.TestCase):
    """AC-1 (README), AC-2, AC-10"""

    def test_several_agents_section_follows_workflow(self):
        text = read("docs/workflow.md")
        self.assertIn("\n## Several agents on one repository\n", text)
        self.assertLess(text.index("\n## Workflow\n"), text.index("\n## Several agents on one repository\n"))
        # the next top-level heading after Workflow is this one
        after_workflow = text.split("\n## Workflow\n", 1)[1]
        self.assertEqual(after_workflow.split("\n## ", 1)[1].split("\n", 1)[0], "Several agents on one repository")

    def test_several_agents_section_content(self):
        body = unwrap(section(read("docs/workflow.md"), "Several agents on one repository"))
        for phrase in ("git worktree add ../<name> -b <branch>",   # one worktree per agent
                       "tree.lock",                                 # a second run in one checkout is refused
                       "`stop`",
                       "<repo>/<branch>/<label>",                   # the sandbox layout
                       "git worktree remove",                       # what the user does afterwards
                       "git pull",
                       "primary tree",
                       "same branch",                               # two clones, one branch: unsupported
                       "not a supported layout"):
            self.assertIn(phrase, body, phrase)
        self.assertIn("detached HEAD", body)

    def test_merge_bullet_lists_the_worktree_side_effects(self):
        body = unwrap(read("docs/side-effects.md").split("# What revali does to your repository\n", 1)[1])
        bullet = body.split("on `revali merge`", 1)[1].split(" - ", 1)[0]
        for phrase in ("`gh pr merge --<method>` without `--delete-branch`",
                       "`git push origin --delete <branch>`",
                       "`git fetch --prune origin <base>`",
                       "`git checkout --detach FETCH_HEAD`",
                       "`git branch -D <branch>`",
                       "MERGED"):
            self.assertIn(phrase, bullet, phrase)
        self.assertIn("gh pr view", bullet)
        self.assertIn("follow-up still runs", bullet)

    def test_status_line(self):
        text = read("README.md")
        paragraph = unwrap(text.split("\nStatus:", 1)[1].split("\n\n", 1)[0])
        self.assertIn("package version %s" % VERSION, paragraph)
        self.assertIn("#21", paragraph)
        for word in ("private", "public", "WSL", "ssh", "Reviewer"):
            self.assertIn(word, paragraph, word)


class SkillWorktree(unittest.TestCase):
    """AC-1 (SKILL.md)"""

    def test_phase_1_says_to_use_a_worktree(self):
        text = read("skill/SKILL.md")
        step1 = text.split("\n1. ", 1)[1].split("\n2. ", 1)[0]
        self.assertIn("git worktree add ../<name> -b <branch>", step1)
        self.assertIn("another session", step1)

    def test_after_a_worktree_merge(self):
        text = read("skill/SKILL.md")
        acting = text.split("Acting on the result", 1)[1]
        self.assertIn("git worktree remove <path>", acting)
        self.assertIn("git pull", acting)
        self.assertIn("when the user asks", acting)


class SshStubRmdir(unittest.TestCase):
    """AC-6: the stub removes every non-flag argument, in order, like rmdir."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="revali-fake-remote-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.remote = os.path.join(self.tmp, "home")
        os.makedirs(self.remote)
        self.log = os.path.join(self.tmp, "calls.log")

    def stub(self, command):
        env = dict(os.environ)
        env["REVALI_FAKE_REMOTE"] = self.remote
        env["REVALI_FAKE_LOG"] = self.log
        env.pop("REVALI_FAKE_SSH_DOWN", None)
        return subprocess.run([sys.executable, SSH_STUB, "box", command], env=env, capture_output=True, text=True)

    def test_every_argument_is_removed_leaf_first(self):
        os.makedirs(os.path.join(self.remote, ".revali", "sandbox", "sample", "feature__mul"))
        res = self.stub("rmdir --ignore-fail-on-non-empty .revali/sandbox/sample/feature__mul .revali/sandbox/sample")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertFalse(os.path.isdir(os.path.join(self.remote, ".revali", "sandbox", "sample", "feature__mul")))
        self.assertFalse(os.path.isdir(os.path.join(self.remote, ".revali", "sandbox", "sample")))
        self.assertTrue(os.path.isdir(os.path.join(self.remote, ".revali", "sandbox")))   # not named: stays

    def test_order_matters_like_rmdir(self):
        # parent first: the parent is not empty yet, so it stays; the leaf still goes
        os.makedirs(os.path.join(self.remote, "sample", "feature__mul"))
        res = self.stub("rmdir --ignore-fail-on-non-empty sample sample/feature__mul")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertFalse(os.path.isdir(os.path.join(self.remote, "sample", "feature__mul")))
        self.assertTrue(os.path.isdir(os.path.join(self.remote, "sample")))


class SshHostCleanup(SshCase):
    """AC-6 end to end: after a scoped run over the ssh stubs, <repo>/<branch> and <repo> are gone."""

    @unittest.skipUnless(HAVE_BASH, "needs bash")
    def test_branch_and_repo_directories_are_gone(self):
        head = git(["rev-parse", "HEAD"], self.repo).strip()
        logs = os.path.join(self.rdir(), "logs")
        report = SshRunner(plat()).run(self.repo, head, [("test", "true")], {}, logs, "validate-r1",
                                       scope="feature__mul")
        self.assertTrue(report.ok, [(s.name, s.returncode) for s in report.steps])
        sandbox = os.path.join(self.remote, ".revali", "sandbox")
        self.assertFalse(os.path.exists(os.path.join(sandbox, "sample", "feature__mul", "validate-r1")))
        self.assertFalse(os.path.exists(os.path.join(sandbox, "sample", "feature__mul")))
        self.assertFalse(os.path.exists(os.path.join(sandbox, "sample")))
        self.assertEqual(self.remote_leftovers(), [])


class SandboxCloneFailure(RepoCase):
    """AC-7: a clone or checkout that fails leaves no <repo>/<branch>/<label> behind."""
    runner = "wsl"

    def setUp(self):
        super().setUp()
        self.use_real_local_runner()
        os.environ["REVALI_WSL_CMD"] = "%s %s" % (_quote(sys.executable), _quote(WSL_STUB))
        self.sand = tempfile.mkdtemp(prefix="revali-sand-")
        self.addCleanup(shutil.rmtree, self.sand, True)

    def make_runner(self):
        return WslRunner(PlatformCfg(runner="wsl", distro="Ubuntu", command_timeout_min=1,
                                     sandbox_dir=self.sand.replace("\\", "/")))

    def test_script_text_calls_cleanup_on_both_exits(self):
        text = self.make_runner().script("/mnt/d/x/sample", "/mnt/d/x/logs", "/mnt/d/x/extra", "abc123",
                                         [("test", "true")], "validate-r1",
                                         "$HOME/.revali/sandbox/sample/feature__mul/validate-r1", 60,
                                         scope="feature__mul")
        clone_exits = [line for line in text.splitlines() if 'printf "clone' in line]
        self.assertEqual(len(clone_exits), 2, text)
        for line in clone_exits:
            self.assertIn("cleanup;", line)
            self.assertLess(line.index("cleanup"), line.index("exit 0"), line)
        self.assertLess(text.index("cleanup() {"), text.index('printf "clone'))

    @unittest.skipUnless(HAVE_BASH, "needs bash")
    def test_failed_checkout_leaves_nothing_on_the_host(self):
        logs = os.path.join(self.rdir(), "logs")
        with self.assertRaises(RunnerError) as ctx:
            self.make_runner().run(self.repo, "0" * 40, [("test", "true")], {}, logs, "validate-r1",
                                   scope="feature__mul")
        self.assertIn("clone/checkout failed", str(ctx.exception))
        self.assertFalse(os.path.exists(os.path.join(self.sand, "sample", "feature__mul", "validate-r1")))
        self.assertFalse(os.path.exists(os.path.join(self.sand, "sample", "feature__mul")))
        self.assertFalse(os.path.exists(os.path.join(self.sand, "sample")))
        self.assertTrue(os.path.isdir(self.sand))

    @unittest.skipUnless(HAVE_BASH, "needs bash")
    def test_failed_clone_leaves_nothing_on_the_host(self):
        logs = os.path.join(self.rdir(), "logs")
        missing = os.path.join(self.tmp, "no-such-repo")
        head = git(["rev-parse", "HEAD"], self.repo).strip()
        with self.assertRaises(RunnerError):
            self.make_runner().run(missing, head, [("test", "true")], {}, logs, "validate-r1",
                                   scope="feature__mul")
        self.assertFalse(os.path.exists(os.path.join(self.sand, "no-such-repo")))
        self.assertTrue(os.path.isdir(self.sand))


if __name__ == "__main__":
    unittest.main()
