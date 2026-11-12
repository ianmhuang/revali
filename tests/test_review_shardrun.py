"""Acceptance tests for feature/ci-windows-shards, the runner side (AC-1, AC-2).

`tests/run_parallel.py --shard K/N`, else RUN_PARALLEL_SHARD=K/N, runs only shard K of N:
whole classes, balanced the way the workers are, the N shards holding every collected test
exactly once; a module that does not import is reported by shard 1 only; the summary names
the shard. A value that is not K/N with 1 <= K <= N exits 1 naming the value before any test
runs; a shard left without a class runs nothing and is OK.

The script is driven in a fresh interpreter against a throwaway tree. Every test of that tree
leaves a file named by its id, so "this test ran" is observed and not read from the summary.
"""

import os
import re
import subprocess
import sys
import tempfile
import unittest
from collections import Counter

from tests.helpers import ROOT, rmtree_force
from tests.run_parallel import shard as worker_split

RUNNER = os.path.join(ROOT, "tests", "run_parallel.py")
VAR = "RUN_PARALLEL_SHARD"
RAN_DIR = "SHARDRUN_RAN"
MISSING = "module_that_shardrun_cannot_import"

HEAD = """import os
import unittest


def ran(test):
    with open(os.path.join(os.environ["%s"], test.id()), "w"):
        pass
""" % RAN_DIR

BROKEN = "import %s\n" % MISSING

# 16 tests in 7 classes of sizes 5, 3, 2, 2, 2, 1, 1
SIZES = (("Five", 5), ("Three", 3), ("TwoA", 2), ("TwoB", 2), ("One", 1))
OTHER = (("Pair", 2), ("Solo", 1))
BAD_VALUES = ("0/4", "5/4", "2", "a/b", "1_0/4")  # AC-2's list


def module(classes):
    """The source of a test module: a class per (name, size), each test recording its run."""
    out = [HEAD]
    for name, size in classes:
        out.append("\n\nclass %s(unittest.TestCase):\n" % name)
        for i in range(size):
            out.append("    def test_%d(self):\n        ran(self)\n\n" % i)
    return "".join(out)


def class_of(test_id):
    return test_id.rsplit(".", 1)[0]


def summary(stdout):
    """The `Ran N tests in ...` line and the verdict line: the last two lines printed."""
    lines = [line.strip() for line in stdout.splitlines() if line.strip()]
    if len(lines) < 2:
        raise AssertionError("no summary in the output: %r" % stdout)
    return lines[-2], lines[-1]


def clean_env():
    return {k: v for k, v in os.environ.items() if k not in (VAR, "RUN_PARALLEL_JOBS")}


