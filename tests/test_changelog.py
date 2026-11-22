"""feature/release-steps: CHANGELOG.md with one section per release (AC-1, AC-2), README's
Status paragraph pointing at it (AC-3), and the release steps in docs/releasing.md naming
every place a version bump touches (AC-4, AC-5)."""

import ast
import datetime
import os
import re
import unittest

from revali import VERSION

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

HEADING = re.compile(r"^## (\d+)\.(\d+)\.(\d+) - (\d{4})-(\d{2})-(\d{2})$")
RELEASED = (
    ("0.6.0", "2026-11-23"),
    ("0.5.0", "2026-11-21"),
    ("0.4.0", "2026-11-19"),
    ("0.3.0", "2026-09-07"),
    ("0.2.0", "2026-09-05"),
    ("0.1.0", "2026-08-30"),
)


def read(rel):
    with open(os.path.join(ROOT, rel), "r", encoding="utf-8", newline="") as fh:
        return fh.read()


def unwrap(text):
    return " ".join(text.split())


def changelog_problems(text, version):
    """What is wrong with a changelog's section headings, as a list of messages."""
    problems = []
    versions = []
    for line in text.splitlines():
        if not line.startswith("## "):
            continue
        m = HEADING.match(line)
        if not m:
            problems.append("heading not `## x.y.z - YYYY-MM-DD`: %s" % line)
            continue
        try:
            datetime.date(*(int(n) for n in m.group(4, 5, 6)))
        except ValueError:
            problems.append("not a date: %s" % line)
        versions.append(tuple(int(n) for n in m.group(1, 2, 3)))
    if not versions:
        return problems + ["no release section"]
    newest = "%d.%d.%d" % versions[0]
    if newest != version:
        problems.append("newest section is %s, VERSION is %s" % (newest, version))
    for before, after in zip(versions, versions[1:], strict=False):
        if not before > after:
            problems.append("%d.%d.%d is not newer than %d.%d.%d" % (before + after))
    return problems


def section(text, version):
    """The body of the `## <version> - ...` section, up to the next `## `."""
    m = re.search(r"^## %s - .*?$(.*?)(?=^## |\Z)" % re.escape(version), text, re.M | re.S)
    return m.group(1) if m else None


def status_paragraph(readme):
    return unwrap(readme.split("\nStatus:", 1)[1].split("\n\n", 1)[0])


def pins_version(source, version):
    """True when an `assert...(...)` call or an `assert` statement in `source` holds a string
    literal naming `version`: the test fails once VERSION moves on, so a release has to
    update it. A changelog heading (`## <version> - `) in a literal is a lookup, not a pin.
    A literal held in a variable and compared later is not seen."""
    token = re.compile(r"(?<![\d.])(?<!## )%s(?!\.?\d)(?! - )" % re.escape(version))
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call):
            name = getattr(node.func, "attr", getattr(node.func, "id", ""))
            if not name.startswith("assert"):
                continue
        elif not isinstance(node, ast.Assert):
            continue
        for inner in ast.walk(node):
            if isinstance(inner, ast.Constant) and isinstance(inner.value, str):
                if token.search(inner.value):
                    return True
    return False


def version_pins(version):
    found = []
    for name in sorted(os.listdir(HERE)):
        if name.startswith("test_") and name.endswith(".py"):
            if pins_version(read("tests/" + name), version):
                found.append("tests/" + name)
    return found


class ChangelogChecker(unittest.TestCase):
    """AC-2: the checker the real changelog goes through catches each kind of mistake (on
    made-up 7.x versions, so these literals never pin the current one)."""

    GOOD = "# Changelog\n\n## 7.3.0 - 2026-09-07\n\n- a\n\n## 7.2.0 - 2026-09-05\n\n- b\n"

    def test_a_good_changelog_has_no_problem(self):
        self.assertEqual(changelog_problems(self.GOOD, "7.3.0"), [])

    def test_newest_section_must_be_the_version(self):
        problems = changelog_problems(self.GOOD, "7.4.0")
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("VERSION is 7.4.0", problems[0])

    def test_heading_format(self):
        for bad in (
            "## v7.3.0 - 2026-09-07",
            "## 7.3.0 (2026-09-07)",
            "## 7.3 - 2026-09-07",
            "## 7.3.0 - 7 Sep 2026",
            "## Unreleased",
        ):
            with self.subTest(bad=bad):
                problems = changelog_problems(
                    self.GOOD.replace("## 7.3.0 - 2026-09-07", bad), "7.3.0"
                )
                self.assertTrue(any("heading not" in p for p in problems), problems)

    def test_date_must_exist(self):
        for bad in ("## 7.3.0 - 2026-13-07", "## 7.3.0 - 2026-02-30", "## 7.3.0 - 2026-09-00"):
            with self.subTest(bad=bad):
                problems = changelog_problems(
                    self.GOOD.replace("## 7.3.0 - 2026-09-07", bad), "7.3.0"
                )
                self.assertEqual(problems, ["not a date: %s" % bad])

    def test_versions_descend(self):
        swapped = "## 7.2.0 - 2026-09-05\n\n## 7.3.0 - 2026-09-07\n"
        self.assertTrue(any("not newer" in p for p in changelog_problems(swapped, "7.2.0")))
        twice = "## 7.3.0 - 2026-09-07\n\n## 7.3.0 - 2026-09-07\n"
        self.assertTrue(any("not newer" in p for p in changelog_problems(twice, "7.3.0")))
        numeric = "## 7.10.0 - 2026-12-01\n\n## 7.9.0 - 2026-11-01\n"
        self.assertEqual(changelog_problems(numeric, "7.10.0"), [])

    def test_no_section_at_all(self):
        self.assertEqual(changelog_problems("# Changelog\n", "7.3.0"), ["no release section"])


