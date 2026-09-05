"""Model selection: explicit names pass through, "auto" is relative to the Developer's model.

Tiers are an engine's ladder, weakest first (defaults.toml [engines.<name>] tiers).
"""

from dataclasses import dataclass
from typing import Iterable, List, Optional

AUTO = "auto"
REVIEWER, DIAGNOSER = "reviewer", "diagnoser"


@dataclass
class Resolved:
    model: str
    fallback: str
    reason: str  # "" when the model was set explicitly


def tier_index(model: str, tiers: List[str]) -> Optional[int]:
    """Index of the tier whose name appears in the model id; None when no tier matches."""
    m = (model or "").strip().lower()
    if not m:
        return None
    for i, tier in enumerate(tiers):
        t = tier.lower()
        if m == t or t in m:
            return i
    return None


def on_foreign_ladder(model: str, ladders: Iterable[List[str]]) -> bool:
    return any(tier_index(model, tiers) is not None for tiers in ladders)


def resolve(
    role: str,
    requested: str,
    fallback_requested: str,
    author_model: str,
    tiers: List[str],
    foreign_ladders: Iterable[List[str]] = (),
) -> Resolved:
    """Pick the model for a role.

    auto, reviewer: one tier above the author (top stays top; unknown or foreign author -> top).
    auto, diagnoser: one tier below the author (bottom stays bottom; unknown -> one below top).
    auto fallback: the tiers below the chosen one, strongest first ("" at the bottom).
    """
    if not tiers:
        raise ValueError("engine has no tiers")
    top = len(tiers) - 1
    reason = ""
    if requested.strip().lower() == AUTO:
        idx = tier_index(author_model, tiers)
        if idx is None:
            if not author_model:
                why = "author model not given"
            elif on_foreign_ladder(author_model, foreign_ladders):
                why = "author %s is on another engine's ladder" % author_model
            else:
                why = "author model %s is not on the ladder" % author_model
            chosen = top if role == REVIEWER else max(0, top - 1)
            reason = "auto: %s, using %s" % (
                why,
                "the top tier" if chosen == top else "one below the top",
            )
        elif role == REVIEWER:
            chosen = min(top, idx + 1)
            reason = (
                "auto: one tier above author %s" % author_model
                if chosen > idx
                else "auto: author %s is already at the top" % author_model
            )
        else:
            chosen = max(0, idx - 1)
            reason = (
                "auto: one tier below author %s" % author_model
                if chosen < idx
                else "auto: author %s is already at the bottom" % author_model
            )
        model = tiers[chosen]
    else:
        model = requested.strip()

    if fallback_requested.strip().lower() == AUTO:
        j = tier_index(model, tiers)
        fallback = ",".join(reversed(tiers[:j])) if j else ""
    else:
        fallback = fallback_requested.strip()
    return Resolved(model=model, fallback=fallback, reason=reason)