class Tree:
    def __init__(self, **modules):
        self.root = tempfile.mkdtemp(prefix="shardrun ")
        self.tests = os.path.join(self.root, "tests")
        os.makedirs(self.tests)
        modules["__init__"] = ""
        for name, text in modules.items():
            path = os.path.join(self.tests, name + ".py")
            with open(path, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(text)
        self.lists = {}

    def run(self, *extra, shard_env=None):
        """Exit code, stdout, stderr and the ids of the tests that ran."""
        ran_dir = tempfile.mkdtemp(prefix="ran-", dir=self.root)
        env = clean_env()
        env[RAN_DIR] = ran_dir
        if shard_env is not None:
            env[VAR] = shard_env
        res = subprocess.run(
            [sys.executable, RUNNER, "-s", self.tests, "-t", self.root, "-j", "2"] + list(extra),
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            cwd=self.root,
            env=env,
        )
        return res.returncode, res.stdout, res.stderr, set(os.listdir(ran_dir))

    def listed(self, part=None):
        """The ids `--list` prints, for the whole tree or for the shard `K/N`."""
        if part not in self.lists:
            extra = ["--list"] + (["--shard", part] if part else [])
            code, out, err, ran = self.run(*extra)
            if code != 0:
                raise AssertionError("%s: exit %d\n%s%s" % (" ".join(extra), code, out, err))
            if ran:
                raise AssertionError("%s ran tests: %r" % (" ".join(extra), sorted(ran)))
            self.lists[part] = [line.strip() for line in out.splitlines() if line.strip()]
        return self.lists[part]


class Partition(unittest.TestCase):
    """AC-1: the N shards hold every collected test once, in whole, balanced classes."""

    @classmethod
    def setUpClass(cls):
        cls.tree = Tree(test_sizes=module(SIZES), test_other=module(OTHER))
        cls.addClassCleanup(rmtree_force, cls.tree.root)
        cls.everything = cls.tree.listed()

    def parts(self, n):
        return [self.tree.listed("%d/%d" % (k, n)) for k in range(1, n + 1)]

    def test_the_shards_of_n_hold_every_test_once_in_whole_classes(self):
        self.assertEqual(len(self.everything), 16)
        for n in range(1, 6):
            parts = self.parts(n)
            with self.subTest(n=n):
                flat = [tid for part in parts for tid in part]
                self.assertEqual(sorted(flat), sorted(self.everything))  # all, none twice
                owners = {}
                for k, part in enumerate(parts, 1):
                    for tid in part:
                        owners.setdefault(class_of(tid), set()).add(k)
                split = sorted(name for name, ks in owners.items() if len(ks) > 1)
                self.assertEqual(split, [], "classes spread over more than one shard")

    def test_the_shards_are_balanced_the_way_the_workers_are(self):
        # the workers' rule: largest classes first, each onto the least loaded bucket
        by_hand = {1: [16], 2: [8, 8], 3: [5, 5, 6]}
        for n in range(1, 6):
            sizes = sorted(len(part) for part in self.parts(n))
            with self.subTest(n=n):
                workers = sorted(len(bucket) for bucket in worker_split(self.everything, n))
                self.assertEqual(sizes, workers)
                if n in by_hand:
                    self.assertEqual(sizes, by_hand[n])

    def test_four_shards_of_this_repositorys_suite_hold_it_once(self):
        def listed(*extra):
            res = subprocess.run(
                [sys.executable, RUNNER, "--list"] + list(extra),
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                cwd=ROOT,
                env=clean_env(),
            )
            self.assertEqual(res.returncode, 0, (extra, res.stderr[-2000:]))
            return [line.strip() for line in res.stdout.splitlines() if line.strip()]

        everything = listed()
        self.assertGreater(len(everything), 1000)
        parts = [listed("--shard", "%d/4" % k) for k in range(1, 5)]
        flat = Counter(tid for part in parts for tid in part)
        whole = Counter(everything)
        self.assertEqual(sorted((flat - whole).elements())[:10], [], "listed too often")
        self.assertEqual(sorted((whole - flat).elements())[:10], [], "in no shard")
        self.assertEqual([k for k, part in enumerate(parts, 1) if not part], [], "empty shards")
        owners = {}
        for k, part in enumerate(parts, 1):
            for tid in part:
                owners.setdefault(class_of(tid), set()).add(k)
        self.assertEqual(sorted(name for name, ks in owners.items() if len(ks) > 1)[:10], [])


class Running(unittest.TestCase):
    """AC-1: what a shard runs, what it prints, and where it takes K/N from."""

    def setUp(self):
        self.tree = Tree(test_sizes=module(SIZES), test_other=module(OTHER))
        self.addCleanup(rmtree_force, self.tree.root)

    def test_each_shard_runs_only_its_tests_and_the_summary_names_it(self):
        everything = self.tree.listed()
        seen = []
        for k in (1, 2, 3):
            code, out, err, ran = self.tree.run("--shard", "%d/3" % k)
            with self.subTest(k=k):
                self.assertEqual(code, 0, out + err)
                self.assertEqual(sorted(ran), sorted(self.tree.listed("%d/3" % k)))
                ran_line, verdict = summary(out)
                self.assertRegex(
                    ran_line, r"^Ran %d tests in \d+\.\d+s \(shard %d of 3\)$" % (len(ran), k)
                )
                self.assertEqual(verdict, "OK")
            seen.extend(ran)
        self.assertEqual(sorted(seen), sorted(everything))  # together: all, none twice

    def test_the_variable_selects_the_shard_and_the_flag_beats_it(self):
        code, out, err, ran = self.tree.run(shard_env="2/3")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(sorted(ran), sorted(self.tree.listed("2/3")))
        self.assertRegex(summary(out)[0], r"^Ran %d tests in .*\(shard 2 of 3\)$" % len(ran))
        code, out, err, ran = self.tree.run("--shard", "1/3", shard_env="2/3")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(sorted(ran), sorted(self.tree.listed("1/3")))
        self.assertRegex(summary(out)[0], r"^Ran %d tests in .*\(shard 1 of 3\)$" % len(ran))

    def test_a_module_that_does_not_import_is_reported_by_shard_one_only(self):
        good = sorted(self.tree.listed())
        broken = Tree(test_sizes=module(SIZES), test_other=module(OTHER), test_broken=BROKEN)
        self.addCleanup(rmtree_force, broken.root)
        for n in (2, 3):
            seen = []
            for k in range(1, n + 1):
                code, out, err, ran = broken.run("--shard", "%d/%d" % (k, n))
                with self.subTest(n=n, k=k):
                    if k == 1:
                        self.assertEqual(code, 1, out + err)
                        self.assertIn(MISSING, out + err)
                        self.assertTrue(summary(out)[1].startswith("FAILED"), out)
                    else:
                        self.assertEqual(code, 0, out + err)
                        self.assertNotIn(MISSING, out + err)
                        self.assertEqual(summary(out)[1], "OK")
                seen.extend(ran)
            with self.subTest(n=n):
                self.assertEqual(sorted(seen), good)  # the modules that import still run once


class Refusals(unittest.TestCase):
    """AC-2: a value that is not K/N with 1 <= K <= N, and a shard with no class."""

    def setUp(self):
        self.tree = Tree(test_sizes=module(SIZES))
        self.addCleanup(rmtree_force, self.tree.root)

    def refused(self, bad, code, out, err, ran):
        self.assertEqual(code, 1, out + err)
        # the value itself, not a longer token that happens to hold it (`2` in `e.g. 2/4`)
        self.assertRegex(out + err, r"(?<![\w/])%s(?![\w/])" % re.escape(bad))
        self.assertEqual(ran, set(), "tests ran")
        self.assertNotRegex(out, r"(?m)^Ran \d+ test")

    def test_a_bad_flag_value_is_refused_before_any_test_runs(self):
        for bad in BAD_VALUES:
            code, out, err, ran = self.tree.run("--shard", bad)
            with self.subTest(value=bad):
                self.refused(bad, code, out, err, ran)

    def test_a_bad_variable_value_is_refused_before_any_test_runs(self):
        for bad in BAD_VALUES:
            code, out, err, ran = self.tree.run(shard_env=bad)
            with self.subTest(value=bad):
                self.refused(bad, code, out, err, ran)

    def test_a_shard_left_without_a_class_runs_nothing_and_is_ok(self):
        small = Tree(test_small=module((("A", 2), ("B", 1))))  # two classes, four shards
        self.addCleanup(rmtree_force, small.root)
        seen = []
        empty = []
        for k in range(1, 5):
            code, out, err, ran = small.run("--shard", "%d/4" % k)
            with self.subTest(k=k):
                self.assertEqual(code, 0, out + err)
                ran_line, verdict = summary(out)
                self.assertRegex(ran_line, r"^Ran %d tests in " % len(ran))
                self.assertEqual(verdict, "OK")
            if not ran:
                empty.append(k)
            seen.extend(ran)
        self.assertEqual(len(empty), 2, "two classes leave two of four shards empty")
        self.assertEqual(sorted(seen), sorted(small.listed()))


if __name__ == "__main__":
    unittest.main()
