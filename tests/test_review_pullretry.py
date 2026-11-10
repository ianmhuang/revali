"""Acceptance tests for fix/primary-pull-retry: after `revali merge` in the primary tree, a
`git pull --prune` that fails is run once more after a pause (AC-1); two failures are named in
a note with git's error and the merge still succeeds (AC-2), the first error before the second
when the two differ (round 1, F1); a pull that times out or cannot be started is a failed
attempt, not a traceback (AC-3), and neither is a git that cannot be started elsewhere in the
follow-up (round 1, F2); a failed checkout of the base means no pull and a note of its own
(AC-4); worktree mode runs what it ran before (AC-5).

Black-box through `revali merge` with the fake gh, which merges nothing and leaves the tree on
feature/mul, so revali checks out main itself and then pulls. Non-zero exits come from a
stand-in set through REVALI_GIT_CMD that fails one subcommand and hands everything else to the
real git. A timeout (300 s in the code) and an executable that cannot start cannot be produced
that way, so for AC-3 `subprocess.run` raises what the operating system would, for the one
git command only."""

import json
import os
import shutil
import subprocess
import sys
from unittest import mock

from revali import EXIT_OK, gitops
from revali.config import history_path
from revali.state import State, read_history
from tests.helpers import RepoCase, _quote, git, run_cli

LOCK_ERROR = "error: cannot lock ref 'refs/remotes/origin/main': is at 3fca889 but expected a0c52e3"
LONG_ERROR = "fatal: unable to access 'https://git.example.invalid/sample.git/': " + "y" * 300
CHECKOUT_ERROR = "error: Your local changes to src/calc.py would be overwritten by checkout"
CONFLICT_ERROR = "CONFLICT (content): Merge conflict in src/calc.py"
UNMERGED_ERROR = "error: Pulling is not possible because you have unmerged files."

# Every git call is recorded (argv and wall-clock time); the first len(errors) calls of one
# subcommand exit 1, the n-th with errors[n] on stderr; the rest and every other command go to
# the real git.
STAND_IN = """import json, os, subprocess, sys, time
args = sys.argv[1:]
record = %(record)r
sub = %(sub)r
errors = %(errors)r
seen = 0
if os.path.exists(record):
    with open(record, encoding="utf-8") as fh:
        seen = sum(1 for line in fh if line.strip() and sub in json.loads(line)["argv"])
with open(record, "a", encoding="utf-8") as fh:
    fh.write(json.dumps({"argv": args, "at": time.time()}) + "\\n")
if sub in args and seen < len(errors):
    sys.stderr.write(errors[seen])
    sys.exit(1)
sys.exit(subprocess.call([%(git)r] + args))
"""


def same_path_in(text, path):
    def norm(value):
        return os.path.normcase(value).replace("\\", "/")

    return norm(os.path.normpath(path)) in norm(text)