class Changelog(unittest.TestCase):
    """AC-1, AC-2"""

    def setUp(self):
        self.text = read("CHANGELOG.md")

    def test_headings_order_and_version(self):
        self.assertEqual(changelog_problems(self.text, VERSION), [])

    def test_released_versions_with_their_tag_dates(self):
        headings = [line for line in self.text.splitlines() if line.startswith("## ")]
        tail = ["## %s - %s" % pair for pair in RELEASED]
        self.assertEqual(headings[-len(tail) :], tail)

    def test_no_unreleased_section(self):
        headings = [line for line in self.text.splitlines() if line.startswith("#")]
        self.assertEqual([h for h in headings if "unreleased" in h.lower()], [])

    def test_each_section_names_its_prs(self):
        expected = {
            "0.1.0": ("#1", "#6", "#10", "#11"),
            "0.2.0": ("#12", "#18", "PR #21 to #24", "#25", "black", "ruff"),
            "0.3.0": ("#26", "PR #28 to #33", "#38", "#41", "#43"),
            "0.4.0": ("PR #44 to #99", "#76", "#80", "#99", "Upgrading", "Known issues"),
            "0.5.0": ("PR #101 to #112", "#103", "#112", "Upgrading", "Known issues"),
            "0.6.0": (
                "PR #114 to #117",
                "#114",
                "#115",
                "#116",
                "#117",
                "Upgrading",
                "Known issues",
            ),
        }
        for version, needles in expected.items():
            body = section(self.text, version)
            self.assertIsNotNone(body, version)
            body = unwrap(body)
            for needle in needles:
                with self.subTest(version=version, needle=needle):
                    self.assertIn(needle, body)

    def test_readme_links_the_changelog(self):
        self.assertIn("(CHANGELOG.md)", read("README.md"))

    def test_lf_only(self):
        self.assertNotIn("\r", self.text)


class StatusParagraph(unittest.TestCase):
    """AC-3"""

    def setUp(self):
        self.readme = read("README.md")
        self.para = status_paragraph(self.readme)

    def test_version_changelog_and_verification(self):
        self.assertIn("package version %s" % VERSION, self.para)
        self.assertIn("[CHANGELOG.md](CHANGELOG.md)", self.para)
        self.assertIn("Verified end to end on a private GitHub repository", self.para)
        self.assertIn("(docs/sandbox.md)", self.para)

    def test_release_notes_left_the_readme(self):
        self.assertNotIn("PR #", self.para)
        self.assertNotIn("archive_dir", self.para)


class ReleaseSteps(unittest.TestCase):
    """AC-4"""

    def setUp(self):
        text = read("docs/releasing.md")
        m = re.search(r"^## Making a release$(.*?)(?=^## |\Z)", text, re.M | re.S)
        self.assertIsNotNone(m, "no '## Making a release' section")
        self.body = m.group(1)
        self.steps = [unwrap(s) for s in re.split(r"^\d+\. ", self.body, flags=re.M)[1:]]

    def test_five_steps_in_order(self):
        self.assertEqual(len(self.steps), 5, self.steps)
        when, audit, version, change, tag = self.steps
        self.assertIn("no fixed schedule", when)
        self.assertIn("tools/doc_audit.py", audit)
        self.assertIn("high or medium finding blocks the release", audit)
        self.assertIn("known issues", audit)
        self.assertIn("minor", version)
        self.assertIn("patch", version)
        self.assertIn("revali/__init__.py", change)
        self.assertIn("CHANGELOG.md", change)
        self.assertIn("Status", change)
        self.assertIn("git tag -a v<version>", tag)
        self.assertIn("git push origin v<version>", tag)
        self.assertIn("maintainer", tag)


class BumpPlaces(unittest.TestCase):
    """AC-5"""

    def test_detector(self):
        # 9.8.7 stands for the current version, so this file pins nothing itself
        pins = (
            'self.assertEqual(VERSION, "9.8.7")',
            'self.assertEqual(out.strip(), "revali 9.8.7")',
            'self.assertIn("9.8.7", line)',
            'self.assertEqual(\n    out,\n    "revali 9.8.7.",\n)',
            'assertEqual(x, "9.8.7")',
            'assert VERSION == "9.8.7"',
        )
        for source in pins:
            with self.subTest(source=source):
                self.assertTrue(pins_version(source, "9.8.7"))
        free = (
            "self.assertIn(VERSION, line)",
            'self.assertIn("19.8.7", line)',
            'self.assertIn("9.8.70", line)',
            'self.assertIn("9.8.7.1", line)',
            'words = ("9.8.7",)',
            'body = text.split("## 9.8.7 - ")',
            # a changelog section looked up inside the assert call is not a pin either
            'self.assertIn("#21", text.split("\\n## 9.8.7 - ", 1)[1])',
            'self.assertIn("## 9.8.7 - 2026-12-01", text)',
            'assert "9.8.7 - " in text',
        )
        for source in free:
            with self.subTest(source=source):
                self.assertFalse(pins_version(source, "9.8.7"))

    def test_the_pins_today_are_found(self):
        # the detector finds the known pins; a new pin needs no edit here, only in the page
        found = version_pins(VERSION)
        for known in ("tests/test_docs_split.py", "tests/test_review_docssplit.py"):
            self.assertIn(known, found)

    def test_releasing_names_every_place(self):
        text = read("docs/releasing.md")
        for place in ["revali/__init__.py", "CHANGELOG.md", "Status"] + version_pins(VERSION):
            with self.subTest(place=place):
                self.assertIn(place, text)


if __name__ == "__main__":
    unittest.main()
