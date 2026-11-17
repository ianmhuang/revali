"""Reviewer acceptance tests for fix/test-dir-spelling (AC-1, AC-3): what the loaded
configuration holds for `[project] test_dir`. Each value is written into the fixture's
revali.toml (or the user file) and read back through `load_project_config`, the way
every command reads it; one refused value also goes through `revali run`.
"""

import os
import re
import unittest

from revali import EXIT_ERROR
from revali.config import ConfigError, load_project_config
from tests.fixtures.make_sample_repo import toml_str
from tests.helpers import RepoCase, claude_entry, run_cli

FILE = "tests/test_review_mul.py"


class ValueCase(RepoCase):
    def spell(self, value):
        """Make `value` the test_dir of the fixture's revali.toml (not committed)."""
        toml = self.read("revali.toml")
        line = "test_dir = " + toml_str(value)
        new, n = re.subn(r"^test_dir = .*$", lambda m: line, toml, flags=re.M)
        self.assertEqual(n, 1, toml)
        self.write("revali.toml", new)

    def loaded(self):
        return load_project_config(self.repo).project.test_dir


class PlainForm(ValueCase):
    """AC-1"""

    def test_each_spelling_loads_as_the_plain_path(self):
        for value, plain in (
            ("./tests", "tests"),
            ("tests/", "tests"),
            ("tests\\", "tests"),
            ("./tests/", "tests"),
            ("tests//", "tests"),
            ("./tests/unit", "tests/unit"),
            ("tests\\unit", "tests/unit"),
            # already plain: kept as written (a dot inside a name is part of the name)
            ("tests", "tests"),
            ("site/tests", "site/tests"),
            (".acceptance", ".acceptance"),
            ("tests.d/unit", "tests.d/unit"),
        ):
            with self.subTest(value=value):
                self.spell(value)
                self.assertEqual(self.loaded(), plain)

    def test_a_spelling_from_the_user_file_is_plain_too(self):
        """The project file leaves the key out and `~/.revali/config.toml` sets it: the
        loaded configuration is the same single value every caller reads."""
        toml = self.read("revali.toml")
        new, n = re.subn(r"^test_dir = .*\n", "", toml, flags=re.M)
        self.assertEqual(n, 1, toml)
        self.write("revali.toml", new)
        path = os.path.join(self.home, "config.toml")
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write('[project]\ntest_dir = "./spec/"\n')
        self.assertEqual(self.loaded(), "spec")


class Refused(ValueCase):
    """AC-3"""

    def test_a_value_that_names_no_directory_inside_the_repository(self):
        for value in (
            "",
            ".",
            "./",
            "/t",
            "\\t",
            "C:\\t",
            "C:t",
            "..",
            "../t",
            "tests/../x",
            "tests\\..\\x",
        ):
            with self.subTest(value=value):
                self.spell(value)
                with self.assertRaises(ConfigError) as caught:
                    load_project_config(self.repo)
                problems = caught.exception.problems
                named = [p for p in problems if "project.test_dir" in p]
                self.assertTrue(named, problems)
                # the value as `review.doc_roots` shows its paths: quoted, as given
                self.assertIn(repr(value), named[0])

    def test_the_problem_is_reported_like_a_doc_root(self):
        """The same value in both keys: each problem names its key and shows the value the
        same way."""
        self.spell("../elsewhere")
        toml = self.read("revali.toml")
        self.assertIn("[review]\n", toml)
        self.write(
            "revali.toml", toml.replace("[review]\n", '[review]\ndoc_roots = ["../elsewhere"]\n')
        )
        with self.assertRaises(ConfigError) as caught:
            load_project_config(self.repo)
        problems = caught.exception.problems
        roots = [p for p in problems if "review.doc_roots" in p]
        dirs = [p for p in problems if "project.test_dir" in p]
        self.assertEqual(len(roots), 1, problems)
        self.assertEqual(len(dirs), 1, problems)
        shown = repr("../elsewhere")
        self.assertIn(shown, roots[0])
        self.assertIn(shown, dirs[0])

    def test_a_run_stops_on_it_before_the_reviewer_starts(self):
        self.spell("../elsewhere")
        self.commit_all("a test_dir outside the repository")
        self.claude(claude_entry())
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertIn("project.test_dir", out)
        self.assertIn(repr("../elsewhere"), out)
        self.assertEqual(self.fake_calls("claude"), [])
        self.assertEqual(self.fake_calls("runner"), [])
        self.assertFalse(self.exists(FILE))


if __name__ == "__main__":
    unittest.main()
