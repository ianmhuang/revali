"""fix/docs-name-search-tests (M3.9): the related documents' name search leaves out the
changed lines of files under `test_dir`; links to those files and their paths still count."""

import os
import unittest

from revali.preflight import preflight
from revali.review import _lines_outside_tests, build_prompt
from revali.state import State
from tests.helpers import RepoCase, git

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class NameSearch(RepoCase):
    """The fixture's `test_dir` is "tests"."""

    def base_docs(self, readme, usage):
        """Commit README.md and docs/usage.md on main (pushed) and restart feature/mul there."""
        git(["checkout", "-q", "main"], self.repo)
        self.write("README.md", readme)
        self.write("docs/usage.md", usage)
        self.commit_all("docs on the base")
        git(["push", "-q", "origin", "main"], self.repo)
        git(["checkout", "-q", "-B", "feature/mul", "main"], self.repo)

    def prompt(self):
        ctx = preflight(self.repo)
        ctx.log = None
        return build_prompt(ctx, State(), self.rdir(), 1)

    def test_a_name_only_a_test_changes_lists_nothing(self):
        """AC-1"""
        self.base_docs("[usage](docs/usage.md)\n", "Call `build_prompt` here.\n")
        self.write("tests/test_x.py", "build_prompt(ctx)\n")
        self.commit_all("test only")
        self.assertNotIn("mentions `build_prompt`", self.prompt())

    def test_the_same_name_outside_test_dir_still_counts(self):
        """AC-2"""
        self.base_docs("[usage](docs/usage.md)\n", "Call `build_prompt` here.\n")
        self.write("tests/test_x.py", "build_prompt(ctx)\n")
        self.write("src/calc.py", self.read("src/calc.py") + "build_prompt = None\n")
        self.commit_all("test and source")
        self.assertIn("`docs/usage.md`: mentions `build_prompt` (line 1)", self.prompt())

    def test_links_to_a_test_file_and_its_path_still_count(self):
        """AC-3"""
        self.base_docs("[usage](docs/usage.md), [t](tests/test_x.py)\n", "See `tests/test_x.py`.\n")
        self.write("tests/test_x.py", "assert True\n")
        self.commit_all("test file")
        p = self.prompt()
        self.assertIn("`README.md`: links to `tests/test_x.py` (line 1)", p)
        self.assertIn("`docs/usage.md`: mentions `tests/test_x.py` (line 1)", p)

    def test_a_changed_document_under_test_dir_is_still_marked(self):
        """AC-4"""
        self.base_docs("[usage](docs/usage.md), [notes](tests/notes.md)\n", "Use `qq_word`.\n")
        self.write("tests/notes.md", "About `src/calc.py`.\n\n`qq_word`\n")
        self.write("src/calc.py", self.read("src/calc.py") + "kept = 1\n")
        self.commit_all("notes")
        p = self.prompt()
        self.assertIn("`tests/notes.md` (changed in this diff): mentions `src/calc.py` (line 1)", p)
        self.assertNotIn("mentions `qq_word`", p)

    def test_a_directory_that_only_begins_like_test_dir_counts(self):
        """AC-5"""
        self.base_docs("[usage](docs/usage.md)\n", "Call `build_prompt` here.\n")
        self.write("tests_data/x.py", "build_prompt(ctx)\n")
        self.commit_all("data")
        self.assertIn("mentions `build_prompt`", self.prompt())


class Filter(unittest.TestCase):
    """AC-5: `test_dir` is matched by `under_test_dir`."""

    LINES = {"tests/a.py": ["t"], "tests/sub/b.py": ["u"], "tests_data/c.py": ["d"], "s.py": ["s"]}

    def test_spellings_of_test_dir(self):
        for test_dir in ("tests", "tests/", "tests\\"):
            self.assertEqual(
                sorted(_lines_outside_tests(self.LINES, test_dir)),
                ["s.py", "tests_data/c.py"],
                test_dir,
            )

    def test_an_empty_test_dir_leaves_everything(self):
        self.assertEqual(_lines_outside_tests(self.LINES, ""), self.LINES)


class Documented(unittest.TestCase):
    """AC-6"""

    def test_configuration_says_test_files_do_not_count(self):
        with open(os.path.join(HERE, "docs", "configuration.md"), encoding="utf-8") as fh:
            text = " ".join(fh.read().split())
        self.assertIn("changed lines of files under `test_dir` do not count", text)
