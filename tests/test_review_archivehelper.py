"""AC-10: the abbreviated-sha joins of recover_test_ownership go through one helper whose
spelling is the log's (ten characters, comma-space separated)."""

import os
import unittest

from revali import review
from tests.helpers import ROOT


class ShortShasTests(unittest.TestCase):
    def test_spelling_matches_the_log_lines(self):
        # AC-10: the same text the inline joins produced
        a, b = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0", "0" * 39 + "1"
        self.assertEqual(review.short_shas([a, b]), "a1b2c3d4e5, 0000000000")
        self.assertEqual(review.short_shas([a]), "a1b2c3d4e5")
        self.assertEqual(review.short_shas([]), "")

    def test_accepts_any_sequence_and_short_input(self):
        # AC-10: tuples and generators as the callers pass them; a short sha stays whole
        self.assertEqual(review.short_shas(("abc", "def")), "abc, def")
        self.assertEqual(review.short_shas(iter(["x" * 12])), "x" * 10)

    def test_the_inline_joins_are_gone(self):
        # AC-10: one helper, no copies of the join left in the module
        with open(os.path.join(ROOT, "revali", "review.py"), "r", encoding="utf-8") as fh:
            source = fh.read()
        self.assertNotIn("c[:10] for c in", source)
        self.assertNotIn("[:10] for c in", source)
        self.assertGreaterEqual(source.count("short_shas("), 5)  # the definition and four uses


if __name__ == "__main__":
    unittest.main()
