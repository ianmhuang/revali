"""Acceptance tests for fix/reviewer-format-cost-encoding, the cost and console halves,
black-box through the CLI: every reviewer attempt's cost is in `revali status` and the
history row however the round ends (lint red twice, an engine error carrying
total_cost_usd, output off the schema, a smoke failure twice, an existing test file
restored twice) and a round that completes counts each attempt once (AC-4); `wait` and
`status` print a stage message with an emoji on a cp950 console instead of dying with
UnicodeEncodeError (AC-5)."""

import json
import os
import subprocess
import sys
import unittest

from revali import EXIT_ERROR, EXIT_OK
from tests.fixtures.make_sample_repo import PY
from tests.helpers import TEST_REVIEW_MUL, RepoCase, approve_response, claude_entry, run_cli

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FILE = "tests/test_review_mul.py"
MARKER = "# needs-format"

# black --check's shape: one "would reformat" line per file, then its emoji summary line.
LINT_STUB = """import glob
import sys

bad = []
for path in sorted(glob.glob("tests/test_review_*.py")):
    with open(path, encoding="utf-8") as fh:
        if "# needs-format" in fh.read():
            bad.append(path.replace("\\\\", "/"))
for path in bad:
    print("would reformat " + path)
if bad:
    print("Oh no! \\U0001f4a5 \\U0001f494 \\U0001f4a5")
sys.exit(1 if bad else 0)
"""
UNFORMATTED = TEST_REVIEW_MUL.replace("import unittest", "import unittest  " + MARKER)


def writes(text, cost, data=None, **kw):
    entry = claude_entry(data, write_tests=False, cost=cost, **kw)
    entry["write_files"] = {FILE: text}
    return entry


class CostRepo(RepoCase):
    def setUp(self):
        super().setUp()
        self.write("lint_stub.py", LINT_STUB)
        self.write(
            "revali.toml",
            self.read("revali.toml").replace('lint = ""', 'lint = "%s lint_stub.py"' % PY),
        )
        self.commit_all("lint line")

    def status_cost(self):
        code, out = run_cli(["status"])
        self.assertEqual(code, EXIT_OK, out)
        line = [x for x in out.splitlines() if "cost: $" in x]
        self.assertEqual(len(line), 1, out)
        return float(line[0].split("cost: $", 1)[1].split()[0])

    def history_cost(self):
        with open(os.path.join(self.home, "history.jsonl"), encoding="utf-8") as fh:
            rows = [json.loads(line) for line in fh if line.strip()]
        self.assertTrue(rows)
        return float(rows[-1]["cost_usd"])


class RoundStops(CostRepo):
    def test_lint_red_twice_keeps_both_attempts(self):
        self.claude(writes(UNFORMATTED, 5.16), writes(UNFORMATTED, 2.05))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertEqual(len(self.fake_calls("claude")), 2)
        self.assertAlmostEqual(self.status_cost(), 7.21, places=2)  # AC-4
        self.assertAlmostEqual(self.history_cost(), 7.21, places=2)  # AC-4

    def test_an_engine_error_with_a_reported_cost_counts_it(self):
        self.claude(writes(TEST_REVIEW_MUL, 0.75, is_error=True, exit=1))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertIn("session failed", out)
        self.assertAlmostEqual(self.status_cost(), 0.75, places=2)  # AC-4
        self.assertAlmostEqual(self.history_cost(), 0.75, places=2)

    def test_output_off_the_schema_counts_the_cost(self):
        self.claude(writes(TEST_REVIEW_MUL, 0.4, data={"verdict": "MAYBE"}))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertIn("schema", out)
        self.assertAlmostEqual(self.status_cost(), 0.4, places=2)  # AC-4
        self.assertAlmostEqual(self.history_cost(), 0.4, places=2)

    def test_a_smoke_failure_twice_keeps_both_attempts(self):
        broken = {"new_test": 2}
        self.runner_scenario(
            {
                "default": 0,
                "results": {"smoke-r1-1": broken, "smoke-r1-2": broken},
                "outputs": {
                    "smoke-r1-1": {"new_test": "ImportError: No module named nope"},
                    "smoke-r1-2": {"new_test": "ImportError: No module named nope"},
                },
            }
        )
        self.claude(writes(TEST_REVIEW_MUL, 1.0), writes(TEST_REVIEW_MUL, 0.5))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertIn("cannot run", out)
        self.assertEqual(len(self.fake_calls("claude")), 2)
        self.assertAlmostEqual(self.status_cost(), 1.5, places=2)  # AC-4
        self.assertAlmostEqual(self.history_cost(), 1.5, places=2)

    def test_a_restored_file_twice_keeps_both_attempts(self):
        touched = {FILE: TEST_REVIEW_MUL, "tests/test_calc.py": "# rewritten by the reviewer\n"}
        first = claude_entry(cost=0.6)
        first["write_files"] = touched
        second = claude_entry(cost=0.3)
        second["write_files"] = touched
        self.claude(first, second)
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertIn("restored", out)
        self.assertEqual(len(self.fake_calls("claude")), 2)
        self.assertAlmostEqual(self.status_cost(), 0.9, places=2)  # AC-4
        self.assertAlmostEqual(self.history_cost(), 0.9, places=2)


