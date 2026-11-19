"""docs/install-release (M3.24): docs/workflow.md says how to install revali at a release tag
and move it (AC-1, AC-2), what the build looks like at a tag (AC-3); docs/releasing.md
step 5 moves the checkout other projects run from (AC-4); README points at it (AC-5).
Documents are read with their whitespace folded, so a rewrap does not matter but dropping
a statement does."""

import os
import re
import unittest

from tests.helpers import ROOT


def raw(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8", newline="") as fh:
        return fh.read()


def flat(text):
    return " ".join(text.split())


def h2(text, title):
    m = re.search(r"^## %s\n(.*?)(?=^## |\Z)" % re.escape(title), text, re.M | re.S)
    return m.group(1) if m else None


class Installing(unittest.TestCase):
    """AC-1, AC-2"""

    def setUp(self):
        self.text = raw("docs", "workflow.md")
        section = h2(self.text, "Installing revali")
        self.assertIsNotNone(section, "no '## Installing revali' section")
        self.section = flat(section)

    def test_it_comes_before_project_setup(self):
        self.assertLess(
            self.text.index("\n## Installing revali\n"), self.text.index("\n## Project setup\n")
        )

    def test_a_release_tag_not_main_and_why(self):
        self.assertIn("check out the newest release tag", self.section)
        self.assertIn(
            "not main: main changes with every merged PR, `PROMPT_VERSION` included", self.section
        )
        self.assertIn("git -C <revali dir> checkout v<version>", self.section)
        self.assertIn("a detached HEAD at the tag", self.section)

    def test_the_skill_link_on_each_platform(self):
        self.assertIn("as `~/.claude/skills/revali`", self.section)
        self.assertIn("a junction on Windows, a symbolic link elsewhere", self.section)
        self.assertIn("New-Item -ItemType Junction", self.section)
        self.assertIn('mklink /J "%USERPROFILE%\\.claude\\skills\\revali"', self.section)
        self.assertIn('ln -s "<revali dir>/skill" ~/.claude/skills/revali', self.section)

    def test_claude_md_names_the_checkout(self):
        self.assertIn(
            "The project's `CLAUDE.md` names that checkout as `<revali dir>`", self.section
        )

    def test_updating(self):
        self.assertIn("git -C <revali dir> fetch --tags", self.section)
        self.assertIn("git -C <revali dir> checkout v<newer version>", self.section)

    def test_development_and_runs_in_progress(self):  # AC-2
        self.assertIn(
            "A checkout used to develop revali is not the one other projects run from",
            self.section,
        )
        self.assertIn(
            "A run in progress still reads files from its checkout after it starts (prompts, "
            "schemas, templates), so move a checkout only while no run from it is in progress",
            self.section,
        )


class BuildAtATag(unittest.TestCase):
    """AC-3"""

    def test_the_tag_alone(self):
        running = flat(h2(raw("docs", "workflow.md"), "Running"))
        # a pattern, not the literal example: a literal version here would pin VERSION
        self.assertRegex(
            running,
            r"a checkout at a release tag shows the tag alone, e\.g\. `(\d+\.\d+\.\d+) \(v\1\)`",
        )


class Releasing(unittest.TestCase):
    """AC-4"""

    def test_step_5_moves_the_checkout_last(self):
        text = raw("docs", "releasing.md")
        steps = re.split(r"^\d+\. ", h2(text, "Making a release"), flags=re.M)
        step5 = flat(steps[5])
        self.assertIn(
            "Last, once the release is published and while no run from it is in progress, "
            "move the checkout other projects run from to the new tag",
            step5,
        )
        self.assertGreater(step5.index("Last, once"), step5.index("gh release create"))
        self.assertIn("[workflow.md](workflow.md)", step5)


class Readme(unittest.TestCase):
    """AC-5"""

    def test_documentation_entry_and_length(self):
        text = raw("README.md")
        entry = [line for line in text.split("\n") if line.startswith("- [docs/workflow.md]")]
        self.assertEqual(len(entry), 1)
        self.assertIn("installing revali at a release tag", entry[0])
        self.assertLessEqual(len(text.rstrip("\n").split("\n")), 200)


if __name__ == "__main__":
    unittest.main()
