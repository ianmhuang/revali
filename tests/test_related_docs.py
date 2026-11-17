"""feature/related-docs (M3.5.2): the reviewer prompt lists the documents of the link graph
that link to a changed file or name a changed name, with a map of the documentation; plus
the two link-graph fixes left from PR #73 (long fences, letter case of link targets)."""

import os
import unittest
from unittest import mock

from revali import PROMPT_VERSION, docgraph, gitops
from revali.preflight import Stop, preflight
from revali.review import build_prompt
from revali.state import State
from tests.helpers import RepoCase, git
from tests.test_doc_sync import TempTree

SECTION = "## Documents related to this change"


class Fences(TempTree):
    """AC-8"""

    def test_a_short_fence_inside_a_long_one_does_not_close_it(self):
        self.put(
            "README.md",
            "````md\n```\n[inside](inside.md)\n```\n````\n[after](after.md)\n",
        )
        self.put("inside.md")
        self.put("after.md")
        g = docgraph.build(self.root, ["README.md"])
        self.assertEqual(list(g.nodes), ["README.md", "after.md"])

    def test_a_fence_closes_only_with_its_own_character(self):
        self.put("README.md", "~~~\n```\n[inside](inside.md)\n~~~\n[after](after.md)\n")
        self.put("inside.md")
        self.put("after.md")
        g = docgraph.build(self.root, ["README.md"])
        self.assertEqual(list(g.nodes), ["README.md", "after.md"])

    def test_a_closing_fence_carries_no_info_string(self):
        self.put("README.md", "```\n```python\n[inside](inside.md)\n```\n[after](after.md)\n")
        self.put("inside.md")
        self.put("after.md")
        g = docgraph.build(self.root, ["README.md"])
        self.assertEqual(list(g.nodes), ["README.md", "after.md"])

    def test_a_longer_closing_fence_closes(self):
        self.put("README.md", "```\n[inside](inside.md)\n`````\n[after](after.md)\n")
        self.put("inside.md")
        self.put("after.md")
        g = docgraph.build(self.root, ["README.md"])
        self.assertEqual(list(g.nodes), ["README.md", "after.md"])


class LetterCase(TempTree):
    """AC-9"""

    def test_a_target_that_differs_in_case_is_broken_everywhere(self):
        self.put("README.md", "[a](Docs/A.md)\n[b](docs/a.md)\n")
        self.put("docs/a.md")
        g = docgraph.build(self.root, ["README.md"])
        self.assertEqual(list(g.nodes), ["README.md", "docs/a.md"])
        self.assertEqual(g.broken, [("README.md", 1, "Docs/A.md")])

    def test_a_root_that_differs_in_case_is_skipped(self):
        self.put("README.md", "x\n")
        g = docgraph.build(self.root, ["readme.md"])
        self.assertEqual(list(g.nodes), [])


class Names(unittest.TestCase):
    """AC-3: what counts as a name"""

    def test_name_shaped_words(self):
        for word in ("max_procs", "--branch", "revali/config.py", "camelCase", "a.md", "v0.3"):
            self.assertTrue(docgraph.name_like(word), word)
        for word in ("run", "true", "0.3.0", "ab", "a_", "---", "README"):
            self.assertFalse(docgraph.name_like(word), word)

    def test_span_names_split_and_strip(self):
        text = (
            "Set `[review] doc_roots` and `max_procs = 4096`.\n"
            "```\n`inside_fence`\n```\n"
            'Run `revali run --branch <b>` or `"quoted_name"`.\n'
        )
        self.assertEqual(
            docgraph.span_names(text),
            [(1, "doc_roots"), (1, "max_procs"), (5, "--branch"), (5, "quoted_name")],
        )


