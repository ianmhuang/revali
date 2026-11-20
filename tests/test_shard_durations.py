"""tests/run_parallel.py weighs test classes by recorded seconds (feature/ci-shard-time, M3.33.1):
`<start dir>/durations.json` gives the weights, `--durations-out` records them."""

import json
import math
import os
import subprocess
import sys
import tempfile
import unittest

from tests.helpers import ROOT, rmtree_force
from tests.run_parallel import (
    DURATIONS_FILE,
    SHARD_ENV,
    class_weights,
    load_durations,
    select_shard,
    shard,
)

RUNNER = os.path.join(ROOT, "tests", "run_parallel.py")

# one slow class of one test, two fast classes of five
UNEVEN = """
import time
import unittest


class SlowCase(unittest.TestCase):
    def test_slow(self):
        time.sleep(0.3)


class FastACase(unittest.TestCase):
%s

class FastBCase(unittest.TestCase):
%s
""" % (
    "".join("    def test_a%d(self):\n        pass\n\n" % i for i in range(5)),
    "".join("    def test_b%d(self):\n        pass\n\n" % i for i in range(5)),
)

SLOW = "tests.test_uneven.SlowCase"
FAST_A = "tests.test_uneven.FastACase"
FAST_B = "tests.test_uneven.FastBCase"


def ids_of(classes):
    """Test ids for {class: number of tests}, in the order given."""
    return ["%s.test_%d" % (cls, i) for cls, n in classes.items() for i in range(n)]


def classes_in(part):
    return sorted({tid.rsplit(".", 1)[0] for tid in part})


class Weights(unittest.TestCase):
    """AC-1, AC-2: the weight of each class."""

    def test_recorded_seconds_are_the_weight(self):
        ids = ids_of({"m.A": 1, "m.B": 4})
        self.assertEqual(class_weights(ids, {"m.A": 9.0, "m.B": 2.0}), {"m.A": 9.0, "m.B": 2.0})

    def test_an_unlisted_class_weighs_its_tests_times_the_median_per_test(self):
        ids = ids_of({"m.A": 2, "m.B": 4, "m.C": 1, "m.New": 3})
        # seconds per test: A 1.0, B 2.0, C 6.0 -> median 2.0
        weights = class_weights(ids, {"m.A": 2.0, "m.B": 8.0, "m.C": 6.0, "m.Gone": 50.0})
        self.assertEqual(weights["m.New"], 6.0)
        self.assertNotIn("m.Gone", weights)  # only collected classes

    def test_no_listed_class_means_no_weights(self):
        ids = ids_of({"m.A": 2})
        self.assertIsNone(class_weights(ids, {}))
        self.assertIsNone(class_weights(ids, {"other.X": 3.0}))


class Split(unittest.TestCase):
    """AC-1, AC-3: classes placed by weight, heaviest first onto the least loaded bucket."""

    def test_a_slow_small_class_gets_a_bucket_of_its_own(self):
        ids = ids_of({"m.Big1": 5, "m.Big2": 5, "m.Slow": 1})
        by_count = shard(ids, 2)
        self.assertEqual(
            sorted(classes_in(b) for b in by_count), [["m.Big1", "m.Slow"], ["m.Big2"]]
        )
        weights = class_weights(ids, {"m.Big1": 5.0, "m.Big2": 5.0, "m.Slow": 100.0})
        by_time = shard(ids, 2, weights)
        self.assertEqual(sorted(classes_in(b) for b in by_time), [["m.Big1", "m.Big2"], ["m.Slow"]])

    def test_without_weights_the_split_is_todays(self):
        ids = ids_of({"m.A": 5, "m.B": 3, "m.C": 2, "m.D": 2, "m.E": 1})
        self.assertEqual(shard(ids, 3, None), shard(ids, 3))
        self.assertEqual(sorted(len(b) for b in shard(ids, 3)), [4, 4, 5])

    def test_shards_hold_every_test_once_and_repeat(self):
        ids = ids_of({"m.%d" % i: 1 + i % 4 for i in range(23)})
        weights = class_weights(ids, {"m.%d" % i: float(i * 7 % 11) for i in range(0, 23, 2)})
        for n in range(1, 8):
            with self.subTest(n=n):
                parts = [select_shard(ids, k, n, weights) for k in range(1, n + 1)]
                self.assertEqual(sorted(t for p in parts for t in p), sorted(ids))
                again = [select_shard(ids, k, n, weights) for k in range(1, n + 1)]
                self.assertEqual(parts, again)

    def test_zero_seconds_everywhere_still_spreads_the_classes(self):
        ids = ids_of({"m.A": 2, "m.B": 2, "m.C": 2, "m.D": 2})
        weights = class_weights(ids, {"m.A": 0.0, "m.B": 0.0, "m.C": 0.0, "m.D": 0.0})
        self.assertEqual(sorted(len(b) for b in shard(ids, 4, weights)), [2, 2, 2, 2])


