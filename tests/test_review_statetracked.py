"""Review of feature/state-dir-untracked: preflight stops with exit 2 while git tracks a file
under the state directory and says how to fix it (AC-1), before anything leaves the machine
(AC-2), and only for files git tracks under that very directory (AC-3).

Black box: the `revali` commands run in the fixture repository with the fake gh and claude;
git is real. Every test holds a refusal, so each fails on a base without the check; the
AC-3 tests first show that preflight passes, then track one file to show the check was live.

Round 2: a removal that is staged but not committed leaves the files in HEAD, which is what
a push carries, so it is refused as well; once the removal is committed preflight passes.

Round 3: with the whole removal staged the refusal asks for the commit only; while a file is
still in the index it asks for the removal.
"""

import os
import re

from revali.state import State
from tests.helpers import RepoCase, git, run_cli

EXIT_OK = 0
EXIT_ACTION = 2  # the author must act
CHANGE = ".revali/feature__mul/change.md"


class TrackedCase(RepoCase):
    def track(self, rel, text="x\n", commit=True, force=True):
        self.write(rel, text)
        git(["add"] + (["-f"] if force else []) + ["--", rel], self.repo)
        if commit:
            git(["commit", "-q", "-m", "track %s" % rel], self.repo)

    def assert_refused(self, code, out, listed, state_dir=".revali"):
        self.assertEqual(code, EXIT_ACTION, out)
        for rel in listed:
            self.assertIn(rel, out)
        self.assertIn("git rm -r --cached %s" % state_dir, out)
        self.assertIn("commit", out)
        self.assertIn(".gitignore", out)

    def assert_passes(self):
        code, out = run_cli(["preflight"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertNotIn("git rm", out)


class Refused(TrackedCase):
    """AC-1"""

    def test_a_file_the_branch_committed(self):
        self.track(CHANGE, self.read(CHANGE))
        code, out = run_cli(["preflight"])
        self.assert_refused(code, out, [CHANGE])

    def test_a_file_the_base_already_had(self):
        old = ".revali/old__topic/change.md"
        git(["checkout", "-q", "main"], self.repo)
        self.track(old)
        git(["push", "-q", "origin", "main"], self.repo)
        git(["checkout", "-q", "feature/mul"], self.repo)
        git(["rebase", "-q", "main"], self.repo)
        # the branch itself did not add it
        added = git(["diff", "--name-only", "main", "HEAD", "--", ".revali"], self.repo)
        self.assertEqual(added, "")
        code, out = run_cli(["preflight"])
        self.assert_refused(code, out, [old])

    def test_a_file_that_is_only_staged(self):
        self.track(".revali/feature__mul/notes.md", commit=False)
        code, out = run_cli(["preflight"])
        self.assert_refused(code, out, [".revali/feature__mul/notes.md"])

    def test_a_removal_staged_but_not_committed_is_still_refused(self):
        # half of the fix the message names: the index is empty, HEAD still carries the file
        stray = ".revali/feature__mul/stray.md"
        self.track(stray)
        git(["rm", "-r", "-q", "--cached", "--", ".revali"], self.repo)
        self.assertEqual(git(["ls-files", "--", ".revali"], self.repo), "")
        code, out = run_cli(["preflight"])
        self.assert_refused(code, out, [stray])
        # round 3: nothing is left in the index, so the message says the removal is staged
        # and asks for the commit, and does not ask for a removal that is already done
        self.assertRegex(out, r"staged but not committed")
        self.assertNotRegex(out, r"(?i)remove them from the index")
        # the other half: committed, nothing is tracked there any more
        git(["commit", "-q", "-m", "untrack the state directory"], self.repo)
        self.assertTrue(self.exists(stray))  # --cached left the file on disk
        self.assert_passes()

    def test_a_partly_staged_removal_lists_the_files_of_index_and_head(self):
        kept = ".revali/feature__mul/kept.md"
        removed = ".revali/feature__mul/removed.md"
        self.track(kept)
        self.track(removed)
        git(["rm", "-q", "--cached", "--", removed], self.repo)
        code, out = run_cli(["preflight"])
        self.assert_refused(code, out, [kept, removed])
        # round 3: one file is still in the index, so the removal is still what is asked for
        self.assertNotRegex(out, r"staged but not committed")
        # what the message asks for, done: the index is empty, the commit is what is left
        git(["rm", "-r", "-q", "--cached", "--", ".revali"], self.repo)
        code, out = run_cli(["preflight"])
        self.assert_refused(code, out, [kept, removed])
        self.assertRegex(out, r"staged but not committed")
        git(["commit", "-q", "-m", "untrack the state directory"], self.repo)
        self.assert_passes()

    def many(self, count):
        for i in range(count):
            self.write(".revali/many/f%02d.txt" % i, "x\n")
        git(["add", "-f", "--", ".revali/many"], self.repo)

    def listed(self, out):
        return set(re.findall(r"\.revali/many/f\d\d\.txt", out))

    def test_twenty_files_are_listed_and_the_rest_counted(self):
        # exactly twenty: all of them, and no count of more
        self.many(20)
        code, out = run_cli(["preflight"])
        self.assert_refused(code, out, [])
        self.assertEqual(len(self.listed(out)), 20, out)
        self.assertNotRegex(out, r"\b\d+ more\b")
        # one over, then three over: still twenty names, and how many are left out
        for count in (21, 23):
            with self.subTest(files=count):
                self.many(count)
                code, out = run_cli(["preflight"])
                self.assert_refused(code, out, [])
                self.assertEqual(len(self.listed(out)), 20, out)
                self.assertRegex(out, r"\b%d more\b" % (count - 20))


class NothingLeavesTheMachine(TrackedCase):
    """AC-2"""

    def setUp(self):
        super().setUp()
        # origin's main moves ahead of the local remote-tracking ref, so a fetch of the base
        # would move that ref and is seen
        self.tracking = self.origin_main()
        git(["push", "-q", "origin", "feature/mul:refs/heads/main"], self.repo)
        git(["update-ref", "refs/remotes/origin/main", self.tracking], self.repo)
        self.remote_heads = self.heads_on_origin()
        self.assertNotIn("feature/mul", self.remote_heads)

    def origin_main(self):
        return git(["rev-parse", "refs/remotes/origin/main"], self.repo).strip()

    def heads_on_origin(self):
        return git(["ls-remote", "--heads", "origin"], self.repo)

    def assert_nothing_left(self):
        self.assertEqual(self.fake_calls("gh"), [])  # no GitHub call, so no PR either
        self.assertEqual(self.fake_calls("claude"), [])  # no reviewer
        self.assertEqual(self.origin_main(), self.tracking, "the base was fetched")
        self.assertEqual(self.heads_on_origin(), self.remote_heads, "something was pushed")

    def test_preflight_stops_before_github_fetch_and_push(self):
        self.track(".revali/feature__mul/stray.md")
        code, out = run_cli(["preflight"])
        self.assert_refused(code, out, [".revali/feature__mul/stray.md"])
        self.assert_nothing_left()

    def test_a_staged_file_stops_it_as_early(self):
        self.track(".revali/feature__mul/stray.md", commit=False)
        code, out = run_cli(["preflight"])
        self.assert_refused(code, out, [".revali/feature__mul/stray.md"])
        self.assert_nothing_left()

    def test_a_staged_removal_stops_a_run_as_early(self):
        # PR #69 round 1 F1: this run used to pass preflight and push the commit with the file
        stray = ".revali/feature__mul/stray.md"
        self.track(stray)
        git(["rm", "-r", "-q", "--cached", "--", ".revali"], self.repo)
        code, out = run_cli(["run", "--foreground"])
        self.assert_refused(code, out, [stray])
        self.assert_nothing_left()

    def test_run_stops_there_and_wait_and_status_report_it(self):
        stray = ".revali/feature__mul/stray.md"
        self.track(stray)
        code, out = run_cli(["run", "--foreground"])
        self.assert_refused(code, out, [stray])
        self.assert_nothing_left()
        self.assertFalse(self.exists("tests/test_review_mul.py"))

        code, waited = run_cli(["wait", "--timeout", "1s"])
        self.assert_refused(code, waited, [stray])

        state = State.load(self.rdir())
        self.assertEqual(state.last_exit, EXIT_ACTION)
        self.assertEqual(state.rounds, [])
        self.assertFalse(state.pr_number)
        _, status = run_cli(["status"])
        # the stage of a run the author must act on, as after any other exit 2
        self.assertIn("stage: needs_action", status)
        self.assertIn(stray, status)
        self.assertIn("git rm -r --cached .revali", status)
        self.assert_nothing_left()


class OnlyTheStateDirectory(TrackedCase):
    """AC-3"""

    def test_ignored_files_on_disk_do_not_stop_it(self):
        # the fixture ignores .revali/: change.md, one more file and an empty directory
        self.write(".revali/feature__mul/notes.md", "ignored\n")
        os.makedirs(os.path.join(self.repo, ".revali", "empty"))
        self.assertEqual(git(["ls-files", "--", ".revali"], self.repo), "")
        self.assert_passes()
        self.track(".revali/feature__mul/notes.md", commit=False)
        code, out = run_cli(["preflight"])
        self.assert_refused(code, out, [".revali/feature__mul/notes.md"])
        self.assertNotIn(CHANGE, out)  # still only on disk

    def test_untracked_files_git_reports_do_not_stop_it(self):
        # without the ignore line git status shows them as untracked; they are not tracked
        self.write(".gitignore", ".venv/\n__pycache__/\n")
        git(["add", "--", ".gitignore"], self.repo)
        git(["commit", "-q", "-m", "stop ignoring the state directory"], self.repo)
        self.write(".revali/feature__mul/notes.md", "untracked\n")
        status = git(["status", "--porcelain", "--untracked-files=all"], self.repo)
        self.assertIn("?? .revali/feature__mul/notes.md", status)
        self.assert_passes()
        self.track(".revali/feature__mul/notes.md", commit=False, force=False)
        code, out = run_cli(["preflight"])
        self.assert_refused(code, out, [".revali/feature__mul/notes.md"])
        self.assertNotIn(CHANGE, out)

    def test_names_that_only_look_like_the_state_directory(self):
        lookalikes = (
            ".revali-notes/x.md",  # starts with the same name
            ".revalix/y.md",
            "docs/.revali/x.md",  # the same name, deeper in the tree
            "src/.revali/feature__mul/change.md",
        )
        for rel in lookalikes:
            self.track(rel)
        self.assert_passes()
        self.track(".revali/feature__mul/stray.md", commit=False)
        code, out = run_cli(["preflight"])
        self.assert_refused(code, out, [".revali/feature__mul/stray.md"])
        for rel in lookalikes:
            with self.subTest(rel=rel):
                self.assertNotIn(rel, out)

    def test_a_configured_state_directory_is_the_one_checked(self):
        self.write("revali.toml", self.read("revali.toml") + '\n[paths]\nstate_dir = ".rv"\n')
        git(["add", "--", "revali.toml"], self.repo)
        git(["commit", "-q", "-m", "state directory .rv"], self.repo)
        self.write(".rv/feature__mul/change.md", self.read(CHANGE))
        # .revali is an ordinary directory now: a tracked file there is the project's business
        self.track(".revali/old/change.md")
        self.assert_passes()
        self.track(".rv/feature__mul/stray.md", commit=False, force=False)
        code, out = run_cli(["preflight"])
        self.assert_refused(code, out, [".rv/feature__mul/stray.md"], state_dir=".rv")
        self.assertNotIn(".revali/old/change.md", out)
        self.assertNotIn("git rm -r --cached .revali", out)
