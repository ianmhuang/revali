import unittest

from revali.models import DIAGNOSER, REVIEWER, resolve, tier_index
from tests.helpers import ROOT  # noqa: F401  (sys.path setup)

TIERS = ["haiku", "sonnet", "opus", "fable"]
OTHER = [["gpt-5-mini", "gpt-5"]]


class TierLookupTests(unittest.TestCase):
    def test_substring_and_exact(self):
        self.assertEqual(tier_index("claude-fable-5", TIERS), 3)
        self.assertEqual(tier_index("opus", TIERS), 2)
        self.assertEqual(tier_index("Claude-Sonnet-5", TIERS), 1)
        self.assertEqual(tier_index("claude-haiku-4-5-20251001", TIERS), 0)

    def test_unknown(self):
        self.assertIsNone(tier_index("fixture", TIERS))
        self.assertIsNone(tier_index("", TIERS))
        self.assertIsNone(tier_index("gpt-5", TIERS))


class AutoResolutionTests(unittest.TestCase):
    def test_reviewer_one_above(self):
        r = resolve(REVIEWER, "auto", "auto", "claude-opus-5", TIERS)
        self.assertEqual((r.model, r.fallback), ("fable", "opus,sonnet,haiku"))
        self.assertIn("one tier above", r.reason)

    def test_reviewer_capped_at_top(self):
        r = resolve(REVIEWER, "auto", "auto", "claude-fable-5", TIERS)
        self.assertEqual(r.model, "fable")
        self.assertIn("already at the top", r.reason)

    def test_reviewer_unknown_author_is_top(self):
        r = resolve(REVIEWER, "auto", "auto", "fixture", TIERS)
        self.assertEqual(r.model, "fable")
        self.assertIn("not on the ladder", r.reason)
        r = resolve(REVIEWER, "auto", "auto", "", TIERS)
        self.assertEqual(r.model, "fable")
        self.assertIn("not given", r.reason)

    def test_reviewer_foreign_author_is_top(self):
        r = resolve(REVIEWER, "auto", "auto", "gpt-5", TIERS, OTHER)
        self.assertEqual(r.model, "fable")
        self.assertIn("another engine", r.reason)

    def test_diagnoser_one_below(self):
        r = resolve(DIAGNOSER, "auto", "auto", "claude-fable-5", TIERS)
        self.assertEqual((r.model, r.fallback), ("opus", "sonnet,haiku"))
        r = resolve(DIAGNOSER, "auto", "auto", "claude-sonnet-5", TIERS)
        self.assertEqual((r.model, r.fallback), ("haiku", ""))

    def test_diagnoser_floor_and_unknown(self):
        r = resolve(DIAGNOSER, "auto", "auto", "claude-haiku-4-5", TIERS)
        self.assertEqual(r.model, "haiku")
        self.assertIn("already at the bottom", r.reason)
        r = resolve(DIAGNOSER, "auto", "auto", "fixture", TIERS)
        self.assertEqual(r.model, "opus")
        self.assertIn("one below the top", r.reason)

    def test_explicit_model_passes_through(self):
        r = resolve(REVIEWER, "sonnet", "auto", "claude-fable-5", TIERS)
        self.assertEqual((r.model, r.fallback, r.reason), ("sonnet", "haiku", ""))
        r = resolve(REVIEWER, "sonnet", "opus", "claude-fable-5", TIERS)
        self.assertEqual((r.model, r.fallback), ("sonnet", "opus"))
        r = resolve(DIAGNOSER, "claude-opus-5", "auto", "", TIERS)
        self.assertEqual((r.model, r.fallback), ("claude-opus-5", "sonnet,haiku"))

    def test_explicit_model_off_ladder_has_no_auto_fallback(self):
        r = resolve(REVIEWER, "my-finetune", "auto", "", TIERS)
        self.assertEqual((r.model, r.fallback), ("my-finetune", ""))

    def test_single_tier_ladder(self):
        r = resolve(REVIEWER, "auto", "auto", "anything", ["only"])
        self.assertEqual((r.model, r.fallback), ("only", ""))
        with self.assertRaises(ValueError):
            resolve(REVIEWER, "auto", "auto", "x", [])


if __name__ == "__main__":
    unittest.main()
