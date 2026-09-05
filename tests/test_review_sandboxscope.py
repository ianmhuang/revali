"""Review tests for feature/sandbox-per-branch: the sandbox layout <repo>/<branch>/<label>
on the WSL and ssh runners and its cleanup (AC-1), the round record's head_sha (AC-2), and
the README wording (AC-7). Black-box: the runners are driven through their public `run`
and the pipeline through the CLI; the scripts execute under the host's bash via the stubs.
"""
import os
import shutil
import sys
import tempfile
import unittest

from tests.helpers import FAKE_BIN, RepoCase, _quote, claude_entry, git, run_cli
from tests.test_ssh_runner import HAVE_BASH, SshCase, plat
from revali import EXIT_OK
from revali import gitops
from revali.config import PlatformCfg
from revali.runners import SshRunner, WslRunner
from revali.state import State

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
WSL_STUB = os.path.join(FAKE_BIN, "wsl_stub.py")


def bash_form(path):
    """A host path bash accepts on both platforms (Git Bash takes D:/x/y)."""
    return path.replace("\\", "/")


class WslScopeCleanupTests(RepoCase):
    """AC-1 on the WSL runner: the script really clones under <repo>/<branch>/<label>, and
    its cleanup removes <label>, then <branch> and <repo> when empty, but never a sibling
    branch's directory."""
    runner = "wsl"

    def setUp(self):
        super().setUp()
        self.use_real_local_runner()
        os.environ["REVALI_WSL_CMD"] = "%s %s" % (_quote(sys.executable), _quote(WSL_STUB))
        # a sandbox root of our own, without whitespace, so the on-disk layout can be inspected
        self.sand = tempfile.mkdtemp(prefix="revali-sand-")
        self.addCleanup(shutil.rmtree, self.sand, True)

    def make_runner(self):
        return WslRunner(PlatformCfg(runner="wsl", distro="Ubuntu", command_timeout_min=1,
                                     sandbox_dir=bash_form(self.sand)))

    def sand_path(self, *parts):
        return os.path.join(self.sand, *parts)

    @unittest.skipUnless(HAVE_BASH, "needs bash")
    def test_clone_lives_under_the_branch_and_all_three_levels_go_when_empty(self):
        head = git(["rev-parse", "HEAD"], self.repo).strip()
        logs = os.path.join(self.rdir(), "logs")
        marker = os.path.join(logs, "seen.txt")
        # the step records where it ran: proof the clone sat under <repo>/<branch>/<label>
        step = 'pwd > "%s"' % bash_form(marker)
        report = self.make_runner().run(self.repo, head, [("test", step)], {}, logs, "validate-r1",
                                        scope="feature__mul")
        self.assertTrue(report.ok, [(s.name, s.returncode, s.stdout[-300:]) for s in report.steps])
        with open(marker, encoding="utf-8") as fh:
            where = fh.read().strip()
        self.assertTrue(where.replace("\\", "/").endswith("/sample/feature__mul/validate-r1/repo"), where)
        # cleanup: <label>, then <branch>, then <repo>; the sandbox root itself stays
        self.assertFalse(os.path.exists(self.sand_path("sample", "feature__mul", "validate-r1")))
        self.assertFalse(os.path.exists(self.sand_path("sample", "feature__mul")))
        self.assertFalse(os.path.exists(self.sand_path("sample")))
        self.assertTrue(os.path.isdir(self.sand))

    @unittest.skipUnless(HAVE_BASH, "needs bash")
    def test_cleanup_leaves_a_sibling_branch_alone(self):
        # another worktree of the same repository is mid-validation on feature/other
        other = self.sand_path("sample", "feature__other", "validate-r1", "repo")
        os.makedirs(other)
        with open(os.path.join(other, "busy"), "w", encoding="utf-8") as fh:
            fh.write("x")
        head = git(["rev-parse", "HEAD"], self.repo).strip()
        logs = os.path.join(self.rdir(), "logs")
        report = self.make_runner().run(self.repo, head, [("test", "true")], {}, logs, "validate-r1",
                                        scope="feature__mul")
        self.assertTrue(report.ok)
        self.assertFalse(os.path.exists(self.sand_path("sample", "feature__mul")))
        self.assertTrue(os.path.isfile(os.path.join(other, "busy")))     # untouched
        self.assertTrue(os.path.isdir(self.sand_path("sample")))         # not empty, so kept

    @unittest.skipUnless(HAVE_BASH, "needs bash")
    def test_failed_step_still_cleans_the_branch_and_repo_dirs(self):
        head = git(["rev-parse", "HEAD"], self.repo).strip()
        logs = os.path.join(self.rdir(), "logs")
        report = self.make_runner().run(self.repo, head,
                                        [("setup", "true"), ("test", "exit 3"), ("new_test", "true")],
                                        {}, logs, "validate-r2", scope="feature__mul")
        self.assertEqual(report.failed.name, "test")
        self.assertEqual(report.failed.returncode, 3)
        self.assertFalse(os.path.exists(self.sand_path("sample", "feature__mul")))
        self.assertFalse(os.path.exists(self.sand_path("sample")))

    @unittest.skipUnless(HAVE_BASH, "needs bash")
    def test_without_scope_the_old_layout_is_kept(self):
        head = git(["rev-parse", "HEAD"], self.repo).strip()
        logs = os.path.join(self.rdir(), "logs")
        marker = os.path.join(logs, "seen.txt")
        report = self.make_runner().run(self.repo, head, [("test", 'pwd > "%s"' % bash_form(marker))], {}, logs,
                                        "validate-r1")
        self.assertTrue(report.ok)
        with open(marker, encoding="utf-8") as fh:
            where = fh.read().strip()
        self.assertTrue(where.replace("\\", "/").endswith("/sample/validate-r1/repo"), where)
        self.assertFalse(os.path.exists(self.sand_path("sample")))