class Related(TempTree):
    """AC-2, AC-3, AC-4"""

    def related(self, paths, changed):
        g = docgraph.build(self.root, ["README.md"])
        return {r.page: r for r in docgraph.related(g, paths, changed)}

    def test_a_link_to_a_changed_file(self):
        self.put("README.md", "[a](docs/a.md)\n")
        self.put("docs/a.md", "x\n\n[code](../src/x.py)\n")
        self.put("src/x.py", "")
        found = self.related(["src/x.py"], {})
        self.assertEqual(list(found), ["docs/a.md"])
        self.assertEqual(found["docs/a.md"].links, [("src/x.py", 3)])

    def test_a_link_to_a_deleted_file(self):
        self.put("README.md", "[gone](gone.py)\n")
        found = self.related(["gone.py"], {})
        self.assertEqual(found["README.md"].links, [("gone.py", 1)])

    def test_a_name_on_a_changed_line(self):
        self.put("README.md", "[a](a.md)\n")
        self.put("a.md", "one\nThe `retry_wait_s` key and `other_key`.\n")
        found = self.related(["src/x.py"], {"src/x.py": ["    retry_wait_s = 3", "x = 1"]})
        self.assertEqual(list(found), ["a.md"])
        self.assertEqual(found["a.md"].names, [("retry_wait_s", 2)])

    def test_only_whole_words_match(self):
        self.put("README.md", "`wait_s` and `max_procs`\n")
        found = self.related(["x.py"], {"x.py": ["retry_wait_s = 1", "max_procs_total = 2"]})
        self.assertEqual(found, {})

    def test_a_plain_word_never_matches(self):
        self.put("README.md", "`run` and `true`\n")
        found = self.related(["x.py"], {"x.py": ["run = true"]})
        self.assertEqual(found, {})

    def test_a_changed_path_but_not_its_last_part(self):
        self.put("README.md", "`src/x.py` then `x.py`\n")
        found = self.related(["src/x.py"], {})
        self.assertEqual(found["README.md"].names, [("src/x.py", 1)])

    def test_a_document_changed_in_the_diff_is_listed_and_marked(self):
        self.put("README.md", "[code](x.py) and `max_procs`\n")
        self.put("x.py", "")
        found = self.related(["README.md", "x.py"], {"README.md": ["[code](x.py) and `max_procs`"]})
        self.assertTrue(found["README.md"].changed)
        self.assertEqual(found["README.md"].links, [("x.py", 1)])
        # its own changed lines do not count as a mention
        self.assertEqual(found["README.md"].names, [])

    def test_a_document_outside_the_graph_is_not_searched(self):
        self.put("README.md", "x\n")
        self.put("orphan.md", "`max_procs`\n")
        found = self.related(["x.py"], {"x.py": ["max_procs = 1"]})
        self.assertEqual(found, {})

    def test_each_name_once_with_its_first_line(self):
        self.put("README.md", "`max_procs`\n`max_procs`\n")
        found = self.related(["x.py"], {"x.py": ["max_procs = 1"]})
        self.assertEqual(found["README.md"].names, [("max_procs", 1)])


class DiffParsing(RepoCase):
    def test_changed_lines_skip_the_file_headers(self):
        diff = (
            "diff --git a/a.md b/a.md\n"
            "index 1..2 100644\n"
            "--- a/a.md\n"
            "+++ b/a.md\n"
            "@@ -1,2 +1,2 @@\n"
            " same\n"
            "--- removed dashes\n"
            "+++ added pluses\n"
            "diff --git a/gone.py b/gone.py\n"
            "deleted file mode 100644\n"
            "--- a/gone.py\n"
            "+++ /dev/null\n"
            "@@ -1 +0,0 @@\n"
            "-old_name = 1\n"
        )
        self.assertEqual(
            gitops.diff_changed_lines(diff),
            {"a.md": ["-- removed dashes", "++ added pluses"], "gone.py": ["old_name = 1"]},
        )

    def test_diff_paths_name_both_sides_of_a_rename(self):
        git(["mv", "src/calc.py", "src/arith.py"], self.repo)
        self.commit_all("rename")
        paths = gitops.diff_paths("main", "HEAD", self.repo, exclude=["tests/**"])
        self.assertIn("src/calc.py", paths)
        self.assertIn("src/arith.py", paths)
        self.assertFalse([p for p in paths if p.startswith("tests/")])


