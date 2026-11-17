"""Acceptance tests for feature/related-docs, AC-8 and AC-9: a long fence is closed only by
a fence of its own character that is at least as long, and a link whose target exists only
in another letter case is broken on every platform."""

import os
import shutil
import tempfile
import unittest
from unittest import mock

from revali import docgraph


def prose_numbers(text):
    return [no for no, _ in docgraph.prose_lines(text)]


class Tree(unittest.TestCase):
    def setUp(self):
        self.root = os.path.realpath(tempfile.mkdtemp(prefix="revali fencecase "))
        self.addCleanup(shutil.rmtree, self.root, True)

    def put(self, rel, text=""):
        path = os.path.join(self.root, *rel.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)


class LongFences(Tree):
    """AC-8"""

    def test_three_backticks_do_not_close_four(self):
        text = "before\n````md\n```\ninside one\n```\ninside two\n````\nafter\n"
        self.assertEqual(prose_numbers(text), [1, 8, 9])  # 9: the empty end of the text

    def test_three_tildes_do_not_close_four(self):
        text = "~~~~\n~~~\ninside one\n~~~\ninside two\n~~~~\nafter"
        self.assertEqual(prose_numbers(text), [7])

    def test_a_long_fence_of_the_other_character_does_not_close(self):
        text = "````\n```\ninside one\n~~~~\ninside two\n```\n````\nafter"
        self.assertEqual(prose_numbers(text), [8])

    def test_a_fence_at_least_as_long_closes(self):
        self.assertEqual(prose_numbers("````\n```\ninside\n````\nafter"), [5])
        self.assertEqual(prose_numbers("````\n```\ninside\n``````\nafter"), [5])
        self.assertEqual(prose_numbers("~~~~~\n~~~~\ninside\n~~~~~\nafter"), [5])

    def test_a_link_inside_a_long_block_is_not_followed(self):
        self.put(
            "README.md",
            "````md\n```\n[inside](inside.md)\n```\n[still inside](still.md)\n````\n"
            "[after](after.md)\n",
        )
        self.put("inside.md")
        self.put("still.md")
        self.put("after.md")
        graph = docgraph.build(self.root, ["README.md"])
        self.assertEqual(list(graph.nodes), ["README.md", "after.md"])
        self.assertEqual(graph.broken, [])


class LetterCase(Tree):
    """AC-9"""

    def exists_ignoring_case(self):
        """os.path.exists as Windows and macOS answer it below the tree: letter case does
        not matter. Paths outside the tree get the real answer."""
        real = os.path.exists
        root = self.root

        def exists(path):
            full = os.path.abspath(os.fspath(path))
            if full != root and not full.startswith(root + os.sep):
                return real(path)
            here = root
            for part in os.path.relpath(full, root).split(os.sep):
                if part == os.curdir:
                    continue
                try:
                    names = os.listdir(here)
                except OSError:
                    return False
                match = [n for n in names if n.lower() == part.lower()]
                if not match:
                    return False
                here = os.path.join(here, match[0])
            return True

        return mock.patch("os.path.exists", side_effect=exists)

    def tree(self):
        self.put(
            "README.md",
            "[file case](docs/A.md)\n[directory case](Docs/a.md)\n[exact](docs/a.md)\n",
        )
        self.put("docs/a.md", "[up](../README.md)\n")

    def check(self, graph):
        self.assertEqual(list(graph.nodes), ["README.md", "docs/a.md"])
        self.assertEqual(
            graph.broken, [("README.md", 1, "docs/A.md"), ("README.md", 2, "Docs/a.md")]
        )
        self.assertEqual(
            docgraph.broken_links(graph),
            ["README.md:1 missing: docs/A.md", "README.md:2 missing: Docs/a.md"],
        )

    def test_on_this_file_system(self):
        self.tree()
        self.check(docgraph.build(self.root, ["README.md"]))

    def test_on_a_file_system_that_ignores_case(self):
        self.tree()
        with self.exists_ignoring_case():
            self.assertTrue(os.path.exists(os.path.join(self.root, "Docs", "A.md")))
            graph = docgraph.build(self.root, ["README.md"])
        self.check(graph)


if __name__ == "__main__":
    unittest.main()
