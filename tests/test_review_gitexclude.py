"""Review of fix/respect-git-exclude: the PR stage asks git whether the state directory is
already ignored before it touches .gitignore (AC-1), still appends the line and commits it when
nothing ignores the directory (AC-2), and docs/side-effects.md states the rule (AC-3).
Pipeline runs use the fake gh, fake claude, and fake runner from tests.helpers; only git is real.
The machine's own excludes are kept out of the decision by pointing `core.excludesFile` at a
file the test controls (a local setting overrides the global one and the XDG default)."""

import os
import unittest

from revali import EXIT_OK
from tests.helpers import ROOT, RepoCase, claude_entry, git, run_cli

PROJECT_IGNORE = ".venv/\n__pycache__/\n"


class GitExcludeCase(RepoCase):
    def setUp(self):
        super().setUp()
        self.excludes_file = os.path.join(self.tmp, "excludes")
        self.set_excludes_file("")

    def set_excludes_file(self, text):
        with open(self.excludes_file, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        git(["config", "core.excludesFile", self.excludes_file], self.repo)

    def replace_gitignore(self, text=PROJECT_IGNORE):
        """Replace the fixture .gitignore (which lists .revali/) and commit only that file, so
        the untracked state directory never enters history."""
        self.write(".gitignore", text)
        git(["add", "--", ".gitignore"], self.repo)
        git(["commit", "-q", "-m", "drop ignore"], self.repo)

    def exclude(self, line):
        path = os.path.join(self.repo, ".git", "info", "exclude")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(line + "\n")

    def subjects(self):
        return git(["log", "--format=%s"], self.repo)

    def head_gitignore(self):
        return git(["show", "HEAD:.gitignore"], self.repo)

    def run_ok(self, *extra):
        self.claude(claude_entry())
        code, out = run_cli(["run", "--foreground"] + list(extra))
        self.assertEqual(code, EXIT_OK, out)
        return out


class StateDirAlreadyIgnored(GitExcludeCase):
    """AC-1: git already ignores the state directory, .gitignore has no such line."""

    def assert_untouched(self, expected=PROJECT_IGNORE):
        self.assertEqual(self.read(".gitignore"), expected)
        self.assertEqual(self.head_gitignore(), expected)
        self.assertNotIn("chore: ignore", self.subjects())

    def test_info_exclude_leaves_gitignore_alone(self):
        self.exclude(".revali/")
        self.replace_gitignore()
        self.run_ok()
        self.assert_untouched()
        self.assertEqual(git(["status", "--porcelain"], self.repo).strip(), "")

    def test_info_exclude_without_trailing_slash(self):
        self.exclude(".revali")
        self.replace_gitignore()
        self.run_ok()
        self.assert_untouched()

    def test_excludes_file_leaves_gitignore_alone(self):
        self.set_excludes_file(".revali/\n")
        self.replace_gitignore()
        self.run_ok()
        self.assert_untouched()

    def test_wider_gitignore_pattern_is_not_duplicated(self):
        # `/.revali/` ignores the directory but is not the exact line the old text scan
        # looked for; git says ignored, so nothing may be appended
        text = PROJECT_IGNORE + "/.revali/\n"
        self.replace_gitignore(text)
        self.run_ok()
        self.assert_untouched(text)

    def test_dry_run_does_not_commit_either(self):
        self.exclude(".revali/")
        self.replace_gitignore()
        code, out = run_cli(["run", "--foreground", "--dry-run"])
        self.assertEqual(code, EXIT_OK, out)
        self.assert_untouched()


class StateDirNotIgnored(GitExcludeCase):
    """AC-2: nothing ignores the state directory, so the line is appended and committed as
    before. The exact-content assertions also pin the append itself: one line, no blank
    separator when the file already ends in a newline (base got that wrong), a separator
    when it does not, nothing but the entry when the file is empty."""

    def chore_commit_files(self):
        sha = git(["log", "-1", "--format=%H", "--grep=^chore: ignore"], self.repo).strip()
        self.assertTrue(sha, self.subjects())
        return git(["show", "--format=", "--name-only", sha], self.repo).split()

    def test_line_appended_and_committed(self):
        self.replace_gitignore()
        self.run_ok()
        self.assertEqual(self.read(".gitignore"), PROJECT_IGNORE + ".revali/\n")
        self.assertIn("chore: ignore .revali/", self.subjects())
        self.assertEqual(self.chore_commit_files(), [".gitignore"])
        self.assertIn(".revali/", self.head_gitignore())

    def test_file_without_trailing_newline_gets_a_separator(self):
        self.replace_gitignore(".venv/")
        self.run_ok()
        self.assertEqual(self.read(".gitignore"), ".venv/\n.revali/\n")
        self.assertIn("chore: ignore .revali/", self.subjects())

    def test_empty_file_gets_only_the_entry(self):
        self.replace_gitignore("")
        self.run_ok()
        self.assertEqual(self.read(".gitignore"), ".revali/\n")
        self.assertIn("chore: ignore .revali/", self.subjects())

    def test_missing_gitignore_is_created(self):
        git(["rm", "-q", "--", ".gitignore"], self.repo)
        git(["commit", "-q", "-m", "no gitignore"], self.repo)
        self.run_ok()
        self.assertEqual(self.read(".gitignore"), ".revali/\n")
        self.assertIn("chore: ignore .revali/", self.subjects())
        self.assertEqual(self.chore_commit_files(), [".gitignore"])


class SideEffectsDoc(unittest.TestCase):
    """AC-3: docs/side-effects.md states the rule and names .git/info/exclude."""

    def setUp(self):
        with open(os.path.join(ROOT, "docs", "side-effects.md"), "r", encoding="utf-8") as fh:
            self.text = fh.read()

    def gitignore_bullet(self):
        bullets = [b for b in self.text.split("\n- ") if "`.gitignore`" in b]
        self.assertEqual(len(bullets), 1, "expected one bullet about .gitignore")
        return " ".join(bullets[0].split())

    def test_append_is_conditional_on_git(self):
        bullet = self.gitignore_bullet()
        self.assertRegex(bullet, r"already ignored|check-ignore")

    def test_names_info_exclude_as_the_way_out(self):
        self.assertIn(".git/info/exclude", self.gitignore_bullet())


if __name__ == "__main__":
    unittest.main()
