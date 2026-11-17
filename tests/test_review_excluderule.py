"""Acceptance tests for fix/exclude-pathspec, AC-1 to AC-5: the files preflight lists as
omitted are the files the reviewer's diff leaves out, wherever they sit in the tree, and a
renamed file is left out (or kept) as one file.

Every file carries a marker line of its own, so "in the diff" is read from the content and
not from how git writes an unusual name in a header.
"""

import os

from revali.preflight import preflight
from revali.review import build_prompt
from revali.state import State
from tests.helpers import RepoCase, git

FENCE = "```diff\n"
OMITTED = "Files changed but omitted from the diff above: "
BODY = "".join("line_%d = %d\n" % (i, i) for i in range(20))
README = "# sample\n\n[old](tests/old_helper.py), [pins](pkg/old.lock), [calc](src/calc.py)\n"


def link_lines(prompt):
    """The entries of the related-documents list that name a linked file."""
    return [ln for ln in prompt.splitlines() if ln.startswith("- `") and "links to" in ln]


class PromptCase(RepoCase):
    def build(self):
        """(ctx, the prompt's diff block, its omitted-files line, the whole prompt)"""
        ctx = preflight(self.repo)
        prompt = build_prompt(ctx, State(), self.rdir(), 1)
        start = prompt.index(FENCE) + len(FENCE)
        end = prompt.index(OMITTED, start)
        omitted = prompt[end + len(OMITTED) :].split("\n", 1)[0]
        return ctx, prompt[start:end], omitted, prompt


class NestedFiles(PromptCase):
    """AC-1, AC-2: the fixture's test pattern is test_review_{topic}.py, exclude ["*.lock"]"""

    def test_a_nested_reviewer_test_and_lock_file_are_left_out_and_named(self):
        self.write("tests/test_review_x.py", "nested_test_marker = 1\n")
        self.write("pkg/deps.lock", "nested-lock-marker\n")
        self.commit_all("nested files")
        ctx, diff, omitted, _ = self.build()
        self.assertIn("def mul", diff)  # the author's change is still there
        self.assertNotIn("nested_test_marker", diff)
        self.assertNotIn("nested-lock-marker", diff)
        self.assertNotIn("test_review_x.py", diff)
        self.assertNotIn("deps.lock", diff)
        self.assertIn("tests/test_review_x.py", omitted)
        self.assertIn("pkg/deps.lock", omitted)
        self.assertEqual(sorted(ctx.excluded_files), ["pkg/deps.lock", "tests/test_review_x.py"])

    def test_a_neighbour_and_names_with_odd_characters_stay(self):
        self.write("tests/test_review_x.py", "nested_test_marker = 1\n")
        self.write("tests/helper_review.py", "neighbour_marker = 1\n")
        self.write("docs/a [b].md", "bracket_marker\n")
        # left out by `*.lock`; read as a pattern, its own name would not match it
        self.write("pkg/x [1].lock", "odd-lock-marker\n")
        star = os.name != "nt"  # `*` cannot be in a Windows file name
        if star:
            self.write("notes*.md", "star_marker\n")
        self.commit_all("neighbours and odd names")
        ctx, diff, omitted, _ = self.build()
        self.assertNotIn("nested_test_marker", diff)
        self.assertNotIn("odd-lock-marker", diff)
        self.assertIn("pkg/x [1].lock", omitted)
        self.assertIn("neighbour_marker = 1", diff)
        self.assertIn("bracket_marker", diff)
        self.assertIn("def mul", diff)
        self.assertIn("tests/helper_review.py", ctx.changed_files)
        self.assertIn("docs/a [b].md", ctx.changed_files)
        self.assertNotIn("helper_review.py", omitted)
        if star:
            self.assertIn("star_marker", diff)
            self.assertIn("notes*.md", ctx.changed_files)


