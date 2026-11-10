"""Acceptance tests for fix/shared-repo-fetch-and-issues, the fetch side: a `git fetch` of the
base that fails is run once more after a pause, in preflight (AC-1, AC-2) and in the follow-up
of `revali merge` from a linked worktree (AC-3).

Black-box through `revali preflight` and `revali merge`. git is replaced through
REVALI_GIT_CMD by a stand-in that fails the first N fetches with what Git for Windows prints
when another worktree moved the ref first, and hands everything else to the real git."""

import json
import os
import shutil
import subprocess
import sys

from revali import EXIT_ACTION, EXIT_OK
from revali.state import State
from tests.helpers import RepoCase, _quote, git, run_cli

LOCK_ERROR = "error: cannot lock ref 'refs/remotes/origin/main': is at 3fca889 but expected a0c52e3"
SECOND_LINE = " ! a0c52e3..3fca889  main -> origin/main  (unable to update local ref)"

# Every fetch is recorded (argv and wall-clock time) before it fails or is forwarded.
FLAKY_GIT = """import json, os, subprocess, sys, time
args = sys.argv[1:]
if "fetch" in args:
    record = %(record)r
    seen = 0
    if os.path.exists(record):
        with open(record, encoding="utf-8") as fh:
            seen = sum(1 for line in fh if line.strip())
    with open(record, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"argv": args, "at": time.time()}) + "\\n")
    if seen < %(fails)d:
        sys.stderr.write(%(stderr)r)
        sys.exit(1)
sys.exit(subprocess.call([%(git)r] + args))
"""