class PullCase(RepoCase):
    """self.repo is the primary tree on feature/mul; main tracks a bare origin."""

    def setUp(self):
        super().setUp()
        os.environ["REVALI_POLL_SECONDS"] = "0.01"
        self.record = os.path.join(self.tmp, "git calls.jsonl")

    def ready(self, root=None):
        root = root or self.repo
        rdir = os.path.join(root, ".revali", "feature__mul")
        State(
            repo="owner/repo",
            branch="feature/mul",
            base="main",
            stage="ready_to_merge",
            message="validation 1 passed",
            last_exit=EXIT_OK,
            pr_number=7,
            head_sha=git(["rev-parse", "HEAD"], root).strip(),
            test_files=["tests/test_review_mul.py"],
        ).save(rdir)
        git(["push", "-q", "-u", "origin", "feature/mul"], root)
        return rdir

    def stand_in(self, sub, fails, stderr=""):
        """From now on revali's git fails its first `fails` calls of `sub`, each with `stderr`;
        a list of texts as `stderr` gives every failing call its own."""
        errors = [stderr] * fails if isinstance(stderr, str) else list(stderr)[:fails]
        script = os.path.join(self.tmp, "stand-in git.py")
        with open(script, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(
                STAND_IN
                % {
                    "record": self.record,
                    "sub": sub,
                    "errors": errors,
                    "git": shutil.which("git"),
                }
            )
        os.environ["REVALI_GIT_CMD"] = "%s %s" % (_quote(sys.executable), _quote(script))

    def recorded(self, sub):
        """The recorded git calls of `sub`, oldest first."""
        if not os.path.isfile(self.record):
            return []
        with open(self.record, "r", encoding="utf-8") as fh:
            calls = [json.loads(line) for line in fh if line.strip()]
        return [c for c in calls if sub in c["argv"]]

    def advance_origin(self):
        """A commit on the remote's main that this clone has not fetched (what the merged PR
        leaves there); returns its sha."""
        other = os.path.join(self.tmp, "second clone")
        git(["clone", "-q", "-b", "main", self.info["remote"], other], self.tmp)
        identity = ["-c", "user.email=fixture@example.invalid", "-c", "user.name=Fixture"]
        git(
            identity
            + ["-c", "commit.gpgsign=false", "commit", "-q", "--allow-empty", "-m", "merged"],
            other,
        )
        git(["push", "-q", "origin", "main"], other)
        return git(["rev-parse", "HEAD"], other).strip()

    def merged(self, rdir):
        """`revali merge` ended as a merge does: exit 0, MERGED, a history row, the state
        directory archived. Returns the output."""
        code, out = run_cli(["merge"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertNotIn("Traceback", out)
        self.assertIn("MERGED: PR #7 into main", out)
        self.assertFalse(os.path.isdir(rdir), "the state directory was not archived:\n" + out)
        rows = read_history(history_path())
        self.assertTrue(any(r.get("stage") == "merged" for r in rows), rows)
        return out

    def line_with(self, out, needle):
        lines = [line for line in out.splitlines() if needle in line]
        self.assertEqual(len(lines), 1, "expected one line with %r in:\n%s" % (needle, out))
        return lines[0]

    def local_main(self):
        return git(["rev-parse", "main"], self.repo).strip()


class RetriedPull(PullCase):
    """AC-1"""

    def test_one_failed_pull_is_run_again_after_a_pause_and_reads_like_a_success(self):
        tip = self.advance_origin()
        rdir = self.ready()
        self.stand_in("pull", fails=1, stderr=LOCK_ERROR + "\n")
        out = self.merged(rdir)
        pulls = self.recorded("pull")
        self.assertEqual(len(pulls), 2, out)  # run once more, and no third time
        for call in pulls:
            self.assertIn("--prune", call["argv"])  # the same pull both times
        self.assertGreaterEqual(pulls[1]["at"] - pulls[0]["at"], 1.0)  # after a pause
        # the output of a merge whose pull worked at once: no note, nothing of git's error
        self.assertNotIn("note:", out)
        self.assertNotIn("cannot lock ref", out)
        self.assertIn("branch feature/mul removed", self.line_with(out, "local: on main"))
        # and the second attempt did bring the tree up to date
        self.assertEqual(gitops.current_branch(self.repo), "main")
        self.assertEqual(self.local_main(), tip)

    def test_a_pull_that_works_is_not_run_twice(self):
        tip = self.advance_origin()
        rdir = self.ready()
        self.stand_in("pull", fails=0)
        out = self.merged(rdir)
        self.assertEqual(len(self.recorded("pull")), 1, out)
        self.assertNotIn("note:", out)
        self.assertEqual(self.local_main(), tip)


class FailedPull(PullCase):
    """AC-2"""

    def test_two_failures_are_one_note_with_gits_error_and_the_merge_succeeds(self):
        rdir = self.ready()
        self.stand_in("pull", fails=99, stderr=LONG_ERROR + "\n")
        out = self.merged(rdir)  # exit 0, MERGED, archived, history row
        self.assertEqual(len(self.recorded("pull")), 2, out)  # both attempts, no third
        line = self.line_with(out, "note: git pull --prune failed")
        # git's error, cut to 200 characters
        self.assertIn(LONG_ERROR[:200], line)
        self.assertNotIn(LONG_ERROR[:201], line)
        # the repository root and, after the error, the advice to run `git pull`
        self.assertTrue(same_path_in(line, gitops.repo_root(self.repo)), line)
        self.assertIn("git pull", line.split(LONG_ERROR[:200], 1)[1])
        self.assertEqual(gitops.current_branch(self.repo), "main")

    def test_a_remote_that_is_gone_is_reported_in_gits_own_words(self):
        rdir = self.ready()
        git(["remote", "set-url", "origin", os.path.join(self.tmp, "no-such-origin")], self.repo)
        self.stand_in("pull", fails=0)  # records only: both failures are the real git's
        out = self.merged(rdir)
        self.assertEqual(len(self.recorded("pull")), 2, out)
        line = self.line_with(out, "note: git pull --prune failed")
        self.assertIn("no-such-origin", line)  # git names the remote it could not read
        self.assertTrue(same_path_in(line, gitops.repo_root(self.repo)), line)

    def test_two_different_errors_are_both_in_the_note_the_first_one_first(self):
        # a first attempt that stops in a conflict makes the second fail for that reason; the
        # note must still carry the cause (round 1, F1)
        rdir = self.ready()
        self.stand_in("pull", fails=2, stderr=[CONFLICT_ERROR + "\n", UNMERGED_ERROR + "\n"])
        out = self.merged(rdir)
        self.assertEqual(len(self.recorded("pull")), 2, out)
        line = self.line_with(out, "note: git pull --prune failed")
        self.assertIn(CONFLICT_ERROR, line)
        self.assertIn(UNMERGED_ERROR, line)
        self.assertLess(line.index(CONFLICT_ERROR), line.index(UNMERGED_ERROR))
        self.assertTrue(same_path_in(line, gitops.repo_root(self.repo)), line)
        self.assertIn("git pull", line.split(UNMERGED_ERROR, 1)[1])


class PullThatCannotRun(PullCase):
    """AC-3"""

    def failing_pull(self, errors):
        """revali's `git pull` raises what the operating system would on its first
        len(errors) attempts ("timeout", "missing" or "denied") without running; later
        attempts and every other command run for real. Returns the list the attempts are
        recorded in."""
        real = subprocess.run
        pending = list(errors)
        attempts = []

        def fake(cmd, *args, **kw):
            argv = [str(c) for c in cmd] if isinstance(cmd, (list, tuple)) else [str(cmd)]
            if "pull" in argv[1:]:
                attempts.append(argv)
                if pending:
                    kind = pending.pop(0)
                    if kind == "timeout":
                        raise subprocess.TimeoutExpired(argv, kw.get("timeout"))
                    if kind == "denied":
                        raise PermissionError(13, "Permission denied", argv[0])
                    raise FileNotFoundError(2, "No such file or directory", argv[0])
            return real(cmd, *args, **kw)

        patcher = mock.patch("subprocess.run", side_effect=fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        return attempts

    def test_one_timeout_is_retried_and_leaves_no_note(self):
        tip = self.advance_origin()
        rdir = self.ready()
        attempts = self.failing_pull(["timeout"])
        out = self.merged(rdir)
        self.assertEqual(len(attempts), 2, out)
        self.assertNotIn("note:", out)
        self.assertEqual(self.local_main(), tip)

    def test_two_timeouts_are_the_note_with_the_reason(self):
        rdir = self.ready()
        attempts = self.failing_pull(["timeout", "timeout"])
        out = self.merged(rdir)  # no exception, exit 0, history row, archived
        self.assertEqual(len(attempts), 2, out)
        line = self.line_with(out, "note: git pull --prune failed")
        self.assertIn("timed out", line)
        self.assertTrue(same_path_in(line, gitops.repo_root(self.repo)), line)
        self.assertIn("branch feature/mul removed", self.line_with(out, "local: on main"))

    def test_a_git_that_cannot_be_started_is_the_note_with_the_reason(self):
        rdir = self.ready()
        attempts = self.failing_pull(["missing", "missing"])
        out = self.merged(rdir)
        self.assertEqual(len(attempts), 2, out)
        line = self.line_with(out, "note: git pull --prune failed")
        self.assertIn("No such file or directory", line)
        self.assertTrue(same_path_in(line, gitops.repo_root(self.repo)), line)

    def test_a_git_the_system_refuses_to_start_is_the_note_with_the_reason(self):
        # a spawn error other than a missing file (round 1, F2)
        rdir = self.ready()
        attempts = self.failing_pull(["denied", "denied"])
        out = self.merged(rdir)
        self.assertEqual(len(attempts), 2, out)
        line = self.line_with(out, "note: git pull --prune failed")
        self.assertIn("Permission denied", line)


class FollowUpThatCannotRun(PullCase):
    """AC-3's "instead of a traceback", for the git calls around the pull (round 1, F2)."""

    def test_git_gone_after_the_pr_merged_is_a_note_and_the_merge_is_recorded(self):
        rdir = self.ready()
        real = subprocess.run
        merged = []
        refused = []

        def fake(cmd, *args, **kw):
            argv = [str(c) for c in cmd] if isinstance(cmd, (list, tuple)) else [str(cmd)]
            # once gh has merged the PR, git cannot be started to ask which branch the tree
            # is on: the first thing the local follow-up does
            if merged and "--abbrev-ref" in argv:
                refused.append(argv)
                raise FileNotFoundError(2, "No such file or directory", argv[0])
            res = real(cmd, *args, **kw)
            if "pr" in argv and "merge" in argv:  # the fake gh's `pr merge`
                merged.append(argv)
            return res

        patcher = mock.patch("subprocess.run", side_effect=fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        out = self.merged(rdir)  # no exception, exit 0, MERGED, history row, archived
        self.assertTrue(merged and refused, out)
        line = self.line_with(out, "note:")
        self.assertIn("No such file or directory", line)


class FailedCheckout(PullCase):
    """AC-4"""

    def test_no_pull_runs_and_the_note_says_where_the_tree_is(self):
        rdir = self.ready()
        self.stand_in("checkout", fails=99, stderr=CHECKOUT_ERROR + "\n")
        out = self.merged(rdir)  # exit 0, MERGED, archived, history row
        checkouts = self.recorded("checkout")
        self.assertTrue(checkouts and "main" in checkouts[0]["argv"], checkouts)
        self.assertEqual(self.recorded("pull"), [], out)  # no pull after a failed checkout
        self.assertNotIn("pull --prune failed", out)
        line = self.line_with(out, "note:")
        self.assertIn("checkout", line)  # names the failed checkout
        self.assertIn(CHECKOUT_ERROR, line)  # with git's error
        self.assertIn("feature/mul", line)  # the branch the tree is still on
        advice = line.split(CHECKOUT_ERROR, 1)[1]
        self.assertIn("main", advice)  # check out the base
        self.assertIn("pull", advice)  # and pull
        # the local branch is kept, as whenever the tree is not on the base
        self.assertIn("branch feature/mul kept", self.line_with(out, "local: on feature/mul"))
        self.assertEqual(gitops.current_branch(self.repo), "feature/mul")
        self.assertIsNotNone(gitops.rev_parse("feature/mul", self.repo))


class WorktreeModeUnchanged(PullCase):
    """AC-5: a regression guard; it passes on the base branch too."""

    def test_same_commands_and_lines_as_before(self):
        git(["checkout", "-q", "main"], self.repo)
        wt = os.path.join(self.tmp, "wt-mul")
        git(["worktree", "add", "--quiet", wt, "feature/mul"], self.repo)

        def drop():
            os.chdir(self.repo)
            subprocess.run(
                ["git", "worktree", "remove", "--force", wt], cwd=self.repo, capture_output=True
            )

        self.addCleanup(drop)
        os.chdir(wt)
        self.ready(wt)
        self.stand_in("pull", fails=0)  # records only
        code, out = run_cli(["merge"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("MERGED: PR #7 into main", out)
        self.assertEqual(self.recorded("pull"), [], out)
        self.assertNotIn("note:", out)
        with open(self.record, "r", encoding="utf-8") as fh:
            calls = [json.loads(line)["argv"] for line in fh if line.strip()]
        # the follow-up docs/side-effects.md lists for a linked worktree, once each
        self.assertEqual(
            [argv for argv in calls if argv[0] in ("push", "fetch", "checkout", "pull")],
            [
                ["push", "--quiet", "origin", "--delete", "feature/mul"],
                ["fetch", "--quiet", "--prune", "origin", "main"],
                ["checkout", "--quiet", "--detach", "FETCH_HEAD"],
            ],
        )
        line = self.line_with(out, "worktree: detached at the merged main")
        self.assertIn("local branch feature/mul removed", line)
        self.assertIn("git worktree remove", line)
        self.assertEqual(gitops.current_branch(wt), "HEAD")
