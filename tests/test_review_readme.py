"""README wording, checked as text: the "Why separate sessions" paragraph, the
removed private-only requirement and the public-repository side effect (PR #11);
the status line, the role table and the note under the diagram (PR #8, #9)."""
import os
import re
import unittest

from tests.helpers import ROOT
from revali import VERSION


def readme():
    with open(os.path.join(ROOT, "README.md"), "r", encoding="utf-8", newline="") as fh:
        return fh.read()


class WhySeparateSessions(unittest.TestCase):
    def setUp(self):
        self.text = readme()

    def paragraph(self):
        m = re.search(r"\*\*Why separate sessions\.?\*\*(.*?)\n\n", self.text, flags=re.S)
        self.assertIsNotNone(m, "README has no 'Why separate sessions' paragraph")
        return m.group(0)

    def test_paragraph_follows_the_three_role_definitions(self):
        text = self.text
        roles = [text.index("**Developer**"), text.index("**Reviewer**"), text.index("**Validator**")]
        why = text.index("**Why separate sessions")
        self.assertGreater(why, max(roles))
        self.assertLess(why, text.index("```mermaid"))

    def test_paragraph_covers_the_four_points(self):
        p = self.paragraph().lower()
        self.assertIn("own work", p)                      # nobody grades their own work
        self.assertIn("reviewer", p)                      # what each role sees
        self.assertIn("validator", p)
        self.assertIn("tier", p)                          # models differ by tier
        self.assertIn("vendor", p)                        # or by vendor
        self.assertIn("blind spot", p)                    # fresh context does not remove shared blind spots
        self.assertIn("revali stats", p)                  # which stats tracks


class PrivateRequirementIsGone(unittest.TestCase):
    def setUp(self):
        self.text = readme()

    def test_no_private_only_wording(self):
        low = self.text.lower()
        self.assertNotIn("or that is public", low)
        self.assertNotIn("only runs on private", low)
        self.assertNotIn("private repos", low)
        self.assertNotIn("must be private", low)

    def test_side_effect_list_describes_the_summary_comments(self):
        start = self.text.index("## What revali does to your repository")
        end = self.text.index("## Development", start)
        section = self.text[start:end].lower()
        self.assertIn("pr comment", section)
        self.assertIn("summar", section)
        self.assertIn("not private", section)
        self.assertIn("request", section)   # the PR body withholds the Request section
        self.assertIn("never runs on a repo you do not own", section)


# --- kept from PR #8 / #9 (this file is shared across rounds; do not drop earlier checks) ---

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
        self.assertIn("until the first review round is recorded", note)
        self.assertIn("`kind: docs`", note)
        self.assertRegex(note, r"`setup`.*`build`", "the note names both sandbox steps")
        self.assertIn("exit 1", note)
        self.assertRegex(note, r"not a FAIL", "a setup / build failure is not a verdict")


if __name__ == "__main__":
    unittest.main()
