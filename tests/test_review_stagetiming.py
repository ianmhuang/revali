"""Reviewer acceptance tests for the run timing: every run that ends on its own closes
revali.log with exactly one `run: timing` line naming the stages and sandbox sessions that
ran, in order, with their wall times (AC-1); the history row carries `stage_s` and
`sandbox_s` as non-negative seconds and `revali stats` reads as before (AC-2); the log line
that reports a sandbox session's result carries its wall time (AC-3)."""

import json
import os
import re
import unittest

from revali import EXIT_ACTION, EXIT_ERROR, EXIT_HUMAN, EXIT_OK
from revali.state import State, read_history
from tests.helpers import RepoCase, approve_response, claude_entry, run_cli

LOG = ".revali/feature__mul/logs/revali.log"
DURATION = r"(?:\d+h)?(?:\d+m)?\d+s"
TIMING_MARK = "] run: timing "


def finding():
    return {
        "id": "F1",
        "file": "src/calc.py",
        "line": 12,
        "severity": "high",
        "kind": "correctness",
        "text": "mul ignores negative numbers",
        "suggestion": "handle the sign",
    }


def diagnosis():
    return {
        "summary": "mul returns a + b; the product test fails.",
        "cause": "code",
        "failures": [
            {
                "test": "tests/test_review_mul.py::MulTests::test_product",
                "cause": "code",
                "note": "expected 12, got 7",
            }
        ],
        "recommendation": "return a * b",
    }


class TimingCase(RepoCase):
    def log_lines(self):
        return [ln for ln in self.read(LOG).splitlines() if ln.strip()]

    def timing_lines(self):
        return [ln for ln in self.log_lines() if TIMING_MARK in ln]

    def the_timing_line(self):
        """AC-1: exactly one `run: timing` line, and it is the last line of the log."""
        lines = self.log_lines()
        timing = [ln for ln in lines if TIMING_MARK in ln]
        self.assertEqual(len(timing), 1, "\n".join(lines))
        self.assertEqual(lines[-1], timing[0], "\n".join(lines[-5:]))
        return timing[0].split(TIMING_MARK, 1)[1]

    def assert_named_in_order(self, text, names):
        """Each name appears with a wall time, and in the given order."""
        positions = []
        for name in names:
            match = re.search(r"(?<![\w-])%s %s" % (re.escape(name), DURATION), text)
            self.assertIsNotNone(match, "%s with a wall time not in: %s" % (name, text))
            positions.append(match.start())
        self.assertEqual(positions, sorted(positions), "order in: %s" % text)

    def history_row(self):
        rows = read_history(os.path.join(self.home, "history.jsonl"))
        self.assertTrue(rows, "no history row")
        return rows[-1]

    def assert_seconds(self, mapping):
        for key, value in mapping.items():
            self.assertIsInstance(value, (int, float), key)
            self.assertNotIsInstance(value, bool, key)
            self.assertGreaterEqual(value, 0, key)


