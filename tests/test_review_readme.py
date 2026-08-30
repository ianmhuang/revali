"""Acceptance test for AC-3: the README explains why the roles run in separate
sessions, no longer requires a private repository, and says what a public
repository's PR receives."""
import os
import re
import unittest

from tests.helpers import ROOT


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


if __name__ == "__main__":
    unittest.main()
