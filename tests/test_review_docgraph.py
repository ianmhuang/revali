"""Reviewer tests for feature/doc-sync-checks, AC-1 (and the broken link of AC-2 / AC-7):
`revali.docgraph.build` returns every file reachable through relative Markdown links from
the root files, at any depth, with each node's depth and the pages that link to it; what
is not a relative link is not followed, a cycle ends, and a link to a missing file is
reported with its page and line."""

import os
import re
import shutil
import tempfile
import unittest

from revali import docgraph


def nodes(graph):
    """path -> node, whether the graph keeps its nodes in a dict or in a list."""
    held = graph.nodes
    if isinstance(held, dict):
        return dict(held)
    return {node.path: node for node in held}


def linkers(node):
    """The pages that link to a node, without the line when the graph records one."""
    return sorted({e if isinstance(e, str) else e[0] for e in node.linked_from})


class TreeCase(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="revali review graph ")
        self.addCleanup(shutil.rmtree, self.root, True)

    def put(self, rel, text=""):
        path = os.path.join(self.root, *rel.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)

    def build(self, *roots):
        return docgraph.build(self.root, list(roots))


class Reachable(TreeCase):
    def test_markdown_is_followed_at_any_depth_and_other_files_are_leaves(self):
        self.put("README.md", "# Title\n\nSee [guide](docs/guide.md) and [conf](defaults.toml).\n")
        self.put("docs/guide.md", "Guide\n\n[deep](more/deep.md) and [tool](../tools/run.py)\n")
        self.put("docs/more/deep.md", "[deeper](deeper.md) [licence](../../LICENSE)\n")
        self.put("docs/more/deeper.md", "the end\n")
        # a leaf is not read: the links these three hold lead nowhere and are not reported
        self.put("tools/run.py", "# [nothing](missing-from-code.md)\n")
        self.put("defaults.toml", "# [nothing](missing-from-toml.md)\n")
        self.put("LICENSE", "MIT [nothing](missing-from-licence.md)\n")
        self.put("docs/unlinked.md", "nobody links here\n")

        graph = self.build("README.md")
        found = nodes(graph)
        self.assertEqual(
            {path: node.depth for path, node in found.items()},
            {
                "README.md": 0,
                "docs/guide.md": 1,
                "defaults.toml": 1,
                "docs/more/deep.md": 2,
                "tools/run.py": 2,
                "docs/more/deeper.md": 3,
                "LICENSE": 3,
            },
        )
        self.assertEqual(list(graph.broken), [])

    def test_each_node_names_the_pages_that_link_to_it(self):
        self.put("README.md", "[a](a.md) and [b](b.md)\n")
        self.put("a.md", "[shared](docs/shared.md)\n")
        self.put("b.md", "[shared](docs/shared.md) [code](tool.py)\n")
        self.put("docs/shared.md", "x\n")
        self.put("tool.py", "x = 1\n")

        found = nodes(self.build("README.md"))
        self.assertEqual(linkers(found["README.md"]), [])
        self.assertEqual(linkers(found["a.md"]), ["README.md"])
        self.assertEqual(linkers(found["docs/shared.md"]), ["a.md", "b.md"])
        self.assertEqual(linkers(found["tool.py"]), ["b.md"])

    def test_depth_is_counted_from_the_nearest_root(self):
        self.put("README.md", "[one](one.md)\n")
        self.put("one.md", "[shared](shared.md)\n")
        self.put("CONVENTIONS.md", "[shared](shared.md)\n")
        self.put("shared.md", "x\n")

        found = nodes(self.build("README.md", "CONVENTIONS.md"))
        self.assertEqual(found["README.md"].depth, 0)
        self.assertEqual(found["CONVENTIONS.md"].depth, 0)
        self.assertEqual(found["one.md"].depth, 1)
        self.assertEqual(found["shared.md"].depth, 1)  # two links from README, one from the other
        self.assertEqual(linkers(found["shared.md"]), ["CONVENTIONS.md", "one.md"])

    def test_an_anchor_is_dropped_from_the_target(self):
        self.put("README.md", "[part](guide.md#the-part)\n")
        self.put("guide.md", "## The part\n")

        graph = self.build("README.md")
        self.assertEqual(set(nodes(graph)), {"README.md", "guide.md"})
        self.assertEqual(list(graph.broken), [])


class NotFollowed(TreeCase):
    def test_fenced_blocks_urls_mailto_and_bare_anchors(self):
        self.put(
            "README.md",
            "```\n[fenced](fenced.md)\n[fenced and missing](fenced-missing.md)\n```\n"
            "\n"
            "```toml\n# [fenced with a language](fenced.md)\n```\n"
            "\n"
            "[web](https://example.com/page.md) [plain](http://example.com/other.md)\n"
            "[mail](mailto:someone@example.com) [top](#usage)\n"
            "[real](real.md)\n",
        )
        self.put("fenced.md", "exists, and is still not part of the graph\n")
        self.put("real.md", "x\n")

        graph = self.build("README.md")
        self.assertEqual(set(nodes(graph)), {"README.md", "real.md"})
        self.assertEqual(list(graph.broken), [])

    def test_a_cycle_ends(self):
        self.put("README.md", "[a](a.md)\n")
        self.put("a.md", "[b](b.md) [self](a.md#top) [home](README.md)\n")
        self.put("b.md", "[a](a.md) [home](README.md)\n")

        found = nodes(self.build("README.md"))
        self.assertEqual(
            {path: node.depth for path, node in found.items()},
            {"README.md": 0, "a.md": 1, "b.md": 2},
        )
        self.assertEqual(linkers(found["README.md"]), ["a.md", "b.md"])
        self.assertEqual(linkers(found["a.md"]), ["README.md", "a.md", "b.md"])