class Prompt(RepoCase):
    """AC-1, AC-2, AC-5, AC-6"""

    def set_review(self, lines):
        toml = self.read("revali.toml")
        self.write("revali.toml", toml.replace("[review]\n", "[review]\n" + lines, 1))

    def docs_on_branch(self):
        self.write("README.md", "# sample\n\n[usage](docs/usage.md), [calc](src/calc.py)\n")
        self.write("docs/usage.md", "# usage\n\nSet `max_retries` to retry.\n")
        calc = self.read("src/calc.py")
        self.write("src/calc.py", calc + "\nmax_retries = 3\n")
        self.commit_all("docs")

    def prompt(self, log=None):
        ctx = preflight(self.repo)
        ctx.log = log
        return build_prompt(ctx, State(), self.rdir(), 1)

    def test_section_lists_links_mentions_and_the_map(self):
        self.docs_on_branch()
        p = self.prompt()
        self.assertIn(SECTION, p)
        self.assertIn(
            "- `README.md` (changed in this diff): links to `docs/usage.md` (line 3), "
            "`src/calc.py` (line 3)\n",
            p,
        )
        self.assertIn("`docs/usage.md` (changed in this diff): mentions `max_retries` (line 3)", p)
        self.assertIn("- `README.md` (depth 0)", p)
        self.assertIn("- `docs/usage.md` (depth 1)", p)
        self.assertIn("- `CONVENTIONS.md` (depth 0)", p)

    def test_excluded_files_do_not_count(self):
        self.docs_on_branch()
        toml = self.read("revali.toml")
        self.write("revali.toml", toml.replace('exclude = ["*.lock"]', 'exclude = ["src/**"]'))
        self.commit_all("exclude src")
        p = self.prompt()
        self.assertNotIn("links to `src/calc.py`", p)
        self.assertNotIn("mentions `max_retries`", p)

    def test_empty_roots_turn_it_off(self):
        self.docs_on_branch()
        self.set_review("doc_roots = []\n")
        self.commit_all("off")
        self.assertNotIn(SECTION, self.prompt())

    def test_missing_roots_say_so(self):
        self.set_review('doc_roots = ["NOPE.md"]\n')
        self.commit_all("nope")
        p = self.prompt()
        self.assertIn(SECTION, p)
        self.assertIn("none of the documentation roots exists (NOPE.md)", p)

    def test_map_is_capped(self):
        self.docs_on_branch()
        self.set_review("doc_map_max = 1\n")
        self.commit_all("cap")
        p = self.prompt()
        self.assertIn("- `README.md` (depth 0)", p)
        self.assertNotIn("- `docs/usage.md` (depth 1)", p)
        self.assertIn("- and 2 more", p)

    def test_at_most_three_names_per_document(self):
        self.write("README.md", "`aa_1` `aa_2` `aa_3` `aa_4` `aa_5`\n")
        self.write("src/calc.py", self.read("src/calc.py") + "\naa_1 = aa_2 = aa_3 = aa_4 = aa_5\n")
        self.commit_all("many")
        p = self.prompt()
        self.assertIn("mentions `aa_1` (line 1), `aa_2` (line 1), `aa_3` (line 1) and 2 more", p)

    def test_an_os_error_is_logged_and_the_review_goes_on(self):
        self.docs_on_branch()
        log = mock.Mock()
        with mock.patch.object(docgraph, "build", side_effect=PermissionError("denied: x.md")):
            p = self.prompt(log=log)
        self.assertIn("the related documents are unavailable: denied: x.md", p)
        messages = [c.args[1] for c in log.stage.call_args_list]
        self.assertEqual(len(messages), 1)
        self.assertIn("denied: x.md", messages[0])

    def test_no_related_document(self):
        p = self.prompt()  # the fixture's branch adds mul(); no document names it
        self.assertIn(SECTION, p)
        self.assertIn("No document in the graph links to a changed file", p)


class Configuration(RepoCase):
    """AC-1, AC-5: the two keys are checked"""

    def bad(self, line):
        toml = self.read("revali.toml")
        self.write("revali.toml", toml.replace("[review]\n", "[review]\n" + line, 1))
        self.commit_all("bad")
        with self.assertRaises(Stop) as cm:
            preflight(self.repo)
        return cm.exception.message

    def test_negative_map_max(self):
        self.assertIn("doc_map_max", self.bad("doc_map_max = -1\n"))

    def test_roots_must_stay_in_the_repository(self):
        self.assertIn("doc_roots", self.bad('doc_roots = ["../x.md"]\n'))

    def test_roots_must_not_be_empty_strings(self):
        self.assertIn("doc_roots", self.bad('doc_roots = [""]\n'))


class Checklist(unittest.TestCase):
    """AC-7"""

    def test_prompt_version_bumped(self):
        self.assertGreaterEqual(int(PROMPT_VERSION), 11)

    def test_item_five_reaches_outside_the_diff(self):
        path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "checklists", "default.md")
        with open(path, encoding="utf-8") as fh:
            self.assertIn("documents the diff does not touch", " ".join(fh.read().split()))


if __name__ == "__main__":
    unittest.main()
