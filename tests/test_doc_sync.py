"""feature/doc-sync-checks (M3.5.1): the repository's documents form one link graph from
README.md, CONVENTIONS.md and CLAUDE.md, and the configuration keys, subcommands and
pipeline stages the code defines are named in the documentation."""

import glob
import os
import re
import shutil
import tempfile
import tomllib
import unittest

from revali import docgraph
from revali.cli import build_parser
from revali.state import STAGES
from tests.helpers import ROOT

ROOTS = ["README.md", "CONVENTIONS.md", "CLAUDE.md"]
# not documentation: test fixtures, and the benchmark's cases (a past round's text, verbatim)
NOT_DOCS = ("tests", "bench/cases")
KEY_DOCS = ["docs/configuration.md", "docs/files.md"]
KEY_TEMPLATES = ["templates/revali.toml", "templates/user-config.toml"]
# a key that is meant to stay out of the documentation, with the reason
UNDOCUMENTED = {}


def read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


def default_keys():
    with open(os.path.join(ROOT, "defaults.toml"), "rb") as fh:
        data = tomllib.load(fh)

    def walk(table, prefix):
        for key, value in table.items():
            if isinstance(value, dict):
                yield from walk(value, prefix + key + ".")
            else:
                yield prefix + key

    return list(walk(data, ""))


def template_section(section):
    """A template's `[validate.<platform>]` table holds the keys defaults.toml keeps under
    `[validate.platform]`."""
    if section.startswith("validate.") and section.count(".") == 1:
        return "validate.platform"
    return section


def undocumented_keys(keys, docs, templates):
    """The dotted keys named neither in a document (the key name in backticks) nor under
    their own section in a template (`name =`, commented or not)."""
    in_templates = set()
    for text in templates:
        for section, key in docgraph.toml_keys(text):
            in_templates.add(template_section(section) + "." + key)
    missing = []
    for dotted in keys:
        if dotted in in_templates:
            continue
        if docgraph.missing_names([dotted.rsplit(".", 1)[-1]], docs):
            missing.append(dotted)
    return missing


def usage_block(readme):
    section = readme.split("\n## Usage\n", 1)[1].split("\n## ", 1)[0]
    return section.split("```", 2)[1]


def commands():
    parser = build_parser()
    for action in parser._actions:
        if action.choices and isinstance(action.choices, dict):
            return sorted(action.choices)
    raise AssertionError("no subcommands registered")


def missing_commands(names, usage):
    return [n for n in names if not re.search(r"\b%s\b" % re.escape(n), usage)]


class TempTree(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="revali docgraph ")
        self.addCleanup(shutil.rmtree, self.root, True)

    def put(self, rel, text=""):
        path = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)


class Graph(TempTree):
    """AC-1"""

    def test_links_are_followed_at_any_depth(self):
        self.put("README.md", "# x\n\nSee [docs/a.md](docs/a.md).\n")
        self.put("docs/a.md", "line\n[b](sub/b.md) and [code](../tool.py)\n")
        self.put("docs/sub/b.md", "[c](c.md)\n")
        self.put("docs/sub/c.md", "the end, [LICENSE](../../LICENSE)\n")
        self.put("tool.py", "[not followed](nowhere.md)\n")
        self.put("LICENSE", "MIT\n")
        g = docgraph.build(self.root, ["README.md"])
        depth = {p: n.depth for p, n in g.nodes.items()}
        self.assertEqual(
            depth,
            {
                "README.md": 0,
                "docs/a.md": 1,
                "docs/sub/b.md": 2,
                "tool.py": 2,
                "docs/sub/c.md": 3,
                "LICENSE": 4,
            },
        )
        self.assertEqual(g.nodes["docs/sub/b.md"].linked_from, [("docs/a.md", 2)])
        self.assertEqual(g.nodes["tool.py"].linked_from, [("docs/a.md", 2)])
        self.assertEqual(g.broken, [])  # tool.py is a leaf: its text is not read

    def test_what_is_not_a_link_to_follow(self):
        self.put(
            "README.md",
            "```\n[fenced](fenced.md)\n```\n"
            "[web](https://example.com/x.md) [mail](mailto:a@b.c) [here](#usage)\n"
            "`[spanned](spanned.md)` is code\n",
        )
        g = docgraph.build(self.root, ["README.md"])
        self.assertEqual(list(g.nodes), ["README.md"])
        self.assertEqual(g.broken, [])

    def test_an_anchor_is_dropped_and_a_cycle_ends(self):
        self.put("README.md", "[a](a.md#part)\n")
        self.put("a.md", "[back](README.md) [self](a.md#top)\n")
        g = docgraph.build(self.root, ["README.md"])
        self.assertEqual(list(g.nodes), ["README.md", "a.md"])
        self.assertEqual(g.nodes["README.md"].linked_from, [("a.md", 1)])

    def test_reference_links_spaces_and_directories(self):
        self.put("README.md", "[a][ref] and [b](my%20notes.md) and [dir](docs/)\n\n[ref]: a.md\n")
        self.put("a.md")
        self.put("my notes.md")
        self.put("docs/x.md")
        g = docgraph.build(self.root, ["README.md"])
        self.assertEqual(set(g.nodes), {"README.md", "a.md", "my notes.md", "docs"})
        self.assertEqual(g.broken, [])

    def test_several_roots_and_a_missing_root(self):
        self.put("README.md", "x\n")
        self.put("CONVENTIONS.md", "[a](a.md)\n")
        self.put("a.md")
        g = docgraph.build(self.root, ["README.md", "CONVENTIONS.md", "CLAUDE.md"])
        self.assertEqual(list(g.nodes), ["README.md", "CONVENTIONS.md", "a.md"])