class RootBoundary(TreeCase):
    """Round 2: a node's path is relative to the repository root, so nothing outside the
    root is a node, and a target starting with "/" is read from the root."""

    def test_a_link_that_leaves_the_root_is_reported_and_not_followed(self):
        self.put("elsewhere/README.md", "[more](more.md) [gone](gone-outside.md)\n")
        self.put("elsewhere/more.md", "x\n")
        self.put("repo/README.md", "[up](../elsewhere/README.md)\n[in](docs/a.md)\n")
        self.put("repo/docs/a.md", "x\n")

        graph = docgraph.build(os.path.join(self.root, "repo"), ["README.md"])
        self.assertEqual(set(nodes(graph)), {"README.md", "docs/a.md"})

        messages = docgraph.broken_links(graph)
        self.assertEqual(len(messages), 1, messages)  # the page outside is not read
        self.assertRegex(messages[0], r"README\.md\D{0,10}1(?!\d)")
        self.assertIn("../elsewhere/README.md", messages[0])

    def test_a_target_starting_with_a_slash_is_relative_to_the_root(self):
        self.put("README.md", "[a](docs/a.md)\n")
        self.put("docs/a.md", "[licence](/LICENSE) [gone](/docs/gone.md)\n")
        self.put("LICENSE", "MIT\n")

        graph = self.build("README.md")
        found = nodes(graph)
        self.assertEqual(set(found), {"README.md", "docs/a.md", "LICENSE"})
        self.assertEqual(linkers(found["LICENSE"]), ["docs/a.md"])

        messages = docgraph.broken_links(graph)
        self.assertEqual(len(messages), 1, messages)
        self.assertRegex(messages[0], r"docs/a\.md\D{0,10}1(?!\d)")
        self.assertIn("gone.md", messages[0])

    def test_a_footnote_definition_is_not_a_link(self):
        self.put("README.md", "Text[^1] and [a][ref].\n\n[^1]: See the note below\n[ref]: a.md\n")
        self.put("a.md", "x\n")

        graph = self.build("README.md")
        self.assertEqual(set(nodes(graph)), {"README.md", "a.md"})
        self.assertEqual(docgraph.broken_links(graph), [])

    def test_a_target_on_another_drive_or_a_share_is_reported_not_raised(self):
        # Round 3: on Windows the first target is a UNC path and the second another drive
        # (the root check cannot compare them with the root); elsewhere both are names of
        # files that do not exist. Either way each is reported once and the walk goes on.
        self.put(
            "README.md",
            "[share](" + r"\\server\docs\x.md" + ")\n[drive](/Z:/elsewhere/x.md)\n[ok](a.md)\n",
        )
        self.put("a.md", "x\n")

        graph = self.build("README.md")
        self.assertEqual(set(nodes(graph)), {"README.md", "a.md"})

        messages = sorted(docgraph.broken_links(graph), key=lambda m: "elsewhere" in m)
        self.assertEqual(len(messages), 2, messages)
        self.assertRegex(messages[0], r"README\.md\D{0,10}1(?!\d)")
        self.assertIn("server", messages[0])
        self.assertRegex(messages[1], r"README\.md\D{0,10}2(?!\d)")
        self.assertIn("elsewhere", messages[1])


class MissingTarget(TreeCase):
    def test_a_missing_file_is_reported_with_page_and_line_and_not_followed(self):
        # the page with the bad link is two links from the root (AC-2: any page of the graph)
        self.put("README.md", "[a](docs/a.md)\n")
        self.put("docs/a.md", "[b](sub/b.md)\n")
        self.put("docs/sub/b.md", "intro\n\n[gone](../gone.md) and [ok](c.md)\n")
        self.put("docs/sub/c.md", "x\n")

        graph = self.build("README.md")
        found = nodes(graph)
        self.assertEqual(set(found), {"README.md", "docs/a.md", "docs/sub/b.md", "docs/sub/c.md"})

        broken = list(graph.broken)
        self.assertEqual(len(broken), 1, broken)
        page, line, target = broken[0]
        self.assertEqual((page, line), ("docs/sub/b.md", 3))
        self.assertIn("gone.md", target)

        messages = docgraph.broken_links(graph)
        self.assertEqual(len(messages), 1, messages)
        self.assertRegex(messages[0], r"docs/sub/b\.md\D{0,10}3(?!\d)")  # page and line
        self.assertIn("gone.md", messages[0])

    def test_a_whole_tree_without_a_missing_target_reports_nothing(self):
        self.put("README.md", "[a](a.md)\n")
        self.put("a.md", "[home](README.md)\n")

        graph = self.build("README.md")
        self.assertEqual(list(graph.broken), [])
        self.assertEqual(docgraph.broken_links(graph), [])

    def test_the_line_is_the_line_of_the_page_not_of_the_prose(self):
        # a fenced block before the link must not shift the reported line
        self.put("README.md", "one\n```\ncode\n```\nfive\n[gone](gone.md)\n")

        broken = list(self.build("README.md").broken)
        self.assertEqual(len(broken), 1, broken)
        self.assertEqual(tuple(broken[0][:2]), ("README.md", 6))
        self.assertIsNotNone(re.search(r"gone\.md", str(broken[0][2])))


if __name__ == "__main__":
    unittest.main()
