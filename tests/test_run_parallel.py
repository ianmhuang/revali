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
from tests.run_parallel import SHARD_ENV, select_shard

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

    def run(self, *extra, env=None):
        environ = {k: v for k, v in os.environ.items() if k not in ("RUN_PARALLEL_JOBS", SHARD_ENV)}
        # the output is read back as UTF-8, so the child writes UTF-8 whatever the console's
        # code page (a refused "١/4" is echoed as is, not as an escape)
        environ["PYTHONIOENCODING"] = "utf-8"
        environ.update(env or {})
        res = subprocess.run(
            [sys.executable, RUNNER, "-s", self.tests, "-t", self.root] + list(extra),
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            cwd=self.root,
            env=environ,
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

    def test_jobs_env_var_is_the_default_and_j_beats_it(self):
        t = self.tree(test_pass=PASSING, test_fail=FAILING)
        code, out = t.run(env={"RUN_PARALLEL_JOBS": "1"})
        self.assertEqual(code, 1)
        self.assertIn("6 tests in 1 worker(s)", out)
        code, out = t.run("-j", "2", env={"RUN_PARALLEL_JOBS": "1"})
        self.assertIn("6 tests in 2 worker(s)", out)
        code, out = t.run(env={"RUN_PARALLEL_JOBS": " "})  # blank: as if unset
        self.assertIn("6 tests in %d worker(s)" % min(3, os.cpu_count() or 1), out)
        for bad in ("0", "-2", "eight", "2.5", "1_6", "+8", "\u0662"):  # not plain digits
            code, out = t.run(env={"RUN_PARALLEL_JOBS": bad})
            self.assertEqual(code, 1, bad)
            self.assertIn("RUN_PARALLEL_JOBS=", out)
            self.assertNotIn("worker(s)", out)  # refused before anything ran

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


MORE = """
import unittest


class ACase(unittest.TestCase):
    def test_a1(self):
        pass

    def test_a2(self):
        pass

    def test_a3(self):
        pass


class BCase(unittest.TestCase):
    def test_b1(self):
        pass


class CCase(unittest.TestCase):
    def test_c1(self):
        pass

    def test_c2(self):
        pass
"""

NO_SHARD_IN_WORKERS = """
import os
import unittest


class EnvCase(unittest.TestCase):
    def test_the_shard_variable_stops_at_the_runner(self):
        self.assertNotIn("RUN_PARALLEL_SHARD", os.environ)
"""


def listed(out):
    return [line for line in out.splitlines() if line.strip()]


class Shards(unittest.TestCase):
    """feature/ci-windows-shards AC-1, AC-2"""

    def tree(self, **modules):
        t = Tree(modules)
        self.addCleanup(t.cleanup)
        return t

    def test_the_shards_of_any_n_hold_every_test_once(self):
        t = self.tree(test_pass=PASSING, test_fail=FAILING, test_more=MORE)
        code, out = t.run("--list")
        self.assertEqual(code, 0, out)
        everything = listed(out)
        self.assertEqual(len(everything), 12)
        for n in range(1, 6):
            parts = []
            for k in range(1, n + 1):
                code, out = t.run("--list", "--shard", "%d/%d" % (k, n))
                self.assertEqual(code, 0, out)
                parts.append(listed(out))
            with self.subTest(n=n):
                flat = [tid for part in parts for tid in part]
                self.assertEqual(sorted(flat), sorted(everything))  # all, and none twice
                for part in parts:  # whole classes
                    classes = {tid.rsplit(".", 1)[0] for tid in part}
                    for other in parts:
                        if other is not part:
                            self.assertFalse(classes & {x.rsplit(".", 1)[0] for x in other})

    def test_a_shard_runs_its_part_and_says_so(self):
        t = self.tree(test_pass=PASSING, test_more=MORE)
        expected = {}
        for k in (1, 2):
            code, out = t.run("--list", "--shard", "%d/2" % k)
            expected[k] = len(listed(out))
        self.assertEqual(expected[1] + expected[2], 10)
        code, out = t.run("-j", "2", "--shard", "2/2")
        self.assertEqual(code, 0, out)
        ran, verdict = last_lines(out)
        self.assertRegex(ran, r"^Ran %d tests in \d+\.\d+s \(shard 2 of 2\)$" % expected[2])
        self.assertIn("%d of 10 tests in" % expected[2], out)
        # the variable does the same, and the flag beats it
        code, env_out = t.run("-j", "2", env={SHARD_ENV: "2/2"})
        self.assertEqual(last_lines(env_out)[0].split(" in ")[0], ran.split(" in ")[0])
        code, out = t.run("-j", "2", "--shard", "1/2", env={SHARD_ENV: "2/2"})
        self.assertRegex(last_lines(out)[0], r"^Ran %d tests in .*\(shard 1 of 2\)$" % expected[1])
        code, out = t.run("-j", "2", env={SHARD_ENV: "  "})  # blank: the whole suite
        self.assertRegex(last_lines(out)[0], r"^Ran 10 tests in \d+\.\d+s$")

    def test_a_broken_import_is_reported_by_shard_one_only(self):
        t = self.tree(test_pass=PASSING, test_more=MORE, test_broken=BROKEN)
        results = {}
        for k in (1, 2, 3):
            results[k] = t.run("--shard", "%d/3" % k)
        self.assertEqual(results[1][0], 1, results[1][1])
        self.assertIn("module_that_does_not_exist_anywhere", results[1][1])
        for k in (2, 3):
            code, out = results[k]
            self.assertEqual(code, 0, out)
            self.assertNotIn("module_that_does_not_exist_anywhere", out)

    def test_a_shard_with_no_class_runs_nothing(self):
        t = self.tree(test_pass=PASSING)  # two classes
        code, out = t.run("--shard", "3/4")
        self.assertEqual(code, 0, out)
        ran, verdict = last_lines(out)
        self.assertRegex(ran, r"^Ran 0 tests in .*\(shard 3 of 4\)$")
        self.assertEqual(verdict, "OK")

    def test_a_bad_value_is_refused_before_anything_runs(self):
        t = self.tree(test_pass=PASSING)
        for bad in ("0/4", "5/4", "2", "a/b", "1_0/4", "1/0", "2/4/6", "-1/4", "١/4", ""):
            for how in ("flag", "env"):
                with self.subTest(value=bad, how=how):
                    if how == "flag":
                        code, out = t.run("--shard=" + bad)  # "-1/4" is a value, not a flag
                        source = "--shard"
                    else:
                        if not bad.strip():
                            continue  # blank in the variable means unset
                        code, out = t.run(env={SHARD_ENV: bad})
                        source = SHARD_ENV
                    self.assertEqual(code, 1, out)
                    self.assertIn(source, out)
                    self.assertIn(repr(bad), out)
                    self.assertNotIn("worker(s)", out)

    def test_the_variable_is_not_passed_to_the_workers(self):
        t = self.tree(test_env=NO_SHARD_IN_WORKERS)
        code, out = t.run(env={SHARD_ENV: "1/1"})
        self.assertEqual(code, 0, out)
        self.assertRegex(last_lines(out)[0], r"^Ran 1 tests in .*\(shard 1 of 1\)$")

    def test_this_repositorys_suite_splits_into_shards_that_hold_it_once(self):
        res = subprocess.run(
            [sys.executable, RUNNER, "--list"],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            cwd=ROOT,
            env={k: v for k, v in os.environ.items() if k != SHARD_ENV},
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        everything = listed(res.stdout)
        self.assertGreater(len(everything), 1000)
        for n in range(1, 6):
            with self.subTest(n=n):
                flat = [tid for k in range(1, n + 1) for tid in select_shard(everything, k, n)]
                self.assertEqual(sorted(flat), sorted(everything))
                if n <= 4:  # every shard CI runs has work
                    self.assertTrue(all(select_shard(everything, k, n) for k in range(1, n + 1)))


class RepositoryWiring(unittest.TestCase):
    def test_revali_toml_and_readme_use_the_runner(self):
        with open(os.path.join(ROOT, "revali.toml"), "r", encoding="utf-8") as fh:
            self.assertIn('test = "python3 tests/run_parallel.py"', fh.read())
        with open(os.path.join(ROOT, "README.md"), "r", encoding="utf-8") as fh:
            self.assertIn("python tests/run_parallel.py", fh.read())


if __name__ == "__main__":
    unittest.main()
