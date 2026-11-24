"""Acceptance tests for release/0.7.1.

AC-1: the package version is the release's or later, `revali --version` and `revali version`
      print `revali <version>`, README's Status paragraph names it, the two test files
      docs/releasing.md lists pin it, and `RELEASED` in tests/test_changelog.py lists the
      release directly above the previous one.
AC-2: CHANGELOG.md has the section `## 0.7.1 - 2026-11-24` directly above
      `## 0.7.0 - 2026-11-23`; the preamble and the released sections are, byte for byte,
      the file as it was before the release.
AC-3: the section opens with one sentence and names every PR merged between v0.7.0 and this
      change (#122 and #123), each as `#n`; the section list in tests/test_changelog.py fails
      when one is left out.
AC-8: the tests of tests/test_release_071.py read the changelog with its whitespace folded
      and look its headings up, so they go on passing when the section is wrapped elsewhere
      and once a later release adds its section above this one.

The version is compared as numbers and the changelog headings are looked up, so no assert
call here holds a literal of the current version: this file is not one of the version pins
docs/releasing.md lists, and a later release does not have to edit it. No sentence of a
document is pinned either: the section is rewrapped and doctored by rule, not by its words.
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

RELEASE = (0, 7, 1)
NEW = "## 0.7.1 - 2026-11-24"
PREVIOUS = "## 0.7.0 - 2026-11-23"
NEW_PAIR = ("0.7.1", "2026-11-24")
PREVIOUS_PAIR = ("0.7.0", "2026-11-23")
# `git log --oneline v0.7.0..main`: two merges, #122 and #123
RELEASE_PRS = [122, 123]
# `git rev-parse main:CHANGELOG.md` at 3c1c55f, the file before this change: the preamble
# followed by the sections released so far
RELEASED_BLOB = "48672a48fa853d3d39a969c8db3bdbea5e4d5a0c"
# the tests docs/releasing.md step 4 names as the pins of the version
PINS = ("tests/test_docs_split.py", "tests/test_review_docssplit.py")
# what a later release would add at the top (made up, so it names no real version)
LATER_HEADING = "## 9.0.0 - 2027-03-01"
LATER = LATER_HEADING + "\n\nA later release (PR #900 to #901).\n\n- something new (#900)\n\n"
CHANGELOG = os.path.join(ROOT, "CHANGELOG.md")
REAL_OPEN = open


def read(*parts):
    with open(os.path.join(ROOT, *parts), "r", encoding="utf-8", newline="") as fh:
        return fh.read()


def fold(text):
    return " ".join(text.split())


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


def with_section(text, body):
    """`text` with the body of the release's section replaced by `body`."""
    old = new_section(text)
    start = text.index("\n" + NEW + "\n") + len(NEW) + 2
    if text[start : start + len(old)] != old:
        raise AssertionError("the section of %s is not where its heading is" % NEW)
    return text[:start] + body + text[start + len(old) :]


def with_later_section(text):
    """`text` as the next release would leave it: one more section between the preamble and
    the newest section."""
    head, sep, rest = text.partition("\n## ")
    if not sep:
        raise AssertionError("CHANGELOG.md has no section")
    return head + "\n" + LATER + "## " + rest


def rewrapped(body):
    """`body` with its lines broken elsewhere: the last word of every line that goes on in
    the next one moves down to it. A line before a blank line or before a list item is left
    alone, and so is a short one, so every paragraph and item starts with the words it had."""
    lines = body.split("\n")
    for i in range(len(lines) - 1):
        line, following = lines[i], lines[i + 1]
        if not line.strip() or not following.strip():
            continue
        if following.lstrip().startswith("- ") or len(line.split()) < 4:
            continue
        head, last = line.rsplit(" ", 1)
        indent = following[: len(following) - len(following.lstrip())]
        lines[i] = head
        lines[i + 1] = indent + last + " " + following.lstrip()
    return "\n".join(lines)


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


def run_module(name, text, case=""):
    """Run the tests of the test module `name`, or of its class `case` alone, with
    CHANGELOG.md reading as `text`: the unittest result."""
    module = importlib.import_module(name)
    loader = unittest.TestLoader()
    if case:
        suite = loader.loadTestsFromTestCase(getattr(module, case))
    else:
        suite = loader.loadTestsFromModule(module)
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
        self.assertIn(VERSION, fold(paragraph))

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
        paragraphs = [fold(p) for p in self.body.split("\n\n") if p.strip()]
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

    def failures(self, text):
        # the class that reads CHANGELOG.md; the module's other classes parse every test file
        result = run_module(self.MODULE, text, case="Changelog")
        self.assertGreater(result.testsRun, 0)
        return problems(result).strip()

    def test_the_section_as_written_passes(self):
        self.assertEqual(self.failures(self.text), "")

    def test_each_pr_left_out_fails(self):
        for number in RELEASE_PRS:
            with self.subTest(left_out=number):
                self.assertIn("#%d" % number, self.body)
                body = self.body.replace("#%d" % number, "%d" % number)
                self.assertNotEqual(self.failures(with_section(self.text, body)), "")


class TheReleaseTestsKeepPassing(unittest.TestCase):
    """AC-8: the release's own tests fold the whitespace of what they read and look their
    headings up, so neither a rewrap of the section nor the next release's section above it
    breaks them (docs/releasing.md step 4)."""

    MODULE = "tests.test_release_071"

    def setUp(self):
        self.text = read("CHANGELOG.md")
        self.body = new_section(self.text)
        self.assertIsNotNone(self.body, "CHANGELOG.md has no section %s" % NEW)

    def test_they_pass_on_the_changelog_as_it_is(self):
        result = run_module(self.MODULE, self.text)
        self.assertGreater(result.testsRun, 0)
        self.assertEqual(problems(result).strip(), "")

    def test_they_pass_under_a_later_section(self):
        later = with_later_section(self.text)
        found = headings(later)
        self.assertEqual(found[0], LATER_HEADING)
        self.assertLess(found.index(LATER_HEADING), found.index(NEW))
        result = run_module(self.MODULE, later)
        self.assertGreater(result.testsRun, 0)
        self.assertEqual(problems(result).strip(), "")

    def test_this_section_is_still_found_under_a_later_one(self):
        later = with_later_section(self.text)
        self.assertEqual(new_section(later), new_section(self.text))

    def test_they_pass_on_the_section_wrapped_elsewhere(self):
        wrapped = rewrapped(self.body)
        # the same words in the same order, on other lines
        self.assertNotEqual(wrapped, self.body)
        self.assertEqual(fold(wrapped), fold(self.body))
        text = with_section(self.text, wrapped)
        self.assertEqual(headings(text), headings(self.text))
        result = run_module(self.MODULE, text)
        self.assertGreater(result.testsRun, 0)
        self.assertEqual(problems(result).strip(), "")

    def test_they_fail_when_the_section_loses_its_parts(self):
        # the lead sentence alone: the Upgrading and Known issues parts are gone
        lead = self.body.strip().split("\n\n", 1)[0]
        text = with_section(self.text, "\n" + lead + "\n")
        self.assertEqual(headings(text), headings(self.text))
        result = run_module(self.MODULE, text)
        self.assertNotEqual(problems(result).strip(), "")


if __name__ == "__main__":
    unittest.main()
