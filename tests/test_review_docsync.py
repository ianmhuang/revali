"""Reviewer tests for feature/doc-sync-checks, AC-2 to AC-7: this repository's documents
form one link graph from README.md, CONVENTIONS.md and CLAUDE.md with no missing target
and no Markdown file outside it, every key of defaults.toml, every subcommand and every
pipeline stage is named where the acceptance criteria say, and each check, given a broken
input, names the item."""

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
# AC-3: not counted
NOT_COUNTED = (".revali", ".git", "tests", "bench/cases")
KEY_DOCS = ("docs/configuration.md", "docs/files.md")
KEY_TEMPLATES = ("templates/revali.toml", "templates/user-config.toml")
SPAN = re.compile(r"`[^`\n]*`")


def read(rel):
    with open(os.path.join(ROOT, *rel.split("/")), encoding="utf-8") as fh:
        return fh.read()


def nodes(graph):
    held = graph.nodes
    if isinstance(held, dict):
        return dict(held)
    return {node.path: node for node in held}


def as_word(name):
    return re.compile(r"(?<![A-Za-z0-9_])%s(?![A-Za-z0-9_])" % re.escape(name))


def in_backticks(name, text):
    word = as_word(name)
    return any(word.search(span) for span in SPAN.findall(text))


def in_template(name, text):
    """`name =` at the start of a line, commented out or not."""
    return re.search(r"(?m)^[ \t]*#?[ \t]*%s[ \t]*=" % re.escape(name), text) is not None


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


def exempted_keys():
    """AC-4: a key meant to stay undocumented is listed, with its reason, in the author's
    test. Read from there when it holds such a list; none is expected today."""
    try:
        from tests import test_doc_sync as author
    except ImportError:
        return set()
    return set(getattr(author, "UNDOCUMENTED", None) or ())


def subcommands():
    for action in build_parser()._actions:
        if isinstance(action.choices, dict) and action.choices:
            return sorted(action.choices)
    raise AssertionError("the parser registers no subcommand")


def usage_block(readme):
    section = readme.split("\n## Usage\n", 1)[1].split("\n## ", 1)[0]
    return section.split("```", 2)[1]


def repository_markdown():
    """AC-3 is about the Markdown files in the repository: what git tracks or would add.
    None when ROOT is not a git checkout, so the caller walks the tree instead."""
    listing = ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"]
    try:
        done = subprocess.run(
            listing + ["--", "*.md"], cwd=ROOT, capture_output=True, check=True, timeout=60
        )
    except (OSError, subprocess.SubprocessError):
        return None
    names = done.stdout.decode("utf-8", "replace").split("\0")
    return sorted(n for n in names if n and os.path.exists(os.path.join(ROOT, *n.split("/"))))


class TreeCase(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="revali review sync ")
        self.addCleanup(shutil.rmtree, self.root, True)

    def put(self, rel, text=""):
        path = os.path.join(self.root, *rel.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)


