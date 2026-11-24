"""Reviewer acceptance tests for fix/rename-mark-path.

AC-1: an existing test file the branch renames to a name `[review] exclude` matches is listed
      under its old name, followed by a mark that names the new path, says the file is omitted
      from the diff above, and says that `git diff` on both names shows its change.
AC-2: an excluded test file modified, deleted or changed in type keeps the earlier mark, word
      for word; a listed file the diff shows, a rename out of an excluded name included,
      carries no mark.
AC-3: PROMPT_VERSION is 20 (or later, once a later change moves it again).

Everything is read from the prompt revali builds for the fixture branch (feature/mul). The
fixture's main carries tests/test_calc.py; setUp adds tests/test_other.py and
tests/test_third.py to it. No constant of the package is imported: the rename's mark is read
for what AC-1 says it tells, and the other mark is compared with the sentence the previous
change gave it. Every scenario holds a rename into an excluded name, so none of them passes
on a revali whose mark leaves the new path out.
"""

import os
import re
import unittest

from revali import PROMPT_VERSION
from revali.preflight import preflight
from revali.review import build_prompt
from revali.state import State
from tests.helpers import RepoCase, git

CALC = "tests/test_calc.py"
OTHER = "tests/test_other.py"
THIRD = "tests/test_third.py"
HEAD = "Existing test files this diff modifies, deletes or renames (a rename under its old name)"
PLAIN = "(omitted from the diff above; `git diff` shows its change)"


def listed(prompt):
    found = re.search(r"^%s: ([^\n]*)$" % re.escape(HEAD), prompt, re.M)
    assert found, "the prompt has no line of existing test files with the known head"
    return found.group(1)


def entries(prompt):
    """{listed path: what follows it} for the line of existing test files. The fixture's paths
    hold no blank and no mark holds a comma, so the entries split at ", " and each one at its
    first blank."""
    line = listed(prompt)
    out = {}
    if line == "(none)":
        return out
    for item in line.split(", "):
        path, _, note = item.partition(" ")
        out[path] = note
    return out


def shown(prompt):
    return re.search(r"```diff\n(.*?)\n```", prompt, re.S).group(1)


def rows(prefix):
    return "".join("%s_row_%d = %d\n" % (prefix, i, i) for i in range(20))


