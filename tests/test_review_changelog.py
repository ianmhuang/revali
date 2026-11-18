"""Acceptance tests for feature/release-steps.

AC-1: CHANGELOG.md at the repository root, linked from README, one section per released
      version, newest first, headed `## <version> - <YYYY-MM-DD>` with the tag's date, each
      naming pull requests of its own release, and no "Unreleased" section.
AC-2: a test fails when the newest section is not `revali.VERSION`, when a heading has
      another format, or when the versions do not descend.
AC-5: a test fails when docs/releasing.md does not name a place a version bump touches,
      a test file that compares `VERSION` with a literal included.

AC-2 and AC-5 are about what the project's own test does, and docs/releasing.md names that
test (tests/test_changelog.py). They are checked from outside: a copy of the repository
gets one thing wrong at a time, and `python -m unittest tests.test_changelog` in the copy
must fail after passing on the untouched copy.

No assert call in this file holds a literal of the current version, so this file is not
one of the pins AC-5 is about.
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

from revali import VERSION

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# version, tag date, lowest and highest PR number merged for it (git log between the tags)
RELEASED = (
    ("0.3.0", "2026-09-07", 26, 43),
    ("0.2.0", "2026-09-05", 12, 25),
    ("0.1.0", "2026-08-30", 1, 11),
)
HEADING = re.compile(r"^## (\d+)\.(\d+)\.(\d+) - (\d{4}-\d{2}-\d{2})$")
MODULE = "tests.test_changelog"
COPIED_DIRS = ("revali", "tests", "docs")
COPIED_FILES = (".md", ".toml", ".py")
PIN_SOURCE = (
    "import unittest\n"
    "\n"
    "from revali import VERSION\n"
    "\n"
    "\n"
    "class UnlistedPin(unittest.TestCase):\n"
    "    def test_version(self):\n"
    '        self.assertEqual(VERSION, "%s")\n'
)


def read(root, rel):
    with open(os.path.join(root, *rel.split("/")), "r", encoding="utf-8", newline="") as fh:
        return fh.read()


def write(root, rel, text):
    with open(os.path.join(root, *rel.split("/")), "w", encoding="utf-8", newline="") as fh:
        fh.write(text)


def split_sections(text):
    """(preamble, sections): a section runs from its `## ` line up to the next one."""
    lines = text.split("\n")
    starts = [i for i, line in enumerate(lines) if line.startswith("## ")]
    if not starts:
        return text, []
    bounds = starts + [len(lines)]
    preamble = "\n".join(lines[: starts[0]])
    sections = ["\n".join(lines[a:b]) for a, b in zip(bounds, bounds[1:], strict=False)]
    return preamble, sections


def join_sections(preamble, sections):
    return "\n".join([preamble] + sections)


def comparing_files():
    """The test files that compare `VERSION` with a literal of the current version."""
    literal = "[\"']%s[\"']" % re.escape(VERSION)
    pattern = re.compile(
        r"\(\s*VERSION\s*,\s*%s\s*[,)]|\(\s*%s\s*,\s*VERSION\s*[,)]" % (literal, literal)
    )
    found = []
    for name in sorted(os.listdir(HERE)):
        if name.startswith("test_") and name.endswith(".py"):
            if pattern.search(read(ROOT, "tests/" + name)):
                found.append("tests/" + name)
    return found


def run_module(root):
    """Run the project's changelog test inside `root`: (exit code, output)."""
    env = dict(os.environ, PYTHONPATH=root, PYTHONDONTWRITEBYTECODE="1")
    env.pop("PYTHONSAFEPATH", None)
    done = subprocess.run(
        [sys.executable, "-m", "unittest", MODULE],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=600,
    )
    return done.returncode, done.stdout + done.stderr


class Changelog(unittest.TestCase):
    """AC-1, on the repository's own CHANGELOG.md."""

    def setUp(self):
        path = os.path.join(ROOT, "CHANGELOG.md")
        self.assertTrue(os.path.isfile(path), "no CHANGELOG.md at the repository root")
        self.text = read(ROOT, "CHANGELOG.md")
        _, self.sections = split_sections(self.text)
        self.headings = [s.split("\n", 1)[0] for s in self.sections]

    def test_every_section_heading_is_version_dash_date(self):
        self.assertNotEqual(self.headings, [])
        for heading in self.headings:
            with self.subTest(heading=heading):
                self.assertRegex(heading, HEADING)

    def test_the_released_versions_close_the_list_with_their_tag_dates(self):
        expected = ["## %s - %s" % (version, date) for version, date, _, _ in RELEASED]
        self.assertEqual(self.headings[-len(expected) :], expected)

    def test_newest_first_and_the_newest_is_the_package_version(self):
        matches = [HEADING.match(h) for h in self.headings]
        versions = [tuple(int(n) for n in m.group(1, 2, 3)) for m in matches if m]
        self.assertNotEqual(versions, [])
        self.assertEqual(versions, sorted(versions, reverse=True))
        self.assertEqual(len(set(versions)), len(versions), "a version has two sections")
        newest = "%d.%d.%d" % versions[0]
        self.assertEqual(newest, VERSION)

    def test_no_unreleased_section(self):
        for line in self.text.split("\n"):
            if line.startswith("#"):
                self.assertNotIn("unreleased", line.lower(), line)

    def test_each_released_section_names_pull_requests_of_its_own_release(self):
        by_heading = {s.split("\n", 1)[0]: s for s in self.sections}
        for version, date, low, high in RELEASED:
            with self.subTest(version=version):
                body = by_heading.get("## %s - %s" % (version, date))
                self.assertIsNotNone(body, version)
                numbers = [int(n) for n in re.findall(r"#(\d+)\b", body)]
                own = [n for n in numbers if low <= n <= high]
                self.assertNotEqual(own, [], "no PR number merged for this release")
                later = [n for n in numbers if n > high]
                self.assertEqual(later, [], "PR numbers merged after this release was tagged")

    def test_readme_links_the_changelog(self):
        self.assertRegex(read(ROOT, "README.md"), r"\]\(CHANGELOG\.md\)")


