"""Review of feature/docs-split, the PR #24 follow-ups in `merge` and `stop`:
AC-6 the primary-tree refusal comes before the CI wait and points at the docs (no unrunnable
command);
AC-7 a failed `git branch -D` after the merge is reported as a kept branch with git's words,
in the primary tree and in worktree mode, and `merge` still returns 0;
AC-8 a branch lock taken between the check and `acquire_lock` is one ERROR line, exit 1;
AC-9 `stop` on a detached HEAD follows a dead run's `tree.lock` record to its branch.

Black-box through the CLI with the fake gh and real git. The `git branch -D` failures are real:
git refuses to delete a branch that another worktree has checked out. The one patched call
(AC-8) is the pre-check, which is the only way to reproduce a lock that arrives between the
check and the acquire."""

import json
import os
import subprocess
import sys
import unittest
from unittest import mock

from revali import EXIT_ERROR, EXIT_OK, gitops, pipeline
from revali.config import history_path
from revali.state import State, lock_path, read_history, tree_lock_path, write_json_atomic
from tests.helpers import ROOT, RepoCase, git, run_cli


def live_child(case):
    """A live process standing in for a detached run. It gets its own session: `revali stop`
    kills the process group of the pid it finds, and a child in the runner's own group would
    take the whole test run down with it. Killed and reaped at the end if still there."""
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)"], start_new_session=True
    )

    def reap():
        if child.poll() is None:
            child.kill()
        child.wait(timeout=10)

    case.addCleanup(reap)
    return child


def dead_pid():
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


def read_doc(name):
    with open(os.path.join(ROOT, "docs", name), "r", encoding="utf-8", newline="") as fh:
        return fh.read()


def same_path_in(text, path):
    return os.path.normcase(os.path.normpath(path)).replace("\\", "/") in os.path.normcase(
        text
    ).replace("\\", "/")


class MergeCase(RepoCase):
    """self.repo is the primary tree on feature/mul, main pushed to a bare origin."""

    def setUp(self):
        super().setUp()
        os.environ["REVALI_POLL_SECONDS"] = "0.01"

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

    def add_worktree(self, branch, name, force=False):
        """A linked worktree holding `branch`. `force` lets a second worktree check out a branch
        that is already checked out, which is what makes `git branch -D` refuse later."""
        path = os.path.join(self.tmp, name)
        argv = ["worktree", "add", "--quiet"] + (["--force"] if force else []) + [path, branch]
        git(argv, self.repo)

        def drop():
            os.chdir(self.repo)
            subprocess.run(
                ["git", "worktree", "remove", "--force", path], cwd=self.repo, capture_output=True
            )

        self.addCleanup(drop)
        return path

    def gh_calls(self, *head):
        return [c["argv"] for c in self.fake_calls("gh") if c["argv"][: len(head)] == list(head)]

    def line_with(self, out, needle):
        lines = [line for line in out.splitlines() if needle in line]
        self.assertEqual(len(lines), 1, "expected one line with %r in:\n%s" % (needle, out))
        return lines[0]


