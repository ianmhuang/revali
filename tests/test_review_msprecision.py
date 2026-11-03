"""Reviewer acceptance tests for fix/timing-precision: `Timing.as_dict` keeps stage and
sandbox seconds to the millisecond, as floats (AC-1, AC-3), while the summary line and
`fmt_duration` still print whole seconds (AC-2)."""

import unittest

from revali.timing import Timing, fmt_duration


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class MillisecondPrecision(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.t = Timing(clock=self.clock)

    def test_a_sandbox_session_of_a_few_hundredths_is_stored_positive(self):
        # AC-1: 0.012 s is stored as 0.012, not 0.0 (one decimal would lose it)
        self.t.sandbox("baseline", 0.012)
        d = self.t.as_dict()
        self.assertEqual(d["sandbox_s"], {"baseline": 0.012})
        self.assertGreater(d["sandbox_s"]["baseline"], 0)

    def test_the_fourth_decimal_is_rounded_away(self):
        # AC-1: three decimals, no more
        self.t.sandbox("baseline", 0.0123)
        self.t.sandbox("smoke-r1-1", 0.0456)
        self.assertEqual(self.t.as_dict()["sandbox_s"], {"baseline": 0.012, "smoke-r1-1": 0.046})

    def test_a_stage_of_a_few_hundredths_is_stored_positive(self):
        # AC-1: the stage clock gets the same precision as the sandbox sessions
        self.t.stage("preflight")
        self.clock.now += 0.034
        self.t.stage("review")
        self.clock.now += 0.0016
        self.t.close()
        d = self.t.as_dict()
        self.assertEqual(d["stage_s"], {"preflight": 0.034, "review": 0.002})
        self.assertGreater(d["stage_s"]["preflight"], 0)

    def test_values_stay_floats(self):
        # AC-1: test_pipeline checks isinstance(value, float); whole seconds stay floats too
        self.t.stage("preflight")
        self.clock.now += 5
        self.t.stage("validate")
        self.clock.now += 0.25
        self.t.close()
        self.t.sandbox("baseline", 3)
        self.t.sandbox("validate-r1", 0.5)
        d = self.t.as_dict()
        for mapping in (d["stage_s"], d["sandbox_s"]):
            for key, value in mapping.items():
                self.assertIsInstance(value, float, key)
        self.assertEqual(d["stage_s"], {"preflight": 5.0, "validate": 0.25})
        self.assertEqual(d["sandbox_s"], {"baseline": 3.0, "validate-r1": 0.5})

    def test_hundredths_of_a_long_session_are_kept(self):
        # AC-3: 262.06 is stored as 262.06 (was 262.1 with one decimal)
        self.t.sandbox("baseline", 499.4)
        self.t.sandbox("smoke-r1-1", 262.06)
        self.assertEqual(self.t.as_dict()["sandbox_s"], {"baseline": 499.4, "smoke-r1-1": 262.06})

    def test_a_label_visited_twice_accumulates_at_the_new_precision(self):
        # AC-1: accumulation happens before rounding, so two 0.0006 s sessions give 0.001
        self.t.sandbox("baseline", 0.0006)
        self.t.sandbox("baseline", 0.0006)
        self.assertEqual(self.t.as_dict()["sandbox_s"], {"baseline": 0.001})

    def test_summary_and_fmt_duration_still_print_whole_seconds(self):
        # AC-2: the `run: timing` line and the per-session log lines are unchanged
        self.t.stage("preflight")
        self.clock.now += 0.012
        self.t.sandbox("baseline", 0.012)
        self.t.stage("review")
        self.clock.now += 262.06
        self.t.sandbox("smoke-r1-1", 262.06)
        self.t.close()
        self.assertEqual(
            self.t.summary(),
            "preflight 0s, review 4m22s; sandbox baseline 0s, smoke-r1-1 4m22s",
        )
        self.assertEqual(fmt_duration(0.012), "0s")
        self.assertEqual(fmt_duration(0.499), "0s")
        self.assertEqual(fmt_duration(262.06), "4m22s")
        self.assertEqual(fmt_duration(3723.999), "1h2m4s")


if __name__ == "__main__":
    unittest.main()
