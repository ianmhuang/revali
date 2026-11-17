"""A diagnosis session that ends with an engine error still has its cost counted
(fix/retry-visible-diagnosis-cost AC-5, AC-6): the platform's and the validation's
`cost_usd`, `state.cost_usd` and the history row."""

import json
import os
import unittest

from revali import EXIT_ACTION
from revali.state import State
from tests.helpers import RepoCase, claude_entry, run_cli
from tests.test_review_platformdiag import windows_table

REVIEWER_COST = 0.5  # claude_entry's default


class Case(RepoCase):
    def failing_run(self, results, *diagnoser_entries):
        self.runner_scenario({"default": 0, "results": results})
        self.claude(claude_entry(), *diagnoser_entries)
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ACTION, out)
        self.assertNotIn("Traceback", out)
        return out

    def last_history_row(self):
        with open(os.path.join(self.home, "history.jsonl"), "r", encoding="utf-8") as fh:
            rows = [json.loads(ln) for ln in fh if ln.strip()]
        return [r for r in rows if r.get("command", "run") == "run"][-1]


class OnePlatform(Case):
    def test_engine_error_with_a_cost_is_counted(self):  # AC-5
        out = self.failing_run(
            {"validate-r1": {"new_test": 1}},
            claude_entry(is_error=True, exit=1, cost=0.3, write_tests=False),
        )
        self.assertIn("diagnosis unavailable: ", out)
        state = State.load(self.rdir())
        v = state.validations[-1]
        self.assertAlmostEqual(v["cost_usd"], 0.3)
        self.assertAlmostEqual(v["platforms"][0]["cost_usd"], 0.3)
        self.assertAlmostEqual(state.cost_usd, REVIEWER_COST + 0.3)
        self.assertAlmostEqual(self.last_history_row()["cost_usd"], REVIEWER_COST + 0.3)

    def test_engine_error_without_a_cost_adds_nothing(self):  # AC-6
        out = self.failing_run(
            {"validate-r1": {"new_test": 1}},
            claude_entry(raw_stdout="not json", exit=1, write_tests=False),
        )
        self.assertIn("diagnosis unavailable: ", out)
        self.assertIn("invalid JSON", out)
        state = State.load(self.rdir())
        self.assertEqual(state.validations[-1]["cost_usd"], 0.0)
        self.assertAlmostEqual(state.cost_usd, REVIEWER_COST)


class TwoPlatforms(Case):
    def test_each_failed_diagnosis_counts_once(self):  # AC-6
        before = self.read("revali.toml")
        after = before.replace('platforms = ["linux"]', 'platforms = ["linux", "windows"]')
        self.assertNotEqual(before, after)
        self.write("revali.toml", after + windows_table())
        self.commit_all("two platforms")
        self.failing_run(
            {"validate-r1-linux": {"new_test": 1}, "validate-r1-windows": {"new_test": 1}},
            claude_entry(is_error=True, exit=1, cost=0.3, write_tests=False),
            claude_entry(is_error=True, exit=1, cost=0.2, write_tests=False),
        )
        state = State.load(self.rdir())
        v = state.validations[-1]
        self.assertEqual([p["name"] for p in v["platforms"]], ["linux", "windows"])
        self.assertAlmostEqual(v["platforms"][0]["cost_usd"], 0.3)
        self.assertAlmostEqual(v["platforms"][1]["cost_usd"], 0.2)
        self.assertAlmostEqual(v["cost_usd"], 0.5)
        self.assertAlmostEqual(state.cost_usd, REVIEWER_COST + 0.5)


if __name__ == "__main__":
    unittest.main()
