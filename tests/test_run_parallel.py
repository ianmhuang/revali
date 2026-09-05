"""tests/run_parallel.py against a throwaway test tree: same collection as unittest discover,
one unittest-style summary, failures and tracebacks reprinted, broken imports and crashed
workers counted as errors, -j and positional names honoured."""

import os
import re
import subprocess
import sys
import tempfile
import unittest

from tests.helpers import ROOT, rmtree_force

RUNNER = os.path.join(ROOT, "tests", "run_parallel.py")

PASSING = """
import unittest


class PassCase(unittest.TestCase):
    def test_one(self):
        self.assertTrue(True)

    def test_two(self):
        self.assertEqual(1 + 1, 2)


class OtherCase(unittest.TestCase):
    def test_three(self):
        pass

    @unittest.skip("not here")
    def test_skipped(self):
        pass
"""

FAILING = """
import unittest


class FailCase(unittest.TestCase):
    def test_fails(self):
        self.assertEqual(1, 2, "one is not two")

    def test_errors(self):
        raise RuntimeError("boom")
"""

BROKEN = "import module_that_does_not_exist_anywhere\n"

CRASHING = """
import os
import unittest


class CrashCase(unittest.TestCase):
    def test_kills_the_worker(self):
        os._exit(3)
"""


class Tree:
    def __init__(self, modules):
        self.root = tempfile.mkdtemp(prefix="run_parallel test ")
        self.tests = os.path.join(self.root, "tests")
        os.makedirs(self.tests)
        self.write("__init__.py", "")
        for name, text in modules.items():
            self.write(name + ".py", text)

    def write(self, name, text):
        with open(os.path.join(self.tests, name), "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)

    def run(self, *extra):
        res = subprocess.run(
            [sys.executable, RUNNER, "-s", self.tests, "-t", self.root] + list(extra),
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            cwd=self.root,
        )
        return res.returncode, res.stdout + res.stderr

    def cleanup(self):
        rmtree_force(self.root)


def last_lines(out):
    lines = [line for line in out.strip().splitlines() if line.strip()]
    return lines[-2], lines[-1]


class RunParallel(unittest.TestCase):
    def tree(self, **modules):
        t = Tree(modules)
        self.addCleanup(t.cleanup)
        return t

    def test_all_pass_gives_exit_0_and_a_unittest_summary(self):
        t = self.tree(test_pass=PASSING)
        code, out = t.run("-j", "2")
        self.assertEqual(code, 0, out)
        ran, verdict = last_lines(out)
        self.assertRegex(ran, r"^Ran 4 tests in \d+\.\d+s$")
        self.assertEqual(verdict, "OK (skipped=1)")
        self.assertNotIn("...", out)  # a passing worker's dots are not reprinted

    def test_list_matches_unittest_discover(self):
        t = self.tree(test_pass=PASSING, test_fail=FAILING)
        code, out = t.run("--list")
        self.assertEqual(code, 0, out)
        listed = sorted(line for line in out.splitlines() if line.strip())
        # unittest discover in a fresh interpreter (this one already holds revali's own
        # `tests` package, so it cannot import the throwaway tree's)
        code = (
            "import unittest\n"
            "def walk(s):\n"
            "    for i in s:\n"
            "        if isinstance(i, unittest.TestSuite): yield from walk(i)\n"
            "        else: yield i.id()\n"
            "suite = unittest.TestLoader().discover(start_dir=%r, top_level_dir=%r)\n"
            "print('\\n'.join(sorted(walk(suite))))\n" % (t.tests, t.root)
        )
        res = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, cwd=t.root
        )
        expected = sorted(line for line in res.stdout.splitlines() if line.strip())
        self.assertEqual(listed, expected)
        self.assertEqual(len(listed), 6)
        self.assertIn("tests.test_fail.FailCase.test_fails", listed)

    def test_failures_and_errors_are_reprinted_with_tracebacks(self):
        t = self.tree(test_pass=PASSING, test_fail=FAILING)
        code, out = t.run("-j", "2")
        self.assertEqual(code, 1, out)
        ran, verdict = last_lines(out)
        self.assertRegex(ran, r"^Ran 6 tests in \d+\.\d+s$")
        self.assertEqual(verdict, "FAILED (failures=1, errors=1, skipped=1)")
        self.assertIn("FAIL: test_fails (tests.test_fail.FailCase.test_fails)", out)
        self.assertIn("ERROR: test_errors (tests.test_fail.FailCase.test_errors)", out)
        self.assertIn("AssertionError: 1 != 2 : one is not two", out)
        self.assertIn("RuntimeError: boom", out)
        self.assertIn("Traceback (most recent call last)", out)

    def test_a_module_that_does_not_import_is_an_error(self):
        t = self.tree(test_pass=PASSING, test_broken=BROKEN)
        code, out = t.run("-j", "2")
        self.assertEqual(code, 1, out)
        ran, verdict = last_lines(out)
        self.assertRegex(ran, r"^Ran 5 tests in ")
        self.assertEqual(verdict, "FAILED (errors=1, skipped=1)")
        self.assertIn("tests.test_broken", out)
        self.assertIn("module_that_does_not_exist_anywhere", out)

    def test_a_worker_that_dies_without_a_result_is_an_error(self):
        t = self.tree(test_pass=PASSING, test_crash=CRASHING)
        code, out = t.run("-j", "2")
        self.assertEqual(code, 1, out)
        self.assertIn("without a result", out)
        self.assertIn("exit 3", out)
        ran, verdict = last_lines(out)
        self.assertRegex(ran, r"^Ran 5 tests in ")  # N stays the collected count
        self.assertTrue(verdict.startswith("FAILED (errors=1"), verdict)

    def test_a_tests_own_output_cannot_fake_the_verdict(self):
        talker = FAILING.replace(
            "    def test_errors(self):",
            '    def test_prints_ok(self):\n        print("\\nOK\\nRan 1 test in 0.0s\\nOK")\n\n'
            "    def test_errors(self):",
        )
        t = self.tree(test_pass=PASSING, test_talk=talker)
        code, out = t.run("-j", "1")
        self.assertEqual(code, 1, out)
        self.assertEqual(last_lines(out)[1], "FAILED (failures=1, errors=1, skipped=1)")

    def test_non_ascii_in_a_traceback_survives(self):
        module = FAILING.replace('"one is not two"', '"一不等於二 – ünïcode"')
        t = self.tree(test_nonascii=module)
        code, out = t.run("-j", "1")
        self.assertEqual(code, 1, out)
        self.assertIn("一不等於二 – ünïcode", out)
        self.assertNotIn("�", out)

    def test_one_worker_and_too_many_workers_agree(self):
        t = self.tree(test_pass=PASSING, test_fail=FAILING)
        code1, out1 = t.run("-j", "1")
        code99, out99 = t.run("-j", "99")
        self.assertEqual((code1, code99), (1, 1))
        self.assertIn("6 tests in 1 worker(s)", out1)
        self.assertIn("6 tests in 3 worker(s)", out99)  # one per class
        self.assertEqual(last_lines(out1)[1], last_lines(out99)[1])
        self.assertEqual(
            re.search(r"^Ran (\d+) tests", last_lines(out1)[0]).group(1),
            re.search(r"^Ran (\d+) tests", last_lines(out99)[0]).group(1),
        )

    def test_positional_names_restrict_the_run(self):
        t = self.tree(test_pass=PASSING, test_fail=FAILING)
        code, out = t.run(
            "-j", "2", "tests.test_pass.PassCase.test_one", "tests.test_pass.OtherCase"
        )
        self.assertEqual(code, 0, out)
        ran, verdict = last_lines(out)
        self.assertRegex(ran, r"^Ran 3 tests in ")
        self.assertEqual(verdict, "OK (skipped=1)")
        code, out = t.run("tests.test_fail")
        self.assertEqual(code, 1, out)
        self.assertRegex(last_lines(out)[0], r"^Ran 2 tests in ")


class RepositoryWiring(unittest.TestCase):
    def test_revali_toml_and_readme_use_the_runner(self):
        with open(os.path.join(ROOT, "revali.toml"), "r", encoding="utf-8") as fh:
            self.assertIn('test = "python3 tests/run_parallel.py"', fh.read())
        with open(os.path.join(ROOT, "README.md"), "r", encoding="utf-8") as fh:
            self.assertIn("python tests/run_parallel.py", fh.read())


if __name__ == "__main__":
    unittest.main()
