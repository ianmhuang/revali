"""feature/doc-sync-checks (M3.5.1): the repository's documents form one link graph from
README.md, CONVENTIONS.md and CLAUDE.md, and the configuration keys, subcommands and
pipeline stages the code defines are named in the documentation."""

import glob
import os
import re
import shutil
import subprocess
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


SECTION_SPAN = re.compile(r"^\[([^\]]+)\]\s+(.*)$")


def doc_section(section):
    """A document's `[validate.<platform>]` (any name in angle brackets) is
    `[validate.platform]`."""
    return template_section(re.sub(r"<[^>]*>", "platform", section))


def documented_in(dotted, spans):
    """A span names the key: `[section] key ...` with the key's own section, or the key as a
    word in a span that names no section."""
    section, _, name = dotted.rpartition(".")
    word = re.compile(r"(?<![A-Za-z0-9_])%s(?![A-Za-z0-9_])" % re.escape(name))
    for span in spans:
        m = SECTION_SPAN.match(span.strip())
        if m:
            if doc_section(m.group(1).strip()) == section and word.search(m.group(2)):
                return True
        elif word.search(span):
            return True
    return False


def undocumented_keys(keys, docs, templates):
    """The dotted keys named neither in a document (in backticks, under its own section
    when the span names one) nor under their own section in a template (`name =`,
    commented or not)."""
    in_templates = set()
    for text in templates:
        for section, key in docgraph.toml_keys(text):
            in_templates.add(template_section(section) + "." + key)
    spans = [s for text in docs for s in docgraph.code_spans(text)]
    return [k for k in keys if k not in in_templates and not documented_in(k, spans)]


def usage_block(readme):
    section = readme.split("\n## Usage\n", 1)[1].split("\n## ", 1)[0]
    return section.split("```", 2)[1]


def tracked_markdown():
    """The Markdown files git tracks or would add (untracked, not ignored)."""
    out = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard", "--", "*.md"],
        cwd=ROOT,
        capture_output=True,
        check=True,
    ).stdout.decode("utf-8")
    return sorted({f for f in out.split("\0") if f and os.path.exists(os.path.join(ROOT, f))})


def commands():
    parser = build_parser()
    for action in parser._actions:
        if action.choices and isinstance(action.choices, dict):
            return sorted(action.choices)
    raise AssertionError("no subcommands registered")


def missing_commands(names, usage):
    """A command counts as a token of its own: between spaces, `|` or the line's ends (not
    inside `--version` or `<branch>`)."""
    return [
        n
        for n in names
        if not re.search(r"(?:^|(?<=[\s|]))%s(?=$|[\s|])" % re.escape(n), usage, re.M)
    ]


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

    def test_a_footnote_is_not_a_link(self):
        self.put("README.md", "Text[^1] and [a][ref].\n\n[^1]: See the note\n[ref]: a.md\n")
        self.put("a.md")
        g = docgraph.build(self.root, ["README.md"])
        self.assertEqual(list(g.nodes), ["README.md", "a.md"])
        self.assertEqual(g.broken, [])

    def test_a_link_out_of_the_root_is_reported_not_followed(self):
        os.makedirs(os.path.join(self.root, "repo", "docs"))
        self.put("other/README.md", "[deeper](deeper.md)\n")
        self.put("other/deeper.md")
        self.put("repo/README.md", "[up](../other/README.md)\n[docs](docs/a.md)\n")
        self.put("repo/docs/a.md", "[rooted](/LICENSE) [far](/../other/README.md)\n")
        self.put("repo/LICENSE", "MIT\n")
        repo = os.path.join(self.root, "repo")
        g = docgraph.build(repo, ["README.md"])
        self.assertEqual(list(g.nodes), ["README.md", "docs/a.md", "LICENSE"])
        self.assertEqual(g.nodes["LICENSE"].linked_from, [("docs/a.md", 1)])  # "/" is the root
        self.assertEqual(
            g.outside,
            [("README.md", 1, "../other/README.md"), ("docs/a.md", 1, "/../other/README.md")],
        )
        self.assertEqual(g.broken, [])
        self.assertEqual(
            docgraph.broken_links(g),
            [
                "README.md:1 outside the repository: ../other/README.md",
                "docs/a.md:1 outside the repository: /../other/README.md",
            ],
        )

    def test_line_numbers_count_newlines_only(self):
        self.put("README.md", "a\x0cb   c\n[gone](gone.md)\n")
        g = docgraph.build(self.root, ["README.md"])
        self.assertEqual(g.broken, [("README.md", 2, "gone.md")])

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
        # given the candidates (what git tracks), a file outside them is not one
        self.put("venv/lib/README.md")
        tracked = ["README.md", "a.md", "lost.md", "tests/fixture.md", ".hidden/notes.md"]
        tracked += ["bench/cases/x/change.md", "tool.py"]
        self.assertEqual(
            docgraph.orphans(self.root, g, exclude=("tests", "bench/cases"), files=tracked),
            ["lost.md"],
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
        # a span that names a section counts for that section's key only
        docs = ["`[review] effort`, `[review] tests`, `[validate.<platform>] sandbox_dir`"]
        self.assertEqual(
            undocumented_keys(
                [
                    "review.effort",
                    "validate.effort",
                    "validate.tests",
                    "validate.platform.sandbox_dir",
                ],
                docs,
                [],
            ),
            ["validate.effort", "validate.tests"],
        )

    def test_a_missing_command(self):
        self.assertEqual(missing_commands(["run", "zap"], "revali.py run  # x\n"), ["zap"])
        self.assertEqual(missing_commands(["run"], "revali.py rerun\n"), ["run"])
        self.assertEqual(missing_commands(["version"], "revali.py --version\n"), ["version"])
        self.assertEqual(missing_commands(["branch"], "revali.py clean <branch>\n"), ["branch"])
        self.assertEqual(missing_commands(["stop", "version"], "x status | stop | version\n"), [])

    def test_a_missing_stage(self):
        self.assertEqual(
            docgraph.missing_names(["review", "merged"], ["the `review` stage", "merged"]),
            ["merged"],
        )
        # a stage is named only by a span of its own, not inside a key or a path
        spans = ["`[validate] effort`, `logs/issue-1-merged.md`, `$pr`, ` review `"]
        self.assertEqual(
            docgraph.missing_names(["validate", "merged", "pr", "review"], spans, exact=True),
            ["validate", "merged", "pr"],
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
        # AC-3; the candidates are what git tracks or would add, not an ignored venv/
        files = tracked_markdown()
        self.assertIn("README.md", files)
        self.assertEqual(docgraph.orphans(ROOT, self.graph, exclude=NOT_DOCS, files=files), [])

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
        self.assertEqual(docgraph.missing_names(list(STAGES), docs, exact=True), [])  # AC-6


if __name__ == "__main__":
    unittest.main()
