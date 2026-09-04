"""feature/sandbox-per-branch: sandbox clones live under <repo>/<branch>/<label> on the WSL
distro and the ssh host (AC-1); the round record keeps the reviewed HEAD (AC-2); `stop` kills
the tree lock's live pid whatever branch it names (AC-3); reserving a lock for a pid that
already wrote it is not a conflict (AC-4); the `wait --branch` hint repeats the branch (AC-5);
`merge` holds the tree lock (AC-6); README shows the layout (AC-7)."""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from tests.helpers import RepoCase, claude_entry, git, run_cli
from tests.test_ssh_runner import HAVE_BASH, SshCase, plat
from revali import EXIT_ERROR, EXIT_OK
from revali import gitops, merge
from revali.config import PlatformCfg
from revali.runners import RunnerError, SshRunner, WslRunner, sandbox_dir
from revali.state import (LockHeld, State, TreeLockHeld, acquire_lock, acquire_tree_lock, lock_path,
                          read_lock, read_tree_lock, tree_lock_path, write_json_atomic)

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def live_child(case):
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"],
                             start_new_session=True)   # own group: kill_tree uses killpg on POSIX
    case.addCleanup(lambda: child.poll() is None and child.kill())
    return child


class SandboxLayoutTests(unittest.TestCase):
    """AC-1: the directory scheme."""

    def test_scoped_and_unscoped_paths(self):
        self.assertEqual(sandbox_dir("$HOME/.revali/sandbox", "sample", "feature__mul", "validate-r1"),
                         "$HOME/.revali/sandbox/sample/feature__mul/validate-r1")
        self.assertEqual(sandbox_dir("$HOME/.revali/sandbox", "sample", "", "validate-r1"),
                         "$HOME/.revali/sandbox/sample/validate-r1")   # direct callers keep the old layout

    def test_two_branches_same_label_differ(self):
        a = sandbox_dir("$HOME/.revali/sandbox", "sample", "feature__a", "validate-r1")
        b = sandbox_dir("$HOME/.revali/sandbox", "sample", "feature__b", "validate-r1")
        self.assertNotEqual(a, b)
        self.assertEqual(os.path.dirname(os.path.dirname(a)), os.path.dirname(os.path.dirname(b)))

    def test_wsl_runner_clones_under_the_branch(self):
        seen = {}
        r = WslRunner(PlatformCfg(runner="wsl", distro="Ubuntu", sandbox_dir="~/.revali/sandbox"))

        def capture(*a, **kw):   # a[0] is the runner: the patch replaces the method on the class
            seen["sandbox"] = a[7]
            seen["scope"] = kw.get("scope", "")
            raise RunnerError("captured")

        logs = tempfile.mkdtemp(prefix="revali-scope-")
        self.addCleanup(shutil.rmtree, logs, True)
        with mock.patch.object(WslRunner, "wslpath", lambda self, p: p), \
             mock.patch.object(WslRunner, "script", capture):
            with self.assertRaises(RunnerError):
                r.run("D:/x/sample", "HEAD", [("test", "true")], {}, logs, "validate-r1", scope="feature__mul")
        self.assertEqual(seen["sandbox"], "$HOME/.revali/sandbox/sample/feature__mul/validate-r1")
        self.assertEqual(seen["scope"], "feature__mul")

    def test_wsl_script_cleans_the_branch_and_repo_dirs_when_empty(self):
        r = WslRunner(PlatformCfg(runner="wsl", distro="Ubuntu"))
        text = r.script("/mnt/d/x/repo", "/mnt/d/x/logs", "/mnt/d/x/extra", "abc123",
                        [("setup", "true"), ("test", "true")], "validate-r1",
                        "$HOME/.revali/sandbox/repo/feature__mul/validate-r1", 60, scope="feature__mul")
        self.assertIn('SCOPE="feature__mul"', text)
        self.assertIn('rm -rf "$SB"', text)
        self.assertIn('rmdir "$(dirname "$SB")"', text)
        self.assertIn('rmdir "$(dirname "$(dirname "$SB")")"', text)
        self.assertIn("cleanup() {", text)
        self.assertIn("|| { cleanup; exit 0; }", text)
        self.assertNotIn("\r", text)
        plain = r.script("/mnt/d/x/repo", "/mnt/d/x/logs", "/mnt/d/x/extra", "abc123",
                         [("test", "true")], "validate-r1", "$HOME/.revali/sandbox/repo/validate-r1", 60)
        self.assertIn('SCOPE=""', plain)