class Loading(unittest.TestCase):
    """AC-2: a file that cannot give weights gives none, and never raises."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="durations ")
        self.addCleanup(rmtree_force, self.dir)
        self.path = os.path.join(self.dir, DURATIONS_FILE)

    def write(self, data):
        with open(self.path, "wb") as fh:
            fh.write(data)

    def test_a_missing_file(self):
        self.assertEqual(load_durations(self.path), {})

    def test_files_that_are_not_a_json_object(self):
        for data in (b"", b"{not json", b"[1, 2]", b'"text"', b"\xff\xfe{\x00}\x00"):
            with self.subTest(data=data):
                self.write(data)
                self.assertEqual(load_durations(self.path), {})

    def test_entries_that_are_not_seconds_are_dropped(self):
        self.write(
            json.dumps(
                {"m.Ok": 1.5, "m.Int": 2, "m.Str": "3", "m.Neg": -1, "m.Bool": True, "m.Null": None}
            ).encode("utf-8")
            + b""
        )
        self.assertEqual(load_durations(self.path), {"m.Ok": 1.5, "m.Int": 2.0})
        self.write(b'{"m.Nan": NaN, "m.Inf": Infinity, "m.Ok": 1}')
        self.assertEqual(load_durations(self.path), {"m.Ok": 1.0})


class Tree:
    def __init__(self, modules):
        self.root = tempfile.mkdtemp(prefix="shard durations ")
        self.tests = os.path.join(self.root, "tests")
        os.makedirs(self.tests)
        for name, text in dict(modules, __init__="").items():
            with open(os.path.join(self.tests, name + ".py"), "w", encoding="utf-8") as fh:
                fh.write(text)

    def durations(self, data):
        with open(os.path.join(self.tests, DURATIONS_FILE), "wb") as fh:
            fh.write(data if isinstance(data, bytes) else json.dumps(data).encode("utf-8"))

    def run(self, *extra):
        env = {k: v for k, v in os.environ.items() if k not in ("RUN_PARALLEL_JOBS", SHARD_ENV)}
        env["PYTHONIOENCODING"] = "utf-8"
        res = subprocess.run(
            [sys.executable, RUNNER, "-s", self.tests, "-t", self.root] + list(extra),
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            cwd=self.root,
            env=env,
        )
        return res.returncode, res.stdout + res.stderr

    def listed(self, *extra):
        code, out = self.run("--list", *extra)
        if code != 0:
            raise AssertionError(out)
        return [line.strip() for line in out.splitlines() if line.strip()]


class Runner(unittest.TestCase):
    """AC-1, AC-2, AC-4 through the script itself."""

    def setUp(self):
        self.tree = Tree({"test_uneven": UNEVEN})
        self.addCleanup(rmtree_force, self.tree.root)

    def shard_classes(self, n):
        return sorted(classes_in(self.tree.listed("--shard", "%d/%d" % (k, n))) for k in (1, 2))

    def test_the_file_next_to_the_tests_decides_the_shards(self):
        self.assertEqual(self.shard_classes(2), [[FAST_A, SLOW], [FAST_B]])  # by count
        self.tree.durations({SLOW: 30.0, FAST_A: 1.0, FAST_B: 1.0})
        self.assertEqual(self.shard_classes(2), [[FAST_A, FAST_B], [SLOW]])

    def test_a_bad_file_splits_by_count_and_fails_nothing(self):
        by_count = self.shard_classes(2)
        for data in (b"{broken", b"[]", b"\xe9\xe9", {"tests.other.Case": 5.0}):
            with self.subTest(data=data):
                self.tree.durations(data)
                self.assertEqual(self.shard_classes(2), by_count)
                code, out = self.tree.run("-j", "2")
                self.assertEqual(code, 0, out)
                self.assertTrue(out.strip().endswith("OK"), out)

    def test_durations_out_records_every_class_it_ran(self):
        out_file = os.path.join(self.tree.root, "measured.json")
        code, out = self.tree.run("-j", "2", "--durations-out", out_file)
        self.assertEqual(code, 0, out)
        with open(out_file, "r", encoding="utf-8") as fh:
            seconds = json.load(fh)
        self.assertEqual(sorted(seconds), [FAST_A, FAST_B, SLOW])
        for name, value in seconds.items():
            self.assertTrue(isinstance(value, float) and math.isfinite(value) and value >= 0, name)
        self.assertGreaterEqual(seconds[SLOW], 0.3)
        self.assertLess(seconds[FAST_A], seconds[SLOW])
        # the recorded file, put next to the tests, is read back as the weights
        os.replace(out_file, os.path.join(self.tree.tests, DURATIONS_FILE))
        self.assertEqual(self.shard_classes(2), [[FAST_A, FAST_B], [SLOW]])

    def test_a_shard_records_only_its_own_classes(self):
        out_file = os.path.join(self.tree.root, "part.json")
        mine = classes_in(self.tree.listed("--shard", "2/2"))
        code, out = self.tree.run("--shard", "2/2", "--durations-out", out_file)
        self.assertEqual(code, 0, out)
        with open(out_file, "r", encoding="utf-8") as fh:
            self.assertEqual(sorted(json.load(fh)), mine)


class ThisRepository(unittest.TestCase):
    """AC-3, AC-5, AC-6: the committed file and the six CI shards of this suite."""

    @classmethod
    def setUpClass(cls):
        res = subprocess.run(
            [sys.executable, RUNNER, "--list"],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            cwd=ROOT,
            env={k: v for k, v in os.environ.items() if k != SHARD_ENV},
        )
        if res.returncode != 0:
            raise AssertionError(res.stderr)
        cls.everything = [line.strip() for line in res.stdout.splitlines() if line.strip()]
        cls.durations = load_durations(os.path.join(ROOT, "tests", DURATIONS_FILE))

    def test_the_committed_file_gives_weights(self):
        self.assertIsNotNone(class_weights(self.everything, self.durations))

    def test_six_weighted_shards_hold_the_suite_once_and_none_is_empty(self):
        weights = class_weights(self.everything, self.durations)
        parts = [select_shard(self.everything, k, 6, weights) for k in range(1, 7)]
        self.assertEqual(sorted(t for p in parts for t in p), sorted(self.everything))
        self.assertTrue(all(parts))

    def test_the_docstring_says_how_to_refresh_the_file(self):
        with open(RUNNER, "r", encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("--durations-out tests/%s" % DURATIONS_FILE, text)


if __name__ == "__main__":
    unittest.main()
