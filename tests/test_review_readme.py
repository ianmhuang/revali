"""AC-3: the README wording. Checked against the file as text, since the README
is the deliverable: the status line names the package version and does not read
as a release version; the role table attributes review-n.md, tests.md and the PR
comment to revali; the Reviewer reads response-n.md; the note under the diagram
covers the baseline and the sandbox setup / build failure.
"""
import os
import re
import unittest

from tests.helpers import ROOT
from revali import VERSION


def readme():
    with open(os.path.join(ROOT, "README.md"), "r", encoding="utf-8", newline="") as fh:
        return fh.read()


def table_row(text, role):
    """Cells of the role table row whose first cell is `role`."""
    for line in text.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if cells and cells[0] == role:
            return cells
    return None


class StatusLine(unittest.TestCase):
    def test_states_the_package_version_and_is_not_a_bare_release_version(self):
        text = readme()
        m = re.search(r"^Status:.*$", text, re.M)
        self.assertIsNotNone(m, "no 'Status:' line in README")
        line = m.group(0)
        self.assertIn(VERSION, line, line)
        # "Status: v1.0." on its own reads as a release version; the sentence must qualify it
        self.assertIsNone(re.match(r"^Status:\s*v\d+(\.\d+)*\.\s", line), line)
        self.assertRegex(line, r"package version\s+%s" % re.escape(VERSION))


class RoleTable(unittest.TestCase):
    def setUp(self):
        self.text = readme()
        header = table_row(self.text, "Role")
        self.assertIsNotNone(header, "role table header not found")
        self.reads = header.index("Reads")
        self.writes = header.index("Writes")

    def test_reviewer_reads_response_n(self):
        row = table_row(self.text, "Reviewer")
        self.assertIsNotNone(row)
        self.assertIn("response-n.md", row[self.reads])

    def test_review_artifacts_are_attributed_to_revali(self):
        row = table_row(self.text, "Reviewer")
        writes = row[self.writes]
        for artifact in ("review-n.md", "tests.md", "PR comment"):
            self.assertIn(artifact, writes, artifact)
        # revali is named as the one that produces them from the Reviewer's answer
        self.assertIn("revali", writes)
        idx_revali = writes.index("revali")
        for artifact in ("review-n.md", "tests.md"):
            self.assertGreater(writes.index(artifact), idx_revali,
                               "%s should be listed as produced by revali, not written by the Reviewer" % artifact)

    def test_validator_tests_md_attributed_to_revali(self):
        row = table_row(self.text, "Validator")
        self.assertIsNotNone(row)
        writes = row[self.writes]
        self.assertIn("tests.md", writes)
        self.assertIn("revali", writes)
        self.assertLess(writes.index("revali"), writes.index("tests.md"))


class NoteUnderDiagram(unittest.TestCase):
    def test_baseline_and_sandbox_failure_note_sits_between_diagram_and_table(self):
        text = readme()
        end_of_diagram = text.index("```", text.index("```mermaid") + 10)
        table_start = text.index("| Role |")
        note = " ".join(text[end_of_diagram:table_start].split())  # the note wraps across lines
        self.assertIn("baseline", note)
        self.assertIn("once per branch", note)
        self.assertIn("`kind: docs`", note)
        self.assertRegex(note, r"`setup`.*`build`", "the note names both sandbox steps")
        self.assertIn("exit 1", note)
        self.assertRegex(note, r"not a FAIL", "a setup / build failure is not a verdict")


if __name__ == "__main__":
    unittest.main()
