"""Reviewer acceptance tests for fix/audit-lows-0.4.0.

AC-10: the reviewer prompt's line of existing test files and its `test_changes` step, and the
       answer schema's `test_changes` description, name renames next to modifications and
       deletions, with a rename listed under its old name; PROMPT_VERSION is 15 or later.

The other criteria are about the wording of documents and are not tested here.

Everything about the prompt is read from the prompt revali builds for the fixture branch
(feature/mul, whose base carries tests/test_calc.py), not from prompts/review.md: the line is
found by its fixed opening, and what is asserted is what its head names and which files follow
the colon.
"""

import json
import os
import re
import unittest

from revali import PROMPT_VERSION
from revali.preflight import preflight
from revali.review import build_prompt
from revali.state import State
from tests.helpers import ROOT, RepoCase, git

OLD = "tests/test_calc.py"
NEW = "tests/test_sums.py"


def listing(prompt):
    """(head, names): the prompt line listing the existing test files the diff touches, split
    at its colon."""
    found = re.search(r"^(Existing test files this diff [^:\n]*): ([^\n]*)$", prompt, re.M)
    assert found, "the prompt has no line of existing test files"
    names = found.group(2).strip()
    return found.group(1), [] if names == "(none)" else [n.strip() for n in names.split(",")]


def numbered_steps(prompt):
    """Every numbered item of the prompt, each with its continuation lines."""
    return re.findall(r"(?ms)^\d+\. .*?(?=^\d+\. |^#|\Z)", prompt)


def described(node, key):
    """The `description` of every object stored under `key`, anywhere in a JSON schema."""
    out = []
    if isinstance(node, dict):
        for name, value in node.items():
            if name == key and isinstance(value, dict) and "description" in value:
                out.append(value["description"])
            out.extend(described(value, key))
    elif isinstance(node, list):
        for value in node:
            out.extend(described(value, key))
    return out


class RenamedTestsInThePrompt(RepoCase):
    def build(self):
        ctx = preflight(self.repo)
        ctx.log = None
        return build_prompt(ctx, State(), self.rdir(), 1)

    def test_a_renamed_test_is_listed_under_its_old_name_on_a_line_that_names_renames(self):
        """AC-10: the branch renames an existing test; the line that lists it says it covers
        renames and under which name, and lists the old name only"""
        git(["mv", OLD, NEW], self.repo)
        self.commit_all("rename the existing test")
        head, names = listing(self.build())
        self.assertRegex(head, r"(?i)\brenam")
        self.assertRegex(head, r"(?i)\bold name\b")
        self.assertEqual(names, [OLD])
        self.assertNotIn(NEW, names)

    def test_the_line_still_names_modifications_and_deletions(self):
        """AC-10: "modifies, deletes or renames": the two earlier cases stay on the line's
        head, and a modified file is still listed"""
        body = self.read(OLD)
        self.write(OLD, body + "\n# a line the branch adds\n")
        self.commit_all("edit the existing test")
        head, names = listing(self.build())
        self.assertRegex(head, r"(?i)\bmodif")
        self.assertRegex(head, r"(?i)\bdelet")
        self.assertRegex(head, r"(?i)\brenam")
        self.assertEqual(names, [OLD])

    def test_no_touched_test_leaves_the_line_empty(self):
        """AC-10, the edge: the fixture branch as it is touches no existing test, and the line
        that names renames says so instead of going missing"""
        head, names = listing(self.build())
        self.assertRegex(head, r"(?i)\brenam")
        self.assertEqual(names, [])

    def test_the_test_changes_step_asks_for_renamed_files_too(self):
        """AC-10: the step that asks for a `test_changes` entry per touched test file names
        renamed files, so the listed rename gets an entry"""
        steps = [s for s in numbered_steps(self.build()) if "`test_changes`" in s]
        self.assertTrue(steps, "no numbered step of the prompt mentions `test_changes`")
        for step in steps:
            with self.subTest(step=step.split("\n")[0]):
                self.assertRegex(step, r"(?i)\bmodif")
                self.assertRegex(step, r"(?i)\bdelet")
                self.assertRegex(step, r"(?i)\brenam")


class SchemaAndVersion(unittest.TestCase):
    def test_the_schema_describes_test_changes_with_renames(self):
        """AC-10: the answer schema's `test_changes` description names renames as well"""
        path = os.path.join(ROOT, "schemas", "review.schema.json")
        with open(path, encoding="utf-8", newline="") as fh:
            schema = json.load(fh)
        descriptions = described(schema, "test_changes")
        self.assertTrue(descriptions, "the schema has no described `test_changes`")
        for text in descriptions:
            self.assertRegex(text, r"(?i)\bmodif")
            self.assertRegex(text, r"(?i)\bdelet")
            self.assertRegex(text, r"(?i)\brenam")

    def test_prompt_version_moved(self):
        """AC-10: the prompt and the schema changed, so PROMPT_VERSION is 15 or later"""
        self.assertGreaterEqual(int(PROMPT_VERSION), 15)
