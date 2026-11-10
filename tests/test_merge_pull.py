"""fix/primary-pull-retry (M3.4.11): a merge in the primary tree retries its `git pull --prune`
once (AC-1), names a pull that still fails (AC-2) or cannot run (AC-3) without failing the
merge, does not pull after a failed checkout of the base (AC-4), and worktree mode is unchanged
(AC-5); the docs say so (AC-6).

Black-box through the CLI with the fake gh and real git. Where git cannot be made to fail the
way a test needs (a transient failure, a timeout, a failed checkout), the one command is
answered by a stand-in and every other command runs for real."""

import os
import subprocess
import unittest
from unittest import mock

from revali import EXIT_OK, gitops, procs
from revali.config import history_path
from revali.procs import ExeNotFound, ProcTimeout, Result
from revali.state import State, read_history
from tests.helpers import ROOT, RepoCase, git, run_cli


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

    def add_worktree(self, branch, name):
        path = os.path.join(self.tmp, name)
        git(["worktree", "add", "--quiet", path, branch], self.repo)

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


def is_git(cmd, sub):
    return sub in cmd and any(os.path.basename(str(c)).startswith("git") for c in cmd[:2])


class GitStandIn:
    """Wraps `procs.run` and `merge.run`: records every git command and
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
        self.assertIn("run `git pull` again", line)
        self.assertNotIn("then:", line)  # the same error twice is named once
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


class FirstErrorKept(PrimaryTreeCase):
    """PR #64 F1: the first attempt's error is the cause when the second fails for another
    reason."""

    def test_both_errors_when_they_differ(self):
        GitStandIn(
            self,
            "pull",
            [
                failed("pull", "CONFLICT (content): Merge conflict in src/calc.py"),
                failed("pull", "error: Pulling is not possible because you have unmerged files."),
            ],
        )
        out = self.merged()
        line = self.line_with(out, "note: git pull --prune failed")
        first = line.index("CONFLICT (content)")
        self.assertLess(first, line.index("then: error: Pulling is not possible"))
        self.assertIn("`git status`", line)

    def test_a_real_conflict(self):
        # local main has a commit origin's main conflicts with; `pull.rebase false` makes the
        # pull merge (newer git refuses divergent branches without a setting, before merging)
        rdir = self.ready(self.repo)
        other = os.path.join(self.tmp, "second clone")
        git(["clone", "-q", "-b", "main", self.info["remote"], other], self.tmp)
        for where, text in ((other, "origin\n"), (self.repo, "local\n")):
            if where == self.repo:
                git(["checkout", "-q", "main"], where)
            with open(os.path.join(where, "clash.txt"), "w", encoding="utf-8") as fh:
                fh.write(text)
            git(["add", "clash.txt"], where)
            git(
                [
                    "-c",
                    "user.email=t@example.invalid",
                    "-c",
                    "user.name=T",
                    "-c",
                    "commit.gpgsign=false",
                    "commit",
                    "-q",
                    "-m",
                    "clash",
                ],
                where,
            )
        git(["push", "-q", "origin", "main"], other)
        git(["checkout", "-q", "feature/mul"], self.repo)
        git(["config", "pull.rebase", "false"], self.repo)
        out = self.merged(rdir)
        line = self.line_with(out, "note: git pull --prune failed")
        self.assertIn("then:", line)  # the second attempt failed for another reason


class FollowUpStops(PrimaryTreeCase):
    """PR #64 F2: git failing anywhere in the primary tree's follow-up is a note, not a
    traceback after the PR merged."""

    def test_current_branch_fails_after_the_merge(self):
        real = gitops.current_branch

        def current_branch(cwd):
            if self.gh_calls("pr", "merge"):
                raise gitops.GitError("git rev-parse --abbrev-ref HEAD failed: index broken")
            return real(cwd)

        with mock.patch("revali.gitops.current_branch", side_effect=current_branch):
            out = self.merged()
        line = self.line_with(out, "note: the local follow-up stopped")
        self.assertIn("index broken", line)
        self.assertIn("run `git pull` there", line)

    def test_a_spawn_error_other_than_a_missing_file(self):
        GitStandIn(self, "pull", [PermissionError(13, "Access is denied")] * 2)
        out = self.merged()
        self.assertIn("Access is denied", self.line_with(out, "note: git pull --prune failed"))


class Docs(unittest.TestCase):
    """AC-6"""

    def read(self, name):
        with open(os.path.join(ROOT, "docs", name), encoding="utf-8") as fh:
            return " ".join(fh.read().split())

    def test_workflow_md(self):
        text = self.read("workflow.md")
        self.assertNotIn("which is not retried", text)
        self.assertIn("and from the primary tree its `git pull --prune`", text)
        self.assertIn("both errors when the two attempts differ", text)

    def test_side_effects_md(self):
        text = self.read("side-effects.md")
        self.assertIn("then `git pull --prune`, tried twice", text)
        self.assertIn("not up to date", text)


if __name__ == "__main__":
    unittest.main()
