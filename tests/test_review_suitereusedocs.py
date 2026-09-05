"""Reviewer acceptance tests for feature/skip-unchanged-suite, the interface side: the
`[validate] reuse_baseline` key exists in defaults.toml (true), is listed in the user template
and documented in docs/configuration.md (AC-4); README.md and docs/sandbox.md describe the
rule (AC-6); the state file layout bump is recorded (AC-1)."""

import os
import re
import tomllib
import unittest

from revali import STATE_VERSION
from revali.config import load_defaults
from revali.state import State

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def read(*parts):
    with open(os.path.join(ROOT, *parts), "r", encoding="utf-8", newline="") as fh:
        return fh.read()


class TheKeyIsDeclared(unittest.TestCase):
    def test_defaults_toml_turns_it_on(self):
        data = tomllib.loads(read("defaults.toml"))
        self.assertIn("reuse_baseline", data["validate"])  # AC-4
        self.assertIs(data["validate"]["reuse_baseline"], True)  # AC-4: default true
        self.assertIs(load_defaults()["validate"]["reuse_baseline"], True)

    def test_the_template_lists_it_under_validate(self):
        text = read("templates", "revali.toml")
        data = tomllib.loads(text)
        self.assertIn("reuse_baseline", data["validate"])  # AC-4
        self.assertIsInstance(data["validate"]["reuse_baseline"], bool)
        # the line carries an explanation, like the template's other keys
        line = [ln for ln in text.splitlines() if ln.startswith("reuse_baseline")]
        self.assertEqual(len(line), 1, text)
        self.assertIn("#", line[0])

    def test_configuration_md_documents_it(self):
        text = read("docs", "configuration.md")
        self.assertIn("reuse_baseline", text)  # AC-4
        idx = text.index("reuse_baseline")
        around = text[max(0, idx - 200) : idx + 900]
        self.assertIn("test_dir", around)  # the rule: only test_dir paths
        self.assertIn("Revali-Round", around)  # the rule: the reviewer's trailer
        self.assertIn("false", around)  # how to turn it off


class TheDocsDescribeTheRule(unittest.TestCase):
    def test_readme_says_when_the_existing_suite_is_left_out(self):
        text = read("README.md")
        # AC-6: the "what a run does" paragraph names the exception
        idx = text.index("What a run does")
        paragraph = text[idx : idx + 1200]
        self.assertIn("existing suite", paragraph)
        self.assertRegex(paragraph, r"reviewer.s test commits")
        self.assertIn("baseline", paragraph)

    def test_sandbox_md_describes_it(self):
        text = read("docs", "sandbox.md")
        self.assertIn("reuse_baseline", text)  # AC-6
        self.assertIn("test_dir", text)
        self.assertRegex(text, r"leaves `test` out|skip(s|ped)? `test`|`test` (is )?not (re)?run")
        self.assertIn("tests.md", text)  # where the decision is recorded


class TheStateLayoutIsBumped(unittest.TestCase):
    def test_state_version_is_four_and_the_field_exists(self):
        self.assertGreaterEqual(STATE_VERSION, 4)  # AC-1: layout change bumps the version
        self.assertEqual(State().baseline_sha, "")
        self.assertEqual(State().version, STATE_VERSION)

    def test_the_field_is_documented_at_its_declaration(self):
        text = read("revali", "state.py")
        self.assertRegex(text, r"baseline_sha: str = \"\".*STATE_VERSION 4")


if __name__ == "__main__":
    unittest.main()