class RefusalBeforeTheCiWait(MergeCase):
    """AC-6"""

    def test_primary_tree_is_refused_without_polling_ci(self):
        linked = self.add_worktree("main", "wt-main")
        rdir = self.ready(self.repo)
        # a PR whose check is still pending: with the old order, merge would poll it first
        self.scenario(
            {
                "checks_sequence": [
                    [{"name": "ci", "state": "PENDING", "bucket": "pending"}],
                    [{"name": "ci", "state": "SUCCESS", "bucket": "pass"}],
                ]
            }
        )
        code, out = run_cli(["merge"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertNotIn("Traceback", out)
        self.assertEqual(
            self.gh_calls("pr", "checks"), [], "the CI wait ran before the refusal:\n" + out
        )
        self.assertNotIn("waiting for CI", out)
        self.assertEqual(self.gh_calls("pr", "merge"), [])
        line = self.line_with(out, "ERROR: main is checked out in")
        self.assertTrue(same_path_in(line, linked), line)
        self.assertIn("remove or switch that worktree, then merge again", line)
        # AC-6 as rewritten after round 1 (F1): no command that cannot run from this state,
        # a pointer at the docs instead
        self.assertNotIn("git worktree add", line)
        self.assertNotIn("`", line, "a command is offered: " + line)  # none can run from this state
        self.assertIn('docs/workflow.md, "Several agents on one repository"', line)
        self.assertIn(
            "\n## Several agents on one repository\n", read_doc("workflow.md")
        )  # the target exists
        # nothing changed and both locks are released
        self.assertEqual(State.load(rdir).stage, "ready_to_merge")
        self.assertEqual(gitops.current_branch(self.repo), "feature/mul")
        self.assertFalse(os.path.isfile(lock_path(rdir)))
        self.assertFalse(os.path.isfile(tree_lock_path(self.repo, ".revali")))


class KeptBranchIsReported(MergeCase):
    """AC-7"""

    def test_primary_tree_success_says_removed(self):
        self.ready(self.repo)
        code, out = run_cli(["merge"])
        self.assertEqual(code, EXIT_OK, out)
        line = self.line_with(out, "local: on main")
        self.assertIn("branch feature/mul removed", line)
        self.assertNotIn("kept", line)
        self.assertIsNone(gitops.rev_parse("feature/mul", self.repo))

    def test_primary_tree_reports_a_kept_branch_with_gits_error(self):
        self.ready(self.repo)
        # the fake gh deletes nothing, so revali's own `git branch -D` runs; a second worktree
        # holding feature/mul makes git refuse it
        holder = self.add_worktree("feature/mul", "wt-holder", force=True)
        code, out = run_cli(["merge"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("MERGED: PR #7 into main", out)
        self.assertEqual(gitops.current_branch(self.repo), "main")
        self.assertIsNotNone(gitops.rev_parse("feature/mul", self.repo))
        line = self.line_with(out, "local: on main")
        self.assertIn("branch feature/mul kept", line)
        self.assertNotIn("removed", line)
        self.assertIn("git branch -D failed", line)
        # git's own words: it names the worktree that holds the branch
        self.assertRegex(line, r"(?i)cannot delete|checked out at|used by worktree")
        self.assertTrue(same_path_in(line, holder) or "wt-holder" in line, line)

    def test_worktree_mode_reports_a_kept_branch_with_gits_error(self):
        git(["checkout", "-q", "main"], self.repo)
        wt = self.add_worktree("feature/mul", "wt-feature")
        holder = self.add_worktree("feature/mul", "wt-holder", force=True)
        os.chdir(wt)
        self.ready(wt)
        code, out = run_cli(["merge"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("MERGED: PR #7 into main", out)
        self.assertEqual(gitops.current_branch(wt), "HEAD")
        self.assertIsNotNone(gitops.rev_parse("feature/mul", wt))
        line = self.line_with(out, "worktree: detached at the merged main")
        self.assertIn("local branch feature/mul kept", line)
        self.assertNotIn("removed", line)
        self.assertIn("git branch -D failed", line)
        self.assertRegex(line, r"(?i)cannot delete|checked out at|used by worktree")
        self.assertIn("git worktree remove", line)
        self.assertTrue(same_path_in(line, holder) or "wt-holder" in line, line)

    def test_worktree_mode_success_says_removed(self):
        git(["checkout", "-q", "main"], self.repo)
        wt = self.add_worktree("feature/mul", "wt-feature")
        os.chdir(wt)
        self.ready(wt)
        code, out = run_cli(["merge"])
        self.assertEqual(code, EXIT_OK, out)
        line = self.line_with(out, "worktree: detached at the merged main")
        self.assertIn("local branch feature/mul removed", line)
        self.assertNotIn("kept", line)
        self.assertIsNone(gitops.rev_parse("feature/mul", wt))


class BranchLockArrivesAfterTheCheck(MergeCase):
    """AC-8"""

    def test_lock_held_is_one_error_line_and_nothing_is_left_behind(self):
        rdir = self.ready(self.repo)
        child = live_child(self)
        write_json_atomic(lock_path(rdir), {"pid": child.pid, "since": "x"})
        # the pre-check saw no run (the `run` arrived just after it); acquire_lock then meets it
        with mock.patch.object(pipeline, "lock_owner_alive", return_value=None):
            code, out = run_cli(["merge"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertNotIn("Traceback", out)
        errors = [line for line in out.splitlines() if line.startswith("ERROR:")]
        self.assertEqual(len(errors), 1, out)
        self.assertIn("pid %d" % child.pid, errors[0])
        self.assertEqual(self.gh_calls("pr", "merge"), [])
        self.assertEqual(self.gh_calls("pr", "checks"), [])
        # the caller took no lock: the tree lock is absent, the branch lock is still the run's
        self.assertFalse(os.path.isfile(tree_lock_path(self.repo, ".revali")))
        with open(lock_path(rdir), "r", encoding="utf-8") as fh:
            self.assertEqual(int(json.load(fh)["pid"]), child.pid)
        self.assertEqual(State.load(rdir).stage, "ready_to_merge")
        self.assertEqual(gitops.current_branch(self.repo), "feature/mul")


class DetachedStopFollowsTheDeadRecord(RepoCase):
    """AC-9"""

    def tree_lock(self):
        return tree_lock_path(self.repo, ".revali")

    def identity(self, branch):
        return "repo: %s  branch: %s" % (gitops.repo_root(self.repo), branch)

    def dead_run(self, stage="validate"):
        State(
            repo="owner/repo",
            branch="feature/mul",
            base="main",
            stage=stage,
            message="validation 1",
            last_exit=-1,
            pr_number=7,
        ).save(self.rdir())
        write_json_atomic(lock_path(self.rdir()), {"pid": dead_pid(), "since": "x"})

    def test_stale_tree_lock_names_the_branch_and_the_run_is_closed(self):
        self.dead_run("validate")
        write_json_atomic(
            self.tree_lock(), {"pid": dead_pid(), "branch": "feature/mul", "since": "x"}
        )
        git(["checkout", "-q", "--detach"], self.repo)
        code, out = run_cli(["stop"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertNotIn("Traceback", out)
        self.assertEqual(out.splitlines()[0], self.identity("feature/mul"))  # the branch it closed
        self.assertIn("found dead at stage 'validate'", out)
        self.assertNotIn("no run in progress", out)
        state = State.load(self.rdir())
        self.assertEqual((state.stage, state.last_exit), ("stopped", EXIT_ERROR))
        self.assertFalse(os.path.isfile(self.tree_lock()))
        self.assertFalse(os.path.isfile(lock_path(self.rdir())))
        self.assertEqual(gitops.current_branch(self.repo), "HEAD")  # the checkout is not touched
        self.assertFalse(os.path.exists(os.path.join(self.repo, ".revali", "HEAD")))
        # the same closing `stop` does on a branch: a history row for `stats`
        rows = read_history(history_path())
        self.assertTrue(any(r.get("stage") == "stopped" for r in rows), rows)
        # and `wait` for that branch now reports a stopped run, not a death
        code, out = run_cli(["wait", "--branch", "feature/mul", "--timeout", "1s"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertIn("stopped:", out)
        self.assertNotIn("died at stage", out)

    def test_without_a_tree_lock_stop_still_says_no_run(self):
        self.dead_run("review")
        git(["checkout", "-q", "--detach"], self.repo)
        code, out = run_cli(["stop"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertEqual(out.splitlines()[0], self.identity("HEAD"))
        self.assertIn("no run in progress", out)
        self.assertEqual(State.load(self.rdir()).stage, "review")  # left alone
        self.assertEqual(read_history(history_path()), [])
        self.assertFalse(os.path.exists(os.path.join(self.repo, ".revali", "HEAD")))

    def test_stale_record_for_a_run_with_a_result_changes_nothing(self):
        State(
            repo="owner/repo",
            branch="feature/mul",
            base="main",
            stage="ready_to_merge",
            message="validation 1 passed",
            last_exit=EXIT_OK,
            pr_number=7,
        ).save(self.rdir())
        write_json_atomic(
            self.tree_lock(), {"pid": dead_pid(), "branch": "feature/mul", "since": "x"}
        )
        git(["checkout", "-q", "--detach"], self.repo)
        code, out = run_cli(["stop"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("no run in progress", out)
        state = State.load(self.rdir())
        self.assertEqual((state.stage, state.last_exit), ("ready_to_merge", EXIT_OK))
        self.assertFalse(os.path.isfile(self.tree_lock()))  # the stale record is cleared
        self.assertEqual(read_history(history_path()), [])

    def test_on_a_branch_a_stale_record_for_another_branch_is_still_ignored(self):
        # PR #22 behaviour kept: on a branch, `stop` looks at that branch, not at a dead record
        other = os.path.join(self.repo, ".revali", "feature__other")
        State(
            repo="owner/repo",
            branch="feature/other",
            base="main",
            stage="review",
            message="reviewer round 1",
            last_exit=-1,
        ).save(other)
        write_json_atomic(
            self.tree_lock(), {"pid": dead_pid(), "branch": "feature/other", "since": "x"}
        )
        code, out = run_cli(["stop"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertEqual(out.splitlines()[0], self.identity("feature/mul"))
        self.assertIn("no run in progress", out)
        self.assertEqual(State.load(other).stage, "review")
        self.assertFalse(os.path.isfile(self.tree_lock()))

    def test_live_run_found_through_tree_lock_is_still_stopped(self):
        # the live path is unchanged by the stale-record rule
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
        self.assertEqual(out.splitlines()[0], self.identity("feature/mul"))
        self.assertIn("stopped pid %d" % child.pid, out)
        self.assertIsNotNone(child.wait(timeout=10))
        self.assertEqual(State.load(self.rdir()).stage, "stopped")


if __name__ == "__main__":
    unittest.main()
