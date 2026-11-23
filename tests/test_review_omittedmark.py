"""Reviewer acceptance tests for fix/excluded-tests-marked.

AC-1: an existing test file the branch modifies, deletes, or renames to a name matching
      `[review] exclude` stays on the prompt's "Existing test files ..." line (a rename under its
      old name), followed by a mark saying it is omitted from the diff above and that `git diff`
      shows its change.
AC-2: a listed file the diff shows carries no mark, a file renamed out of an excluded name
      included; with nothing excluded the line is what it was.
AC-3: the line's head in the rendered prompt is unchanged, and PROMPT_VERSION is 19 or later.

Everything is read from the prompt revali builds for the fixture branch (feature/mul). The
fixture's main carries tests/test_calc.py; setUp adds tests/test_other.py to it. The mark is not
compared with a constant of the package: what is asserted is that the text after a listed path
says "omitted" and names `git diff`, or that there is none.
"""

import re
import unittest

from revali import PROMPT_VERSION
from revali.preflight import preflight
from revali.review import build_prompt
from revali.state import State
from tests.helpers import RepoCase, git

CALC = "tests/test_calc.py"
OTHER = "tests/test_other.py"
HEAD = "Existing test files this diff modifies, deletes or renames (a rename under its old name)"


def listed(prompt):
    found = re.search(r"^%s: ([^\n]*)$" % re.escape(HEAD), prompt, re.M)
    assert found, "the prompt has no line of existing test files with the known head"
    return found.group(1)


def notes(line, paths):
    """What follows each of `paths` on the line, up to the next of them or the line's end."""
    spots = sorted((line.index(p), p) for p in paths)
    ends = [start for start, _ in spots[1:]] + [len(line)]
    return {
        p: line[start + len(p) : end].strip(" ,")
        for (start, p), end in zip(spots, ends, strict=True)
    }


def shown(prompt):
    return re.search(r"```diff\n(.*?)\n```", prompt, re.S).group(1)


class OmittedTestFilesAreMarked(RepoCase):
    def setUp(self):
        super().setUp()
        git(["checkout", "-q", "main"], self.repo)
        self.write(OTHER, "".join("other_row_%d = %d\n" % (i, i) for i in range(12)))
        self.commit_all("a second existing test")
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

    def assert_marked(self, note):
        self.assertRegex(note, r"(?i)\bomitted\b")
        self.assertRegex(note, r"(?i)\bdiff above\b")
        self.assertIn("git diff", note)

    def edit(self, path, marker):
        self.write(path, self.read(path) + "\n%s = 1\n" % marker)

    def test_a_modified_test_excluded_by_its_whole_path_is_listed_with_the_mark(self):
        """AC-1, AC-3: the exclude names the file by its whole path; its change is not in the
        diff, the file is on the line whose head is unchanged, and the mark follows it"""
        self.exclude(CALC)
        self.edit(CALC, "hidden_calc_marker")
        self.commit_all("exclude test_calc and edit it")
        prompt = self.build()
        self.assertNotIn("hidden_calc_marker", shown(prompt))
        line = listed(prompt)
        self.assertTrue(line.startswith(CALC + " "), line)
        self.assertNotIn(OTHER, line)
        self.assert_marked(notes(line, [CALC])[CALC])

    def test_a_deleted_test_under_an_excluded_directory_is_listed_with_the_mark(self):
        """AC-1: `tests/**` leaves the deletion out of the diff; the file is still listed,
        marked"""
        self.exclude("tests/**")
        git(["rm", "-q", OTHER], self.repo)
        self.commit_all("exclude the test directory and delete a test")
        prompt = self.build()
        self.assertNotIn("other_row_3", shown(prompt))
        line = listed(prompt)
        self.assertTrue(line.startswith(OTHER + " "), line)
        self.assert_marked(notes(line, [OTHER])[OTHER])

    def test_a_test_renamed_to_an_excluded_name_is_marked_under_its_old_name(self):
        """AC-1: the new name matches the exclude, so the rename is not in the diff; the line
        lists the old name, marked, and not the new one"""
        new = "tests/calc.golden"
        self.exclude("*.golden")
        git(["mv", CALC, new], self.repo)
        self.commit_all("rename test_calc to a golden file")
        prompt = self.build()
        self.assertNotIn(new, shown(prompt))
        line = listed(prompt)
        self.assertTrue(line.startswith(CALC + " "), line)
        self.assertNotIn(new, line)
        self.assert_marked(notes(line, [CALC])[CALC])

    def test_a_test_renamed_out_of_an_excluded_name_is_unmarked_next_to_a_marked_one(self):
        """AC-1, AC-2: both old names match the exclude. The one renamed to a name off the
        exclude is in the diff and carries no mark; the one edited in place is omitted and
        marked"""
        new = "tests/test_sums.py"
        self.exclude("test_calc.py", "test_other.py")
        git(["mv", CALC, new], self.repo)
        self.edit(OTHER, "hidden_other_marker")
        self.commit_all("rename one excluded test, edit the other")
        prompt = self.build()
        self.assertIn("rename to %s" % new, shown(prompt))
        self.assertNotIn("hidden_other_marker", shown(prompt))
        line = listed(prompt)
        self.assertIn(CALC, line)
        self.assertIn(OTHER, line)
        found = notes(line, [CALC, OTHER])
        self.assertEqual(found[CALC], "")
        self.assert_marked(found[OTHER])

    def test_the_mark_comes_only_once_an_exclude_names_the_file(self):
        """AC-2, then AC-1: with nothing excluding the tests, two edited tests are listed bare,
        as on main; excluding one of them marks that one and leaves the other bare"""
        self.edit(CALC, "shown_calc_marker")
        self.edit(OTHER, "second_other_marker")
        self.commit_all("edit both tests")
        prompt = self.build()
        self.assertEqual(sorted(listed(prompt).split(", ")), [CALC, OTHER])
        self.assertIn("shown_calc_marker", shown(prompt))
        self.assertIn("second_other_marker", shown(prompt))

        self.exclude("test_other.py")
        self.commit_all("exclude test_other")
        prompt = self.build()
        self.assertIn("shown_calc_marker", shown(prompt))
        self.assertNotIn("second_other_marker", shown(prompt))
        found = notes(listed(prompt), [CALC, OTHER])
        self.assertEqual(found[CALC], "")
        self.assert_marked(found[OTHER])

    def test_an_excluded_file_outside_the_test_directory_does_not_join_the_list(self):
        """AC-2, the edge, next to AC-1: a lock file the exclude leaves out is not a test file
        and is not listed, while the excluded test beside it is listed and marked"""
        self.exclude("test_calc.py")
        self.write("pkg/deps.lock", "lock_marker\n")
        self.edit(CALC, "hidden_calc_marker")
        self.commit_all("a lock file and an excluded test edit")
        line = listed(self.build())
        self.assertNotIn("deps.lock", line)
        self.assertTrue(line.startswith(CALC + " "), line)
        self.assert_marked(notes(line, [CALC])[CALC])


class Version(unittest.TestCase):
    def test_prompt_version_is_19_or_later(self):
        """AC-3: the rendered prompt changes, so PROMPT_VERSION moved past 18"""
        self.assertGreaterEqual(int(PROMPT_VERSION), 19)
