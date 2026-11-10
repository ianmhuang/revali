"""fix/primary-pull-retry (M3.4.11): a merge in the primary tree retries its `git pull --prune`
once (AC-1), names a pull that still fails (AC-2) or cannot run (AC-3) without failing the
merge, does not pull after a failed checkout of the base (AC-4), and worktree mode is unchanged
(AC-5); the docs say so (AC-6).

Black-box through the CLI with the fake gh and real git. Where git cannot be made to fail the
way a test needs (a transient failure, a timeout, a failed checkout), the one command is
answered by a stand-in and every other command runs for real."""

import os
import unittest
from unittest import mock

from revali import EXIT_OK, gitops, procs
from revali.config import history_path
from revali.procs import ExeNotFound, ProcTimeout, Result
from revali.state import read_history
from tests.helpers import ROOT, git, run_cli
from tests.test_review_mergestop import MergeCase


def is_git(cmd, sub):
    return sub in cmd and any(os.path.basename(str(c)).startswith("git") for c in cmd[:2])


class GitStandIn:
    """Wraps `procs.run` (used by `run_retry`) and `merge.run`: records every git command and
    answers the n-th call of one subcommand from `answers` (a Result, or an exception to
    raise); anything else runs for real."""

    def __init__(self, case, sub, answers):
        self.sub = sub
        self.answers = list(answers)
        self.seen = []
        self.calls = []
        real = procs.run

        def fake(cmd, *a, **kw):
            self.calls.append([str(c) for c in cmd])
            if is_git(cmd, sub):
                self.seen.append(cmd)
                if self.answers:
                    answer = self.answers.pop(0)
                    if isinstance(answer, BaseException):
                        raise answer
                    if answer is not None:
                        return answer
            return real(cmd, *a, **kw)

        for target in ("revali.procs.run", "revali.merge.run"):
            patcher = mock.patch(target, side_effect=fake)
            patcher.start()
            case.addCleanup(patcher.stop)

    def git_subcommands(self):
        return [c for c in self.calls if any(os.path.basename(x).startswith("git") for x in c[:2])]


def failed(cmd, text):
    return Result(cmd=["git", cmd], returncode=1, stdout="", stderr=text, duration=0.0)