class RoundCompletes(CostRepo):
    def test_each_attempt_is_counted_once(self):
        self.claude(writes(UNFORMATTED, 1.5), writes(TEST_REVIEW_MUL, 0.25))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("READY TO MERGE", out)
        self.assertEqual(len(self.fake_calls("claude")), 2)
        self.assertAlmostEqual(self.status_cost(), 1.75, places=2)  # AC-4: not 3.5
        self.assertAlmostEqual(self.history_cost(), 1.75, places=2)
        rec = json.loads(self.read(".revali/feature__mul/review-1.json"))
        self.assertAlmostEqual(float(rec["meta"]["cost_usd"]), 1.75, places=2)

    def test_a_single_clean_attempt_is_counted_once(self):
        self.claude(writes(TEST_REVIEW_MUL, 0.5))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertAlmostEqual(self.status_cost(), 0.5, places=2)  # AC-4
        self.assertAlmostEqual(self.history_cost(), 0.5, places=2)

    def test_a_partial_ac_bounce_counts_both_attempts_once(self):
        partial = approve_response(
            tests=[{"path": FILE, "purpose": "product", "covers": ["AC-1"], "expected": "12"}]
        )
        self.claude(writes(TEST_REVIEW_MUL, 0.7, partial), writes(TEST_REVIEW_MUL, 0.2))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertAlmostEqual(self.status_cost(), 0.9, places=2)  # AC-4
        self.assertAlmostEqual(self.history_cost(), 0.9, places=2)


class Cp950Console(CostRepo):
    """`wait` and `status` in a child process whose stdout/stderr encode as cp950, the
    Traditional Chinese Windows console; the codec is built into CPython on every platform."""

    def revali(self, *argv):
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "cp950"
        env.pop("PYTHONUTF8", None)
        return subprocess.run(
            [sys.executable, os.path.join(ROOT, "revali.py")] + list(argv),
            cwd=self.repo,
            env=env,
            capture_output=True,
            timeout=120,
        )

    def test_wait_and_status_print_the_emoji_message(self):
        self.claude(writes(UNFORMATTED, 0.5), writes(UNFORMATTED, 0.5))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR, out)  # lint red twice; black's emoji is in the message
        for argv, expected in ((["wait"], EXIT_ERROR), (["status"], EXIT_OK)):
            res = self.revali(*argv)
            stderr = res.stderr.decode("utf-8", "replace")
            self.assertNotIn("UnicodeEncodeError", stderr)  # AC-5
            self.assertNotIn("Traceback", stderr)
            self.assertEqual(res.returncode, expected, stderr)
            self.assertIn(b"would reformat", res.stdout)  # AC-5: the message came through
            self.assertIn(b"Oh no!", res.stdout)

    def test_the_stream_encoding_is_kept(self):
        # AC-5: only the error handler changes; ASCII output is byte-identical
        self.claude(writes(TEST_REVIEW_MUL, 0.5))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        res = self.revali("status")
        self.assertEqual(res.returncode, EXIT_OK, res.stderr)
        self.assertIn(b"cost: $0.50", res.stdout)
        self.assertIn(b"stage: ", res.stdout)


if __name__ == "__main__":
    unittest.main()