class ThisRepository(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.graph = docgraph.build(ROOT, ROOTS)
        cls.nodes = nodes(cls.graph)

    def test_the_three_roots_start_the_graph(self):
        for root in ROOTS:
            self.assertIn(root, self.nodes)
            self.assertEqual(self.nodes[root].depth, 0, root)

    def test_the_graph_holds_pages_the_readme_does_not_link_itself(self):
        # AC-2: every page of the graph is checked, so it must reach past the first level
        deep = [p for p, n in self.nodes.items() if p.lower().endswith(".md") and n.depth >= 2]
        self.assertNotEqual(deep, [])

    def test_no_page_of_the_graph_links_to_a_missing_file(self):
        self.assertEqual(list(self.graph.broken), [])  # AC-2
        self.assertEqual(docgraph.broken_links(self.graph), [])

    def test_every_node_of_the_graph_exists(self):
        gone = [p for p in self.nodes if not os.path.exists(os.path.join(ROOT, *p.split("/")))]
        self.assertEqual(gone, [])

    def test_no_markdown_file_is_outside_the_graph(self):
        files = repository_markdown()
        if files is not None:
            self.assertIn("README.md", files)
        found = docgraph.orphans(ROOT, self.graph, exclude=NOT_COUNTED, files=files)
        self.assertEqual(list(found), [])  # AC-3

    def test_every_key_of_defaults_toml_is_named(self):
        docs = [read(p) for p in KEY_DOCS]
        templates = [read(p) for p in KEY_TEMPLATES]
        exempt = exempted_keys()
        keys = default_keys()
        self.assertIn("review.max_diff_lines", keys)  # dotted paths, as in the AC
        missing = []
        for dotted in keys:
            name = dotted.rsplit(".", 1)[-1]
            named = any(in_backticks(name, d) for d in docs)
            named = named or any(in_template(name, t) for t in templates)
            if not named and dotted not in exempt:
                missing.append(dotted)
        self.assertEqual(missing, [])  # AC-4

    def test_the_keys_that_were_documented_nowhere_are_settled(self):
        docs = [read(p) for p in KEY_DOCS]
        templates = [read(p) for p in KEY_TEMPLATES]
        exempt = exempted_keys()
        named_in_the_ac = [
            "engines.claude.helper_prefix",
            "project.change_source",
            "project.context_files",
            "review.small_max_lines",
            "review.security_paths",
            "validate.platform.network",
            "validate.platform.check_name",
            "merge.auto_merge",
        ]
        unsettled = []
        for dotted in named_in_the_ac:
            name = dotted.rsplit(".", 1)[-1]
            named = any(in_backticks(name, d) for d in docs)
            named = named or any(in_template(name, t) for t in templates)
            if not named and dotted not in exempt:
                unsettled.append(dotted)
        self.assertEqual(unsettled, [])  # AC-4: documented, or listed with a reason

    def test_every_subcommand_is_in_the_usage_block(self):
        names = subcommands()
        self.assertIn("run", names)
        self.assertIn("merge", names)
        usage = usage_block(read("README.md"))
        # the command as a token of its own: `--version` is a flag and `<branch>` an argument
        missing = [
            n for n in names if not re.search(r"(?<![^\s|])%s(?![^\s|])" % re.escape(n), usage)
        ]
        self.assertEqual(missing, [])  # AC-5

    def test_every_stage_is_named_in_backticks_in_docs(self):
        docs = [
            read("docs/" + os.path.basename(p))
            for p in sorted(glob.glob(os.path.join(ROOT, "docs", "*.md")))
        ]
        self.assertIn("ready_to_merge", STAGES)
        spans = {span for d in docs for span in SPAN.findall(d)}
        missing = [s for s in STAGES if "`%s`" % s not in spans]
        self.assertEqual(missing, [])  # AC-6
        self.assertEqual(list(docgraph.missing_names(list(STAGES), docs, exact=True)), [])


class BrokenInputs(TreeCase):
    """AC-7: each check is a function of its inputs and names the item that is wrong."""

    def test_a_missing_link_target_is_named_with_page_and_line(self):
        self.put("README.md", "[guide](docs/guide.md)\n")
        self.put("docs/guide.md", "first line\n[removed](removed.md)\n")

        graph = docgraph.build(self.root, ["README.md"])
        messages = docgraph.broken_links(graph)
        self.assertEqual(len(messages), 1, messages)
        self.assertRegex(messages[0], r"docs/guide\.md\D{0,10}2(?!\d)")
        self.assertIn("removed.md", messages[0])

    def test_an_orphan_is_named_and_the_excluded_directories_are_not_counted(self):
        self.put("README.md", "[a](docs/a.md) [bench](bench/README.md)\n")
        self.put("docs/a.md", "x\n")
        self.put("bench/README.md", "x\n")
        self.put("docs/lost.md", "nobody links here\n")
        self.put("bench/notes.md", "nor here\n")
        self.put("docs/data.toml", "# not Markdown, so not an orphan\n")
        self.put("bench/cases/case-1/change.md", "case data\n")
        self.put("tests/fixtures/sample.md", "a fixture\n")
        self.put(".revali/feature__x/change.md", "a branch's state\n")
        self.put(".git/info/notes.md", "git's own\n")

        graph = docgraph.build(self.root, ["README.md"])
        found = docgraph.orphans(self.root, graph, exclude=NOT_COUNTED)
        self.assertEqual(sorted(found), ["bench/notes.md", "docs/lost.md"])

    def test_a_tree_with_every_page_linked_has_no_orphan(self):
        self.put("README.md", "[a](docs/a.md)\n")
        self.put("docs/a.md", "[b](b.md)\n")
        self.put("docs/b.md", "x\n")

        graph = docgraph.build(self.root, ["README.md"])
        self.assertEqual(list(docgraph.orphans(self.root, graph, exclude=NOT_COUNTED)), [])

    def test_a_missing_stage_is_named(self):
        texts = ["`needs_human` means a human must decide", "the PR is merged, without backticks"]
        self.assertEqual(list(docgraph.missing_names(["needs_human", "merged"], texts)), ["merged"])
        self.assertEqual(list(docgraph.missing_names(["needs_human"], texts)), [])

    def test_a_stage_inside_a_longer_span_is_still_missing(self):
        # AC-6: the stage in backticks, not a key or a path that happens to hold the word
        texts = ["`[validate] effort` and `logs/issue-1-merged.md`, then `review` on its own"]
        self.assertEqual(
            list(docgraph.missing_names(["validate", "merged", "review"], texts, exact=True)),
            ["validate", "merged"],
        )

    def test_given_the_repository_files_only_those_are_candidates(self):
        self.put("README.md", "[a](docs/a.md)\n")
        self.put("docs/a.md", "x\n")
        self.put("docs/lost.md", "in the repository, linked from nowhere\n")
        self.put("venv/lib/site/README.md", "on disk, ignored by git\n")

        graph = docgraph.build(self.root, ["README.md"])
        tracked = ["README.md", "docs/a.md", "docs/lost.md", "revali/docgraph.py"]
        tracked += ["tests/fixtures/sample.md", "bench/cases/case-1/change.md"]
        found = docgraph.orphans(self.root, graph, exclude=NOT_COUNTED, files=tracked)
        self.assertEqual(list(found), ["docs/lost.md"])

    def test_a_name_inside_a_longer_word_does_not_count(self):
        texts = ["`max_diff_lines_total` and `premerged`"]
        self.assertEqual(
            list(docgraph.missing_names(["max_diff_lines", "merged"], texts)),
            ["max_diff_lines", "merged"],
        )

    def test_template_keys_are_read_with_their_section(self):
        text = (
            '[review]\neffort = "high"\n# budget_usd = 1.0\n' '\n[validate.linux]\nrunner = "wsl"\n'
        )
        self.assertEqual(
            [tuple(k) for k in docgraph.toml_keys(text)],
            [("review", "effort"), ("review", "budget_usd"), ("validate.linux", "runner")],
        )

    def test_an_undocumented_key_and_a_missing_command_are_named(self):
        # the key and command checks live beside the author's repository tests
        from tests import test_doc_sync as author

        missing = author.undocumented_keys(
            ["review.max_diff_lines", "review.budget_usd", "review.brand_new_key"],
            ["`max_diff_lines` is the limit of the diff"],
            ["[review]\nbudget_usd = 1.0\n"],
        )
        self.assertEqual(list(missing), ["review.brand_new_key"])
        self.assertEqual(
            list(author.missing_commands(["run", "zap"], "python revali.py run   # detached\n")),
            ["zap"],
        )
        # AC-5: a flag or an argument that holds the command's name is not the command
        usage = "python revali.py status | clean <branch>\npython revali.py --version\n"
        self.assertEqual(
            list(author.missing_commands(["status", "clean", "version", "branch"], usage)),
            ["version", "branch"],
        )


if __name__ == "__main__":
    unittest.main()