class SshLayoutTests(SshCase):
    """AC-1 on the ssh runner, through the ssh/scp stubs."""

    @unittest.skipUnless(HAVE_BASH, "needs bash")
    def test_staging_dirs_sit_under_the_branch(self):
        r = SshRunner(plat())
        logs = os.path.join(self.rdir(), "logs")
        head = git(["rev-parse", "HEAD"], self.repo).strip()
        report = r.run(self.repo, head, [("setup", "true"), ("test", "true")], {}, logs, "validate-r1",
                       scope="feature__mul")
        self.assertTrue(report.ok, [(s.name, s.returncode) for s in report.steps])
        calls = self.calls()
        self.assertTrue(calls[1][1][-1].startswith("box:.revali/sandbox/sample/feature__mul/validate-r1-in"),
                        calls[1][1])
        self.assertEqual(calls[3][1][-2:], ["box:.revali/sandbox/sample/feature__mul/validate-r1-logs/.", "."])
        self.assertIn("mkdir -p ", calls[0][1][-1])
        self.assertIn("feature__mul", calls[0][1][-1])
        cleanup = calls[4][1][-1]
        self.assertTrue(cleanup.startswith("rm -rf "), cleanup)
        self.assertIn("feature__mul/validate-r1", cleanup)
        self.assertIn("rmdir --ignore-fail-on-non-empty", cleanup)
        self.assertEqual(self.remote_leftovers(), [])
        # the <branch> and <repo> directories are gone too (PR #23 F6: the stub now removes every rmdir argument)
        sandbox = os.path.join(self.remote, ".revali", "sandbox")
        self.assertFalse(os.path.isdir(os.path.join(sandbox, "sample", "feature__mul")))
        self.assertFalse(os.path.isdir(os.path.join(sandbox, "sample")))

    @unittest.skipUnless(HAVE_BASH, "needs bash")
    def test_without_scope_the_layout_is_unchanged(self):
        r = SshRunner(plat())
        logs = os.path.join(self.rdir(), "logs")
        head = git(["rev-parse", "HEAD"], self.repo).strip()
        r.run(self.repo, head, [("test", "true")], {}, logs, "validate-r1")
        calls = self.calls()
        self.assertTrue(calls[1][1][-1].startswith("box:.revali/sandbox/sample/validate-r1-in"), calls[1][1])


class PipelineScopeTests(RepoCase):
    """AC-1 (the pipeline passes the branch) and AC-2 (the round record)."""

    def test_every_runner_call_carries_the_branch(self):
        self.claude(claude_entry())
        head_before = gitops.rev_parse("HEAD", self.repo)
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        calls = self.fake_calls("runner")
        labels = [c["label"] for c in calls]
        self.assertIn("baseline", labels)
        self.assertIn("smoke-r1-1", labels)
        self.assertIn("validate-r1", labels)
        for c in calls:
            self.assertEqual(c.get("scope"), "feature__mul", c)
        # AC-2: the round record names the reviewed HEAD, not the test commit
        state = State.load(self.rdir())
        self.assertEqual(state.rounds[0]["head_sha"], head_before)
        self.assertEqual(state.rounds[0]["test_commit"], gitops.rev_parse("HEAD", self.repo))
        self.assertNotEqual(state.rounds[0]["head_sha"], state.rounds[0]["test_commit"])
        self.assertEqual(state.head_sha, state.rounds[0]["test_commit"])   # the run's latest commit


