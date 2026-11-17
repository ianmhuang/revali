"""Acceptance tests for fix/docs-section-test-note, AC-1 to AC-3: the lead sentence of the
related-documents section names the project's `test_dir` and says what counts there; the
rest of the section is as before; PROMPT_VERSION moved.

The section is cut out of the prompt by its heading, so a `tests/` in the rest of the prompt
(the diff, the instructions about where to write tests) is not taken for the note. Every
test of the section also asks for the note in the lead, so each one fails on the base.
"""

import re
import unittest
from unittest import mock

from revali import PROMPT_VERSION, docgraph
from revali.preflight import preflight
from revali.review import build_prompt
from revali.state import State
from tests.helpers import RepoCase, git

HEADING = "\n## Documents related to this change\n"


def section(prompt):
    """The related-documents section without its heading, up to the next heading."""
    body = prompt[prompt.index(HEADING) + len(HEADING) :]
    nxt = re.search(r"\n#+ ", body)
    return body[: nxt.start()] if nxt else body


def lead(prompt):
    """The first paragraph of the section."""
    return section(prompt).strip().split("\n\n")[0]


def mentioned(prompt):
    """Every line of the section that lists a mention."""
    return "\n".join(line for line in section(prompt).splitlines() if "mentions `" in line)


class NoteCase(RepoCase):
    """The fixture's `test_dir` is "tests". main (pushed) carries the documents; feature/mul
    restarts from it."""

    def base(self, files):
        git(["checkout", "-q", "main"], self.repo)
        for path, text in files.items():
            self.write(path, text)
        self.commit_all("documents on the base")
        git(["push", "-q", "origin", "main"], self.repo)
        git(["checkout", "-q", "-B", "feature/mul", "main"], self.repo)

    def replace_in_toml(self, old, new):
        toml = self.read("revali.toml")
        self.assertIn(old, toml)
        self.write("revali.toml", toml.replace(old, new, 1))

    def set_review(self, lines):
        self.replace_in_toml("[review]\n", "[review]\n" + lines)

    def change_code(self, text="\n\nretry_limit = 3\n"):
        self.write("src/calc.py", self.read("src/calc.py") + text)

    def prompt(self):
        self.commit_all("the change")
        ctx = preflight(self.repo)
        return build_prompt(ctx, State(), self.rdir(), 1)


class LeadNamesTestDir(NoteCase):
    """AC-1"""

    def test_a_nested_test_dir_is_named_and_is_the_directory_whose_lines_do_not_count(self):
        self.base(
            {
                "README.md": "[guide](docs/guide.md)\n",
                "docs/guide.md": (
                    "Set `inner_name` and `outer_name`.\nRun `tests/acceptance/test_x.py`.\n"
                ),
            }
        )
        self.replace_in_toml('test_dir = "tests"', 'test_dir = "tests/acceptance"')
        self.write("tests/acceptance/test_x.py", "inner_name = 1\n")
        self.write("tests/test_outer.py", "outer_name = 2\n")  # outside the nested test_dir
        p = self.prompt()
        sentence = lead(p)
        self.assertIn("`tests/acceptance/`", sentence)
        # what the sentence says about that directory, after naming it
        after = sentence.split("`tests/acceptance/`", 1)[1]
        self.assertRegex(after, r"(?i)\bnot\b[^.;]*\bcount", sentence)
        self.assertRegex(after, r"(?i)\blink\b", sentence)
        self.assertRegex(after, r"(?i)\bpath\b", sentence)
        # and the list under it behaves as the sentence says
        listed = mentioned(p)
        self.assertIn("`outer_name` (line 1)", listed)
        self.assertIn("`tests/acceptance/test_x.py` (line 2)", listed)
        self.assertNotIn("`inner_name`", listed)

    def test_the_fixtures_test_dir_is_named_however_the_key_spells_it(self):
        self.change_code()
        sentence = lead(self.prompt())
        self.assertIn("`tests/`", sentence)
        self.assertNotIn("acceptance", sentence)
        self.replace_in_toml('test_dir = "tests"', 'test_dir = "./tests/"')
        sentence = lead(self.prompt())
        self.assertIn("`tests/`", sentence)
        self.assertNotIn("./tests", sentence)
        self.assertNotIn("tests//", sentence)


