"""Acceptance tests for tests/run_parallel.py (feature/parallel-suite), derived from the
acceptance criteria and exercised through the command line only: a throwaway test tree is
built in a temp dir and the runner starts as a subprocess, the way revali.toml and the README
start it. Plain `python -m unittest` in a fresh interpreter is the reference for collection,
test counts and verdict wording."""

import os
import re
import subprocess
import sys
import tempfile
import tomllib
import unittest

from tests.helpers import ROOT, rmtree_force

RUNNER = os.path.join(ROOT, "tests", "run_parallel.py")

# Every test of these modules appends its worker's pid to a file named after its id, so a
# run can be checked for "each class in one process", "no test twice" and "N processes".
RECORDING = """
import os
import unittest

RECORD = %r


class _Recording(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(RECORD, self.id()), "a", encoding="utf-8") as fh:
            fh.write("%%d\\n" %% os.getpid())
"""

SKIPPING = """
import unittest


class SkipCase(unittest.TestCase):
    def test_runs(self):
        pass

    @unittest.skip("not on this platform")
    def test_skipped(self):
        pass
"""

EXPECTED_FAILURE = """
import unittest


class ExpectedCase(unittest.TestCase):
    @unittest.expectedFailure
    def test_known_bug(self):
        self.assertEqual(1, 2)
"""

UNEXPECTED_SUCCESS = """
import unittest


class SurpriseCase(unittest.TestCase):
    @unittest.expectedFailure
    def test_fixed_without_notice(self):
        self.assertEqual(2, 2)
"""

FAILING = """
import unittest


class FailCase(unittest.TestCase):
    def test_fails(self):
        self.assertEqual(1, 2, "one is not two")
"""

ERRORING = """
import unittest


class ErrorCase(unittest.TestCase):
    def test_errors(self):
        raise RuntimeError("boom in the worker")
"""

NON_ASCII_FAILURE = """
import unittest


class UnicodeCase(unittest.TestCase):
    def test_message_with_non_ascii(self):
        self.fail("\\u4e2d\\u6587 message")
"""

NOISY_PASSING = """
import sys
import unittest


class NoisyCase(unittest.TestCase):
    def test_talks_and_passes(self):
        print("NOISE_ON_STDOUT_FROM_A_PASSING_TEST")
        sys.stderr.write("NOISE_ON_STDERR_FROM_A_PASSING_TEST\\n")

    def test_quiet(self):
        pass
"""

# A passing test that prints the word OK on a line of its own, next to a real failure in the
# same class: the run's answer must come from unittest's result, not from the test's output.
# (A worker's stdout is block-buffered when redirected, so the print lands after unittest's
# summary in the worker's log.)
FAKE_SUMMARY = """
import unittest


class FakeSummaryCase(unittest.TestCase):
    def test_prints_ok(self):
        print("OK")

    def test_really_fails(self):
        self.assertTrue(False, "the real failure")
"""

BROKEN = "import module_that_does_not_exist_anywhere_at_all\n"

CRASHING = """
import os
import unittest


class CrashCase(unittest.TestCase):
    def test_kills_the_worker(self):
        os._exit(7)
"""


def recording_module(record, classes, per_class):
    parts = [RECORDING % record]
    for c in range(classes):
        parts.append("\n\nclass C%d(_Recording):\n" % c)
        for t in range(per_class):
            parts.append("    def test_%d(self):\n        pass\n\n" % t)
    return "".join(parts)


def decode(data):
    return data.decode("utf-8", errors="replace")


def tail(out):
    """(the `Ran ...` line, the verdict line): the last two non-blank lines of a run."""
    lines = [line.rstrip() for line in out.strip().splitlines() if line.strip()]
    return lines[-2], lines[-1]


def ran_count(line):
    m = re.match(r"^Ran (\d+) tests? in \d+\.\d+s$", line)
    if not m:
        raise AssertionError("not a unittest `Ran` line: %r" % line)
    return int(m.group(1))