class FetchCase(RepoCase):
    def flaky_git(self, fails):
        """From now on revali's git fails its first `fails` fetches."""
        self.fetch_record = os.path.join(self.tmp, "fetches.jsonl")
        script = os.path.join(self.tmp, "flaky git.py")
        with open(script, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(
                FLAKY_GIT
                % {
                    "record": self.fetch_record,
                    "fails": fails,
                    "stderr": LOCK_ERROR + "\n" + SECOND_LINE + "\n",
                    "git": shutil.which("git"),
                }
            )
        os.environ["REVALI_GIT_CMD"] = "%s %s" % (_quote(sys.executable), _quote(script))

    def base_fetches(self):
        """The recorded fetches of `origin main`, oldest first."""
        if not os.path.isfile(self.fetch_record):
            return []
        with open(self.fetch_record, "r", encoding="utf-8") as fh:
            calls = [json.loads(line) for line in fh if line.strip()]
        return [c for c in calls if "origin" in c["argv"] and "main" in c["argv"]]

    def advance_origin(self):
        """A commit on the remote's main that this clone has not fetched; returns its sha.
        feature/mul still descends from the old tip: behind origin/main once that is fetched,
        not behind the local main."""
        other = os.path.join(self.tmp, "second clone")
        # -b: the fixture's bare remote may have a HEAD that names no branch
        git(["clone", "-q", "-b", "main", self.info["remote"], other], self.tmp)
        identity = ["-c", "user.email=fixture@example.invalid", "-c", "user.name=Fixture"]
        git(
            identity
            + ["-c", "commit.gpgsign=false", "commit", "-q", "--allow-empty", "-m", "moved"],
            other,
        )
        git(["push", "-q", "origin", "main"], other)
        return git(["rev-parse", "HEAD"], other).strip()


class PreflightFetchRetry(FetchCase):
    def test_one_failed_fetch_is_tried_again_after_a_pause_and_leaves_no_note(self):
        self.flaky_git(fails=1)
        code, out = run_cli(["preflight"])
        fetches = self.base_fetches()
        self.assertEqual(len(fetches), 2, out)  # AC-1: run once more
        # AC-1: after a pause (the retry that worked in the Request's measurement waited 1 s)
        self.assertGreaterEqual(fetches[1]["at"] - fetches[0]["at"], 1.0)
        self.assertEqual(code, EXIT_OK, out)
        self.assertNotIn("could not fetch", out)  # AC-1: no fetch note

    def test_the_retry_compares_against_the_fetched_origin_base(self):
        tip = self.advance_origin()
        self.flaky_git(fails=1)
        code, out = run_cli(["preflight"])
        self.assertEqual(len(self.base_fetches()), 2, out)
        # AC-1: the comparison is against origin/main as the second fetch left it; against the
        # local main the branch would not be behind
        self.assertEqual(code, EXIT_ACTION, out)
        self.assertIn("branch is behind origin/main", out)
        self.assertEqual(git(["rev-parse", "origin/main"], self.repo).strip(), tip)
        self.assertNotIn("could not fetch", out)

    def test_two_failed_fetches_note_the_local_base_and_gits_first_line(self):
        self.advance_origin()
        self.flaky_git(fails=99)
        code, out = run_cli(["preflight"])
        self.assertEqual(len(self.base_fetches()), 2, out)  # AC-2: both attempts, no third
        notes = [line for line in out.splitlines() if "could not fetch origin/main" in line]
        self.assertEqual(len(notes), 1, out)
        self.assertIn("comparing against local main", notes[0])  # AC-2: still says so
        self.assertIn(LOCK_ERROR, notes[0])  # AC-2: the first line of git's error output
        self.assertNotIn("unable to update local ref", out)  # AC-2: only the first line
        # AC-2: the run goes on as today, against the local main the branch descends from
        self.assertEqual(code, EXIT_OK, out)
        self.assertNotIn("branch is behind", out)


class MergeFetchRetry(FetchCase):
    """AC-3: `revali merge` in a linked worktree while the primary tree holds main."""

    def setUp(self):
        super().setUp()
        os.environ["REVALI_POLL_SECONDS"] = "0.01"
        git(["checkout", "-q", "main"], self.repo)
        self.wt = os.path.join(self.tmp, "wt-mul")
        git(["worktree", "add", "--quiet", self.wt, "feature/mul"], self.repo)
        self.addCleanup(self.drop_worktree)
        os.chdir(self.wt)
        git(["push", "-q", "-u", "origin", "feature/mul"], self.wt)
        State(
            repo="owner/repo",
            branch="feature/mul",
            base="main",
            stage="ready_to_merge",
            message="validation 1 passed",
            last_exit=EXIT_OK,
            pr_number=7,
            head_sha=git(["rev-parse", "HEAD"], self.wt).strip(),
            test_files=["tests/test_review_mul.py"],
        ).save(os.path.join(self.wt, ".revali", "feature__mul"))

    def drop_worktree(self):
        os.chdir(self.repo)
        subprocess.run(
            ["git", "worktree", "remove", "--force", self.wt], cwd=self.repo, capture_output=True
        )

    def test_a_retried_fetch_ends_detached_at_the_merged_base(self):
        merged_tip = self.advance_origin()  # what the merge left on the remote's main
        self.flaky_git(fails=1)
        code, out = run_cli(["merge"])
        self.assertEqual(code, EXIT_OK, out)
        fetches = self.base_fetches()
        self.assertEqual(len(fetches), 2, out)  # AC-3: retried
        for call in fetches:
            self.assertIn("--prune", call["argv"])  # AC-3: the same fetch both times
        self.assertGreaterEqual(fetches[1]["at"] - fetches[0]["at"], 1.0)  # after a pause
        # AC-3: the end state of a merge whose fetch did not collide
        self.assertNotIn("git fetch failed", out)
        self.assertIn("detached at the merged main", out)
        self.assertEqual(git(["rev-parse", "--abbrev-ref", "HEAD"], self.wt).strip(), "HEAD")
        self.assertEqual(git(["rev-parse", "HEAD"], self.wt).strip(), merged_tip)
        branches = git(["branch", "--list", "feature/mul"], self.wt).strip()
        self.assertEqual(branches, "", out)  # the local branch went, as without a collision

    def test_a_fetch_that_fails_twice_is_reported_once_and_not_tried_a_third_time(self):
        self.flaky_git(fails=99)
        code, out = run_cli(["merge"])
        self.assertEqual(code, EXIT_OK, out)  # the PR is merged; that is the result
        self.assertEqual(len(self.base_fetches()), 2, out)  # AC-3: one retry
        self.assertIn("note: git fetch failed: %s" % LOCK_ERROR, out)
        self.assertIn("still on feature/mul", out)
        self.assertNotIn("detached at", out)
