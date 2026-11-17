"""Acceptance tests for feature/related-docs, AC-1 to AC-7: the review prompt lists the
documents of the link graph that link to a changed file or name a changed name, and ends
with a map of the documentation.

The documents live on the base branch, so they are outside the diff: a line of the prompt
that names one of them comes from the related-documents section, not from the diff.
"""

import re
import unittest
from unittest import mock

from revali import PROMPT_VERSION, docgraph
from revali.preflight import preflight
from revali.review import build_prompt
from revali.state import State
from tests.helpers import RepoCase, git

README = (
    "# sample\n"
    "\n"
    "[usage](docs/usage.md), [guide](docs/guide.md), [keys](docs/keys.md),\n"
    "[names](docs/names.md), [plain](docs/plain.md)\n"
)
USAGE = (
    "# usage\n"
    "\n"
    "The code is in [calc](../src/calc.py).\n"
    "Old helpers: [gone](../src/gone.py),\n"
    "[old](../src/old.py),\n"
    "and soon [extra](../src/extra.py).\n"
)
GUIDE = (
    "# guide\n"
    "\n"
    "Set `retry_limit` to choose how often a call is retried.\n"
    "Call `run` to start, it returns `true`.\n"
    "The flag `--dry-mode` and the hook `onRetry`.\n"
    "\n"
    "See [deep](deep.md).\n"
)
KEYS = (
    "# keys\n"
    "\n"
    "In the file: `[limits] burst_size = 10`.\n"
    "Not this one: `wait_s`.\n"
    "The module `src/calc.py`.\n"
)
NAMES = "# names\n\n`alpha_one` `beta_two` `gamma_three` `delta_four` `epsilon_five`\n"
FIVE = ("alpha_one", "beta_two", "gamma_three", "delta_four", "epsilon_five")
PLAIN = "# plain\n\nNothing here names the code.\n"
DEEP = "# deep\n\nThe end.\n"
ORPHAN = "# orphan\n\n`retry_limit` and [calc](src/calc.py)\n"

BRANCH_CODE = (
    "\n\n"
    "retry_limit = 3\n"
    "run = True\n"
    "true = 1\n"
    "# --dry-mode skips the call\n"
    "onRetry = None\n"
    "burst_size = 10\n"
    "retry_wait_s = 5\n"
    "alpha_one = beta_two = gamma_three = delta_four = epsilon_five = 0\n"
)

MAP_ENTRY = re.compile(r"`[^`\n]+\.md`[^\n]*\bdepth\W*\d")


def entries(prompt, page, verb):
    """The lines of the prompt that start with `page` in a code span (after a list marker,
    if any) and carry `verb`: the document's entry in the related list."""
    return [
        line
        for line in prompt.splitlines()
        if line.lstrip("-* ").startswith("`%s`" % page) and verb in line
    ]


class DocsCase(RepoCase):
    """main (pushed) carries the documents and the files they link to; feature/mul restarts
    from it and changes only code, unless a test says otherwise."""

    def setUp(self):
        super().setUp()
        git(["checkout", "-q", "main"], self.repo)
        self.write("README.md", README)
        self.write("docs/usage.md", USAGE)
        self.write("docs/guide.md", GUIDE)
        self.write("docs/keys.md", KEYS)
        self.write("docs/names.md", NAMES)
        self.write("docs/plain.md", PLAIN)
        self.write("docs/deep.md", DEEP)
        self.write("orphan.md", ORPHAN)
        self.write("src/gone.py", "def gone():\n    return 0\n")
        self.write("src/old.py", "def helper():\n    return 1\n")
        self.commit_all("documents on the base")
        git(["push", "-q", "origin", "main"], self.repo)
        git(["checkout", "-q", "-B", "feature/mul", "main"], self.repo)

    def change_code(self):
        self.write("src/calc.py", self.read("src/calc.py") + BRANCH_CODE)
        git(["rm", "-q", "src/gone.py"], self.repo)
        git(["mv", "src/old.py", "src/renamed.py"], self.repo)
        self.write("src/extra.py", "def extra():\n    return 2\n")

    def set_review(self, lines):
        toml = self.read("revali.toml")
        self.assertIn("[review]\n", toml)
        self.write("revali.toml", toml.replace("[review]\n", "[review]\n" + lines, 1))

    def prompt(self):
        self.commit_all("the change")
        ctx = preflight(self.repo)
        return build_prompt(ctx, State(), self.rdir(), 1)


