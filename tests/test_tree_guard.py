"""feature/tree-guard: one run per working tree (AC-1), a run that stops when the branch or
HEAD moves under it (AC-2), `wait --branch` and a `stop` that acts on the tree's run from any
branch (AC-3), the identity line on every command and no traceback on a detached HEAD (AC-4),
and the help texts (AC-5)."""
import os
import re
import subprocess
import sys
import unittest
from unittest import mock

from tests.helpers import RepoCase, claude_entry, git, run_cli
from revali import EXIT_ACTION, EXIT_ERROR, EXIT_OK
from revali import gitops, review
from revali.state import (State, lock_owner_alive, lock_path, read_history, tree_lock_path,
                          write_json_atomic)

TREE_MSG = ("ERROR: a revali run is already in progress in this working tree on branch %s (pid %d); "
            "use `revali wait --branch %s` or `revali stop`")


class TreeCase(RepoCase):
    def tree_lock(self):
        return tree_lock_path(self.repo, ".revali")

    def hold_tree_lock(self, branch, pid=None):
        os.makedirs(os.path.dirname(self.tree_lock()), exist_ok=True)
        write_json_atomic(self.tree_lock(), {"pid": pid or os.getpid(), "branch": branch,
                                             "since": "2026-09-04T00:00:00"})

    def hold_branch_lock(self, pid=None):
        os.makedirs(self.rdir(), exist_ok=True)
        write_json_atomic(lock_path(self.rdir()), {"pid": pid or os.getpid(), "since": "2026-09-04T00:00:00"})

    def identity(self, branch="feature/mul"):
        return "repo: %s  branch: %s" % (gitops.repo_root(self.repo), branch)

    def first_line(self, out):
        return out.splitlines()[0] if out else ""

    def live_child(self):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"],
                                 start_new_session=True)   # own group: kill_tree uses killpg on POSIX
        self.addCleanup(lambda: child.poll() is None and child.kill())
        return child

    def revali_round_commits(self, ref):
        out = git(["log", "--format=%B", ref], self.repo)
        return out.count("Revali-Round:")

    def gh_comments(self):
        return [c for c in self.fake_calls("gh") if c["argv"][:2] == ["pr", "comment"]]


