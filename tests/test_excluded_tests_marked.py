"""fix/excluded-tests-marked (M4.7): an existing test file that `[review] exclude` cuts from the
reviewer's diff stays on the prompt's "Existing test files ..." line, marked as omitted from the
diff above with a pointer to `git diff`; a listed file the diff shows is not marked.

The fixture's main carries tests/test_calc.py; feature/mul adds a `mul` function. Each test
commits its own `[review] exclude` on the branch.
"""

import re

from revali import PROMPT_VERSION
from revali.preflight import preflight
from revali.review import OMITTED_TEST_MARK, build_prompt
from revali.state import State
from tests.helpers import RepoCase, git

CALC = "tests/test_calc.py"
HEAD = "Existing test files this diff modifies, deletes or renames (a rename under its old name)"


def modified_line(prompt):
    return re.search(r"^%s: (.*)$" % re.escape(HEAD), prompt, re.M).group(1)


def diff_block(prompt):
    return re.search(r"```diff\n(.*?)\n```", prompt, re.S).group(1)


def marked(path):
    return "%s %s" % (path, OMITTED_TEST_MARK)


class ExcludedTestFiles(RepoCase):
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

    def weaken_calc(self):
        self.write(CALC, self.read(CALC) + "\nweakened_marker = 1\n")

    def test_the_mark_says_omitted_and_how_to_see_it(self):
        """AC-1: the mark's wording"""
        self.assertIn("omitted from the diff above", OMITTED_TEST_MARK)
        self.assertIn("git diff", OMITTED_TEST_MARK)

    def test_a_modified_excluded_test_is_listed_and_marked(self):
        """AC-1"""
        self.exclude("test_calc.py")
        self.weaken_calc()
        self.commit_all("exclude and weaken test_calc")
        prompt = self.build()
        self.assertNotIn("weakened_marker", diff_block(prompt))
        self.assertEqual(modified_line(prompt), marked(CALC))

    def test_a_deleted_excluded_test_is_listed_and_marked(self):
        """AC-1"""
        self.exclude("test_calc.py")
        git(["rm", "-q", CALC], self.repo)
        self.commit_all("exclude and drop test_calc")
        self.assertEqual(modified_line(self.build()), marked(CALC))

    def test_a_test_renamed_into_an_excluded_name_is_marked_under_its_old_name(self):
        """AC-1"""
        self.exclude("tests/golden/**")
        self.write("tests/golden/test_calc.py", self.read(CALC))
        git(["rm", "-q", CALC], self.repo)
        self.commit_all("move test_calc under golden")
        prompt = self.build()
        self.assertNotIn("tests/golden/test_calc.py", diff_block(prompt))
        self.assertEqual(modified_line(prompt), marked(CALC))

    def test_a_test_renamed_out_of_an_excluded_name_is_not_marked(self):
        """AC-2: the diff shows it under its new name"""
        self.exclude("test_calc.py")
        git(["mv", CALC, "tests/test_arith.py"], self.repo)
        self.commit_all("rename test_calc")
        prompt = self.build()
        self.assertIn("rename to tests/test_arith.py", diff_block(prompt))
        self.assertEqual(modified_line(prompt), CALC)

    def test_only_the_excluded_one_of_two_is_marked(self):
        """AC-1, AC-2"""
        git(["checkout", "-q", "main"], self.repo)
        self.write("tests/test_other.py", "other_marker = 1\n")
        self.commit_all("a second test on the base")
        git(["push", "-q", "origin", "main"], self.repo)
        git(["checkout", "-q", "feature/mul"], self.repo)
        git(["rebase", "-q", "main"], self.repo)
        self.exclude("test_other.py")
        self.weaken_calc()
        self.write("tests/test_other.py", "other_marker = 2\n")
        self.commit_all("change both")
        prompt = self.build()
        self.assertEqual(modified_line(prompt), "%s, %s" % (CALC, marked("tests/test_other.py")))
        self.assertIn("weakened_marker", diff_block(prompt))
        self.assertNotIn("other_marker", diff_block(prompt))

    def test_nothing_excluded_leaves_the_line_unmarked(self):
        """AC-2"""
        self.weaken_calc()
        self.commit_all("weaken test_calc")
        prompt = self.build()
        self.assertEqual(modified_line(prompt), CALC)
        self.assertNotIn(OMITTED_TEST_MARK, prompt)

    def test_an_excluded_file_outside_the_tests_is_not_listed(self):
        """AC-2: the list itself does not grow"""
        self.write("pkg/deps.lock", "lock_marker\n")
        self.commit_all("a lock file")
        self.assertEqual(modified_line(self.build()), "(none)")


class Version(RepoCase):
    def test_prompt_version(self):
        """AC-3"""
        self.assertGreaterEqual(int(PROMPT_VERSION), 19)
