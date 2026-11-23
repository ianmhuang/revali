"""Reviewer acceptance tests for feature/review-untouched-tests.

AC-1: the round-1 prompt tells the reviewer to search the test directory for existing tests
      the diff does not modify that exercise what it changes, and to report one that now
      fails, or passes for a reason that no longer holds, as a `correctness` finding naming
      the test and the assertion; the configured `test_dir` is filled in.
AC-2: the manifest line says it lists the manifests in the diff shown, so one omitted by
      `[review] exclude` is not in it; which manifests are listed is as before.
AC-3: PROMPT_VERSION is 18 (or later, once a later change moves it again).

Everything is read from the prompt revali builds for the fixture branch (feature/mul), not
from prompts/review.md. The instruction is found among the numbered steps under "How to
review" by what it must name (the test directory as a code span, and existing tests), the
manifest line by its fixed opening; what is asserted is what the AC lists.
"""

import re
import unittest

from revali import PROMPT_VERSION
from revali.preflight import preflight
from revali.review import build_prompt
from revali.state import State
from tests.fixtures.make_sample_repo import toml_str
from tests.helpers import RepoCase

ALTERED = ("functions", "fields", "messages", "defaults", "order", "timing")


def flat(text):
    return " ".join(text.split())


def review_steps(prompt):
    """The numbered steps of the "How to review" section, each on one line."""
    found = re.search(r"(?ms)^# How to review\n(.*?)^## Severity", prompt)
    assert found, "the prompt has no How to review section"
    return [flat(s) for s in re.findall(r"(?ms)^\d+\. .*?(?=^\d+\. |\Z)", found.group(1))]


def search_steps(prompt, test_dir):
    """The steps that send the reviewer to `test_dir` for existing tests."""
    span = "`%s/`" % test_dir
    return [s for s in review_steps(prompt) if span in s and re.search(r"(?i)existing tests\b", s)]


def manifest_line(prompt):
    """(head, names) of the prompt line that lists the dependency manifests."""
    found = re.search(r"^(Dependency manifests[^\n]*): ([^\n]*)$", prompt, re.M)
    assert found, "the prompt has no line of dependency manifests"
    names = found.group(2).strip()
    return found.group(1), [] if names == "(none)" else [n.strip() for n in names.split(",")]


def omitted(prompt):
    found = re.search(r"^Files changed but omitted from the diff above: ([^\n]*)$", prompt, re.M)
    assert found, "the prompt has no line of omitted files"
    names = found.group(1).strip()
    return [] if names == "(none)" else [n.strip() for n in names.split(",")]


class PromptCase(RepoCase):
    def build(self, round_no=1):
        ctx = preflight(self.repo)
        ctx.log = None
        return build_prompt(ctx, State(), self.rdir(), round_no)


class UntouchedTestsInstruction(PromptCase):
    """AC-1"""

    def test_round_one_asks_for_existing_tests_the_diff_does_not_modify(self):
        steps = search_steps(self.build(), "tests")
        self.assertEqual(len(steps), 1, steps)
        step = steps[0]
        self.assertRegex(step, r"(?i)existing tests the diff does not modify")
        for word in ALTERED:
            with self.subTest(word=word):
                self.assertRegex(step, r"\b%s\b" % word)

    def test_what_it_finds_is_a_correctness_finding_naming_test_and_assertion(self):
        steps = search_steps(self.build(), "tests")
        self.assertEqual(len(steps), 1, steps)
        found = re.search(r"(?i)A test that now fails\b.*", steps[0])
        self.assertTrue(found, steps[0])
        sentence = found.group(0)
        self.assertRegex(sentence, r"(?i)\bpasses\b.*\breason\b.*\bno longer holds\b")
        self.assertIn("`correctness`", sentence)
        self.assertRegex(sentence, r"(?i)\bnaming the test\b")
        self.assertRegex(sentence, r"(?i)\bassertion\b")

    def test_a_nested_test_dir_is_the_one_named(self):
        """`[project] test_dir = "checks/unit"`: the search is sent there, not to `tests/`,
        and no placeholder is left in the prompt."""
        toml = self.read("revali.toml")
        line = "test_dir = " + toml_str("checks/unit")
        new, n = re.subn(r"^test_dir = .*$", lambda m: line, toml, flags=re.M)
        self.assertEqual(n, 1, toml)
        self.write("revali.toml", new)
        self.write("checks/unit/__init__.py", "")
        self.commit_all("tests live in checks/unit")
        prompt = self.build()
        self.assertNotIn("$test_dir", prompt)
        self.assertNotIn("${test_dir}", prompt)
        steps = search_steps(prompt, "checks/unit")
        self.assertEqual(len(steps), 1, steps)
        self.assertNotIn("`tests/`", steps[0])
        self.assertEqual(search_steps(prompt, "tests"), [])


class ManifestLine(PromptCase):
    """AC-2"""

    def assert_head_names_the_diff_shown(self, head):
        self.assertRegex(head, r"(?i)\bin the diff\b")
        self.assertRegex(head, r"(?i)\b(omitted|left out|excluded)\b")
        self.assertRegex(head, r"(?i)\bnot (listed|in it)\b")

    def test_manifests_in_the_diff_are_listed(self):
        """A manifest the branch modifies and one it adds, neither excluded: both listed, as
        before, and a changed file that is no manifest is not."""
        self.write("requirements.txt", "pytest\nrequests\n")
        self.write("package.json", "{}\n")
        self.commit_all("two dependencies")
        prompt = self.build()
        head, names = manifest_line(prompt)
        self.assert_head_names_the_diff_shown(head)
        self.assertEqual(sorted(names), ["package.json", "requirements.txt"])
        self.assertEqual(omitted(prompt), [])

    def test_a_manifest_the_exclude_rule_omits_is_not_listed(self):
        """The fixture's `[review] exclude = ["*.lock"]` takes poetry.lock out of the diff:
        the line lists only the manifest still in it and says the omitted one is not there."""
        self.write("requirements.txt", "pytest\nrequests\n")
        self.write("poetry.lock", "# locked\n")
        self.commit_all("a dependency and its lock file")
        prompt = self.build()
        head, names = manifest_line(prompt)
        self.assert_head_names_the_diff_shown(head)
        self.assertEqual(names, ["requirements.txt"])
        self.assertEqual(omitted(prompt), ["poetry.lock"])

    def test_only_an_omitted_manifest_leaves_the_list_empty(self):
        self.write("poetry.lock", "# locked\n")
        self.commit_all("a lock file")
        prompt = self.build()
        head, names = manifest_line(prompt)
        self.assert_head_names_the_diff_shown(head)
        self.assertEqual(names, [])
        self.assertEqual(omitted(prompt), ["poetry.lock"])

    def test_a_diff_without_manifests_lists_none(self):
        head, names = manifest_line(self.build())
        self.assert_head_names_the_diff_shown(head)
        self.assertEqual(names, [])


class Version(unittest.TestCase):
    """AC-3"""

    def test_prompt_version_moved_to_18(self):
        self.assertRegex(PROMPT_VERSION, r"^\d+$")
        self.assertGreaterEqual(int(PROMPT_VERSION), 18)


if __name__ == "__main__":
    unittest.main()