class Renames(PromptCase):
    """AC-3, AC-4. main (pushed) carries a helper, a lock file and a README that links to
    both; feature/mul restarts from it."""

    def setUp(self):
        super().setUp()
        git(["checkout", "-q", "main"], self.repo)
        self.write("README.md", README)
        self.write("tests/old_helper.py", BODY)
        self.write("pkg/old.lock", BODY)
        self.commit_all("files on the base")
        git(["push", "-q", "origin", "main"], self.repo)
        git(["checkout", "-q", "-B", "feature/mul", "main"], self.repo)

    def rename(self, old, new, edit):
        """Rename with one changed line (19 of 20 lines stay, so git pairs the names), next
        to a one-line change of src/calc.py."""
        git(["mv", old, new], self.repo)
        self.write(new, BODY.replace("line_3 = 3\n", edit + " = 3\n"))
        self.write("src/calc.py", self.read("src/calc.py") + "kept_marker = 1\n")
        self.commit_all("rename %s" % old)

    def test_a_rename_into_the_test_pattern_is_left_out_whole(self):
        self.rename("tests/old_helper.py", "tests/test_review_moved.py", "moved_edit")
        ctx, diff, omitted, prompt = self.build()
        self.assertIn("kept_marker = 1", diff)
        # no half-rename: the old name is not shown as a deletion
        self.assertNotIn("old_helper.py", diff)
        self.assertNotIn("test_review_moved.py", diff)
        self.assertNotIn("moved_edit", diff)
        self.assertEqual(ctx.excluded_files, ["tests/test_review_moved.py"])
        self.assertIn("tests/test_review_moved.py", omitted)
        self.assertEqual(ctx.changed_files, ["src/calc.py"])
        self.assertEqual(ctx.diff_lines, 1)  # the calc line; the rename's two are not counted
        # AC-4: the related documents see the same files as the diff
        links = link_lines(prompt)
        self.assertTrue([ln for ln in links if "`src/calc.py`" in ln], links)
        self.assertFalse([ln for ln in links if "old_helper.py" in ln], links)

    def test_a_rename_out_of_an_excluded_name_is_left_out_whole(self):
        """Either name: here the old one matches `*.lock`."""
        self.rename("pkg/old.lock", "pkg/pins.txt", "pins_edit")
        ctx, diff, omitted, _ = self.build()
        self.assertIn("kept_marker = 1", diff)
        self.assertNotIn("old.lock", diff)
        self.assertNotIn("pins.txt", diff)
        self.assertNotIn("pins_edit", diff)
        self.assertEqual(ctx.excluded_files, ["pkg/pins.txt"])
        self.assertIn("pkg/pins.txt", omitted)
        self.assertEqual(ctx.changed_files, ["src/calc.py"])
        self.assertEqual(ctx.diff_lines, 1)


class PlainRename(PromptCase):
    """AC-3: neither name matches"""

    def test_it_stays_in_the_diff_and_is_counted_once_under_its_new_name(self):
        before = preflight(self.repo).diff_lines
        git(["mv", "src/calc.py", "src/arith.py"], self.repo)
        self.commit_all("rename calc")
        ctx, diff, omitted, _ = self.build()
        self.assertEqual(ctx.changed_files, ["src/arith.py"])
        self.assertEqual(ctx.excluded_files, [])
        self.assertEqual(omitted, "(none)")
        self.assertEqual(ctx.diff_lines, before)  # a rename adds no line to the size
        self.assertIn("rename from src/calc.py", diff)
        self.assertIn("rename to src/arith.py", diff)
        self.assertIn("def mul", diff)


class NothingExcluded(PromptCase):
    """AC-5. A regression guard: this holds on the base branch as well, since the AC says
    the diff does not change."""

    def plain(self):
        args = ["diff", "--src-prefix=a/", "--dst-prefix=b/", "origin/main...HEAD"]
        return git(args, self.repo).strip()

    def shown(self):
        _, diff, omitted, _ = self.build()
        self.assertEqual(omitted, "(none)")
        diff = diff.rstrip()
        self.assertTrue(diff.endswith("```"), diff[-40:])
        return diff[:-3].rstrip()

    def test_no_changed_file_matches_a_pattern(self):
        self.assertEqual(self.shown(), self.plain())

    def test_exclude_is_empty(self):
        toml = self.read("revali.toml")
        self.assertIn('exclude = ["*.lock"]', toml)
        self.write("revali.toml", toml.replace('exclude = ["*.lock"]', "exclude = []"))
        self.write("docs/notes.md", "kept\n")
        self.commit_all("no exclude")
        self.assertEqual(self.shown(), self.plain())