class StopAndLockTests(RepoCase):
    """AC-3, AC-4, AC-5"""

    def tree_lock(self):
        return tree_lock_path(self.repo, ".revali")

    def test_stop_kills_the_tree_locks_pid_on_the_same_branch_without_a_branch_lock(self):
        child = live_child(self)
        State(repo="owner/repo", branch="feature/mul", base="main", stage="review", message="reviewer round 1",
              last_exit=-1).save(self.rdir())
        os.makedirs(os.path.dirname(self.tree_lock()), exist_ok=True)
        write_json_atomic(self.tree_lock(), {"pid": child.pid, "branch": "feature/mul", "since": "x"})
        self.assertFalse(os.path.isfile(lock_path(self.rdir())))
        code, out = run_cli(["stop"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("stopped pid %d" % child.pid, out)
        self.assertIsNotNone(child.wait(timeout=10))
        self.assertEqual(State.load(self.rdir()).stage, "stopped")
        self.assertFalse(os.path.isfile(self.tree_lock()))

    def test_stop_never_drops_a_live_tree_lock_without_killing(self):
        child = live_child(self)
        os.makedirs(os.path.dirname(self.tree_lock()), exist_ok=True)
        write_json_atomic(self.tree_lock(), {"pid": child.pid, "branch": "feature/mul", "since": "x"})
        code, out = run_cli(["stop"])   # no state file at all for that branch
        self.assertEqual(code, EXIT_OK, out)
        self.assertIsNotNone(child.wait(timeout=10))
        self.assertFalse(os.path.isfile(self.tree_lock()))

    def test_reserving_a_lock_the_child_already_wrote(self):
        child = live_child(self)
        os.makedirs(self.rdir(), exist_ok=True)
        write_json_atomic(lock_path(self.rdir()), {"pid": child.pid, "since": "x"})
        acquire_lock(self.rdir(), pid=child.pid)   # no LockHeld: that pid is the one we reserve for
        self.assertEqual(read_lock(self.rdir())["pid"], child.pid)
        write_json_atomic(self.tree_lock(), {"pid": child.pid, "branch": "feature/mul", "since": "x"})
        acquire_tree_lock(self.tree_lock(), "feature/mul", pid=child.pid)
        self.assertEqual(read_tree_lock(self.tree_lock())["pid"], child.pid)
        with self.assertRaises(LockHeld):
            acquire_lock(self.rdir(), pid=999999999)   # someone else's live pid is still a conflict
        with self.assertRaises(TreeLockHeld):
            acquire_tree_lock(self.tree_lock(), "feature/mul", pid=999999999)

    def test_detached_run_prints_no_traceback(self):
        self.claude(claude_entry())
        code, out = run_cli(["run"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertNotIn("Traceback", out)
        run_cli(["wait", "--timeout", "90s"])

    def test_wait_branch_timeout_hint_repeats_the_branch(self):
        os.makedirs(self.rdir(), exist_ok=True)
        write_json_atomic(lock_path(self.rdir()), {"pid": os.getpid(), "since": "x"})
        self.addCleanup(lambda: os.path.isfile(lock_path(self.rdir())) and os.remove(lock_path(self.rdir())))
        git(["checkout", "-q", "main"], self.repo)
        code, out = run_cli(["wait", "--branch", "feature/mul", "--timeout", "1s"])
        self.assertEqual(code, EXIT_OK + 4, out)
        self.assertIn("call `revali wait --branch feature/mul` again", out)
        git(["checkout", "-q", "feature/mul"], self.repo)
        code, out = run_cli(["wait", "--timeout", "1s"])
        self.assertEqual(code, EXIT_OK + 4, out)
        self.assertIn("call `revali wait` again", out)
        self.assertNotIn("--branch", out)


class MergeLockTests(RepoCase):
    """AC-6"""

    def tree_lock(self):
        return tree_lock_path(self.repo, ".revali")

    def ready(self):
        State(repo="owner/repo", branch="feature/mul", base="main", stage="ready_to_merge",
              message="validation 1 passed", last_exit=EXIT_OK, pr_number=7).save(self.rdir())

    def test_merge_holds_the_tree_lock_and_a_run_is_refused_meanwhile(self):
        self.ready()
        seen = {}

        def fake_merge(cwd, rdir, state, log):
            seen["lock"] = read_tree_lock(self.tree_lock())
            git(["checkout", "-q", "main"], self.repo)   # a second session on another branch
            try:
                seen["run"] = run_cli(["run"])
            finally:
                git(["checkout", "-q", "feature/mul"], self.repo)
            return EXIT_OK

        with mock.patch.object(merge, "do_merge", fake_merge), \
             mock.patch.object(merge, "merge_summary", lambda state, base: "MERGED (fake)"), \
             mock.patch.object(merge, "remove_tree", lambda path: None):
            code, out = run_cli(["merge"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertEqual(seen["lock"]["branch"], "feature/mul")
        self.assertEqual(seen["lock"]["pid"], os.getpid())
        self.assertEqual(seen["run"][0], EXIT_ERROR)
        self.assertIn("already in progress in this working tree on branch feature/mul", seen["run"][1])
        self.assertFalse(os.path.isfile(self.tree_lock()))
        self.assertFalse(os.path.isfile(lock_path(self.rdir())))

    def test_merge_releases_the_tree_lock_on_failure(self):
        self.ready()
        from revali.preflight import Stop

        def failing(cwd, rdir, state, log):
            raise Stop(EXIT_ERROR, "gh pr merge failed (fake)")

        with mock.patch.object(merge, "do_merge", failing):
            code, out = run_cli(["merge"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertFalse(os.path.isfile(self.tree_lock()))
        self.assertFalse(os.path.isfile(lock_path(self.rdir())))

    def test_merge_refused_while_a_run_holds_the_tree(self):
        self.ready()
        child = live_child(self)
        os.makedirs(os.path.dirname(self.tree_lock()), exist_ok=True)
        write_json_atomic(self.tree_lock(), {"pid": child.pid, "branch": "feature/other", "since": "x"})
        with mock.patch.object(merge, "do_merge", side_effect=AssertionError("must not run")):
            code, out = run_cli(["merge"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertIn("ERROR: a run is in progress", out)
        self.assertTrue(os.path.isfile(self.tree_lock()))   # not ours to remove


class WorktreeMergeTests(RepoCase):
    """AC-9 of PR #23, re-based on a real linked worktree since feature/worktree-docs AC-3: the
    primary tree (self.repo) holds main, feature/mul lives in the linked worktree self.wt, and
    every command runs there."""

    def setUp(self):
        super().setUp()
        git(["checkout", "-q", "main"], self.repo)
        self.wt = os.path.join(self.tmp, "wt")
        git(["worktree", "add", "--quiet", self.wt, "feature/mul"], self.repo)

        def drop():
            os.chdir(self.repo)
            subprocess.run(["git", "worktree", "remove", "--force", self.wt], cwd=self.repo, capture_output=True)

        self.addCleanup(drop)
        os.chdir(self.wt)

    def rdir(self):
        return os.path.join(self.wt, ".revali", "feature__mul")

    def ready(self):
        State(repo="owner/repo", branch="feature/mul", base="main", stage="ready_to_merge",
              message="validation 1 passed", last_exit=EXIT_OK, pr_number=7,
              head_sha=gitops.rev_parse("HEAD", self.wt), test_files=["tests/test_review_mul.py"]).save(self.rdir())
        git(["push", "-q", "-u", "origin", "feature/mul"], self.wt)   # what the pr stage does in a real run

    def remote_heads(self):
        return sorted(l.split("refs/heads/")[1] for l in git(["ls-remote", "--heads", "origin"], self.wt).splitlines())

    def test_worktree_holding(self):
        self.assertEqual(os.path.normpath(gitops.worktree_holding("main", self.wt)), os.path.normpath(self.repo))
        self.assertEqual(gitops.worktree_holding("feature/mul", self.wt), "")
        self.assertEqual(gitops.worktree_holding("nope", self.wt), "")

    def test_merge_from_the_worktree(self):
        self.ready()
        self.assertIn("feature/mul", self.remote_heads())
        code, out = run_cli(["merge"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("MERGED: PR #7 into main", out)
        merge_calls = [c["argv"] for c in self.fake_calls("gh") if c["argv"][:2] == ["pr", "merge"]]
        self.assertEqual(merge_calls, [["pr", "merge", "7", "--squash"]])   # no --delete-branch
        self.assertNotIn("feature/mul", self.remote_heads())               # deleted by revali
        self.assertEqual(gitops.current_branch(self.wt), "HEAD")            # detached
        self.assertIsNone(gitops.rev_parse("feature/mul", self.wt))         # local branch gone
        self.assertEqual(gitops.rev_parse("HEAD", self.wt), gitops.rev_parse("origin/main", self.wt))
        self.assertEqual(gitops.current_branch(self.repo), "main")          # the primary tree is untouched
        self.assertIn("worktree mode", out)          # the stage lines go to stdout; the log dir is removed
        self.assertIn("detached at the merged main, local branch feature/mul removed", out)
        self.assertIn("git worktree remove", out)
        self.assertIn(os.path.normpath(self.repo), os.path.normpath(out))   # where to `git pull`
        self.assertFalse(os.path.isfile(tree_lock_path(self.wt, ".revali")))

    def test_gh_error_after_the_pr_merged_still_counts(self):
        self.ready()
        self.scenario({"merge_exit": 1, "pr_create": {"number": 7, "url": "https://github.example/pr/7",
                                                      "state": "MERGED", "isDraft": False}})
        code, out = run_cli(["merge"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("MERGED: PR #7 into main", out)
        self.assertEqual(gitops.current_branch(self.wt), "HEAD")
        views = [c["argv"] for c in self.fake_calls("gh") if c["argv"][:2] == ["pr", "view"]]
        self.assertTrue(views, "gh pr view was consulted")

    def test_gh_error_on_an_unmerged_pr_is_still_an_error(self):
        self.ready()
        self.scenario({"merge_exit": 1, "pr_create": {"number": 7, "url": "https://github.example/pr/7",
                                                      "state": "OPEN", "isDraft": False}})
        code, out = run_cli(["merge"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertIn("gh pr merge failed", out)
        self.assertEqual(gitops.current_branch(self.wt), "feature/mul")
        self.assertIn("feature/mul", self.remote_heads())
        self.assertEqual(State.load(self.rdir()).stage, "ready_to_merge")


class ReadmeTests(unittest.TestCase):
    """AC-7"""

    def test_readme_shows_the_layout(self):
        with open(os.path.join(ROOT, "README.md"), encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("<repo>/<branch>/<label>", text)
        self.assertNotIn("sandbox/<repo>/<label>", text)
        self.assertIn("`merge` holds", text)


if __name__ == "__main__":
    unittest.main()