class SshScopeTests(SshCase):
    """AC-1 on the ssh runner: inbox, logs and clone under <repo>/<branch>, the host-side
    cleanup removing <branch> then <repo>, a sibling branch left alone."""

    def remote_dir(self, *parts):
        return os.path.join(self.remote, ".revali", "sandbox", *parts)

    @unittest.skipUnless(HAVE_BASH, "needs bash")
    def test_staging_and_clone_sit_under_the_branch(self):
        r = SshRunner(plat())
        logs = os.path.join(self.rdir(), "logs")
        head = git(["rev-parse", "HEAD"], self.repo).strip()
        report = r.run(self.repo, head, [("setup", "true"), ("test", "true")], {}, logs, "validate-r1",
                       scope="feature__mul")
        self.assertTrue(report.ok, [(s.name, s.returncode) for s in report.steps])
        calls = self.calls()
        self.assertEqual([c[0] for c in calls], ["ssh", "scp", "ssh", "scp", "ssh"])
        mkdir = calls[0][1][-1]
        self.assertIn("sandbox/sample/feature__mul/validate-r1-in", mkdir)
        self.assertIn("sandbox/sample/feature__mul/validate-r1-logs", mkdir)
        self.assertEqual(calls[1][1][-1], "box:.revali/sandbox/sample/feature__mul/validate-r1-in/")
        self.assertEqual(calls[3][1][-2], "box:.revali/sandbox/sample/feature__mul/validate-r1-logs/.")
        cleanup = calls[4][1][-1]
        rm, _, rmdir = cleanup.partition("&&")
        self.assertTrue(rm.startswith("rm -rf "), cleanup)
        for name in ("validate-r1-in", "validate-r1-logs", "validate-r1"):
            self.assertIn("sandbox/sample/feature__mul/%s" % name, rm)
        # the parents go innermost first, and only when empty
        self.assertIn("rmdir --ignore-fail-on-non-empty", rmdir)
        self.assertLess(rmdir.index("sandbox/sample/feature__mul"), rmdir.rindex("sandbox/sample"))
        self.assertTrue(rmdir.rstrip().endswith("/.revali/sandbox/sample"), rmdir)
        self.assertEqual(self.remote_leftovers(), [])
        self.assertFalse(os.path.exists(self.remote_dir("sample", "feature__mul")))

    @unittest.skipUnless(HAVE_BASH, "needs bash")
    def test_two_branches_do_not_share_a_directory(self):
        # feature/other is mid-validation on the host with the same label
        other = self.remote_dir("sample", "feature__other", "validate-r1", "repo")
        os.makedirs(other)
        with open(os.path.join(other, "busy"), "w", encoding="utf-8") as fh:
            fh.write("x")
        r = SshRunner(plat())
        logs = os.path.join(self.rdir(), "logs")
        head = git(["rev-parse", "HEAD"], self.repo).strip()
        report = r.run(self.repo, head, [("test", "true")], {}, logs, "validate-r1", scope="feature__mul")
        self.assertTrue(report.ok)
        self.assertTrue(os.path.isfile(os.path.join(other, "busy")))
        self.assertFalse(os.path.exists(self.remote_dir("sample", "feature__mul")))
        self.assertTrue(os.path.isdir(self.remote_dir("sample")))

    @unittest.skipUnless(HAVE_BASH, "needs bash")
    def test_pipeline_over_ssh_uses_the_branch_directory(self):
        self.claude(claude_entry())
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("READY TO MERGE", out)
        uploads = [c[1][-1] for c in self.calls() if c[0] == "scp" and c[1][-1].startswith("box:")]
        self.assertTrue(uploads)
        for target in uploads:
            self.assertTrue(target.startswith("box:.revali/sandbox/sample/feature__mul/"), target)
        self.assertEqual(self.remote_leftovers(), [])


