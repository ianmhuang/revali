"""Acceptance tests for fix/issue-union-dedup, the `already_open` side: the union of earlier
issues that together name a validation's failing tests no longer keeps an issue that older
issues make redundant (AC-2, AC-3, AC-4), while a single newest cover and an uncovered test
behave as before (AC-1). Black-box against `already_open`; the greedy join set AC-4 talks
about is recomputed here from the AC wording, not read from the implementation."""

import random
import unittest
from typing import Dict, List, Set

from revali.issues import already_open
from revali.state import State

A, B, C, D = "t::a", "t::b", "t::c", "t::d"


def state_of(issues: Dict[int, List[str]]) -> State:
    return State(
        issues=[{"number": n, "url": "u%d" % n, "tests": ts} for n, ts in sorted(issues.items())]
    )


def numbers(issues: Dict[int, List[str]], tests: List[str]) -> List[int]:
    return [i["number"] for i in already_open(state_of(issues), tests)]


def greedy_joins(issues: Dict[int, List[str]], tests: List[str]) -> Dict[int, Set[str]]:
    """What each issue 'joined for' in AC-4's words: walking newest first, an issue joins with
    the failing tests it names that no newer issue names."""
    left = set(tests)
    joined: Dict[int, Set[str]] = {}
    for number in sorted(issues, reverse=True):
        named = left & set(issues[number])
        if named:
            joined[number] = named
            left -= named
        if not left:
            break
    return joined


class DocumentedExamples(unittest.TestCase):
    def test_ac3_first_example(self):
        # #42 joins for a, #41 for c, #40 for b; #40 also names a, so #42 is redundant
        issues = {40: [A, B], 41: [C], 42: [A]}
        self.assertEqual(numbers(issues, [A, B, C]), [41, 40])  # AC-3 (was [42, 41, 40])

    def test_ac3_second_example(self):
        issues = {40: [A, B], 41: [A], 42: [C]}
        self.assertEqual(numbers(issues, [A, B, C]), [42, 40])  # AC-3

    def test_order_of_the_failing_tests_does_not_matter(self):
        issues = {40: [A, B], 41: [C], 42: [A]}
        for order in ([A, B, C], [C, B, A], [B, A, C], [C, A, B]):
            self.assertEqual(numbers(issues, order), [41, 40], order)  # AC-3

    def test_redundant_issue_in_the_middle_goes_too(self):
        # #41 joins for b only, and #40 (kept for a) names b as well
        issues = {40: [A, B], 41: [B], 42: [C]}
        self.assertEqual(numbers(issues, [A, B, C]), [42, 40])  # AC-2, AC-4

    def test_newer_issues_stay_when_they_alone_cover(self):
        # every joined issue contributes a test nobody older kept names; nothing is thinned
        issues = {40: [A, B, D], 41: [B], 42: [C], 43: [A]}
        self.assertEqual(numbers(issues, [A, B, C]), [43, 42, 41])  # AC-2: #40 never joins
        self.assertEqual(numbers(issues, [A, B, D]), [40])  # AC-1: a single cover wins


class Contract(unittest.TestCase):
    """AC-1, AC-2 and AC-4 as invariants over seeded random issue histories. The AC-3 fixtures
    are in the sample too, so the check is not vacuous on a branch that keeps the old greedy
    result."""

    TESTS = [A, B, C, D, "t::e"]

    def random_states(self, rng):
        yield {40: [A, B], 41: [C], 42: [A]}, [A, B, C]
        yield {40: [A, B], 41: [A], 42: [C]}, [A, B, C]
        for _ in range(400):
            count = rng.randint(1, 6)
            issues = {
                40 + i: rng.sample(self.TESTS, rng.randint(1, len(self.TESTS)))
                for i in range(count)
            }
            failing = rng.sample(self.TESTS, rng.randint(1, len(self.TESTS)))
            yield issues, failing

    def check(self, issues: Dict[int, List[str]], failing: List[str]):
        got = numbers(issues, failing)
        wanted = set(failing)
        label = "%r %r -> %r" % (issues, failing, got)
        singles = [n for n in issues if wanted <= set(issues[n])]
        if singles:
            self.assertEqual(got, [max(singles)], label)  # AC-1: the newest single cover
            return
        named_anywhere = set().union(*(set(ts) for ts in issues.values()))
        if not wanted <= named_anywhere:
            self.assertEqual(got, [], label)  # AC-1: some failing test nobody names
            return
        # AC-2: a list, newest first, together naming every failing test
        self.assertTrue(got, label)
        self.assertEqual(got, sorted(got, reverse=True), label)
        self.assertEqual(len(got), len(set(got)), label)
        self.assertTrue(wanted <= set().union(*(set(issues[n]) for n in got)), label)
        # AC-2: every issue names a failing test no other issue in the list names
        for n in got:
            others = set().union(*(set(issues[m]) for m in got if m != n), set())
            unique = (set(issues[n]) & wanted) - others
            self.assertTrue(unique, "%s: #%d is redundant" % (label, n))
        # AC-4: the oldest issue joined is always kept ...
        joined = greedy_joins(issues, failing)
        self.assertIn(min(joined), got, label)
        self.assertTrue(set(got) <= set(joined), label)  # only joined issues are listed
        # ... and a newer one is dropped only when the older issues kept name what it joined for
        for n, contribution in joined.items():
            if n in got:
                continue
            older_kept = set().union(*(set(issues[m]) for m in got if m < n), set())
            self.assertTrue(contribution <= older_kept, "%s: #%d dropped early" % (label, n))

    def test_invariants_over_seeded_random_histories(self):
        rng = random.Random(20260907)
        for issues, failing in self.random_states(rng):
            self.check(issues, failing)


if __name__ == "__main__":
    unittest.main()