class Links(DocsCase):
    """AC-2, and the default roots of AC-1 (the fixture's revali.toml names none)"""

    def test_a_document_linking_to_a_changed_file_is_listed_with_the_line(self):
        self.change_code()
        p = self.prompt()
        for path, line in (
            ("src/calc.py", 3),  # modified
            ("src/gone.py", 4),  # deleted
            ("src/old.py", 5),  # renamed away
            ("src/extra.py", 6),  # added
        ):
            self.assertRegex(
                p,
                r"`docs/usage\.md`[^\n]*links to[^\n]*`%s` \(line %d\)" % (re.escape(path), line),
            )

    def test_a_document_with_no_link_and_no_name_is_not_listed(self):
        self.change_code()
        p = self.prompt()
        self.assertTrue(entries(p, "docs/usage.md", "links to"))
        self.assertEqual(entries(p, "docs/plain.md", "links to"), [])
        self.assertEqual(entries(p, "docs/plain.md", "mentions"), [])
        self.assertEqual(entries(p, "docs/deep.md", "mentions"), [])

    def test_a_file_the_reviewer_diff_leaves_out_does_not_count(self):
        self.change_code()
        toml = self.read("revali.toml")
        self.assertIn('exclude = ["*.lock"]', toml)
        self.write(
            "revali.toml", toml.replace('exclude = ["*.lock"]', 'exclude = ["*.lock", "src/**"]')
        )
        p = self.prompt()
        # the section is there (its map names the document), with nothing from src/ in it
        self.assertRegex(p, r"`docs/usage\.md`[^\n]*\bdepth\W*1\b")
        self.assertEqual(entries(p, "docs/usage.md", "links to"), [])
        self.assertEqual(entries(p, "docs/guide.md", "mentions"), [])
        self.assertEqual(entries(p, "docs/keys.md", "mentions"), [])


class Mentions(DocsCase):
    """AC-3"""

    def test_names_on_changed_lines_are_listed_with_their_line(self):
        self.change_code()
        p = self.prompt()
        for name, line in (("retry_limit", 3), ("--dry-mode", 5), ("onRetry", 5)):
            self.assertRegex(
                p,
                r"`docs/guide\.md`[^\n]*mentions[^\n]*`%s` \(line %d\)" % (re.escape(name), line),
            )

    def test_a_span_is_split_and_a_changed_path_is_a_name(self):
        self.change_code()
        p = self.prompt()
        # `[limits] burst_size = 10`: the bracketed section and the value are not the name
        self.assertRegex(p, r"`docs/keys\.md`[^\n]*mentions[^\n]*`burst_size` \(line 3\)")
        self.assertRegex(p, r"`docs/keys\.md`[^\n]*mentions[^\n]*`src/calc\.py` \(line 5\)")

    def test_plain_words_and_parts_of_words_never_match(self):
        self.change_code()
        p = self.prompt()
        mentions = "\n".join(line for line in p.splitlines() if "mentions" in line)
        self.assertIn("`retry_limit`", mentions)
        # `run` and `true` are on changed lines, but they are plain words
        self.assertNotIn("`run`", mentions)
        self.assertNotIn("`true`", mentions)
        # `wait_s` is only part of `retry_wait_s`
        self.assertNotIn("`wait_s`", mentions)

    def test_at_most_three_names_and_how_many_more(self):
        self.change_code()
        p = self.prompt()
        found = entries(p, "docs/names.md", "mentions")
        self.assertEqual(len(found), 1, found)
        shown = [n for n in FIVE if "`%s`" % n in found[0]]
        self.assertEqual(len(shown), 3, found[0])
        self.assertRegex(found[0], r"\b2 more\b")


class ChangedAndOutside(DocsCase):
    """AC-4"""

    def test_a_document_the_diff_changes_is_marked_and_the_others_are_not(self):
        self.change_code()
        self.write("docs/usage.md", USAGE + "More to come.\n")
        p = self.prompt()
        usage = entries(p, "docs/usage.md", "links to")
        self.assertEqual(len(usage), 1, usage)
        self.assertIn("(changed in this diff)", usage[0])
        self.assertIn("`src/calc.py` (line 3)", usage[0])
        guide = entries(p, "docs/guide.md", "mentions")
        self.assertEqual(len(guide), 1, guide)
        self.assertNotIn("changed in this diff", guide[0])

    def test_a_document_outside_the_graph_is_not_searched(self):
        self.change_code()
        p = self.prompt()
        self.assertTrue(entries(p, "docs/guide.md", "mentions"))
        # orphan.md names `retry_limit` and links to src/calc.py, but no root reaches it
        self.assertNotIn("orphan.md", p)


