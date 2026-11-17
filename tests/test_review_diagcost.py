"""Acceptance tests for fix/retry-visible-diagnosis-cost, AC-5 and AC-6: what a diagnosis
session spent before it ended with an engine error is counted.

Black box through `revali run --foreground` with the fake gh, claude and runner: the
validation fails, the diagnosis session answers with an error result, and the numbers are
read from state.json (the validation record, its `platforms` entries, the run's total) and
from the history row. The reviewer session costs 0.5 in every test, so the total is 0.5 plus
whatever the failed diagnosis sessions spent.
"""

import json
import os
import unittest

from revali import EXIT_ACTION
from revali.state import State, read_history
from tests.fixtures.make_sample_repo import LOCAL_NEW_TEST, LOCAL_TEST, toml_str
from tests.helpers import RepoCase, claude_entry, run_cli

REVIEWER_COST = 0.5


def out_of_budget(cost):
    """The result object of a session that ran out of budget after spending `cost`."""
    return json.dumps(
        {
            "type": "result",
            "subtype": "error_max_budget_usd",
            "is_error": True,
            "num_turns": 7,
            "total_cost_usd": cost,
            "session_id": "fake-session",
        }
    )


class Case(RepoCase):
    def failing_run(self, results, *diagnoser_entries):
        self.runner_scenario({"default": 0, "results": results})
        self.claude(claude_entry(), *diagnoser_entries)
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ACTION, out)
        self.assertNotIn("Traceback", out)
        return out

    def validation(self):
        state = State.load(self.rdir())
        self.assertEqual(len(state.validations), 1, state.validations)
        return state, state.validations[0]

    def history_row(self):
        rows = read_history(os.path.join(self.home, "history.jsonl"))
        self.assertTrue(rows, "no history row")
        return rows[-1]


class OnePlatform(Case):
    def test_budget_ran_out_and_the_spent_amount_is_counted(self):  # AC-5
        out = self.failing_run(
            {"validate-r1": {"new_test": 1}},
            claude_entry(raw_stdout=out_of_budget(0.3), exit=1, write_tests=False),
        )
        # the message is the one from before the change
        self.assertIn("diagnosis unavailable: ", out)
        self.assertIn("ran out of budget", out)
        state, v = self.validation()
        self.assertAlmostEqual(v["cost_usd"], 0.3)
        self.assertEqual(len(v["platforms"]), 1)
        self.assertAlmostEqual(v["platforms"][0]["cost_usd"], 0.3)
        self.assertAlmostEqual(state.cost_usd, REVIEWER_COST + 0.3)
        self.assertAlmostEqual(self.history_row()["cost_usd"], REVIEWER_COST + 0.3)

    def test_failed_session_with_a_cost_is_counted(self):  # AC-5
        out = self.failing_run(
            {"validate-r1": {"new_test": 1}},
            claude_entry(is_error=True, exit=1, cost=0.25, write_tests=False),
        )
        self.assertIn("diagnosis unavailable: ", out)
        state, v = self.validation()
        self.assertAlmostEqual(v["cost_usd"], 0.25)
        self.assertAlmostEqual(v["platforms"][0]["cost_usd"], 0.25)
        self.assertAlmostEqual(state.cost_usd, REVIEWER_COST + 0.25)
        self.assertAlmostEqual(self.history_row()["cost_usd"], REVIEWER_COST + 0.25)

    def test_error_without_a_cost_adds_nothing(self):  # AC-6
        out = self.failing_run(
            {"validate-r1": {"new_test": 1}},
            claude_entry(raw_stdout="not json", exit=1, write_tests=False),
        )
        self.assertIn("diagnosis unavailable: ", out)
        state, v = self.validation()
        self.assertEqual(v["cost_usd"], 0.0)
        self.assertEqual(v["platforms"][0]["cost_usd"], 0.0)
        self.assertAlmostEqual(state.cost_usd, REVIEWER_COST)
        self.assertAlmostEqual(self.history_row()["cost_usd"], REVIEWER_COST)


class TwoPlatforms(Case):
    def two_platforms(self):
        before = self.read("revali.toml")
        after = before.replace('platforms = ["linux"]', 'platforms = ["linux", "windows"]')
        self.assertNotEqual(before, after, "fixture platforms line not found")
        table = '\n[validate.windows]\nrunner = "ssh"\nhost = "winbox"\n' + (
            "test = %s\nnew_test = %s\n" % (toml_str(LOCAL_TEST), toml_str(LOCAL_NEW_TEST))
        )
        self.write("revali.toml", after + table)
        self.commit_all("list linux, windows")

    def test_each_failed_diagnosis_counts_once(self):  # AC-6
        self.two_platforms()
        self.failing_run(
            {"validate-r1-linux": {"new_test": 1}, "validate-r1-windows": {"new_test": 1}},
            claude_entry(is_error=True, exit=1, cost=0.3, write_tests=False),
            claude_entry(raw_stdout=out_of_budget(0.2), exit=1, write_tests=False),
        )
        state, v = self.validation()
        self.assertEqual([p["name"] for p in v["platforms"]], ["linux", "windows"])
        self.assertAlmostEqual(v["platforms"][0]["cost_usd"], 0.3)
        self.assertAlmostEqual(v["platforms"][1]["cost_usd"], 0.2)
        self.assertAlmostEqual(v["cost_usd"], 0.5)
        self.assertAlmostEqual(state.cost_usd, REVIEWER_COST + 0.5)
        self.assertAlmostEqual(self.history_row()["cost_usd"], REVIEWER_COST + 0.5)

    def test_one_platform_failing_with_a_cost_the_other_passing(self):  # AC-6
        self.two_platforms()
        self.failing_run(
            {"validate-r1-windows": {"new_test": 1}},
            claude_entry(is_error=True, exit=1, cost=0.3, write_tests=False),
        )
        state, v = self.validation()
        by_name = {p["name"]: p["cost_usd"] for p in v["platforms"]}
        self.assertEqual(by_name["linux"], 0.0)
        self.assertAlmostEqual(by_name["windows"], 0.3)
        self.assertAlmostEqual(v["cost_usd"], 0.3)
        self.assertAlmostEqual(state.cost_usd, REVIEWER_COST + 0.3)


if __name__ == "__main__":
    unittest.main()