class RestAsBefore(NoteCase):
    """AC-2"""

    def test_the_roots_and_the_instruction_of_the_sentence(self):
        self.base({"docs/index.md": "# index\n\nNothing here names the code.\n"})
        self.set_review('doc_roots = ["README.md", "docs/index.md"]\n')
        self.change_code()
        sentence = lead(self.prompt())
        self.assertIn("`tests/`", sentence)  # AC-1, so the test fails on the base
        self.assertTrue(
            sentence.startswith(
                "Documents reachable by relative links from README.md, docs/index.md that "
                "link to a file this diff changes, or name in a code span something it adds "
                "or removes"
            ),
            sentence,
        )
        self.assertTrue(
            sentence.endswith(
                "They are not in the diff unless marked; read each one and compare it with "
                "the new behaviour."
            ),
            sentence,
        )

    def test_the_entries_and_the_map(self):
        self.base(
            {
                "README.md": "# sample\n\n[usage](docs/usage.md), [guide](docs/guide.md)\n",
                "docs/usage.md": "# usage\n\nThe code is in [calc](../src/calc.py).\n",
                "docs/guide.md": "# guide\n\nSet `retry_limit` first.\n",
            }
        )
        self.change_code()
        self.write("docs/guide.md", self.read("docs/guide.md") + "\nMore.\n")
        p = self.prompt()
        sentence = lead(p)
        self.assertIn("`tests/`", sentence)  # AC-1, so the test fails on the base
        lines = section(p).splitlines()
        self.assertIn("- `docs/usage.md`: links to `src/calc.py` (line 3)", lines)
        self.assertIn(
            "- `docs/guide.md` (changed in this diff): mentions `retry_limit` (line 3)", lines
        )
        self.assertIn("Map of the documentation (depth = links from the nearest root):", lines)
        self.assertIn("- `README.md` (depth 0)", lines)
        self.assertIn("- `docs/usage.md` (depth 1)", lines)
        self.assertIn("- `docs/guide.md` (depth 1)", lines)
        # the lead is one paragraph, then the list: nothing of the note leaks into an entry
        rest = [line for line in lines if line != sentence]
        self.assertEqual(len(rest), len(lines) - 1, lines)  # the sentence is one line
        self.assertEqual([line for line in rest if "`tests/`" in line], [])

    def test_the_no_match_line(self):
        self.set_review('doc_roots = ["README.md"]\n')  # the fixture's README names nothing
        self.change_code()
        p = self.prompt()
        self.assertIn("`tests/`", lead(p))  # AC-1, so the test fails on the base
        lines = section(p).splitlines()
        self.assertIn(
            "No document in the graph links to a changed file or names a changed name.", lines
        )
        self.assertEqual([line for line in lines if line.startswith("- ") and ":" in line], [])
        self.assertIn("- `README.md` (depth 0)", lines)

    def test_the_messages_that_replace_the_list_are_as_before_and_carry_no_note(self):
        self.change_code()
        self.assertIn("`tests/`", lead(self.prompt()))  # the note is in the lead, AC-1

        self.set_review('doc_roots = ["NOPE.md"]\n')
        no_graph = section(self.prompt())
        self.assertEqual(
            no_graph.strip(),
            "There is no documentation graph: none of the documentation roots exists (NOPE.md).",
        )

        ctx = preflight(self.repo)
        ctx.log = mock.Mock()
        with mock.patch.object(docgraph, "build", side_effect=OSError("disk gone")):
            unavailable = section(build_prompt(ctx, State(), self.rdir(), 1))
        self.assertEqual(
            unavailable.strip(), "This round the related documents are unavailable: disk gone"
        )


class Version(unittest.TestCase):
    """AC-3"""

    def test_prompt_version_is_past_eleven(self):
        self.assertGreaterEqual(int(PROMPT_VERSION), 12)
