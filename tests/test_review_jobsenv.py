"""Acceptance tests for fix/issue-union-dedup, the runner side: `tests/run_parallel.py` takes
its worker count from `-j`, else the environment variable RUN_PARALLEL_JOBS, else the CPU
count, a blank value counting as unset (AC-6); a value that is not a whole number of 1 or
more exits 1 naming the variable and the value before any worker starts (AC-7). Each case
drives the script in a fresh interpreter against a throwaway tree of three passing classes."""

import os
import subprocess
import sys
import tempfile
import unittest

from tests.helpers import ROOT, rmtree_force

RUNNER = os.path.join(ROOT, "tests", "run_parallel.py")
VAR = "RUN_PARALLEL_JOBS"

THREE_CLASSES = """
import unittest


class One(unittest.TestCase):
    def test_a(self):
        pass


class Two(unittest.TestCase):
    def test_b(self):
        pass


class Three(unittest.TestCase):
    def test_c(self):
        pass
"""

CPUS = os.cpu_count() or 1
# a worker count the CPU-count default cannot produce here, so a runner that ignores the
# variable is told apart from one that honours it whatever the sandbox's CPU count is
NOT_THE_DEFAULT = 2 if CPUS == 1 else 1


class JobsEnv(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="jobsenv ")
        self.addCleanup(rmtree_force, self.root)
        tests = os.path.join(self.root, "tests")
        os.makedirs(tests)
        for name, text in (("__init__.py", ""), ("test_three.py", THREE_CLASSES)):
            with open(os.path.join(tests, name), "w", encoding="utf-8", newline="\n") as fh:
                fh.write(text)
        self.tests = tests

    def run_runner(self, *extra, jobs_env=None):
        env = {k: v for k, v in os.environ.items() if k != VAR}
        if jobs_env is not None:
            env[VAR] = jobs_env
        res = subprocess.run(
            [sys.executable, RUNNER, "-s", self.tests, "-t", self.root] + list(extra),
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            cwd=self.root,
            env=env,
        )
        return res.returncode, res.stdout + res.stderr

    def workers_line(self, n):
        return "3 tests in %d worker(s)" % n

    def test_the_variable_sets_the_worker_count(self):
        code, out = self.run_runner(jobs_env=str(NOT_THE_DEFAULT))
        self.assertEqual(code, 0, out)
        self.assertIn(self.workers_line(NOT_THE_DEFAULT), out)  # AC-6

    def test_j_beats_the_variable(self):
        other = 3 if NOT_THE_DEFAULT != 3 else 2
        code, out = self.run_runner("-j", str(other), jobs_env=str(NOT_THE_DEFAULT))
        self.assertEqual(code, 0, out)
        self.assertIn(self.workers_line(other), out)  # AC-6: -j first
        self.assertNotIn(self.workers_line(NOT_THE_DEFAULT), out)

    def test_unset_and_blank_fall_back_to_the_cpu_count(self):
        expected = self.workers_line(min(CPUS, 3))  # whole classes per worker: at most 3
        code, out = self.run_runner()
        self.assertEqual(code, 0, out)
        self.assertIn(expected, out)  # AC-6: unset
        for blank in ("", " ", "\t"):
            code, out = self.run_runner(jobs_env=blank)
            self.assertEqual(code, 0, (blank, out))
            self.assertIn(expected, out, blank)  # AC-6: blank counts as unset
        # and the variable is really what the fallback ignores, not the whole feature
        code, out = self.run_runner(jobs_env=str(NOT_THE_DEFAULT))
        self.assertIn(self.workers_line(NOT_THE_DEFAULT), out)

    def test_a_bad_value_exits_1_before_any_worker_starts(self):
        # the last three are what int() alone would have read as 16, 8 and 2 (round 1, F2):
        # an underscore, a sign, and a digit outside ASCII
        arabic_two = chr(0x662)  # a digit int() reads as 2, outside ASCII
        bads = ("0", "-1", "-8", "eight", "2.5", "1e1", "0x8", "8 workers", "1_6", "+8")
        for bad in bads + (arabic_two,):
            code, out = self.run_runner(jobs_env=bad)
            self.assertEqual(code, 1, (bad, out))  # AC-7
            self.assertIn(VAR, out, bad)  # names the variable
            if bad.isascii():  # a non-ASCII value may be backslash-escaped on the child's stderr
                self.assertIn(bad, out, bad)  # and the value
            self.assertNotIn("worker(s)", out, bad)  # AC-7: refused before the workers start
            self.assertNotIn("Ran ", out, bad)
            self.assertNotIn("OK", out, bad)

    def test_a_refused_value_stops_list_too(self):
        # one rule for the variable: `--list` does not get to ignore a typo (AC-7: before any
        # worker starts, and here before collection is printed)
        code, out = self.run_runner("--list", jobs_env="1_6")
        self.assertEqual(code, 1, out)
        self.assertIn(VAR, out)
        self.assertNotIn("test_three", out)


if __name__ == "__main__":
    unittest.main()
