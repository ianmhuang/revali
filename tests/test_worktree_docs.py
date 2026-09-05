"""feature/worktree-docs: the several-agents documentation (AC-1, AC-2, AC-10), the worktree
merge follow-up reporting what happened and refusing to detach the primary tree (AC-3), the
tree lock taken inside a try in `merge` (AC-4), `head_sha` in review-n.json (AC-5), the ssh
stub's rmdir (AC-6, asserted in test_sandbox_scope), cleanup on a failed sandbox clone (AC-7),
`stop` from a detached HEAD (AC-8), and taskkill without a window (AC-9)."""

import json
import os
import subprocess
import sys
import unittest
from unittest import mock

from revali import EXIT_ERROR, EXIT_OK, VERSION, gitops, merge, pipeline, procs
from revali.config import PlatformCfg
from revali.runners import WslRunner
from revali.state import State, TreeLockHeld, lock_path, tree_lock_path, write_json_atomic
from tests.helpers import RepoCase, claude_entry, git, run_cli

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


def live_child(case):
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)"], start_new_session=True
    )
    case.addCleanup(lambda: child.poll() is None and child.kill())
    return child


class DocsTests(unittest.TestCase):
    """AC-1, AC-2, AC-10"""

    def test_readme_has_the_several_agents_section(self):
        text = read("docs/workflow.md")
        self.assertIn("## Several agents on one repository", text)
        section = text.split("## Several agents on one repository", 1)[1].split("\n## ", 1)[0]
        for phrase in (
            "git worktree add",
            "git worktree remove",
            "tree.lock",
            "git pull",
            "<repo>/<branch>/<label>",
            "same branch",
        ):
            self.assertIn(phrase, section, phrase)

    def test_skill_tells_the_session_to_use_a_worktree(self):
        text = read("skill/SKILL.md")
        self.assertIn("git worktree add", text)
        self.assertIn("git worktree remove", text)

    def test_merge_bullet_lists_the_worktree_side_effects(self):
        text = read("docs/side-effects.md")
        bullet = " ".join(
            text.split("# What revali does to your repository", 1)[1].split()
        )  # unwrap lines
        for phrase in (
            "`gh pr merge --<method>` without `--delete-branch`",
            "git push origin --delete",
            "git fetch --prune origin",
            "git checkout --detach FETCH_HEAD",
            "git branch -D",
            "MERGED",
        ):
            self.assertIn(phrase, bullet, phrase)

    def test_status_line(self):
        text = read("README.md")
        line = next(line for line in text.splitlines() if line.startswith("Status:"))
        self.assertIn(VERSION, line + text.split("Status:", 1)[1][:400])
        self.assertIn("#21", text.split("Status:", 1)[1][:400])


