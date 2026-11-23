"""Acceptance tests for release/0.7.0.

AC-1: the package version is the release's or later, `revali --version` and `revali version`
      print `revali <version>`, README's Status paragraph names it, the two test files
      docs/releasing.md lists pin it, and `RELEASED` in tests/test_changelog.py lists the
      release directly above the previous one.
AC-2: CHANGELOG.md has the section `## 0.7.0 - 2026-11-23` directly above
      `## 0.6.0 - 2026-11-23`; the preamble and the released sections are, byte for byte,
      the file as it was before the release.
AC-3: the section opens with one sentence and names every PR merged between v0.6.0 and this
      change (#119 and #120), each as `#n`; the section list in tests/test_changelog.py fails
      when one is left out.
AC-6: when git cannot read a commit's tree during the release-merge check, the message of
      the failure (exit 1) carries what git said, and nothing is created.
AC-8: the changelog tests of tests/test_release_070.py look headings up, so they go on
      passing once a later release adds its section above this one.

The version is compared as numbers and the changelog headings are looked up, so no assert
call here holds a literal of the current version: this file is not one of the version pins
docs/releasing.md lists, and a later release does not have to edit it.
"""

import hashlib
import importlib
import io
import os
import re
import unittest
from unittest import mock

from revali import EXIT_ERROR, EXIT_OK, NAME, VERSION, release
from tests.helpers import ROOT, git, run_cli
from tests.test_release_branch import TwoBranches

RELEASE = (0, 7, 0)
NEW = "## 0.7.0 - 2026-11-23"
PREVIOUS = "## 0.6.0 - 2026-11-23"
NEW_PAIR = ("0.7.0", "2026-11-23")
PREVIOUS_PAIR = ("0.6.0", "2026-11-23")
# `git log --oneline v0.6.0..main`: two merges, #119 and #120
RELEASE_PRS = [119, 120]
# `git rev-parse main:CHANGELOG.md` at c8955c6, the file before this change: the preamble
# followed by the sections released so far
RELEASED_BLOB = "8cdab6e6ab79ded46dd9f3694d10bef96a78510d"
# the tests docs/releasing.md step 4 names as the pins of the version
PINS = ("tests/test_docs_split.py", "tests/test_review_docssplit.py")
# what a later release would add at the top (made up, so it names no real version)
LATER_HEADING = "## 9.0.0 - 2027-03-01"
LATER = LATER_HEADING + "\n\nA later release (PR #900 to #901).\n\n- something new (#900)\n\n"
CHANGELOG = os.path.join(ROOT, "CHANGELOG.md")
REAL_OPEN = open
# an object id no repository of these tests holds
NO_SUCH_COMMIT = "1234567890abcdef1234567890abcdef12345678"


def read(*parts):
    with open(os.path.join(ROOT, *parts), "r", encoding="utf-8", newline="") as fh:
        return fh.read()


def headings(text):
    return [line for line in text.split("\n") if line.startswith("## ")]


def new_section(text):
    """The body of the release's section (up to the next `## ` line), or None."""
    lines = text.split("\n")
    if NEW not in lines:
        return None
    start = lines.index(NEW) + 1
    end = next((i for i in range(start, len(lines)) if lines[i].startswith("## ")), len(lines))
    return "\n".join(lines[start:end])


def with_later_section(text):
    """`text` as the next release would leave it: one more section between the preamble and
    the newest section."""
    head, sep, rest = text.partition("\n## ")
    if not sep:
        raise AssertionError("CHANGELOG.md has no section")
    return head + "\n" + LATER + "## " + rest


def reading(text):
    """A stand-in for `open` that gives `text` for a read of the repository's CHANGELOG.md
    and passes every other call on."""
    target = os.path.normcase(os.path.realpath(CHANGELOG))

    def fake(file, mode="r", *args, **kwargs):
        if isinstance(file, (str, os.PathLike)) and "r" in mode and "+" not in mode:
            if os.path.normcase(os.path.realpath(os.fspath(file))) == target:
                if "b" in mode:
                    return io.BytesIO(text.encode("utf-8"))
                return io.StringIO(text)
        return REAL_OPEN(file, mode, *args, **kwargs)

    return fake


def run_changelog_tests(name, text):
    """Run the `Changelog` class of the test module `name` with CHANGELOG.md reading as
    `text`: the unittest result."""
    module = importlib.import_module(name)
    suite = unittest.TestLoader().loadTestsFromTestCase(module.Changelog)
    result = unittest.TestResult()
    with mock.patch("builtins.open", reading(text)):
        suite.run(result)
    return result