class TreeLockTests(TreeCase):
    """AC-1"""

    def test_run_refused_when_another_branch_holds_the_tree(self):
        self.hold_tree_lock("feature/other")
        code, out = run_cli(["run"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertEqual(self.first_line(out), self.identity())
        self.assertIn(TREE_MSG % ("feature/other", os.getpid(), "feature/other"), out)
        self.assertFalse(os.path.isfile(lock_path(self.rdir())))
        self.assertIsNone(State.load(self.rdir()))
        self.assertTrue(os.path.isfile(self.tree_lock()))   # the other run's lock is left alone

    def test_foreground_run_is_refused_the_same_way(self):
        child = self.live_child()   # the tree lock ignores its own pid, which a foreground run shares
        self.hold_tree_lock("feature/other", pid=child.pid)
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertIn(TREE_MSG % ("feature/other", child.pid, "feature/other"), out)
        self.assertFalse(os.path.isfile(lock_path(self.rdir())))   # the branch lock was let go again

    def test_same_branch_refusal_keeps_its_wording(self):
        self.hold_branch_lock()
        self.hold_tree_lock("feature/mul")
        code, out = run_cli(["run"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertIn("ERROR: a revali run is already in progress (pid %d); use `revali wait` or `revali stop`"
                      % os.getpid(), out)
        self.assertNotIn("in this working tree", out)

    def test_stale_tree_lock_is_removed_and_ignored(self):
        self.hold_tree_lock("feature/other", pid=999999999)
        code, out = run_cli(["run", "--dry-run"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertNotIn("already in progress", out)
        self.assertFalse(os.path.isfile(self.tree_lock()))   # released with the branch lock

    def test_detached_run_takes_and_releases_both_locks(self):
        self.claude(claude_entry())
        code, out = run_cli(["run"])
        self.assertEqual(code, EXIT_OK, out)
        pid = int(re.search(r"started revali run \(pid (\d+)\)", out).group(1))
        if lock_owner_alive(self.rdir()):   # the child may already be done on a fast machine
            with open(self.tree_lock(), encoding="utf-8") as fh:
                lock = fh.read()
            self.assertIn('"pid": %d' % pid, lock)
            self.assertIn('"branch": "feature/mul"', lock)
        code, out = run_cli(["wait", "--timeout", "90s"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertFalse(os.path.isfile(self.tree_lock()))
        self.assertFalse(os.path.isfile(lock_path(self.rdir())))

    def test_tree_lock_released_when_preflight_fails(self):
        self.write("src/dirty.py", "# uncommitted\n")
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertIn("not clean", out)
        self.assertFalse(os.path.isfile(self.tree_lock()))
        self.assertFalse(os.path.isfile(lock_path(self.rdir())))

    def test_a_second_worktree_has_its_own_lock(self):
        self.hold_tree_lock("feature/mul")   # a live run in the primary tree
        wt = os.path.join(self.tmp, "wt")
        git(["worktree", "add", "--quiet", "-b", "feature/wt", wt, "feature/mul"], self.repo)
        def drop_worktree():
            os.chdir(self.repo)   # cannot remove the directory we stand in
            subprocess.run(["git", "worktree", "remove", "--force", wt], cwd=self.repo, capture_output=True)

        self.addCleanup(drop_worktree)
        os.makedirs(os.path.join(wt, ".revali", "feature__wt"))
        with open(self.change_md(), encoding="utf-8") as fh:
            doc = fh.read()
        with open(os.path.join(wt, ".revali", "feature__wt", "change.md"), "w", encoding="utf-8",
                  newline="\n") as fh:
            fh.write(doc)
        os.chdir(wt)
        code, out = run_cli(["run", "--dry-run"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertEqual(self.first_line(out), "repo: %s  branch: feature/wt" % gitops.repo_root(wt))
        self.assertNotIn("already in progress", out)
        self.assertFalse(os.path.isfile(tree_lock_path(gitops.repo_root(wt), ".revali")))
        self.assertTrue(os.path.isfile(self.tree_lock()))   # the primary tree's lock is untouched


class TreeGuardTests(TreeCase):
    """AC-2: the branch or HEAD changes while the reviewer session runs, or before validation."""

    def _run_with_reviewer_side_effect(self, effect):
        self.claude(claude_entry())
        real = review.spawn_reviewer

        def spawn_then_move(ctx, *a, **kw):
            rr = real(ctx, *a, **kw)
            effect()
            return rr

        with mock.patch.object(review, "spawn_reviewer", side_effect=spawn_then_move):
            return run_cli(["run", "--foreground"])

    def test_branch_switched_during_the_reviewer(self):
        before_mul = self.revali_round_commits("feature/mul")
        head = gitops.rev_parse("HEAD", self.repo)

        def switch():
            git(["checkout", "-q", "-b", "feature/other"], self.repo)   # the untracked test file comes along

        code, out = self._run_with_reviewer_side_effect(switch)
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertIn("ERROR: the working tree moved under the run: expected branch feature/mul at %s, "
                      "found feature/other at %s; nothing was committed or pushed" % (head[:10], head[:10]), out)
        state = State.load(self.rdir())
        self.assertEqual(state.stage, "error")
        self.assertEqual(state.last_exit, EXIT_ERROR)
        self.assertEqual(self.revali_round_commits("feature/mul"), before_mul)
        self.assertEqual(self.revali_round_commits("feature/other"), before_mul)
        self.assertFalse(self.exists("tests/test_review_mul.py"))   # discarded like an interruption
        self.assertFalse(state.reviewer_running)
        self.assertEqual(self.gh_comments(), [])
        self.assertFalse(os.path.isfile(self.tree_lock()))
        self.assertFalse(os.path.isfile(lock_path(self.rdir())))

    def test_head_moved_during_the_reviewer(self):
        head = gitops.rev_parse("HEAD", self.repo)
        foreign = {}

        def commit_something_else():
            self.write("src/other.py", "# another session's commit\n")
            git(["add", "src/other.py"], self.repo)
            git(["commit", "-q", "-m", "someone else"], self.repo)
            foreign["sha"] = gitops.rev_parse("HEAD", self.repo)

        code, out = self._run_with_reviewer_side_effect(commit_something_else)
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertIn("expected branch feature/mul at %s, found feature/mul at %s"
                      % (head[:10], foreign["sha"][:10]), out)
        self.assertEqual(gitops.rev_parse("HEAD", self.repo), foreign["sha"])   # the other commit stays
        self.assertEqual(self.revali_round_commits("feature/mul"), 0)
        self.assertFalse(self.exists("tests/test_review_mul.py"))
        self.assertEqual(State.load(self.rdir()).stage, "error")

    def test_tree_moved_before_validation(self):
        self.claude(claude_entry())
        real = review.run_round

        def round_then_switch(*a, **kw):
            outcome = real(*a, **kw)
            git(["checkout", "-q", "-b", "feature/other"], self.repo)
            return outcome

        with mock.patch.object(review, "run_round", side_effect=round_then_switch):
            code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertIn("the working tree moved under the run: expected branch feature/mul", out)
        self.assertIn("found feature/other", out)
        state = State.load(self.rdir())
        self.assertEqual(state.stage, "error")
        self.assertEqual(self.revali_round_commits("feature/mul"), 1)   # the run's own commit, before the move
        self.assertEqual(state.validations, [])
        names = [c["argv"][3] if len(c["argv"]) > 3 else "" for c in self.gh_comments()]
        self.assertFalse(any("validate" in " ".join(c["argv"]) for c in self.gh_comments()), names)

    def test_untouched_tree_passes_every_check(self):
        self.claude(claude_entry())
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertNotIn("moved under the run", out)
        self.assertEqual(State.load(self.rdir()).stage, "ready_to_merge")


class CrossBranchTests(TreeCase):
    """AC-3"""

    def test_wait_branch_from_another_branch(self):
        State(branch="feature/mul", base="main", stage="needs_action", message="changes requested in round 1",
              last_exit=EXIT_ACTION).save(self.rdir())
        git(["checkout", "-q", "main"], self.repo)
        code, out = run_cli(["wait", "--branch", "feature/mul", "--timeout", "1s"])
        self.assertEqual(code, EXIT_ACTION, out)
        self.assertEqual(self.first_line(out), self.identity("feature/mul"))
        self.assertIn("needs_action: changes requested in round 1", out)
        code, out = run_cli(["wait", "--timeout", "1s"])   # main itself has no run
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertEqual(self.first_line(out), self.identity("main"))

    def test_wait_branch_still_running(self):
        self.hold_branch_lock()
        self.hold_tree_lock("feature/mul")
        git(["checkout", "-q", "main"], self.repo)
        code, out = run_cli(["wait", "--branch", "feature/mul", "--timeout", "1s"])
        self.assertEqual(code, EXIT_OK + 4, out)
        self.assertEqual(self.first_line(out), self.identity("feature/mul"))
        self.assertIn("still running (pid %d)" % os.getpid(), out)

    def test_stop_from_another_branch_stops_the_trees_run(self):
        child = self.live_child()
        State(repo="owner/repo", branch="feature/mul", base="main", stage="review", message="reviewer round 1",
              last_exit=-1, reviewer_running=True).save(self.rdir())
        self.hold_branch_lock(pid=child.pid)
        self.hold_tree_lock("feature/mul", pid=child.pid)
        git(["checkout", "-q", "main"], self.repo)
        code, out = run_cli(["stop"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertEqual(self.first_line(out), self.identity("feature/mul"))
        self.assertIn("stopped pid %d" % child.pid, out)
        self.assertIsNotNone(child.wait(timeout=10))
        state = State.load(self.rdir())
        self.assertEqual(state.stage, "stopped")
        self.assertEqual(state.last_exit, EXIT_ERROR)
        self.assertTrue(state.reviewer_running)   # the next run's cleanup still knows
        self.assertFalse(os.path.isfile(self.tree_lock()))
        self.assertFalse(os.path.isfile(lock_path(self.rdir())))
        rows = read_history(os.path.join(self.home, "history.jsonl"))
        self.assertEqual(rows[-1]["stage"], "stopped")
        self.assertEqual(rows[-1]["branch"], "feature/mul")

    def test_stop_with_no_run_anywhere(self):
        git(["checkout", "-q", "main"], self.repo)
        code, out = run_cli(["stop"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertEqual(self.first_line(out), self.identity("main"))
        self.assertIn("no run in progress", out)

    def test_stop_removes_a_stale_tree_lock(self):
        self.hold_tree_lock("feature/other", pid=999999999)
        code, out = run_cli(["stop"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("no run in progress", out)
        self.assertFalse(os.path.isfile(self.tree_lock()))


class IdentityEverywhereTests(TreeCase):
    """AC-4 and AC-5"""

    def test_stop_reset_merge_print_the_identity_line_first(self):
        for argv in (["stop"], ["reset"], ["merge"]):
            with self.subTest(argv=argv):
                code, out = run_cli(argv)
                self.assertEqual(self.first_line(out), self.identity(), out)

    def test_clean_prints_the_identity_of_its_argument(self):
        code, out = run_cli(["clean", "feature/gone"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertEqual(self.first_line(out), self.identity("feature/gone"))
        self.assertIn("nothing to clean", out)

    def test_status_on_a_detached_head(self):
        git(["checkout", "-q", "--detach"], self.repo)
        # `stop` left this list with feature/worktree-docs AC-8: it resolves the run through tree.lock
        for argv in (["status"], ["run"], ["wait", "--timeout", "1s"], ["reset"]):
            with self.subTest(argv=argv):
                code, out = run_cli(argv)
                self.assertEqual(code, EXIT_ERROR, out)
                self.assertIn("ERROR: detached HEAD", out)
                self.assertNotIn("Traceback", out)
                self.assertNotIn("repo:", out)

    def test_outside_a_repository(self):
        os.chdir(self.tmp)
        for argv in (["status"], ["stop"], ["reset"], ["clean", "x"], ["merge"]):
            with self.subTest(argv=argv):
                code, out = run_cli(argv)
                self.assertEqual(code, EXIT_ERROR, out)
                self.assertIn("ERROR: not inside a git repository", out)
                self.assertNotIn("repo:", out)

    def test_help_texts(self):
        from revali.cli import build_parser
        sub = next(a for a in build_parser()._actions if getattr(a, "choices", None) and "wait" in a.choices)
        wait_help = " ".join(sub.choices["wait"].format_help().split())
        self.assertIn("--branch", wait_help)
        stop_help = " ".join(sub.choices["stop"].format_help().split())
        self.assertIn("working tree", stop_help)


if __name__ == "__main__":
    unittest.main()
