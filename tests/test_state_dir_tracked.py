"""feature/state-dir-untracked (M3.2.6): preflight refuses while git tracks a file under the
state directory, before anything leaves the machine, and says how to fix it."""

import os

from revali import EXIT_ACTION, EXIT_OK, gitops
from revali.state import State
from tests.helpers import RepoCase, git, run_cli

HEADER = "under .revali/, revali's state directory:"
FIX = "git rm -r --cached .revali"


class Case(RepoCase):
    def force_add(self, rel, text="x\n", commit=True):
        self.write(rel, text)
        git(["add", "-f", rel], self.repo)
        if commit:
            git(["commit", "-q", "-m", "add %s" % rel], self.repo)

    def assert_refused(self, code, out, *files):
        self.assertEqual(code, EXIT_ACTION, out)
        self.assertIn(HEADER, out)
        for rel in files:
            self.assertIn("  " + rel, out)
        self.assertIn("`%s`" % FIX, out)
        self.assertIn(".gitignore", out)

    def assert_nothing_left_the_machine(self):
        self.assertEqual(self.fake_calls("gh"), [])
        self.assertEqual(self.fake_calls("claude"), [])
        remote = git(["ls-remote", "--heads", "origin", "feature/mul"], self.repo)
        self.assertEqual(remote.strip(), "")


class Refused(Case):
    """AC-1, AC-2"""

    def test_a_file_the_branch_committed(self):
        rel = ".revali/feature__mul/change.md"
        self.force_add(rel, self.read(rel))
        code, out = run_cli(["preflight"])
        self.assert_refused(code, out, rel)
        self.assertIn("git tracks 1 file(s)", out)
        self.assert_nothing_left_the_machine()

    def test_a_file_the_base_already_had(self):
        git(["checkout", "-q", "main"], self.repo)
        self.force_add(".revali/old__branch/change.md")
        git(["checkout", "-q", "feature/mul"], self.repo)
        git(["rebase", "-q", "main"], self.repo)
        code, out = run_cli(["preflight"])
        self.assert_refused(code, out, ".revali/old__branch/change.md")
        self.assert_nothing_left_the_machine()

    def test_a_file_only_staged(self):
        self.force_add(".revali/feature__mul/notes.md", commit=False)
        code, out = run_cli(["preflight"])
        self.assert_refused(code, out, ".revali/feature__mul/notes.md")
        self.assert_nothing_left_the_machine()

    def test_a_removal_only_staged(self):
        # round 1 F1: `git rm --cached` without the commit leaves HEAD carrying the file
        rel = ".revali/feature__mul/stray.md"
        self.force_add(rel)
        git(["rm", "-q", "-r", "--cached", ".revali"], self.repo)
        self.assertEqual(gitops.tracked_under(self.repo, ".revali"), [])
        code, out = run_cli(["preflight"])
        self.assert_refused(code, out, rel)
        self.assertIn("1 of them are out of the index already but still in HEAD", out)
        self.assert_nothing_left_the_machine()

    def test_twenty_listed_then_counted(self):
        for i in range(25):
            self.write(".revali/many/f%02d.txt" % i, "x\n")
        git(["add", "-f", ".revali/many"], self.repo)
        git(["commit", "-q", "-m", "many"], self.repo)
        code, out = run_cli(["preflight"])
        self.assert_refused(code, out, ".revali/many/f00.txt", ".revali/many/f19.txt")
        self.assertIn("git tracks 25 file(s)", out)
        self.assertNotIn(".revali/many/f20.txt", out)
        self.assertIn("... and 5 more", out)

    def test_a_run_reports_it_like_other_preflight_stops(self):
        self.force_add(".revali/feature__mul/stray.md")
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ACTION, out)
        self.assertIn(HEADER, out)
        self.assert_nothing_left_the_machine()
        code, waited = run_cli(["wait", "--timeout", "1s"])
        self.assertEqual(code, EXIT_ACTION, waited)
        self.assertIn(HEADER, waited)
        code, status = run_cli(["status"])
        self.assertIn(".revali/feature__mul/stray.md", status)
        self.assertEqual(State.load(self.rdir()).last_exit, EXIT_ACTION)


class Allowed(Case):
    """AC-3"""

    def preflight_passes(self):
        code, out = run_cli(["preflight"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertNotIn("git tracks", out)

    def test_the_removal_once_committed(self):
        self.force_add(".revali/feature__mul/stray.md")
        git(["rm", "-q", "-r", "--cached", ".revali"], self.repo)
        git(["commit", "-q", "-m", "untrack the state directory"], self.repo)
        self.preflight_passes()

    def test_files_only_on_disk(self):
        self.write(".revali/feature__mul/notes.md", "ignored\n")
        os.makedirs(os.path.join(self.repo, ".revali", "empty"), exist_ok=True)
        self.preflight_passes()

    def test_a_name_that_only_starts_the_same(self):
        self.write(".revali-notes/x.md", "tracked\n")
        self.commit_all("notes")
        self.preflight_passes()

    def test_a_state_dir_name_deeper_in_the_tree(self):
        self.force_add("docs/.revali/x.md")
        self.preflight_passes()

    def test_another_state_dir_is_the_one_checked(self):
        self.write(
            "revali.toml",
            self.read("revali.toml") + '\n[paths]\nstate_dir = ".rv"\n',
        )
        self.commit_all("state dir")
        self.write(".rv/feature__mul/change.md", self.read(".revali/feature__mul/change.md"))
        self.force_add(".revali/old/change.md")  # no longer the state directory
        self.preflight_passes()
        self.force_add(".rv/feature__mul/stray.md")
        code, out = run_cli(["preflight"])
        self.assertEqual(code, EXIT_ACTION, out)
        self.assertIn("under .rv/, revali's state directory:", out)
        self.assertIn("  .rv/feature__mul/stray.md", out)
        self.assertIn("`git rm -r --cached .rv`", out)


class TrackedUnder(Case):
    """gitops.tracked_under reads the index with a literal pathspec"""

    def test_glob_characters_match_only_themselves(self):
        self.force_add("a[b]/x.md")
        self.force_add("ab/y.md")
        self.assertEqual(gitops.tracked_under(self.repo, "a[b]"), ["a[b]/x.md"])
        self.assertEqual(gitops.tracked_under(self.repo, ".revali"), [])
        self.assertEqual(gitops.committed_under(self.repo, "a[b]"), ["a[b]/x.md"])

    def test_no_commit_yet_holds_nothing(self):
        tmp = os.path.join(self.tmp, "unborn")
        os.makedirs(tmp)
        git(["init", "-q"], tmp)
        git(["config", "user.email", "test@example.invalid"], tmp)
        git(["config", "user.name", "revali test"], tmp)
        self.assertEqual(gitops.committed_under(tmp, ".revali"), [])