def problems(result):
    return "\n".join(trace for _, trace in result.failures + result.errors)


class Version(unittest.TestCase):
    """AC-1"""

    def setUp(self):
        self.assertRegex(VERSION, r"^\d+\.\d+\.\d+$")
        self.assertGreaterEqual(tuple(int(n) for n in VERSION.split(".")), RELEASE)

    def test_the_flag_and_the_command_print_the_package_version(self):
        for argv in (["--version"], ["version"]):
            with self.subTest(argv=argv):
                code, out = run_cli(argv)
                self.assertEqual(code, EXIT_OK, out)
                self.assertEqual(out.strip(), "%s %s" % (NAME, VERSION))

    def test_the_status_paragraph_names_the_package_version(self):
        readme = read("README.md")
        status = [line for line in readme.split("\n") if line.startswith("Status:")]
        self.assertEqual(len(status), 1, status)
        paragraph = readme[readme.index(status[0]) :].split("\n\n", 1)[0]
        self.assertIn(VERSION, " ".join(paragraph.split()))

    def test_the_listed_test_files_pin_the_package_version(self):
        module = importlib.import_module("tests.test_changelog")
        found = module.version_pins(VERSION)
        for name in PINS:
            with self.subTest(pin=name):
                self.assertIn(name, found)

    def test_the_changelog_test_lists_the_release_above_the_previous_one(self):
        module = importlib.import_module("tests.test_changelog")
        released = [tuple(pair) for pair in module.RELEASED]
        self.assertIn(NEW_PAIR, released)
        self.assertEqual(released.count(NEW_PAIR), 1, released)
        self.assertEqual(released[released.index(NEW_PAIR) + 1], PREVIOUS_PAIR)


class Section(unittest.TestCase):
    """AC-2, AC-3"""

    def setUp(self):
        self.text = read("CHANGELOG.md")
        self.body = new_section(self.text)
        self.assertIsNotNone(self.body, "CHANGELOG.md has no section %s" % NEW)

    def test_it_sits_directly_above_the_previous_release(self):
        found = headings(self.text)
        self.assertEqual(found.count(NEW), 1, found)
        self.assertEqual(found.count(PREVIOUS), 1, found)
        self.assertEqual(found[found.index(NEW) + 1], PREVIOUS)

    def test_the_preamble_and_the_released_sections_are_the_file_as_it_was(self):
        # the preamble (up to the first section, whichever it is) and everything from the
        # previous release's heading on, put together, are the file main held before
        preamble = self.text[: self.text.index("\n## ") + 1]
        released = self.text[self.text.index("\n" + PREVIOUS + "\n") + 1 :]
        data = (preamble + released).encode("utf-8")
        blob = hashlib.sha1(b"blob %d\x00" % len(data) + data, usedforsecurity=False)
        self.assertEqual(blob.hexdigest(), RELEASED_BLOB)

    def test_every_pr_of_the_release_is_named(self):
        named = {int(n) for n in re.findall(r"#(\d+)\b", self.body)}
        self.assertEqual(sorted(set(RELEASE_PRS) - named), [], "PRs of the release not named")

    def test_it_opens_with_one_sentence(self):
        paragraphs = [" ".join(p.split()) for p in self.body.split("\n\n") if p.strip()]
        self.assertGreater(len(paragraphs), 1, paragraphs)
        lead = paragraphs[0]
        self.assertFalse(lead.startswith(("-", "*", "#")), lead)
        self.assertFalse(lead.endswith(":"), lead)
        self.assertTrue(lead.endswith("."), lead)
        # one sentence: nothing follows a full stop, a question mark or an exclamation mark
        self.assertIsNone(re.search(r"[.!?]\s+\S", lead), lead)


class TheListIsHeld(unittest.TestCase):
    """AC-3: the section list in tests/test_changelog.py fails when the section leaves a PR
    of the release out."""

    MODULE = "tests.test_changelog"

    def setUp(self):
        self.text = read("CHANGELOG.md")
        self.body = new_section(self.text)
        self.assertIsNotNone(self.body, "CHANGELOG.md has no section %s" % NEW)

    def doctored(self, old, new):
        """The changelog with `old` replaced by `new` in the release's section only."""
        self.assertIn(old, self.body)
        start = self.text.index("\n" + NEW + "\n") + len(NEW) + 2
        self.assertEqual(self.text[start : start + len(self.body)], self.body)
        changed = self.body.replace(old, new)
        return self.text[:start] + changed + self.text[start + len(self.body) :]

    def failures(self, text):
        return problems(run_changelog_tests(self.MODULE, text)).strip()

    def test_the_section_as_written_passes(self):
        self.assertEqual(self.failures(self.text), "")

    def test_each_pr_left_out_fails(self):
        self.assertEqual(self.failures(self.text), "")
        for number in RELEASE_PRS:
            with self.subTest(left_out=number):
                text = self.doctored("#%d" % number, "%d" % number)
                self.assertNotEqual(self.failures(text), "")


