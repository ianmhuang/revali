"""Reviewer acceptance tests for feature/tree-guard, AC-3 to AC-5.

AC-3: `wait --branch <b>` waits for another branch's run from any checkout with the usual
outcomes and exit codes and names `<b>` in its identity line; `stop` acts on the working
tree's live run whatever branch is checked out and closes that branch's state.
AC-4: `stop`, `reset`, `clean` and `merge` open with the identity line; a detached HEAD or
a directory outside a repository is an `ERROR:` line with exit 1, no traceback; the
wording after the identity line and the exit codes are unchanged.
AC-5: `wait -h` describes `--branch`, `stop -h` its working-tree scope, and the README
documents `tree.lock`, the mid-run check, `wait --branch` and the `stop` scope.

Black-box through the CLI on the fixture repository (fake gh / claude / runner, real git).
Locks and state are written directly to stage each outcome. On the base branch `wait` has
no `--branch`, `stop` only sees the checked-out branch, and `stop` / `reset` / `merge` /
`clean` do not print the identity line, so every test here fails there."""
import argparse
import json
import os
import subprocess
import sys
import unittest

from tests.helpers import RepoCase, git, run_cli
from revali import EXIT_ACTION, EXIT_ERROR, EXIT_OK
from revali import gitops
from revali.state import State, lock_path, read_history

EXIT_STILL_RUNNING = 4          # `wait` only, fixed by CONVENTIONS.md
DEAD_PID = 999999999            # no such process on any host
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class CrossBranchCase(RepoCase):
    def root(self):
        return gitops.repo_root(self.repo)

    def tree_lock(self):
        return os.path.join(self.root(), ".revali", "tree.lock")

    def hold_tree_lock(self, branch, pid):
        path = self.tree_lock()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            json.dump({"pid": pid, "branch": branch, "since": "2026-09-04T00:00:00"}, fh)

    def hold_branch_lock(self, pid):
        os.makedirs(self.rdir(), exist_ok=True)
        with open(lock_path(self.rdir()), "w", encoding="utf-8", newline="\n") as fh:
            json.dump({"pid": pid, "since": "2026-09-04T00:00:00"}, fh)

    def identity(self, branch="feature/mul"):
        return "repo: %s  branch: %s" % (self.root(), branch)

    def lines(self, out):
        return out.splitlines()

    def state(self, stage, message, last_exit, **fields):
        State(repo="me/sample", branch="feature/mul", base="main", stage=stage, message=message,
              last_exit=last_exit, **fields).save(self.rdir())

    def live_child(self):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"],
                                 start_new_session=True)   # own group: kill_tree uses killpg on POSIX
        self.addCleanup(lambda: child.poll() is None and child.kill())
        return child


