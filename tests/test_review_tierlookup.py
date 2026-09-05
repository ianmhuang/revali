"""AC-1: Engine.model_family is the same lookup as models.tier_index.

White-box on purpose: the AC is about where the answer comes from, not only
what it is (the two lookups gave identical answers before, which is exactly
how a future edit to one of them would drift unnoticed).
"""

import unittest
from unittest import mock

from revali import models
from revali.config import EngineCfg
from revali.engines.base import Engine
from revali.engines.claude import ClaudeEngine
from tests.helpers import ROOT  # noqa: F401  (sys.path setup)

LADDER = ["haiku", "sonnet", "opus", "fable"]


def engine(tiers=LADDER, cls=Engine):
    return cls(EngineCfg(name="claude", tiers=list(tiers), helper_prefix="claude-haiku"))


class ModelFamilyDelegates(unittest.TestCase):
    def test_family_is_taken_from_tier_index(self):
        # tier_index is consulted with the model id and the engine's ladder, and its
        # index picks the family; a lookup that does not go through it fails here
        eng = engine()
        with mock.patch.object(models, "tier_index", wraps=models.tier_index) as lookup:
            self.assertEqual(eng.model_family("claude-opus-5"), "opus")
        self.assertEqual(
            [(c.args[0], list(c.args[1])) for c in lookup.call_args_list],
            [("claude-opus-5", LADDER)],
        )

    def test_tier_index_answer_wins_over_substring_matching(self):
        # if the shared lookup says "index 0", the family is tiers[0], whatever the id looks like
        eng = engine()
        with mock.patch.object(models, "tier_index", return_value=0):
            self.assertEqual(eng.model_family("claude-opus-5"), "haiku")
        with mock.patch.object(models, "tier_index", return_value=None):
            self.assertEqual(eng.model_family("claude-opus-5"), "claude-opus-5")

    def test_known_and_unknown_ids(self):
        eng = engine(cls=ClaudeEngine)
        self.assertEqual(eng.model_family("claude-opus-5"), "opus")
        self.assertEqual(eng.model_family("opus"), "opus")
        self.assertEqual(eng.model_family("CLAUDE-FABLE-5"), "fable")
        # no tier inside the id: lowercased, otherwise unchanged
        self.assertEqual(eng.model_family("Vendor-X-9"), "vendor-x-9")
        self.assertEqual(eng.model_family(""), "")
        self.assertEqual(eng.model_family(None), "")

    def test_same_answer_as_tier_index_for_every_case(self):
        # the property the AC protects: for any id, model_family == tiers[tier_index] when
        # tier_index finds something, on a ladder whose casing differs from the ids
        tiers = ["Mini", "Standard", "Max"]
        eng = engine(tiers=tiers)
        for model in (
            "vendor-max-2",
            "VENDOR-mini",
            "standard",
            "max",
            "vendor-other",
            " Max ",
            "",
        ):
            idx = models.tier_index(model, tiers)
            expected = tiers[idx].lower() if idx is not None else model.lower()
            self.assertEqual(eng.model_family(model), expected, model)


if __name__ == "__main__":
    unittest.main()