class ALaterSectionAbove(unittest.TestCase):
    """AC-8: the release's own changelog tests look their headings up, so the next release's
    section does not break them (docs/releasing.md step 4)."""

    MODULE = "tests.test_release_070"

    def setUp(self):
        self.text = read("CHANGELOG.md")
        self.assertIsNotNone(new_section(self.text), "CHANGELOG.md has no section %s" % NEW)

    def test_the_tests_pass_on_the_changelog_as_it_is(self):
        result = run_changelog_tests(self.MODULE, self.text)
        self.assertGreater(result.testsRun, 0)
        self.assertEqual(problems(result).strip(), "")

    def test_the_tests_pass_under_a_later_section(self):
        later = with_later_section(self.text)
        found = headings(later)
        self.assertEqual(found[0], LATER_HEADING)
        self.assertLess(found.index(LATER_HEADING), found.index(NEW))
        result = run_changelog_tests(self.MODULE, later)
        self.assertGreater(result.testsRun, 0)
        self.assertEqual(problems(result).strip(), "")

    def test_this_section_is_still_found_under_a_later_one(self):
        later = with_later_section(self.text)
        self.assertEqual(new_section(later), new_section(self.text))


class UnreadableTree(TwoBranches):
    """AC-6: the tree of the side the release merge brought in cannot be read. The release
    stops with exit 1 and one ERROR line that ends in git's own words, and makes nothing."""

    def setUp(self):
        super().setUp()
        # a commit straight on main, so the release merge's tree is not develop's tree and
        # develop's root tree object is read by nothing before the tree check
        self.write("main-only.txt", "not on develop\n")
        self.commit_all("straight on main")
        git(["push", "-q", "origin", "main"], self.repo)
        self.merge_release()
        self.merged = self.parents()[1]
        head_tree = git(["rev-parse", "HEAD^{tree}"], self.repo).strip()
        tree = git(["rev-parse", "%s^{tree}" % self.merged], self.repo).strip()
        self.assertNotEqual(tree, head_tree)
        loose = os.path.join(self.repo, ".git", "objects", tree[:2], tree[2:])
        self.assertTrue(os.path.isfile(loose), "the merged side's tree is not a loose object")
        os.chmod(loose, 0o644)
        os.remove(loose)

    def assert_failed_with_gits_words(self, argv):
        code, out = run_cli(argv)
        self.assertEqual(code, EXIT_ERROR, out)
        errors = self.error_lines(out)
        self.assertEqual(len(errors), 1, out)
        self.assertNotIn("Traceback", out)
        marker = "git rev-parse %s^{tree} failed:" % self.merged[:12]
        self.assertIn(marker, errors[0])
        reason = errors[0].split(marker, 1)[1].strip()
        self.assertNotEqual(reason, "", "the message gives no reason: %r" % errors[0])
        self.assertEqual(self.local_tag(), "")
        self.assertEqual(self.origin_tag(), "")
        self.assertEqual(self.gh_release_calls("create"), [])

    def test_the_release_fails_with_what_git_said(self):
        self.assert_failed_with_gits_words(["release", "7.8.9"])

    def test_the_dry_run_fails_the_same_way(self):
        self.assert_failed_with_gits_words(["release", "7.8.9", "--dry-run"])


class TreeOfACommit(TwoBranches):
    """AC-6, at the function the criterion names: a commit git does not have gives a Failed
    whose message carries a reason, and a commit it has still gives its tree alone."""

    def test_an_unknown_commit_fails_with_a_reason_and_a_known_one_gives_its_tree(self):
        with self.assertRaises(release.Failed) as caught:
            release._tree(NO_SUCH_COMMIT, self.repo)
        message = str(caught.exception)
        self.assertIn("failed:", message)
        self.assertNotEqual(message.split("failed:", 1)[1].strip(), "", message)
        # the success path prints the tree's id and nothing more
        head = git(["rev-parse", "HEAD"], self.repo).strip()
        expected = git(["rev-parse", "HEAD^{tree}"], self.repo).strip()
        self.assertEqual(release._tree(head, self.repo), expected)


if __name__ == "__main__":
    unittest.main()