class PrimaryTreeCase(MergeCase):
    def merged(self, rdir=None):
        rdir = rdir or self.ready(self.repo)
        code, out = run_cli(["merge"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertNotIn("Traceback", out)
        self.assertIn("MERGED: PR #7 into main", out)
        # archived as for any merge, and a history row
        self.assertFalse(os.path.isdir(rdir), "state directory not archived")
        self.assertTrue(any(r.get("stage") == "merged" for r in read_history(history_path())))
        return out

    def notes(self, out):
        return [line for line in out.splitlines() if "note:" in line]


class PullRetried(PrimaryTreeCase):
    """AC-1"""

    def test_a_second_attempt_that_works_looks_like_today(self):
        git_ = GitStandIn(self, "pull", [failed("pull", "error: cannot lock ref 'refs/remotes/x'")])
        out = self.merged()
        self.assertEqual(len(git_.seen), 2)
        self.assertEqual(self.notes(out), [])
        self.assertIn("branch feature/mul removed", self.line_with(out, "local: on main"))

    def test_one_attempt_when_it_works(self):
        git_ = GitStandIn(self, "pull", [])
        out = self.merged()
        self.assertEqual(len(git_.seen), 1)
        self.assertEqual(self.notes(out), [])


class PullFails(PrimaryTreeCase):
    """AC-2: a real failure, an origin that is gone."""

    def test_named_with_gits_error_and_the_merge_still_succeeds(self):
        rdir = self.ready(self.repo)
        git(["remote", "set-url", "origin", os.path.join(self.tmp, "no-such-origin")], self.repo)
        out = self.merged(rdir)
        line = self.line_with(out, "note: git pull --prune failed")
        self.assertIn("no-such-origin", line)  # git's own words name the remote
        self.assertIn("run `git pull` there", line)
        self.assertIn(gitops.repo_root(self.repo).replace("\\", "/"), line.replace("\\", "/"))
        self.assertEqual(gitops.current_branch(self.repo), "main")

    def test_gits_error_is_cut_to_200_characters(self):
        GitStandIn(self, "pull", [failed("pull", "x" * 300), failed("pull", "y" * 300)])
        out = self.merged()
        line = self.line_with(out, "note: git pull --prune failed")
        self.assertIn("y" * 200, line)
        self.assertNotIn("y" * 201, line)


class PullCannotRun(PrimaryTreeCase):
    """AC-3"""

    def test_a_timeout_is_retried(self):
        git_ = GitStandIn(self, "pull", [ProcTimeout("timed out after 300s: git")])
        out = self.merged()
        self.assertEqual(len(git_.seen), 2)
        self.assertEqual(self.notes(out), [])

    def test_two_timeouts_give_the_note(self):
        GitStandIn(self, "pull", [ProcTimeout("timed out after 300s: git")] * 2)
        out = self.merged()
        line = self.line_with(out, "note: git pull --prune failed")
        self.assertIn("timed out after 300s", line)

    def test_git_that_cannot_start(self):
        GitStandIn(self, "pull", [ExeNotFound("cannot execute git: gone")] * 2)
        out = self.merged()
        self.assertIn("cannot execute git", self.line_with(out, "note: git pull --prune failed"))


class CheckoutFails(PrimaryTreeCase):
    """AC-4: the fake gh leaves the tree on feature/mul, so revali checks out main itself."""

    def test_no_pull_and_the_note_says_where_the_tree_is(self):
        git_ = GitStandIn(
            self, "checkout", [failed("checkout", "error: your local changes would be overwritten")]
        )
        out = self.merged()
        self.assertEqual(len(git_.seen), 1)
        self.assertFalse([c for c in git_.git_subcommands() if "pull" in c], git_.calls)
        line = self.line_with(out, "note: git checkout main failed")
        self.assertIn("your local changes would be overwritten", line)
        self.assertIn("still on feature/mul", line)
        self.assertIn("check out main there and run `git pull`", line)
        self.assertNotIn("git pull --prune failed", out)
        # the branch is kept, as today when the tree is not on the base
        self.assertIn("branch feature/mul kept", self.line_with(out, "local: on feature/mul"))
        self.assertEqual(gitops.current_branch(self.repo), "feature/mul")

    def test_checkout_that_cannot_run(self):
        GitStandIn(self, "checkout", [ProcTimeout("timed out after 60s: git")])
        out = self.merged()
        self.assertIn("timed out after 60s", self.line_with(out, "note: git checkout main failed"))


class WorktreeModeUnchanged(MergeCase):
    """AC-5: no pull, no checkout of the base, no note."""

    def test_same_commands_and_lines(self):
        git(["checkout", "-q", "main"], self.repo)
        wt = self.add_worktree("feature/mul", "wt-feature")
        os.chdir(wt)
        self.ready(wt)
        git_ = GitStandIn(self, "pull", [])
        code, out = run_cli(["merge"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertEqual(git_.seen, [])
        self.assertNotIn("note:", out)
        self.line_with(out, "worktree: detached at the merged main")


class RunRetry(unittest.TestCase):
    """`procs.run_retry(retry_errors=True)`, which the pull goes through."""

    def test_an_error_then_success(self):
        ok = Result(cmd=["x"], returncode=0, stdout="", stderr="", duration=0.0)
        calls = []

        def fake(cmd, **_kw):
            calls.append(cmd)
            if len(calls) == 1:
                raise ProcTimeout("t")
            return ok

        with mock.patch("revali.procs.run", side_effect=fake):
            self.assertIs(procs.run_retry(["x"], wait=0, retry_errors=True), ok)
        self.assertEqual(len(calls), 2)

    def test_the_last_error_is_raised(self):
        with mock.patch("revali.procs.run", side_effect=[ProcTimeout("a"), ExeNotFound("b")]):
            with self.assertRaises(ExeNotFound):
                procs.run_retry(["x"], wait=0, retry_errors=True)

    def test_without_retry_errors_an_error_is_raised_at_once(self):
        with mock.patch("revali.procs.run", side_effect=[ProcTimeout("a")]) as run:
            with self.assertRaises(ProcTimeout):
                procs.run_retry(["x"], wait=0)
        self.assertEqual(run.call_count, 1)


class Docs(unittest.TestCase):
    """AC-6"""

    def read(self, name):
        with open(os.path.join(ROOT, "docs", name), encoding="utf-8") as fh:
            return " ".join(fh.read().split())

    def test_workflow_md(self):
        text = self.read("workflow.md")
        self.assertNotIn("which is not retried", text)
        self.assertIn("from the primary tree its `git pull --prune` is retried the same way", text)

    def test_side_effects_md(self):
        text = self.read("side-effects.md")
        self.assertIn("then `git pull --prune`, tried twice", text)
        self.assertIn("not up to date", text)


if __name__ == "__main__":
    unittest.main()