class TheTimingLine(TimingCase):
    def test_full_round_exit_0_names_every_stage_and_session_in_order(self):
        self.claude(claude_entry())
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        text = self.the_timing_line()  # AC-1: one line, last of the run
        self.assert_named_in_order(text, ["preflight", "pr", "review", "validate"])  # AC-1
        self.assert_named_in_order(text, ["baseline", "smoke-r1-1", "validate-r1"])  # AC-1
        row = self.history_row()
        self.assertEqual(list(row["stage_s"]), ["preflight", "pr", "review", "validate"])  # AC-2
        self.assertEqual(list(row["sandbox_s"]), ["baseline", "smoke-r1-1", "validate-r1"])
        self.assert_seconds(row["stage_s"])  # AC-2: numbers of seconds >= 0
        self.assert_seconds(row["sandbox_s"])

    def test_changes_requested_exit_2_stops_after_review(self):
        cr = approve_response(verdict="CHANGES_REQUESTED", findings=[finding()])
        self.claude(claude_entry(cr))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ACTION, out)
        text = self.the_timing_line()  # AC-1
        self.assert_named_in_order(text, ["preflight", "pr", "review"])
        self.assert_named_in_order(text, ["baseline", "smoke-r1-1"])
        self.assertNotIn("validate", text)  # AC-1: only the stages that ran
        row = self.history_row()
        self.assertEqual(list(row["stage_s"]), ["preflight", "pr", "review"])  # AC-2
        self.assertEqual(list(row["sandbox_s"]), ["baseline", "smoke-r1-1"])
        self.assert_seconds(row["stage_s"])
        self.assert_seconds(row["sandbox_s"])

    def test_a_run_that_stops_in_preflight_exit_1_still_names_preflight(self):
        """The baseline fails: the run ends with exit 1 inside preflight. Preflight ran (for
        as long as the baseline took), so the timing line and the row name it."""
        self.runner_scenario({"default": 0, "results": {"baseline": {"test": 1}}})
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR, out)
        text = self.the_timing_line()  # AC-1: exit 1 ends with the line too
        self.assert_named_in_order(text, ["preflight"])  # AC-1: the stage that ran
        self.assert_named_in_order(text, ["baseline"])  # AC-1: the session that ran
        self.assertNotIn("pr ", text)
        row = self.history_row()
        self.assertEqual(list(row["stage_s"]), ["preflight"])  # AC-2
        self.assertEqual(list(row["sandbox_s"]), ["baseline"])
        self.assert_seconds(row["stage_s"])
        self.assert_seconds(row["sandbox_s"])

    def test_a_stage_wall_time_covers_its_sandbox_session(self):
        """With the real local runner every sandbox session takes measurable time (a git
        worktree and a python process). AC-1 asks for the wall time of each stage, so the
        preflight time includes the baseline it ran, the review time the smoke run, and the
        validate time the validation run; a stage clock started after its session would be
        shorter than the session itself."""
        self.use_real_local_runner()
        self.claude(claude_entry())
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        row = self.history_row()
        stage, sandbox = row["stage_s"], row["sandbox_s"]
        self.assertGreater(sandbox["baseline"], 0, sandbox)  # the session really ran
        self.assertGreaterEqual(stage["preflight"], sandbox["baseline"], row)  # AC-1
        self.assertGreaterEqual(stage["review"], sandbox["smoke-r1-1"], row)  # AC-1
        self.assertGreaterEqual(stage["validate"], sandbox["validate-r1"], row)  # AC-1
        self.the_timing_line()  # AC-1: one line, last of the run

    def test_the_line_ends_the_log_even_when_the_state_file_cannot_be_written(self):
        """state.json is a directory, so no state write can succeed: the run ends on its own
        with exit 1 and reports that the state file could not be updated. AC-1 still wants
        the `run: timing` line as the last line of the run, and AC-2 the history row."""
        os.makedirs(os.path.join(self.rdir(), "state.json"))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertIn("state file could not be updated", out)
        self.the_timing_line()  # AC-1: exactly one, and the last line
        row = self.history_row()
        self.assertEqual(row["exit"], EXIT_ERROR)
        self.assertIn("stage_s", row)  # AC-2
        self.assertIn("sandbox_s", row)
        self.assert_seconds(row["stage_s"])
        self.assert_seconds(row["sandbox_s"])

    def test_needs_human_exit_3_ends_with_the_line(self):
        cr = approve_response(verdict="CHANGES_REQUESTED", findings=[finding()])
        self.claude(
            claude_entry(cr),
            claude_entry(cr, write_tests=False),
            claude_entry(cr, write_tests=False),
        )
        self.write(
            "revali.toml", self.read("revali.toml").replace("max_fixes = 2", "max_fixes = 1")
        )
        self.commit_all("limit")
        self.assertEqual(run_cli(["run", "--foreground"])[0], EXIT_ACTION)
        self.write("src/calc.py", self.read("src/calc.py") + "\n# try 1\n")
        self.commit_all("try 1")
        self.assertEqual(run_cli(["run", "--foreground"])[0], EXIT_ACTION)
        self.write("src/calc.py", self.read("src/calc.py") + "\n# try 2\n")
        self.commit_all("try 2")
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_HUMAN, out)
        lines = self.log_lines()
        timing = self.timing_lines()
        self.assertEqual(len(timing), 3, "\n".join(lines))  # AC-1: one per run, three runs
        self.assertEqual(lines[-1], timing[-1])  # AC-1: the last line of the third run
        text = timing[-1].split(TIMING_MARK, 1)[1]
        self.assert_named_in_order(text, ["preflight"])  # AC-1: the stage the run reached
        row = self.history_row()
        self.assertEqual(row["exit"], EXIT_HUMAN)
        self.assertIn("preflight", row["stage_s"])  # AC-2
        self.assert_seconds(row["stage_s"])

    def test_dry_run_ends_with_the_line(self):
        code, out = run_cli(["run", "--foreground", "--dry-run"])
        self.assertEqual(code, EXIT_OK, out)
        text = self.the_timing_line()  # AC-1: dry runs included
        self.assert_named_in_order(text, ["preflight"])
        self.assertNotIn("pr ", text)
        self.assertNotIn("review", text)

    def test_stop_records_no_timing(self):
        self.claude(claude_entry())
        self.assertEqual(run_cli(["run", "--foreground"])[0], EXIT_OK)
        self.assertEqual(len(self.timing_lines()), 1)
        # the run is now found dead (as after a kill); `stop` closes it
        state = State.load(self.rdir())
        state.stage, state.last_exit = "review", -1
        state.save(self.rdir())
        code, out = run_cli(["stop"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertEqual(State.load(self.rdir()).stage, "stopped")
        self.assertEqual(len(self.timing_lines()), 1)  # AC-1: not a line for the stop
        rows = read_history(os.path.join(self.home, "history.jsonl"))
        self.assertEqual([r["stage"] for r in rows], ["ready_to_merge", "stopped"])
        self.assertIn("stage_s", rows[0])  # AC-2: the run's row has it


class StatsIsUnchanged(TimingCase):
    def test_stats_output_is_the_same_with_and_without_the_fields(self):
        self.claude(claude_entry())
        self.assertEqual(run_cli(["run", "--foreground"])[0], EXIT_OK)
        path = os.path.join(self.home, "history.jsonl")
        rows = read_history(path)
        self.assertIn("stage_s", rows[0])  # AC-2: the fields are there
        self.assertIn("sandbox_s", rows[0])
        code, with_fields = run_cli(["stats"])
        self.assertEqual(code, EXIT_OK, with_fields)
        for word in ("stage_s", "sandbox_s", "timing", "preflight", "baseline"):
            self.assertNotIn(word, with_fields)  # AC-2: nothing new is printed
        stripped = []
        for row in rows:
            row = dict(row)
            row.pop("stage_s", None)
            row.pop("sandbox_s", None)
            stripped.append(row)
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            for row in stripped:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        code, without_fields = run_cli(["stats"])
        self.assertEqual(code, EXIT_OK, without_fields)
        self.assertEqual(with_fields, without_fields)  # AC-2: unchanged


class SessionResultLinesCarryTheirTime(TimingCase):
    def test_baseline_smoke_and_validation_pass_lines(self):
        self.claude(claude_entry())
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        log = self.read(LOG)
        self.assertRegex(log, r"preflight: baseline passed \(%s\)" % DURATION)  # AC-3
        self.assertRegex(log, r"review: smoke run[^\n]*\(%s\)" % DURATION)  # AC-3
        self.assertRegex(log, r"validate: run 1: PASS \(%s\)" % DURATION)  # AC-3

    def test_baseline_failure_line(self):
        self.runner_scenario({"default": 0, "results": {"baseline": {"test": 1}}})
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertRegex(
            self.read(LOG), r"preflight: baseline failed at test \(%s\)" % DURATION
        )  # AC-3

    def test_validation_failure_line(self):
        self.runner_scenario(
            {
                "default": 0,
                "results": {"validate-r1": {"new_test": 1}},
                "outputs": {"validate-r1": {"new_test": "AssertionError: 12 != 7"}},
            }
        )
        self.claude(claude_entry(), claude_entry(diagnosis(), write_tests=False))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ACTION, out)
        log = self.read(LOG)
        self.assertRegex(log, r"validate: run 1: FAIL \(new_test\) \(%s\)" % DURATION)  # AC-3
        text = self.the_timing_line()  # AC-1: exit 2 out of validation
        self.assert_named_in_order(text, ["preflight", "pr", "review", "validate"])
        self.assert_named_in_order(text, ["baseline", "smoke-r1-1", "validate-r1"])


if __name__ == "__main__":
    unittest.main()