class DocumentationMap(DocsCase):
    """AC-5"""

    DEPTHS = (
        ("README.md", 0),
        ("CONVENTIONS.md", 0),
        ("docs/usage.md", 1),
        ("docs/guide.md", 1),
        ("docs/keys.md", 1),
        ("docs/names.md", 1),
        ("docs/plain.md", 1),
        ("docs/deep.md", 2),
    )

    def test_every_markdown_document_of_the_graph_with_its_depth(self):
        self.change_code()
        p = self.prompt()
        for path, depth in self.DEPTHS:
            self.assertRegex(p, r"`%s`[^\n]*\bdepth\W*%d\b" % (re.escape(path), depth))
        # the default cap of 100 cuts nothing here, and only Markdown documents are mapped
        self.assertEqual(len(MAP_ENTRY.findall(p)), len(self.DEPTHS))
        self.assertNotRegex(p, r"`src/calc\.py`[^\n]*\bdepth\W*\d")
        self.assertNotRegex(p, r"(?m)^\W*and \d+ more\W*$")

    def test_the_map_is_cut_at_doc_map_max(self):
        self.change_code()
        self.set_review("doc_map_max = 3\n")
        p = self.prompt()
        self.assertEqual(len(MAP_ENTRY.findall(p)), 3)
        self.assertRegex(p, r"`README\.md`[^\n]*\bdepth\W*0\b")
        self.assertNotRegex(p, r"`docs/deep\.md`[^\n]*\bdepth\W*2\b")
        self.assertRegex(p, r"\band %d more\b" % (len(self.DEPTHS) - 3))
        # the cap is the map's: the related documents are still listed
        self.assertTrue(entries(p, "docs/guide.md", "mentions"))


class OffAndFailures(DocsCase):
    """AC-1 (an empty list turns it off), AC-6"""

    def test_an_empty_list_of_roots_leaves_the_section_out(self):
        self.change_code()
        self.set_review("doc_roots = []\n")
        p = self.prompt()
        self.assertIn("retry_limit = 3", p)  # the diff is there
        self.assertEqual(MAP_ENTRY.findall(p), [])
        self.assertEqual(entries(p, "docs/usage.md", "links to"), [])
        self.assertEqual(entries(p, "docs/guide.md", "mentions"), [])
        self.assertNotIn("docs/plain.md", p)

    def test_roots_that_do_not_exist_are_said_in_one_line(self):
        self.change_code()
        self.set_review('doc_roots = ["NOPE.md", "docs/ALSO-NOPE.md"]\n')
        p = self.prompt()
        self.assertIn("retry_limit = 3", p)  # the review goes on, with the diff
        no_root = re.compile(r"(?i)none of the[^\n]*roots? exist")
        said = [
            line
            for line in p.splitlines()
            if not line.startswith(("+", "-")) and no_root.search(line)
        ]
        self.assertEqual(len(said), 1, said)
        self.assertEqual(MAP_ENTRY.findall(p), [])
        self.assertEqual(entries(p, "docs/usage.md", "links to"), [])

    def test_an_os_error_is_logged_once_and_the_prompt_is_still_built(self):
        self.change_code()
        self.commit_all("the change")
        ctx = preflight(self.repo)
        ctx.log = mock.Mock()
        with mock.patch.object(docgraph, "build", side_effect=OSError("disk gone")):
            p = build_prompt(ctx, State(), self.rdir(), 1)
        self.assertIn("retry_limit = 3", p)
        self.assertRegex(p, r"(?i)unavailable")
        self.assertEqual(MAP_ENTRY.findall(p), [])
        logged = [
            arg
            for call in ctx.log.mock_calls
            for arg in call.args
            if isinstance(arg, str) and "disk gone" in arg
        ]
        self.assertEqual(len(logged), 1, ctx.log.mock_calls)
        self.assertNotIn("\n", logged[0])


class Version(unittest.TestCase):
    """AC-7: the checklist changed, so the prompt version did"""

    def test_prompt_version_is_past_ten(self):
        self.assertGreater(int(PROMPT_VERSION), 10)