class WorktreeFollowUpTests(RepoCase):
    """AC-3"""

    def add_worktree(self, branch):
        """A linked worktree holding `branch`; self.repo stays the primary tree."""
        path = os.path.join(self.tmp, "wt-" + branch.replace("/", "__"))
        git(["worktree", "add", "--quiet", path, branch], self.repo)

        def drop():
            os.chdir(self.repo)
            subprocess.run(
                ["git", "worktree", "remove", "--force", path], cwd=self.repo, capture_output=True
            )

        self.addCleanup(drop)
        return path

    def ready(self, root):
        rdir = os.path.join(root, ".revali", "feature__mul")
        State(
            repo="owner/repo",
            branch="feature/mul",
            base="main",
            stage="ready_to_merge",
            message="validation 1 passed",
            last_exit=EXIT_OK,
            pr_number=7,
            head_sha=gitops.rev_parse("HEAD", root),
            test_files=["tests/test_review_mul.py"],
        ).save(rdir)
        git(["push", "-q", "-u", "origin", "feature/mul"], root)
        return rdir

    def test_is_linked_worktree(self):
        self.assertFalse(gitops.is_linked_worktree(self.repo))
        wt = self.add_worktree("main")
        self.assertFalse(gitops.is_linked_worktree(self.repo))  # self.repo holds the .git directory
        self.assertTrue(gitops.is_linked_worktree(wt))

    def test_primary_tree_refuses_when_a_linked_worktree_holds_the_base(self):
        linked = self.add_worktree(
            "main"
        )  # main lives in a linked worktree; self.repo is the primary tree
        rdir = self.ready(self.repo)
        code, out = run_cli(["merge"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertIn("ERROR: main is checked out in", out)
        self.assertIn(os.path.normpath(linked), os.path.normpath(out))
        self.assertIn("remove or switch that worktree, then merge again", out)
        self.assertFalse(any(c["argv"][:2] == ["pr", "merge"] for c in self.fake_calls("gh")))
        self.assertEqual(gitops.current_branch(self.repo), "feature/mul")
        self.assertEqual(State.load(rdir).stage, "ready_to_merge")
        self.assertIn("feature/mul", git(["ls-remote", "--heads", "origin"], self.repo))

    def test_follow_up_reports_a_failed_fetch(self):
        # a linked worktree merge whose remote vanished after the PR merged: the tree stays on
        # the branch and the message says so instead of claiming a detach
        git(["checkout", "-q", "main"], self.repo)
        wt = self.add_worktree("feature/mul")
        os.chdir(wt)
        self.ready(wt)
        git(["remote", "set-url", "origin", os.path.join(self.tmp, "gone.git")], wt)
        code, out = run_cli(["merge"])
        self.assertEqual(
            code, EXIT_OK, out
        )  # GitHub merged the PR; the local follow-up is best effort
        self.assertIn("still on feature/mul", out)
        self.assertIn("local branch feature/mul kept", out)
        self.assertNotIn("detached at", out)
        self.assertIn("git fetch failed", out)
        self.assertIn("could not delete origin/feature/mul", out)
        self.assertEqual(gitops.current_branch(wt), "feature/mul")
        self.assertIsNotNone(gitops.rev_parse("feature/mul", wt))


class MergeLockRaceTests(RepoCase):
    """AC-4"""

    def test_tree_lock_taken_between_check_and_acquire(self):
        State(
            repo="owner/repo",
            branch="feature/mul",
            base="main",
            stage="ready_to_merge",
            message="validation 1 passed",
            last_exit=EXIT_OK,
            pr_number=7,
        ).save(self.rdir())

        def held(path, branch, pid=None):
            raise TreeLockHeld(123, "feature/other", "2026-09-04T00:00:00")

        with (
            mock.patch.object(pipeline, "acquire_tree_lock", held),
            mock.patch.object(merge, "do_merge", side_effect=AssertionError("must not run")),
        ):
            code, out = run_cli(["merge"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertIn(
            "already in progress in this working tree on branch feature/other (pid 123)", out
        )
        self.assertNotIn("Traceback", out)
        self.assertFalse(os.path.isfile(lock_path(self.rdir())))  # the branch lock was let go


class ReviewJsonTests(RepoCase):
    """AC-5"""

    def test_review_json_carries_the_reviewed_head(self):
        self.claude(claude_entry())
        head = gitops.rev_parse("HEAD", self.repo)
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        with open(os.path.join(self.rdir(), "review-1.json"), encoding="utf-8") as fh:
            data = json.load(fh)
        self.assertEqual(data["head_sha"], head)
        self.assertEqual(data["commit"], gitops.rev_parse("HEAD", self.repo))
        self.assertNotEqual(data["head_sha"], data["commit"])


class SandboxCleanupTests(unittest.TestCase):
    """AC-7"""

    def test_clone_failures_clean_up(self):
        r = WslRunner(PlatformCfg(runner="wsl", distro="Ubuntu"))
        text = r.script(
            "/mnt/d/x/repo",
            "/mnt/d/x/logs",
            "/mnt/d/x/extra",
            "abc123",
            [("test", "true")],
            "validate-r1",
            "$HOME/.revali/sandbox/repo/feature__mul/validate-r1",
            60,
            scope="feature__mul",
        )
        self.assertIn('printf "clone\\t128\\t0\\n" >> "$RES"; cleanup; exit 0', text)
        self.assertIn('printf "clone\\t1\\t0\\n" >> "$RES"; cleanup; exit 0', text)
        self.assertLess(
            text.index("cleanup() {"), text.index('printf "clone')
        )  # defined before use


class DetachedStopTests(RepoCase):
    """AC-8"""

    def tree_lock(self):
        return tree_lock_path(self.repo, ".revali")

    def test_stop_from_a_detached_head_stops_the_trees_run(self):
        child = live_child(self)
        State(
            repo="owner/repo",
            branch="feature/mul",
            base="main",
            stage="review",
            message="reviewer round 1",
            last_exit=-1,
        ).save(self.rdir())
        write_json_atomic(lock_path(self.rdir()), {"pid": child.pid, "since": "x"})
        write_json_atomic(
            self.tree_lock(), {"pid": child.pid, "branch": "feature/mul", "since": "x"}
        )
        git(["checkout", "-q", "--detach"], self.repo)
        code, out = run_cli(["stop"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertEqual(
            out.splitlines()[0], "repo: %s  branch: feature/mul" % gitops.repo_root(self.repo)
        )
        self.assertIn("stopped pid %d" % child.pid, out)
        self.assertIsNotNone(child.wait(timeout=10))
        self.assertEqual(State.load(self.rdir()).stage, "stopped")
        self.assertFalse(os.path.isfile(self.tree_lock()))

    def test_stop_from_a_detached_head_with_nothing_running(self):
        git(["checkout", "-q", "--detach"], self.repo)
        code, out = run_cli(["stop"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertEqual(
            out.splitlines()[0], "repo: %s  branch: HEAD" % gitops.repo_root(self.repo)
        )
        self.assertIn("no run in progress", out)
        for argv in (["run"], ["wait", "--timeout", "1s"], ["status"], ["reset"], ["merge"]):
            with self.subTest(argv=argv):
                code, out = run_cli(argv)
                self.assertEqual(code, EXIT_ERROR, out)
                self.assertIn("ERROR: detached HEAD", out)


class TaskkillTests(unittest.TestCase):
    """AC-9"""

    def test_taskkill_has_no_window(self):
        calls = []

        def fake_run(argv, **kw):
            calls.append((argv, kw))
            return subprocess.CompletedProcess(argv, 0, b"", b"")

        with (
            mock.patch("os.name", "nt"),
            mock.patch.object(procs, "pid_alive", lambda pid: True),
            mock.patch("subprocess.run", fake_run),
        ):
            procs.kill_tree(4242)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0][:3], ["taskkill", "/PID", "4242"])
        self.assertEqual(calls[0][1].get("creationflags", 0) & 0x08000000, 0x08000000)


if __name__ == "__main__":
    unittest.main()