class WaitForAnotherBranch(CrossBranchCase):
    def test_a_recorded_result_from_main(self):                                        # AC-3
        self.state("needs_action", "changes requested in round 1 (2 findings)", EXIT_ACTION)
        git(["checkout", "-q", "main"], self.repo)
        code, out = run_cli(["wait", "--branch", "feature/mul", "--timeout", "1s"])
        self.assertEqual(code, EXIT_ACTION, out)                                       # the run's own exit code
        lines = self.lines(out)
        self.assertEqual(lines[0], self.identity("feature/mul"))                       # names <b>, not main
        self.assertEqual(lines[1], "needs_action: changes requested in round 1 (2 findings)")

    def test_ready_to_merge_and_error_from_main(self):                                 # AC-3
        git(["checkout", "-q", "main"], self.repo)
        for stage, message, exit_code in (("ready_to_merge", "validation 1 passed", EXIT_OK),
                                          ("error", "configuration: bad key", EXIT_ERROR)):
            with self.subTest(stage=stage):
                self.state(stage, message, exit_code)
                code, out = run_cli(["wait", "--branch", "feature/mul", "--timeout", "1s"])
                self.assertEqual(code, exit_code, out)
                self.assertEqual(self.lines(out)[0], self.identity("feature/mul"))
                self.assertEqual(self.lines(out)[1], "%s: %s" % (stage, message))

    def test_still_running_from_main(self):                                            # AC-3
        self.state("review", "reviewer round 1", -1)
        self.hold_branch_lock(os.getpid())
        self.hold_tree_lock("feature/mul", os.getpid())
        git(["checkout", "-q", "main"], self.repo)
        code, out = run_cli(["wait", "--branch", "feature/mul", "--timeout", "1s"])
        self.assertEqual(code, EXIT_STILL_RUNNING, out)
        self.assertEqual(self.lines(out)[0], self.identity("feature/mul"))
        self.assertIn("still running (pid %d), stage review" % os.getpid(), out)

    def test_no_run_recorded_for_that_branch(self):                                    # AC-3
        git(["checkout", "-q", "main"], self.repo)
        code, out = run_cli(["wait", "--branch", "feature/nothing", "--timeout", "1s"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertEqual(self.lines(out)[0], self.identity("feature/nothing"))
        self.assertEqual(self.lines(out)[1], "no revali run recorded for this branch")


class StopActsOnTheTreesRun(CrossBranchCase):
    def test_stop_from_main_kills_the_run_of_the_other_branch(self):                   # AC-3
        child = self.live_child()
        self.state("review", "reviewer round 1", -1, reviewer_running=True,
                   pending_test_files=["tests/test_review_x.py"], rounds=[], fixes=1)
        self.hold_branch_lock(child.pid)
        self.hold_tree_lock("feature/mul", child.pid)
        git(["checkout", "-q", "main"], self.repo)
        code, out = run_cli(["stop"])
        self.assertEqual(code, EXIT_OK, out)
        lines = self.lines(out)
        self.assertEqual(lines[0], self.identity("feature/mul"))                       # that branch's identity line
        self.assertIn("stopped pid %d" % child.pid, out)
        self.assertNotIn("no run in progress", out)
        child.wait(timeout=10)                                                         # the process is gone
        state = State.load(self.rdir())
        self.assertEqual(state.stage, "stopped")                                       # closed like `stop` today
        self.assertEqual(state.last_exit, EXIT_ERROR)
        self.assertIn("stopped by user", state.message)
        self.assertIn("'review'", state.message)
        self.assertTrue(state.reviewer_running)                                        # cleanup flags kept
        self.assertEqual(state.pending_test_files, ["tests/test_review_x.py"])
        self.assertEqual(state.fixes, 1)
        self.assertFalse(os.path.isfile(lock_path(self.rdir())))                       # both locks released
        self.assertFalse(os.path.isfile(self.tree_lock()))
        rows = read_history(os.path.join(self.home, "history.jsonl"))
        self.assertTrue(rows, "no history row was appended")
        self.assertEqual(rows[-1]["stage"], "stopped")                                 # history row for that branch
        self.assertEqual(rows[-1]["branch"], "feature/mul")
        self.assertEqual(rows[-1]["exit"], EXIT_ERROR)
        self.assertEqual(gitops.current_branch(self.repo), "main")                     # the checkout was not touched
        self.assertIsNone(State.load(os.path.join(self.root(), ".revali", "main")))    # nothing recorded for main

    def test_stop_with_no_run_anywhere_from_main(self):                                # AC-3, AC-4
        git(["checkout", "-q", "main"], self.repo)
        code, out = run_cli(["stop"])
        self.assertEqual(code, EXIT_OK, out)
        lines = self.lines(out)
        self.assertEqual(lines[0], self.identity("main"))
        self.assertEqual(lines[1], "no run in progress")

    def test_stop_ignores_and_removes_a_dead_owner_on_another_branch(self):            # AC-3
        self.hold_tree_lock("feature/other", DEAD_PID)
        code, out = run_cli(["stop"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertEqual(self.lines(out)[0], self.identity())                          # stays on the checked-out branch
        self.assertIn("no run in progress", out)
        self.assertNotIn("stopped pid", out)
        self.assertFalse(os.path.isfile(self.tree_lock()))

    def test_stop_on_the_own_branch_still_releases_the_tree_lock(self):                # AC-3
        child = self.live_child()
        self.state("review", "reviewer round 1", -1)
        self.hold_branch_lock(child.pid)
        self.hold_tree_lock("feature/mul", child.pid)
        code, out = run_cli(["stop"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("stopped pid %d" % child.pid, out)
        child.wait(timeout=10)
        self.assertFalse(os.path.isfile(self.tree_lock()))
        self.assertFalse(os.path.isfile(lock_path(self.rdir())))
        code, out = run_cli(["run", "--dry-run"])                                      # the tree is free again
        self.assertEqual(code, EXIT_OK, out)
        self.assertNotIn("already in progress", out)


class EveryCommandOpensWithTheIdentityLine(CrossBranchCase):
    def test_stop_reset_merge_wording_and_exit_codes_after_the_line(self):             # AC-4
        cases = ((["stop"], EXIT_OK, "no run in progress"),
                 (["reset"], EXIT_OK, "no state to remove"),
                 (["merge"], EXIT_ERROR, "ERROR: this branch is not ready to merge (stage: none); "
                                         "run `revali run` first"))
        for argv, exit_code, second in cases:
            with self.subTest(argv=argv):
                code, out = run_cli(argv)
                self.assertEqual(code, exit_code, out)
                lines = self.lines(out)
                self.assertEqual(lines[0], self.identity())
                self.assertEqual(lines[1], second)
                self.assertEqual(out.count("repo: "), 1)

    def test_clean_names_its_argument(self):                                           # AC-4
        code, out = run_cli(["clean", "feature/gone"])
        self.assertEqual(code, EXIT_ERROR, out)
        lines = self.lines(out)
        self.assertEqual(lines[0], self.identity("feature/gone"))
        self.assertEqual(lines[1], "nothing to clean for 'feature/gone'")
        self.state("needs_action", "changes requested", EXIT_ACTION)
        code, out = run_cli(["clean", "feature/mul"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertEqual(self.lines(out)[0], self.identity("feature/mul"))
        self.assertFalse(os.path.isdir(self.rdir()))

    def test_detached_head_is_an_error_line_not_a_traceback(self):                     # AC-4
        git(["checkout", "-q", "--detach"], self.repo)
        # `stop` left this list with feature/worktree-docs AC-8: it resolves the run through tree.lock
        for argv in (["status"], ["run", "--dry-run"], ["wait", "--timeout", "1s"], ["reset"],
                     ["merge"]):
            with self.subTest(argv=argv):
                code, out = run_cli(argv)
                self.assertEqual(code, EXIT_ERROR, out)
                self.assertTrue(any(l.startswith("ERROR: detached HEAD") for l in self.lines(out)), out)
                self.assertNotIn("Traceback", out)
                self.assertNotIn("repo: ", out)                                        # no identity line to print
                self.assertNotIn("branch: HEAD", out)

    def test_wait_branch_still_works_on_a_detached_head(self):                         # AC-3
        self.state("ready_to_merge", "validation 1 passed", EXIT_OK)
        git(["checkout", "-q", "--detach"], self.repo)
        code, out = run_cli(["wait", "--branch", "feature/mul", "--timeout", "1s"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertEqual(self.lines(out)[0], self.identity("feature/mul"))

    def test_outside_a_repository(self):                                               # AC-4
        os.chdir(self.tmp)
        for argv in (["status"], ["stop"], ["reset"], ["clean", "x"], ["merge"], ["run", "--dry-run"],
                     ["wait", "--timeout", "1s"]):
            with self.subTest(argv=argv):
                code, out = run_cli(argv)
                self.assertEqual(code, EXIT_ERROR, out)
                self.assertTrue(any(l.startswith("ERROR: not inside a git repository") for l in self.lines(out)),
                                out)
                self.assertNotIn("Traceback", out)
                self.assertNotIn("repo: ", out)


class HelpAndReadme(unittest.TestCase):
    def subparser(self, name):
        from revali.cli import build_parser
        parser = build_parser()
        for action in parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                return action.choices[name]
        self.fail("no subcommands")

    def test_wait_help_describes_branch(self):                                         # AC-5
        text = " ".join(self.subparser("wait").format_help().split())
        self.assertIn("--branch", text)
        self.assertIn("--timeout", text)                                               # the old option stays

    def test_stop_help_describes_the_tree_scope(self):                                 # AC-5
        text = " ".join(self.subparser("stop").format_help().split()).lower()
        self.assertIn("working tree", text)
        self.assertIn("branch", text)

    def test_readme(self):                                                             # AC-5
        with open(os.path.join(ROOT, "README.md"), "r", encoding="utf-8", newline="") as fh:
            readme = fh.read()

        def section(title):
            body = readme.split("\n## " + title + "\n", 1)[1]
            return body.split("\n## ", 1)[0]

        files = section("Files")
        self.assertTrue(any(l.startswith("| `tree.lock`") for l in files.splitlines()), "no tree.lock row")
        effects = section("What revali does to your repository")
        self.assertIn("tree.lock", effects)
        self.assertIn("HEAD", effects)                                                 # the mid-run check
        usage = section("Usage")
        self.assertIn("wait --branch", usage)
        self.assertIn("stop", usage)
        self.assertIn("working tree", usage)


if __name__ == "__main__":
    unittest.main()
