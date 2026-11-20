"""`revali init` and a config file that is UTF-8 but not valid TOML (M3.37).

init takes `[paths] state_dir` for the ignore rule from the project's revali.toml and the
user's config.toml. One it cannot parse stops it with the line `preflight` gives, before
`.gitignore` is touched, instead of the default state directory going in without a word.
"""

import os
import unittest

from revali import EXIT_OK, config
from tests.helpers import RepoCase
from tests.test_config_not_utf8 import run

BROKEN = b'\n[paths]\nstate_dir = ".rv"\n= not toml\n'


class Case(RepoCase):
    def project_file(self):
        return os.path.join(self.repo, "revali.toml")

    def user_file(self):
        return os.path.join(self.home, "config.toml")

    def append_bytes(self, path, data):
        body = b""
        if os.path.isfile(path):
            with open(path, "rb") as fh:
                body = fh.read()
        with open(path, "wb") as fh:
            fh.write(body + data)

    def gitignore_bytes(self):
        path = os.path.join(self.repo, ".gitignore")
        if not os.path.isfile(path):
            return None
        with open(path, "rb") as fh:
            return fh.read()

    def gitignore_without_the_state_dir(self):
        # review round 1 F1: the fixture's .gitignore already ignores .revali/, so an init
        # that went on with the default state directory would leave it alone; start from one
        # that ignores nothing revali uses, so going on would have to change it
        path = os.path.join(self.repo, ".gitignore")
        with open(path, "wb") as fh:
            fh.write(b"build/\n")
        return b"build/\n"

    def error_lines(self, out):
        return [ln for ln in out.splitlines() if ln.startswith("ERROR")]


class NotValidToml(Case):
    """AC-1."""

    def test_the_projects_file_stops_init(self):
        self.append_bytes(self.project_file(), BROKEN)
        before = self.gitignore_without_the_state_dir()
        code, out = run(["init"])
        self.assertEqual(
            self.gitignore_bytes(), before, out
        )  # first: fails on its own if init went on
        self.assertEqual(code, 1, out)
        self.assertNotIn("Traceback", out)
        errors = self.error_lines(out)
        self.assertEqual(len(errors), 1, out)
        self.assertTrue(
            errors[0].startswith("ERROR: %s: not valid TOML: " % self.project_file()), out
        )
        self.assertIn("kept revali.toml", out.splitlines())  # reported before it stopped

    def test_the_users_file_stops_init_and_is_the_one_named(self):
        self.append_bytes(self.user_file(), b"= not toml\n")
        before = self.gitignore_without_the_state_dir()
        code, out = run(["init"])
        self.assertEqual(
            self.gitignore_bytes(), before, out
        )  # first: fails on its own if init went on
        self.assertEqual(code, 1, out)
        self.assertNotIn("Traceback", out)
        errors = self.error_lines(out)
        self.assertEqual(len(errors), 1, out)
        self.assertTrue(errors[0].startswith("ERROR: %s: not valid TOML: " % self.user_file()), out)
        self.assertNotIn(self.project_file(), errors[0])

    def test_the_message_is_the_one_preflight_gives(self):
        self.append_bytes(self.project_file(), BROKEN)
        _, init_out = run(["init"])
        _, preflight_out = run(["preflight"])
        line = self.error_lines(init_out)[0][len("ERROR: ") :]
        self.assertIn(line, preflight_out)


class UnknownKey(Case):
    """AC-2: a file that parses still lets init through, its [paths] in effect."""

    def test_init_completes_with_the_files_state_dir(self):
        self.append_bytes(self.project_file(), b'\n[paths]\nstate_dir = ".rv"\nno_such_key = 1\n')
        code, out = run(["init"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertEqual(self.error_lines(out), [], out)
        self.assertIn(".rv/", self.gitignore_bytes().decode("utf-8").splitlines())
        code, out = run(["preflight"])
        self.assertEqual(code, 1, out)
        self.assertIn("no_such_key", out)


class OtherFileStillApplies(Case):
    """AC-3, the behaviour the docs now describe: a damaged file loses only its own
    [paths]."""

    def test_the_users_state_dir_with_a_broken_project_file(self):
        self.append_bytes(self.user_file(), b'[paths]\nstate_dir = ".u"\n')
        self.append_bytes(self.project_file(), b"\n= not toml\n")
        self.assertEqual(config.paths_for(self.repo).state_dir, ".u")


if __name__ == "__main__":
    unittest.main()