class PipelineScopeAndRecordTests(RepoCase):
    """AC-1: every runner call of a run carries the branch; AC-2: the round record."""

    def test_baseline_smoke_and_validation_all_carry_the_branch(self):
        self.claude(claude_entry())
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        calls = self.fake_calls("runner")
        by_label = {c["label"]: c for c in calls}
        for label in ("baseline", "smoke-r1-1", "validate-r1"):
            self.assertIn(label, by_label)
            self.assertEqual(by_label[label].get("scope"), "feature__mul", by_label[label])

    def test_round_record_keeps_the_reviewed_head(self):
        self.claude(claude_entry())
        reviewed = gitops.rev_parse("HEAD", self.repo)
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        state = State.load(self.rdir())
        self.assertEqual(len(state.rounds), 1)
        record = state.rounds[0]
        test_commit = gitops.rev_parse("HEAD", self.repo)
        self.assertNotEqual(reviewed, test_commit)                 # the round did commit tests
        self.assertEqual(record["head_sha"], reviewed)             # what the reviewer saw
        self.assertEqual(record["test_commit"], test_commit)       # what the round made
        self.assertEqual(state.test_commits, [test_commit])
        self.assertEqual(state.head_sha, test_commit)              # the run's own latest commit
        self.assertEqual(state.stage, "ready_to_merge")

    def test_round_without_a_test_commit_records_head_as_both(self):
        self.claude(claude_entry(write_tests=False))
        reviewed = gitops.rev_parse("HEAD", self.repo)
        run_cli(["run", "--foreground"])
        state = State.load(self.rdir())
        self.assertTrue(state.rounds)
        self.assertEqual(state.rounds[0]["head_sha"], reviewed)
        self.assertEqual(state.rounds[0]["test_commit"], "")
        self.assertEqual(gitops.rev_parse("HEAD", self.repo), reviewed)


class ReadmeLayoutTests(unittest.TestCase):
    """AC-7"""

    def setUp(self):
        with open(os.path.join(ROOT, "docs", "sandbox.md"), encoding="utf-8") as fh:
            self.text = fh.read()
        with open(os.path.join(ROOT, "docs", "files.md"), encoding="utf-8") as fh:
            self.text += fh.read()
        with open(os.path.join(ROOT, "docs", "side-effects.md"), encoding="utf-8") as fh:
            self.effects = fh.read()

    def section(self, text, heading):
        start = text.index(heading)
        end = text.find("\n## ", start + 1)
        return text[start:end if end >= 0 else len(text)]

    def test_files_table_and_sandbox_section_show_the_branch_level(self):
        self.assertIn("`~/.revali/sandbox/<repo>/<branch>/<label>/`", self.text)
        self.assertNotIn("sandbox/<repo>/<label>/", self.text)
        section = self.section(self.text, "# Sandbox")
        self.assertIn("<repo>/<branch>/<label>", section)
        self.assertIn("__", section)   # how <branch> is written

    def test_what_revali_does_says_merge_holds_the_tree_lock(self):
        section = self.section(self.effects, "# What revali does to your repository")
        lock = [p for p in section.split("\n- ") if "tree.lock" in p]
        self.assertEqual(len(lock), 1, section)
        self.assertIn("`merge`", lock[0])
        self.assertIn("lock", lock[0])


if __name__ == "__main__":
    unittest.main()