class ReleasingNamesThePlaces(unittest.TestCase):
    """AC-5, on the repository's own docs/releasing.md."""

    def test_every_place_a_version_bump_touches_is_named(self):
        page = read(ROOT, "docs/releasing.md")
        files = comparing_files()
        self.assertNotEqual(files, [], "no test compares VERSION with a literal any more")
        for place in ["revali/__init__.py", "CHANGELOG.md"] + files:
            with self.subTest(place=place):
                self.assertIn(place, page)


class TheProjectTestCatchesIt(unittest.TestCase):
    """AC-2 and AC-5: the project's test fails on a copy with one mistake in it."""

    @classmethod
    def setUpClass(cls):
        cls.copy = tempfile.mkdtemp(prefix="revali review changelog ")
        cls.addClassCleanup(shutil.rmtree, cls.copy, True)
        for name in sorted(os.listdir(ROOT)):
            src = os.path.join(ROOT, name)
            if os.path.isfile(src) and name.endswith(COPIED_FILES):
                shutil.copy2(src, os.path.join(cls.copy, name))
        skip = shutil.ignore_patterns("__pycache__", "*.pyc")
        for name in COPIED_DIRS:
            shutil.copytree(os.path.join(ROOT, name), os.path.join(cls.copy, name), ignore=skip)
        cls.base_code, cls.base_out = run_module(cls.copy)

    def setUp(self):
        # a mistake proves nothing unless the untouched copy passes
        self.assertEqual(self.base_code, 0, "the untouched copy does not pass:\n" + self.base_out)
        self.changelog = read(self.copy, "CHANGELOG.md")
        self.page = read(self.copy, "docs/releasing.md")

    def run_with(self, rel, text):
        """Put `text` at `rel` in the copy, run the project's test, put the file back."""
        path = os.path.join(self.copy, *rel.split("/"))
        before = read(self.copy, rel) if os.path.isfile(path) else None
        self.assertNotEqual(text, before, "the mistake changed nothing in %s" % rel)
        write(self.copy, rel, text)
        try:
            return run_module(self.copy)
        finally:
            if before is None:
                os.remove(path)
            else:
                write(self.copy, rel, before)

    def test_newest_section_is_not_the_package_version(self):  # AC-2
        preamble, sections = split_sections(self.changelog)
        major, minor, _ = (int(n) for n in VERSION.split("."))
        ahead = "## %d.%d.0 - 2099-01-01\n\n- something later (#9999)\n" % (major, minor + 1)
        code, out = self.run_with("CHANGELOG.md", join_sections(preamble, [ahead] + sections))
        self.assertNotEqual(code, 0, out)

    def test_heading_in_another_format(self):  # AC-2
        preamble, sections = split_sections(self.changelog)
        self.assertGreaterEqual(len(sections), 2)
        heading, _, rest = sections[1].partition("\n")
        m = HEADING.match(heading)
        self.assertIsNotNone(m, heading)
        version = "%s.%s.%s" % m.group(1, 2, 3)
        date = m.group(4)
        cases = {
            "v before the version": [sections[0], "## v%s - %s\n%s" % (version, date, rest)]
            + sections[2:],
            "date in brackets": [sections[0], "## %s (%s)\n%s" % (version, date, rest)]
            + sections[2:],
            "a heading that is no release": [sections[0], "## Known issues\n\n- one\n"]
            + sections[1:],
        }
        for label, mutated in cases.items():
            with self.subTest(label=label):
                code, out = self.run_with("CHANGELOG.md", join_sections(preamble, mutated))
                self.assertNotEqual(code, 0, out)

    def test_versions_that_do_not_descend(self):  # AC-2
        preamble, sections = split_sections(self.changelog)
        self.assertGreaterEqual(len(sections), 3)
        # the newest stays first, so only the order below it is wrong
        swapped = sections[:1] + [sections[2], sections[1]] + sections[3:]
        code, out = self.run_with("CHANGELOG.md", join_sections(preamble, swapped))
        self.assertNotEqual(code, 0, out)

    def test_a_new_pin_the_page_does_not_name(self):  # AC-5
        rel = "tests/test_zz_unlisted_pin.py"
        self.assertNotIn(rel, self.page)
        code, out = self.run_with(rel, PIN_SOURCE % VERSION)
        self.assertNotEqual(code, 0, out)

    def test_a_page_that_no_longer_names_a_place(self):  # AC-5
        files = comparing_files()
        self.assertNotEqual(files, [], "no test compares VERSION with a literal any more")
        for place in ["revali/__init__.py", files[0]]:
            with self.subTest(place=place):
                self.assertIn(place, self.page)
                code, out = self.run_with(
                    "docs/releasing.md", self.page.replace(place, "somewhere")
                )
                self.assertNotEqual(code, 0, out)


if __name__ == "__main__":
    unittest.main()