class RenamedIntoAnExcludedName(RepoCase):
    def setUp(self):
        super().setUp()
        git(["checkout", "-q", "main"], self.repo)
        self.write(OTHER, rows("other"))
        self.write(THIRD, rows("third"))
        self.commit_all("two more existing tests")
        git(["push", "-q", "origin", "main"], self.repo)
        git(["checkout", "-q", "feature/mul"], self.repo)
        git(["rebase", "-q", "main"], self.repo)

    def exclude(self, *patterns):
        toml = self.read("revali.toml")
        old = 'exclude = ["*.lock"]'
        self.assertIn(old, toml)
        new = "exclude = [%s]" % ", ".join('"%s"' % p for p in ("*.lock",) + patterns)
        self.write("revali.toml", toml.replace(old, new))

    def build(self):
        ctx = preflight(self.repo)
        ctx.log = None
        return build_prompt(ctx, State(), self.rdir(), 1)

    def move(self, old, new, extra=""):
        """Rename `old` to `new` (its directory is made), with `extra` appended."""
        text = self.read(old)
        git(["rm", "-q", old], self.repo)
        self.write(new, text + extra)

    def edit(self, path, marker):
        self.write(path, self.read(path) + "\n%s = 1\n" % marker)

    def assert_names_the_rename(self, note, new):
        """AC-1: the mark names the new path, says the file is omitted from the diff above and
        that `git diff` on both names shows its change."""
        self.assertIn(new, note)
        self.assertRegex(note, r"(?i)\bomitted from the diff above\b")
        self.assertIn("git diff", note)
        self.assertRegex(note, r"(?i)\bboth names\b")

    def test_a_renamed_and_edited_test_names_the_path_git_diff_needs(self):
        """AC-1: test_calc.py moves to a name `*.golden` excludes, with a line added. The
        rename is not in the diff; the old name is listed with a mark naming the new path. What
        the mark says is then tried: `git diff` on the old name alone shows a deletion, on both
        names the rename and the added line."""
        new = "tests/calc.golden"
        self.exclude("*.golden")
        self.move(CALC, new, "\nmoved_marker = 1\n")
        self.commit_all("rename test_calc to a golden file and edit it")
        prompt = self.build()
        self.assertNotIn(new, shown(prompt))
        self.assertNotIn("moved_marker", shown(prompt))
        found = entries(prompt)
        self.assertEqual(list(found), [CALC])
        self.assert_names_the_rename(found[CALC], new)
        self.assertEqual(listed(prompt).count(new), 1)

        diff = ["diff", "-M", "--no-color", "--no-ext-diff", "main...HEAD", "--"]
        alone = git(diff + [CALC], self.repo)
        self.assertIn("deleted file mode", alone)
        self.assertNotIn("moved_marker", alone)
        both = git(diff + [CALC, new], self.repo)
        self.assertIn("rename from %s" % CALC, both)
        self.assertIn("rename to %s" % new, both)
        self.assertIn("+moved_marker = 1", both)

    def test_a_test_renamed_out_of_the_test_directory_names_where_it_went(self):
        """AC-1, the edge: the new name is outside test_dir, under a directory `vendor/**`
        excludes. The file is still listed under its old name, and the mark is the only place
        that says where it went."""
        new = "vendor/moved/test_other.py"
        self.exclude("vendor/**")
        self.move(OTHER, new)
        self.commit_all("move test_other under vendor")
        prompt = self.build()
        self.assertNotIn("other_row_3", shown(prompt))
        found = entries(prompt)
        self.assertEqual(list(found), [OTHER])
        self.assert_names_the_rename(found[OTHER], new)

    def test_modified_and_deleted_excluded_tests_keep_the_earlier_mark(self):
        """AC-2 next to AC-1: three excluded changes in one commit. The rename's mark names its
        new path; the modified file and the deleted file carry the earlier mark word for
        word, with no path in it."""
        new = "tests/golden/test_calc.py"
        self.exclude("tests/golden/**", "test_other.py", "test_third.py")
        self.move(CALC, new)
        self.edit(OTHER, "hidden_other_marker")
        git(["rm", "-q", THIRD], self.repo)
        self.commit_all("rename one excluded test, edit one, delete one")
        prompt = self.build()
        self.assertNotIn("hidden_other_marker", shown(prompt))
        self.assertNotIn("third_row_3", shown(prompt))
        found = entries(prompt)
        self.assertEqual(sorted(found), [CALC, OTHER, THIRD])
        self.assert_names_the_rename(found[CALC], new)
        self.assertEqual(found[OTHER], PLAIN)
        self.assertEqual(found[THIRD], PLAIN)
        self.assertEqual(listed(prompt).count(new), 1)

    def test_two_renames_each_name_their_own_new_path(self):
        """AC-1: two tests renamed into excluded names in one commit; each mark names the new
        path of its own file and not the other's."""
        new_calc, new_other = "tests/calc.golden", "tests/other.golden"
        self.exclude("*.golden")
        self.move(CALC, new_calc)
        self.move(OTHER, new_other)
        self.commit_all("rename two tests to golden files")
        found = entries(self.build())
        self.assertEqual(sorted(found), [CALC, OTHER])
        self.assert_names_the_rename(found[CALC], new_calc)
        self.assert_names_the_rename(found[OTHER], new_other)
        self.assertNotIn(new_other, found[CALC])
        self.assertNotIn(new_calc, found[OTHER])

    def test_files_the_diff_shows_carry_no_mark_next_to_a_marked_rename(self):
        """AC-2 next to AC-1: test_calc.py matches the exclude under its old name and is renamed
        to a name off it, so the diff shows the rename and the entry is bare; test_third.py is
        edited and not excluded, bare as well; test_other.py is renamed into an excluded name
        and marked with its new path."""
        shown_new, hidden_new = "tests/test_sums.py", "tests/other.golden"
        self.exclude("test_calc.py", "*.golden")
        self.move(CALC, shown_new)
        self.move(OTHER, hidden_new)
        self.edit(THIRD, "shown_third_marker")
        self.commit_all("rename one test out of the exclude and one into it, edit a third")
        prompt = self.build()
        self.assertIn("rename to %s" % shown_new, shown(prompt))
        self.assertIn("shown_third_marker", shown(prompt))
        self.assertNotIn(hidden_new, shown(prompt))
        found = entries(prompt)
        self.assertEqual(sorted(found), [CALC, OTHER, THIRD])
        self.assertEqual(found[CALC], "")
        self.assertEqual(found[THIRD], "")
        self.assert_names_the_rename(found[OTHER], hidden_new)
        self.assertNotIn(shown_new, listed(prompt))

    @unittest.skipIf(os.name == "nt", "symbolic links")
    def test_a_type_change_keeps_the_earlier_mark_next_to_a_marked_rename(self):
        """AC-2 next to AC-1: an excluded test file that becomes a symbolic link has one name
        and keeps the earlier mark word for word; the rename beside it names its new path."""
        new = "tests/calc.golden"
        self.exclude("test_third.py", "*.golden")
        self.move(CALC, new)
        os.remove(os.path.join(self.repo, THIRD))
        os.symlink("elsewhere", os.path.join(self.repo, THIRD))
        self.commit_all("test_third becomes a link, test_calc a golden file")
        found = entries(self.build())
        self.assertEqual(sorted(found), [CALC, THIRD])
        self.assertEqual(found[THIRD], PLAIN)
        self.assert_names_the_rename(found[CALC], new)


class Version(unittest.TestCase):
    def test_prompt_version_is_20_or_later(self):
        """AC-3: the rendered prompt changes, so PROMPT_VERSION moved past 19"""
        self.assertRegex(PROMPT_VERSION, r"^\d+$")
        self.assertGreaterEqual(int(PROMPT_VERSION), 20)