class Checks(TempTree):
    """AC-7: each check, given a broken input, names the item"""

    def test_a_missing_link_target(self):
        self.put("README.md", "[a](a.md)\n")
        self.put("a.md", "one\ntwo [gone](gone.md)\n")
        g = docgraph.build(self.root, ["README.md"])
        self.assertEqual(g.broken, [("a.md", 2, "gone.md")])
        self.assertEqual(docgraph.broken_links(g), ["a.md:2 missing: gone.md"])
        self.assertNotIn("gone.md", g.nodes)

    def test_an_orphan(self):
        self.put("README.md", "[a](a.md)\n")
        self.put("a.md")
        self.put("lost.md")
        self.put("docs/lost-too.md")
        self.put("tests/fixture.md")
        self.put(".hidden/notes.md")
        g = docgraph.build(self.root, ["README.md"])
        self.assertEqual(
            docgraph.orphans(self.root, g, exclude=("tests",)), ["docs/lost-too.md", "lost.md"]
        )

    def test_an_undocumented_key(self):
        docs = ["`max_diff_lines` is the limit", "[review] budget"]
        templates = ["[review]\n# effort = 'high'\n", "# [paths]\n# archive_dir = 'a'\n"]
        keys = ["review.max_diff_lines", "review.effort", "paths.archive_dir", "review.budget_usd"]
        self.assertEqual(undocumented_keys(keys, docs, templates), ["review.budget_usd"])
        # a key under the wrong section of a template does not count
        self.assertEqual(undocumented_keys(["merge.effort"], [], templates), ["merge.effort"])
        self.assertEqual(
            undocumented_keys(["validate.platform.distro"], [], ["[validate.linux]\ndistro = 'U'"]),
            [],
        )

    def test_a_missing_command(self):
        self.assertEqual(missing_commands(["run", "zap"], "revali.py run  # x\n"), ["zap"])
        self.assertEqual(missing_commands(["run"], "revali.py rerun\n"), ["run"])

    def test_a_missing_stage(self):
        self.assertEqual(
            docgraph.missing_names(["review", "merged"], ["the `review` stage", "merged"]),
            ["merged"],
        )

    def test_toml_keys(self):
        text = "top = 1\n[a]\nx = 1\n  # y = 2   # note\n# [b.c]\n#z=3\n# prose = words\n"
        self.assertEqual(
            docgraph.toml_keys(text),
            [("", "top"), ("a", "x"), ("a", "y"), ("b.c", "z"), ("b.c", "prose")],
        )


class Repository(unittest.TestCase):
    """AC-2 to AC-6, on this repository"""

    @classmethod
    def setUpClass(cls):
        cls.graph = docgraph.build(ROOT, ROOTS)

    def test_no_link_in_the_graph_is_broken(self):
        self.assertEqual(docgraph.broken_links(self.graph), [])  # AC-2

    def test_the_graph_reaches_past_the_first_level(self):
        self.assertIn("bench/README.md", self.graph.nodes)
        self.assertGreaterEqual(self.graph.nodes["bench/README.md"].depth, 2)

    def test_no_markdown_file_is_outside_the_graph(self):
        self.assertEqual(docgraph.orphans(ROOT, self.graph, exclude=NOT_DOCS), [])  # AC-3

    def test_every_default_key_is_documented(self):
        docs = [read(p) for p in KEY_DOCS]
        templates = [read(p) for p in KEY_TEMPLATES]
        missing = [
            k for k in undocumented_keys(default_keys(), docs, templates) if k not in UNDOCUMENTED
        ]
        self.assertEqual(missing, [])  # AC-4
        self.assertEqual([k for k in UNDOCUMENTED if k not in default_keys()], [])

    def test_every_command_is_in_the_usage_block(self):
        names = commands()
        self.assertIn("run", names)
        self.assertEqual(missing_commands(names, usage_block(read("README.md"))), [])  # AC-5

    def test_every_stage_is_named_in_docs(self):
        docs = [
            read(os.path.relpath(p, ROOT)) for p in glob.glob(os.path.join(ROOT, "docs", "*.md"))
        ]
        self.assertEqual(docgraph.missing_names(list(STAGES), docs), [])  # AC-6


if __name__ == "__main__":
    unittest.main()
