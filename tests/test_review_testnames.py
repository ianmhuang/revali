"""Acceptance tests for fix/docs-name-search-tests, AC-1 to AC-5: the related documents'
name search leaves out the changed lines of files under `test_dir`; a link to such a file,
its whole path, and the "changed in this diff" mark of a document there still count.

The documents live on the base branch, so they are outside the diff: a line of the prompt
that says "mentions" or "links to" comes from the related-documents section. Every test
also holds a name that only a file under `test_dir` changes, so each one fails on the base.
"""

from revali.preflight import preflight
from revali.review import build_prompt
from revali.state import State
from tests.helpers import RepoCase, git


def entries(prompt, page, verb):
    """The related-list lines of `page` that carry `verb`."""
    return [
        line
        for line in prompt.splitlines()
        if line.lstrip("-* ").startswith("`%s`" % page) and verb in line
    ]


def mentioned(prompt):
    """Every line of the prompt that lists a mention."""
    return "\n".join(line for line in prompt.splitlines() if "mentions `" in line)


class TestNamesCase(RepoCase):
    """The fixture's `test_dir` is "tests". main (pushed) carries the documents; feature/mul
    restarts from it."""

    def base(self, files):
        git(["checkout", "-q", "main"], self.repo)
        for path, text in files.items():
            self.write(path, text)
        self.commit_all("documents on the base")
        git(["push", "-q", "origin", "main"], self.repo)
        git(["checkout", "-q", "-B", "feature/mul", "main"], self.repo)

    def prompt(self):
        self.commit_all("the change")
        ctx = preflight(self.repo)
        return build_prompt(ctx, State(), self.rdir(), 1)

    def assert_section(self, prompt):
        """The section is in the prompt: its map names the guide."""
        self.assertRegex(prompt, r"`docs/guide\.md`[^\n]*\bdepth\W*1\b")


class OnlyTests(TestNamesCase):
    """AC-1"""

    def test_names_a_test_adds_or_removes_list_no_document(self):
        self.base(
            {
                "README.md": "[guide](docs/guide.md)\n",
                "docs/guide.md": (
                    "Call `fixture_helper` first.\n"
                    "The rule is `AC-7`.\n"
                    "Formerly `old_fixture`.\n"
                ),
                "tests/test_old.py": "old_fixture = 1\n",
            }
        )
        self.write("tests/test_x.py", "def test_it():\n    fixture_helper()  # AC-7\n")
        git(["rm", "-q", "tests/test_old.py"], self.repo)
        self.write("src/calc.py", self.read("src/calc.py") + "\n\nkept_value = 1\n")
        p = self.prompt()
        self.assert_section(p)
        self.assertIn("fixture_helper()", p)  # the test file is in the reviewer's diff
        self.assertEqual(entries(p, "docs/guide.md", "mentions"), [])
        listed = mentioned(p)
        for name in ("fixture_helper", "AC-7", "old_fixture"):
            self.assertNotIn("`%s`" % name, listed)


class OutsideTests(TestNamesCase):
    """AC-2"""

    def test_a_name_a_source_file_changes_is_listed_whatever_the_tests_change(self):
        self.base(
            {
                "README.md": "[guide](docs/guide.md)\n",
                "docs/guide.md": "# guide\n\nSet `shared_name`, never `test_only_name`.\n",
            }
        )
        self.write("src/calc.py", self.read("src/calc.py") + "\n\nshared_name = 1\n")
        self.write("tests/test_x.py", "shared_name = test_only_name = 2\n")
        p = self.prompt()
        self.assertRegex(p, r"`docs/guide\.md`[^\n]*mentions[^\n]*`shared_name` \(line 3\)")
        self.assertNotIn("`test_only_name`", mentioned(p))


class LinksAndPaths(TestNamesCase):
    """AC-3"""

    def test_a_link_to_a_test_file_and_its_whole_path_are_listed(self):
        self.base(
            {
                "README.md": "[guide](docs/guide.md), [t](tests/test_x.py)\n",
                "docs/guide.md": "Run `tests/test_x.py`.\nIt sets `probe_token`.\n",
            }
        )
        self.write("tests/test_x.py", "probe_token = 1\n")
        p = self.prompt()
        self.assertRegex(p, r"`README\.md`[^\n]*links to[^\n]*`tests/test_x\.py` \(line 1\)")
        self.assertRegex(p, r"`docs/guide\.md`[^\n]*mentions[^\n]*`tests/test_x\.py` \(line 1\)")
        self.assertNotIn("`probe_token`", mentioned(p))


class ChangedDocument(TestNamesCase):
    """AC-4"""

    def test_a_changed_document_under_test_dir_is_marked_and_its_lines_do_not_count(self):
        self.base(
            {
                "README.md": "[guide](docs/guide.md), [notes](tests/notes.md)\n",
                "docs/guide.md": "Use `notes_word`.\n",
                "tests/notes.md": "# notes\n",
            }
        )
        self.write("tests/notes.md", "# notes\n\nAbout `src/calc.py`.\n\nSet `notes_word`.\n")
        self.write("src/calc.py", self.read("src/calc.py") + "\n\nkept_value = 1\n")
        p = self.prompt()
        self.assert_section(p)
        notes = entries(p, "tests/notes.md", "mentions")
        self.assertEqual(len(notes), 1, notes)
        self.assertIn("(changed in this diff)", notes[0])
        self.assertIn("`src/calc.py` (line 3)", notes[0])
        self.assertNotIn("`notes_word`", mentioned(p))


class Matching(TestNamesCase):
    """AC-5"""

    DOCS = {
        "README.md": "[guide](docs/guide.md)\n",
        "docs/guide.md": "Data: `data_word`.\nTests: `test_only_name`.\n",
    }

    def change(self):
        self.write("tests_data/x.py", "data_word = 1\n")
        self.write("tests/sub/test_x.py", "test_only_name = 2\n")

    def check(self, prompt, where):
        self.assertRegex(
            prompt, r"`docs/guide\.md`[^\n]*mentions[^\n]*`data_word` \(line 1\)", where
        )
        self.assertNotIn("`test_only_name`", mentioned(prompt), where)

    def test_a_directory_that_only_begins_like_test_dir_still_counts(self):
        self.base(self.DOCS)
        self.change()
        self.check(self.prompt(), "tests")

    def test_a_trailing_slash_or_a_backslash_in_test_dir_does_not_matter(self):
        self.base(self.DOCS)
        self.change()
        written = 'test_dir = "tests"'
        for spelled in ('test_dir = "tests/"', "test_dir = 'tests\\'"):
            toml = self.read("revali.toml")
            self.assertIn(written, toml)
            self.write("revali.toml", toml.replace(written, spelled, 1))
            written = spelled
            self.check(self.prompt(), spelled)