class Tree:
    """<root>/tests/<modules>.py with an __init__.py, plus a pid record directory."""

    def __init__(self, modules):
        self.root = tempfile.mkdtemp(prefix="revali parsuite ")
        self.tests = os.path.join(self.root, "tests")
        self.record = os.path.join(self.root, "pids")
        os.makedirs(self.tests)
        os.makedirs(self.record)
        self.write("__init__.py", "")
        for name, text in modules.items():
            if text is not None:
                self.write(name + ".py", text)

    def write(self, name, text):
        with open(os.path.join(self.tests, name), "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)

    def parallel(self, *extra):
        """The runner over this tree; (exit code, combined output)."""
        res = subprocess.run(
            [sys.executable, RUNNER, "-s", self.tests, "-t", self.root] + list(extra),
            capture_output=True,
            cwd=self.root,
        )
        return res.returncode, decode(res.stdout + res.stderr)

    def serial(self, *names):
        """The reference: `python -m unittest discover` (or `<names>`) in a fresh interpreter."""
        args = [sys.executable, "-m", "unittest"]
        if names:
            args += list(names)
        else:
            args += ["discover", "-s", self.tests, "-t", self.root]
        res = subprocess.run(args, capture_output=True, cwd=self.root)
        return res.returncode, decode(res.stdout + res.stderr)

    def discover_ids(self):
        code = (
            "import unittest\n"
            "def walk(s):\n"
            "    for i in s:\n"
            "        if isinstance(i, unittest.TestSuite): yield from walk(i)\n"
            "        else: yield i.id()\n"
            "suite = unittest.TestLoader().discover(start_dir=%r, top_level_dir=%r)\n"
            "print('\\n'.join(walk(suite)))\n" % (self.tests, self.root)
        )
        res = subprocess.run([sys.executable, "-c", code], capture_output=True, cwd=self.root)
        return [line for line in decode(res.stdout).splitlines() if line.strip()]

    def pids(self):
        """test id -> list of pids that ran it (one entry per run of that test)."""
        out = {}
        for name in os.listdir(self.record):
            with open(os.path.join(self.record, name), "r", encoding="utf-8") as fh:
                out[name] = [int(x) for x in fh.read().split()]
        return out

    def cleanup(self):
        rmtree_force(self.root)


class ParsuiteCase(unittest.TestCase):
    def tree(self, **modules):
        t = Tree(modules)
        self.addCleanup(t.cleanup)
        return t

    def recording_tree(self, classes, per_class, **modules):
        """A tree whose `test_a` module records pids, plus `modules`."""
        t = self.tree(**modules)
        t.write("test_a.py", recording_module(t.record, classes, per_class))
        return t

    def assert_same_answer(self, parallel_out, serial_out):
        """Same N and the same verdict line as plain unittest (AC-2: unittest's wording)."""
        p_ran, p_verdict = tail(parallel_out)
        s_ran, s_verdict = tail(serial_out)
        self.assertEqual(ran_count(p_ran), ran_count(s_ran), (p_ran, s_ran))
        self.assertEqual(p_verdict, s_verdict)


class Collection(ParsuiteCase):
    """AC-1: the same tests as `unittest discover`, once each, whole classes per worker,
    `-j` and the CPU-count default."""

    def test_list_is_exactly_unittest_discover(self):
        t = self.recording_tree(2, 3, test_b=SKIPPING, test_c=EXPECTED_FAILURE)
        code, out = t.parallel("--list")
        self.assertEqual(code, 0, out)
        listed = [line for line in out.splitlines() if line.strip()]
        self.assertEqual(sorted(listed), sorted(t.discover_ids()))
        self.assertEqual(len(listed), len(set(listed)), "a test id listed twice")
        self.assertEqual(len(listed), 9)

    def test_every_test_runs_once_and_n_is_the_collected_count(self):
        t = self.recording_tree(3, 4, test_b=SKIPPING)
        code, out = t.parallel("-j", "2")
        self.assertEqual(code, 0, out)
        pids = t.pids()
        self.assertEqual(len(pids), 12, sorted(pids))
        self.assertTrue(all(len(v) == 1 for v in pids.values()), pids)
        ran, verdict = tail(out)
        self.assertEqual(ran_count(ran), 14)  # 12 recorded + 1 run + 1 skipped
        self.assertEqual(verdict, "OK (skipped=1)")

    def test_classes_stay_whole_and_j_is_the_worker_count(self):
        t = self.recording_tree(3, 3)
        code, out = t.parallel("-j", "2")
        self.assertEqual(code, 0, out)
        by_class = {}
        for tid, pid in t.pids().items():
            by_class.setdefault(tid.rsplit(".", 1)[0], set()).update(pid)
        self.assertEqual(len(by_class), 3)
        for cls, pids in by_class.items():
            self.assertEqual(len(pids), 1, "%s ran in %d processes" % (cls, len(pids)))
        self.assertEqual(len(set().union(*by_class.values())), 2, by_class)

    def test_default_worker_count_is_the_cpu_count(self):
        cpus = os.cpu_count()
        if not cpus:
            self.skipTest("os.cpu_count() is unknown here")
        t = self.recording_tree(cpus + 3, 1)
        code, out = t.parallel()
        self.assertEqual(code, 0, out)
        seen = set()
        for pid in t.pids().values():
            seen.update(pid)
        self.assertEqual(len(seen), cpus, "%d worker processes for %d CPUs" % (len(seen), cpus))


class Summary(ParsuiteCase):
    """AC-2: `Ran <N> tests in <X>s` then the verdict in unittest's wording, N = collected."""

    def test_all_pass_reads_like_unittest(self):
        t = self.recording_tree(2, 2)
        code, out = t.parallel("-j", "2")
        self.assertEqual(code, 0, out)
        ran, verdict = tail(out)
        self.assertRegex(ran, r"^Ran 4 tests in \d+\.\d+s$")
        self.assertEqual(verdict, "OK")
        self.assert_same_answer(out, t.serial()[1])

    def test_skips_and_expected_failures_use_unittest_wording(self):
        t = self.tree(test_a=SKIPPING, test_b=EXPECTED_FAILURE)
        code, out = t.parallel("-j", "2")
        self.assertEqual(code, 0, out)
        self.assertEqual(tail(out)[1], "OK (skipped=1, expected failures=1)")
        self.assert_same_answer(out, t.serial()[1])

    def test_failures_and_errors_from_different_workers_are_summed(self):
        t = self.tree(test_a=FAILING, test_b=ERRORING, test_c=SKIPPING)
        code, out = t.parallel("-j", "3")
        self.assertEqual(code, 1, out)
        ran, verdict = tail(out)
        self.assertEqual(ran_count(ran), 4)
        self.assertEqual(verdict, "FAILED (failures=1, errors=1, skipped=1)")
        self.assert_same_answer(out, t.serial()[1])

    def test_unexpected_success_fails_the_run_like_unittest(self):
        t = self.tree(test_a=UNEXPECTED_SUCCESS, test_b=SKIPPING)
        code, out = t.parallel("-j", "2")
        s_code, s_out = t.serial()
        self.assertEqual((code, s_code), (1, 1), out)
        self.assertEqual(tail(out)[1], "FAILED (skipped=1, unexpected successes=1)")
        self.assert_same_answer(out, s_out)

    def test_a_tests_own_output_does_not_change_the_answer(self):
        t = self.tree(test_a=FAKE_SUMMARY)
        code, out = t.parallel("-j", "1")
        self.assertEqual(code, 1, out)
        ran, verdict = tail(out)
        self.assertEqual(ran_count(ran), 2)
        self.assertEqual(verdict, "FAILED (failures=1)")
        self.assertIn("the real failure", out)
        self.assert_same_answer(out, t.serial()[1])


class FailureOutput(ParsuiteCase):
    """AC-3: a failure means exit 1, its id and traceback in the output; a worker that passed
    contributes its summary only."""

    def test_failing_test_id_and_traceback_are_in_the_output(self):
        t = self.tree(test_a=NOISY_PASSING, test_b=FAILING, test_c=ERRORING)
        code, out = t.parallel("-j", "3")
        self.assertEqual(code, 1, out)
        self.assertIn("tests.test_b.FailCase.test_fails", out)
        self.assertIn("tests.test_c.ErrorCase.test_errors", out)
        self.assertIn("Traceback (most recent call last)", out)
        self.assertIn("AssertionError: 1 != 2 : one is not two", out)
        self.assertIn("RuntimeError: boom in the worker", out)

    def test_a_passing_worker_contributes_no_dots_or_noise(self):
        t = self.tree(test_a=NOISY_PASSING, test_b=SKIPPING)
        code, out = t.parallel("-j", "2")
        self.assertEqual(code, 0, out)
        self.assertNotIn("NOISE_ON_STDOUT_FROM_A_PASSING_TEST", out)
        self.assertNotIn("NOISE_ON_STDERR_FROM_A_PASSING_TEST", out)
        self.assertIsNone(re.search(r"^[.sxEF]+$", out, re.M), "progress dots leaked")

    def test_non_ascii_in_a_traceback_arrives_intact(self):
        """`as its worker printed it`: a message the worker wrote must not come back mangled or
        crash the parent, on a Linux sandbox and on a Windows host alike."""
        t = self.tree(test_a=NON_ASCII_FAILURE, test_b=SKIPPING)
        code, out = t.parallel("-j", "2")
        self.assertEqual(code, 1, out)
        self.assertIn("\u4e2d\u6587 message", out)
        self.assertNotIn("\ufffd", out)
        self.assertEqual(tail(out)[1], "FAILED (failures=1, skipped=1)")


class BrokenModule(ParsuiteCase):
    """AC-4: a module that does not import is one error naming the module, exit 1."""

    def test_import_error_is_reported_like_unittest(self):
        t = self.tree(test_a=SKIPPING, test_broken=BROKEN)
        code, out = t.parallel("-j", "2")
        s_code, s_out = t.serial()
        self.assertEqual((code, s_code), (1, 1), out)
        self.assertIn("tests.test_broken", out)
        self.assertIn("module_that_does_not_exist_anywhere_at_all", out)
        ran, verdict = tail(out)
        self.assertEqual(ran_count(ran), 3)  # unittest counts the failed import as one test
        self.assertEqual(verdict, "FAILED (errors=1, skipped=1)")
        self.assert_same_answer(out, s_out)


class WorkerCrash(ParsuiteCase):
    """AC-5: a worker that ends without a summary is an error carrying its exit code."""

    def test_crashed_worker_is_an_error_with_its_exit_code(self):
        t = self.tree(test_a=CRASHING, test_b=SKIPPING)
        code, out = t.parallel("-j", "2")
        self.assertEqual(code, 1, out)
        self.assertRegex(out, r"exit(?: code)?[ :=]+7\b")
        ran, verdict = tail(out)
        self.assertTrue(verdict.startswith("FAILED (errors=1"), verdict)
        self.assertGreaterEqual(ran_count(ran), 2)  # the other worker's tests still count

    def test_crash_in_the_only_worker_is_still_exit_1(self):
        t = self.tree(test_a=CRASHING)
        code, out = t.parallel("-j", "1")
        self.assertEqual(code, 1, out)
        self.assertTrue(tail(out)[1].startswith("FAILED (errors=1"), out)


class JobsExtremes(ParsuiteCase):
    """AC-6: `-j 1` is one worker, `-j` above the class count is one worker per class; both
    give the same N and verdict."""

    def test_one_worker(self):
        t = self.recording_tree(3, 2, test_b=FAILING)
        code, out = t.parallel("-j", "1")
        self.assertEqual(code, 1, out)
        seen = set()
        for pid in t.pids().values():
            seen.update(pid)
        self.assertEqual(len(seen), 1, "more than one process for -j 1")
        self.assertEqual(ran_count(tail(out)[0]), 7)
        self.assertEqual(tail(out)[1], "FAILED (failures=1)")

    def test_more_workers_than_classes_is_one_per_class(self):
        t = self.recording_tree(3, 2, test_b=FAILING)
        code, out = t.parallel("-j", "99")
        self.assertEqual(code, 1, out)
        seen = set()
        for pid in t.pids().values():
            seen.update(pid)
        self.assertEqual(len(seen), 3, "each recorded class should have its own worker")
        self.assertEqual(ran_count(tail(out)[0]), 7)
        self.assertEqual(tail(out)[1], "FAILED (failures=1)")

    def test_both_agree_with_serial_unittest(self):
        t = self.tree(test_a=SKIPPING, test_b=FAILING, test_c=EXPECTED_FAILURE)
        _, one = t.parallel("-j", "1")
        _, many = t.parallel("-j", "99")
        _, serial = t.serial()
        self.assert_same_answer(one, serial)
        self.assert_same_answer(many, serial)


class Names(ParsuiteCase):
    """AC-7: positional names restrict the run like `python -m unittest <names>`."""

    def test_module_class_and_method_names(self):
        t = self.recording_tree(2, 3, test_b=SKIPPING, test_c=FAILING)
        code, out = t.parallel("-j", "2", "tests.test_a.C0.test_1", "tests.test_b")
        self.assertEqual(code, 0, out)
        ran, verdict = tail(out)
        self.assertEqual(ran_count(ran), 3)
        self.assertEqual(verdict, "OK (skipped=1)")
        self.assert_same_answer(out, t.serial("tests.test_a.C0.test_1", "tests.test_b")[1])

        code, out = t.parallel("tests.test_a.C1")
        self.assertEqual(code, 0, out)
        self.assertEqual(ran_count(tail(out)[0]), 3)
        self.assertEqual(tail(out)[1], "OK")

        code, out = t.parallel("tests.test_c")
        self.assertEqual(code, 1, out)
        self.assertEqual(ran_count(tail(out)[0]), 1)
        self.assertEqual(tail(out)[1], "FAILED (failures=1)")
        self.assertIn("tests.test_c.FailCase.test_fails", out)


class Wiring(unittest.TestCase):
    """AC-8: revali.toml's linux `test` step and the README's Development block use the runner."""

    def test_revali_toml_test_step_runs_the_parallel_runner(self):
        with open(os.path.join(ROOT, "revali.toml"), "rb") as fh:
            cfg = tomllib.load(fh)
        test_cmd = cfg["validate"]["linux"]["test"]
        self.assertIn("tests/run_parallel.py", test_cmd)
        self.assertNotIn("unittest discover", test_cmd)

    def test_readme_development_block_shows_the_runner(self):
        with open(os.path.join(ROOT, "README.md"), "r", encoding="utf-8") as fh:
            readme = fh.read()
        dev = readme.split("## Development", 1)[1]
        self.assertIn("python tests/run_parallel.py", dev)

    def test_the_runner_exists_and_is_standard_library_only(self):
        self.assertTrue(os.path.isfile(RUNNER))
        with open(RUNNER, "r", encoding="utf-8") as fh:
            text = fh.read()
        imported = set()
        for m in re.finditer(r"^(?:import|from)\s+([A-Za-z_][\w]*)", text, re.M):
            imported.add(m.group(1))
        self.assertTrue(imported)
        self.assertTrue(imported <= set(sys.stdlib_module_names), imported)


if __name__ == "__main__":
    unittest.main()
