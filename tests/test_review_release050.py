"""Acceptance tests for release/0.5.0.

AC-1: the package version is the release's or later, `revali --version` and `revali version`
      print `revali <version>`, README's Status paragraph names it, and `RELEASED` in
      tests/test_changelog.py lists the release directly above the previous one.
AC-2: CHANGELOG.md has the section `## 0.5.0 - 2026-11-21` directly above
      `## 0.4.0 - 2026-11-19`; the preamble and the released sections are, byte for byte,
      the file as it was before the release.
AC-3: the section opens with one sentence and names every PR merged between v0.4.0 and this
      change (#101 to #112) and no other number; a test of the project fails when one is
      left out or a number outside the release is named.
AC-4: the section has an "Upgrading" part that points at the PRs a user of the previous
      release has to know about.
AC-5: the section has a "Known issues" part that names the released section it is about.
AC-6: the changelog tests of this release and of the previous one go on passing once a
      later release adds its section above them.

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

from revali import EXIT_OK, NAME, VERSION
from tests.helpers import ROOT, run_cli

RELEASE = (0, 5, 0)
NEW = "## 0.5.0 - 2026-11-21"
PREVIOUS = "## 0.4.0 - 2026-11-19"
NEW_PAIR = ("0.5.0", "2026-11-21")
PREVIOUS_VERSION = "0.4.0"
# `git log --oneline v0.4.0..main`: twelve merges, #101 to #112
RELEASE_PRS = list(range(101, 113))
# `git rev-parse v0.4.0:CHANGELOG.md`, the blob main held before this change: the preamble
# followed by the sections released so far
RELEASED_BLOB = "27bd90f4d153921bffcbddae722254d39fa0728f"
# what a later release would add at the top (made up, so it names no real version)
LATER_HEADING = "## 9.0.0 - 2027-03-01"
LATER = LATER_HEADING + "\n\nA later release (PR #900 to #901).\n\n- something new (#900)\n\n"
CHANGELOG = os.path.join(ROOT, "CHANGELOG.md")
REAL_OPEN = open


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
    """Run the changelog tests of the test module `name` (its `Changelog` class, the whole
    module when it has none) with CHANGELOG.md reading as `text`: the unittest result."""
    module = importlib.import_module(name)
    loader = unittest.TestLoader()
    case = getattr(module, "Changelog", None)
    suite = loader.loadTestsFromTestCase(case) if case else loader.loadTestsFromModule(module)
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

    def test_the_changelog_test_lists_the_release_above_the_previous_one(self):
        module = importlib.import_module("tests.test_changelog")
        released = [tuple(pair) for pair in module.RELEASED]
        self.assertIn(NEW_PAIR, released)
        versions = [pair[0] for pair in released]
        self.assertEqual(versions.count(NEW_PAIR[0]), 1, versions)
        self.assertEqual(versions[versions.index(NEW_PAIR[0]) + 1], PREVIOUS_VERSION)


class Section(unittest.TestCase):
    """AC-2 to AC-5"""

    def setUp(self):
        self.text = read("CHANGELOG.md")
        self.body = new_section(self.text)
        self.assertIsNotNone(self.body, "CHANGELOG.md has no section %s" % NEW)

    def part(self, opening):
        """The paragraph of the section that opens with `opening`, folded; "" without one."""
        for paragraph in self.body.split("\n\n"):
            if paragraph.strip().startswith(opening):
                return " ".join(paragraph.split())
        return ""

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

    def test_every_pr_of_the_release_is_named_and_no_other_number(self):
        named = {int(n) for n in re.findall(r"#(\d+)\b", self.body)}
        self.assertEqual(sorted(set(RELEASE_PRS) - named), [], "PRs of the release not named")
        self.assertEqual(sorted(named - set(RELEASE_PRS)), [], "numbers outside the release")

    def test_it_opens_with_one_sentence(self):
        paragraphs = [" ".join(p.split()) for p in self.body.split("\n\n") if p.strip()]
        self.assertGreater(len(paragraphs), 1, paragraphs)
        lead = paragraphs[0]
        self.assertFalse(lead.startswith(("-", "*", "#")), lead)
        self.assertFalse(lead.endswith(":"), lead)
        self.assertTrue(lead.endswith("."), lead)
        # one sentence: nothing follows a full stop, a question mark or an exclamation mark
        self.assertIsNone(re.search(r"[.!?]\s+\S", lead), lead)

    def test_the_upgrading_part_points_at_the_changes_a_user_meets(self):
        part = self.part("Upgrading")
        self.assertTrue(part, "no paragraph that opens with Upgrading")
        # the two commands (#103, #112) and the config file reads (#109, #110, #111)
        for number in (103, 112, 109, 110, 111):
            with self.subTest(pr=number):
                self.assertRegex(part, r"#%d\b" % number)

    def test_the_known_issues_part_names_the_released_section(self):
        part = self.part("Known issues")
        self.assertTrue(part, "no paragraph that opens with Known issues")
        self.assertIn("0.2.0", part)


class TheListIsHeld(unittest.TestCase):
    """AC-3: a test of the project fails when the section leaves a PR of the release out, or
    names a number outside it."""

    MODULES = ("tests.test_release_050", "tests.test_changelog")

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
        return "\n".join(problems(run_changelog_tests(name, text)) for name in self.MODULES)

    def test_the_section_as_written_passes(self):
        self.assertEqual(self.failures(self.text).strip(), "")

    def test_a_pr_left_out_fails(self):
        self.assertEqual(self.failures(self.text).strip(), "")
        for number in (101, 107, 112):
            with self.subTest(left_out=number):
                text = self.doctored("#%d" % number, "%d" % number)
                self.assertNotEqual(self.failures(text).strip(), "")

    def test_a_number_outside_the_release_fails(self):
        self.assertEqual(self.failures(self.text).strip(), "")
        for number in (99, 200):
            with self.subTest(added=number):
                text = self.doctored("#105", "#105, #%d" % number)
                self.assertNotEqual(self.failures(text).strip(), "")


class ALaterSection(unittest.TestCase):
    """AC-6: the changelog tests of this release and of the previous one look their headings
    up, so the section the next release adds above them does not break them."""

    def setUp(self):
        self.text = read("CHANGELOG.md")
        self.later = with_later_section(self.text)
        # the premise: the made-up section is the newest one, the real ones follow unchanged
        self.assertEqual(headings(self.later), [LATER_HEADING] + headings(self.text))

    def check(self, name):
        for label, text in (("as it is", self.text), ("with a later section", self.later)):
            with self.subTest(changelog=label):
                result = run_changelog_tests(name, text)
                self.assertGreater(result.testsRun, 0)
                self.assertTrue(result.wasSuccessful(), problems(result))

    def test_the_tests_of_the_previous_release(self):
        self.check("tests.test_release_040")

    def test_the_tests_of_this_release(self):
        self.check("tests.test_release_050")


if __name__ == "__main__":
    unittest.main()
