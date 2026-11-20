"""Acceptance tests for feature/ci-shard-time (M3.33.1), AC-1 to AC-6.

`tests/run_parallel.py` weighs each test class by the seconds `<start dir>/durations.json`
records for it (a class the file does not list: its test count times the median seconds per
test of the listed ones) and places the classes heaviest first onto the least loaded shard
or worker. A file that gives no weights leaves the split by test count, and fails nothing.
`--durations-out FILE` writes the seconds of each class the run ran. The repository commits
such a file, and CI runs the Windows leg as six shards.

The script is driven in a fresh interpreter against throwaway trees. Where it matters that a
test ran, and in which worker, each test of the tree appends its process id to a file named
by its id, so both are observed and not read from the summary.
"""

import json
import math
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import unittest
from collections import Counter

from tests.helpers import ROOT, rmtree_force

RUNNER = os.path.join(ROOT, "tests", "run_parallel.py")
WORKFLOW = os.path.join(ROOT, ".github", "workflows", "test.yml")
COMMITTED = os.path.join(ROOT, "tests", "durations.json")
VAR = "RUN_PARALLEL_SHARD"
JOBS = "RUN_PARALLEL_JOBS"
RAN_DIR = "SHARDTIME_RAN"
DIRECTORY = object()  # Tree.durations: put a directory where the file is expected

HEAD = (
    "import os\n"
    "import time\n"
    "import unittest\n"
    "\n"
    "\n"
    "def ran(test):\n"
    '    with open(os.path.join(os.environ["' + RAN_DIR + '"], test.id()), "a") as fh:\n'
    '        fh.write(str(os.getpid()) + " ")\n'
)

# 16 tests in 7 classes of sizes 5, 3, 2, 2, 2, 1, 1
SIZES = (("Five", 5), ("Three", 3), ("TwoA", 2), ("TwoB", 2), ("One", 1))
OTHER = (("Pair", 2), ("Solo", 1))


def module(classes, sleep=None, record=True):
    """The source of a test module: a class per (name, size). A test records its run unless
    `record` is false, and sleeps `sleep[name]` seconds when its class is named there."""
    sleep = sleep or {}
    out = [HEAD]
    for name, size in classes:
        out.append("\n\nclass %s(unittest.TestCase):\n" % name)
        for i in range(size):
            out.append("    def test_%d(self):\n" % i)
            if record:
                out.append("        ran(self)\n")
            if name in sleep:
                out.append("        time.sleep(%r)\n" % sleep[name])
            if not record and name not in sleep:
                out.append("        pass\n")
            out.append("\n")
    return "".join(out)


def class_of(test_id):
    return test_id.rsplit(".", 1)[0]


def cid(mod, name):
    return "tests.%s.%s" % (mod, name)


def split_of(mod, *groups):
    """A split as a set of class sets: one group of class names of `mod` per shard."""
    return {frozenset(cid(mod, name) for name in group) for group in groups}


def partition(parts):
    """The class sets of the non-empty parts; fails when a class is in more than one part."""
    sets = [frozenset(class_of(tid) for tid in part) for part in parts if part]
    names = [name for group in sets for name in group]
    if len(names) != len(set(names)):
        raise AssertionError("a class is spread over more than one part: %r" % (sets,))
    return set(sets)


def count_split(ids, n):
    """The split the runner made before this change: whole classes, largest first onto the
    bucket with the fewest tests (the first of them on a tie)."""
    groups = {}
    for tid in ids:
        groups.setdefault(class_of(tid), []).append(tid)
    buckets = [[] for _ in range(max(1, min(n, len(groups))))]
    for members in sorted(groups.values(), key=len, reverse=True):
        min(buckets, key=len).extend(members)
    buckets = [b for b in buckets if b]
    return [buckets[k] if k < len(buckets) else [] for k in range(n)]


def summary(output):
    """The `Ran N tests in ...` line and the verdict line: the last two lines printed."""
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    if len(lines) < 2:
        raise AssertionError("no summary in the output: %r" % output)
    return lines[-2], lines[-1]


def clean_env():
    env = {k: v for k, v in os.environ.items() if k not in (VAR, JOBS, RAN_DIR)}
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def call(args, cwd, env):
    res = subprocess.run(
        [sys.executable, RUNNER] + list(args),
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        cwd=cwd,
        env=env,
    )
    return res.returncode, res.stdout, res.stderr


def lines_of(text):
    return [line.strip() for line in text.splitlines() if line.strip()]


class Tree:
    def __init__(self, **modules):
        self.root = tempfile.mkdtemp(prefix="shardtime ")
        self.tests = os.path.join(self.root, "tests")
        os.makedirs(self.tests)
        modules["__init__"] = ""
        for name, text in modules.items():
            path = os.path.join(self.tests, name + ".py")
            with open(path, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(text)
        self.file = os.path.join(self.tests, "durations.json")

    def durations(self, data):
        """Put `data` at `<start dir>/durations.json`: bytes as they are, DIRECTORY as a
        directory of that name, anything else as JSON. None leaves no file."""
        if os.path.isdir(self.file):
            shutil.rmtree(self.file)
        elif os.path.exists(self.file):
            os.remove(self.file)
        if data is None:
            return
        if data is DIRECTORY:
            os.mkdir(self.file)
            return
        with open(self.file, "wb") as fh:
            fh.write(data if isinstance(data, bytes) else json.dumps(data).encode("utf-8"))

    def run(self, *extra):
        """Exit code, the output, and {test id: the process ids that ran it}."""
        ran_dir = tempfile.mkdtemp(prefix="ran-", dir=self.root)
        env = clean_env()
        env[RAN_DIR] = ran_dir
        code, out, err = call(["-s", self.tests, "-t", self.root] + list(extra), self.root, env)
        ran = {}
        for name in os.listdir(ran_dir):
            with open(os.path.join(ran_dir, name), "r", encoding="utf-8") as fh:
                ran[name] = fh.read().split()
        return code, out + err, ran

    def listed(self, *extra):
        """The ids `--list` prints, for the whole tree or with e.g. `--shard K/N`."""
        args = ["-s", self.tests, "-t", self.root, "--list"] + list(extra)
        code, out, err = call(args, self.root, clean_env())
        if code != 0:
            raise AssertionError("%s: exit %d\n%s%s" % (" ".join(extra), code, out, err))
        return lines_of(out)

    def parts(self, n):
        return [self.listed("--shard", "%d/%d" % (k, n)) for k in range(1, n + 1)]


class TreeCase(unittest.TestCase):
    def tree(self, **modules):
        tree = Tree(**modules)
        self.addCleanup(rmtree_force, tree.root)
        return tree


class Weights(TreeCase):
    """AC-1: recorded seconds, or the median per test for a class the file does not list,
    place the classes of a shard and the classes over the workers."""

    def test_recorded_seconds_place_the_classes_of_a_shard(self):
        tree = self.tree(test_w=module([(name, i + 1) for i, name in enumerate("UVWXYZ")]))
        # by test count (6, 5, 4, 3, 2, 1 tests): Z, Y, X open the shards, then W, V, U
        self.assertEqual(partition(tree.parts(3)), split_of("test_w", "ZU", "YV", "XW"))
        seconds = {"U": 9.0, "V": 8.0, "W": 3.0, "X": 3.5, "Y": 2.0, "Z": 1.0}
        tree.durations({cid("test_w", name): value for name, value in seconds.items()})
        # heaviest first onto the least loaded: U 9 | V 8 | X 3.5, then W -> 6.5, Y -> 8.5
        # on the third, and Z onto the second (8 against 9 and 8.5)
        self.assertEqual(partition(tree.parts(3)), split_of("test_w", "U", "VZ", "XWY"))

    def test_an_unlisted_class_weighs_its_tests_times_the_median_per_test(self):
        sizes = (("A", 1), ("B", 2), ("C", 3), ("New", 4))
        tree = self.tree(test_m=module(sizes))
        tree.durations(
            {
                cid("test_m", "A"): 10.0,  # 10 s per test
                cid("test_m", "B"): 4.0,  # 2 s per test, the median
                cid("test_m", "C"): 3.0,  # 1 s per test
                "tests.test_gone.Gone": 500.0,  # not collected: no weight, no say
            }
        )
        # New weighs 4 x 2 = 8: A 10 | New 8, then B -> 12 beside New, C -> 13 beside A.
        # The mean per test (4.33 -> 17.3) would give New | A B C, the test count (4)
        # A | B New C, and no weight at all A | B C New.
        expected = split_of("test_m", ("A", "C"), ("New", "B"))
        self.assertEqual(partition(tree.parts(2)), expected)

    def test_the_weights_also_place_the_classes_over_the_workers(self):
        tree = self.tree(test_w=module((("Big1", 5), ("Big2", 5), ("Slow", 1))))

        def workers():
            code, out, ran = tree.run("-j", "2")
            self.assertEqual(code, 0, out)
            self.assertEqual(sorted(ran), sorted(tree.listed()))
            by_pid = {}
            for tid, pids in ran.items():
                self.assertEqual(len(pids), 1, "%s ran %d times" % (tid, len(pids)))
                by_pid.setdefault(pids[0], set()).add(class_of(tid))
            return {frozenset(names) for names in by_pid.values()}

        self.assertEqual(workers(), split_of("test_w", ("Big1", "Slow"), ("Big2",)))
        seconds = {"Big1": 5.0, "Big2": 5.0, "Slow": 100.0}
        tree.durations({cid("test_w", name): value for name, value in seconds.items()})
        self.assertEqual(workers(), split_of("test_w", ("Slow",), ("Big1", "Big2")))


BAD_FILES = (
    ("no file", None),
    ("an empty file", b""),
    ("broken JSON", b'{"tests.test_sizes.One": 50.0'),
    ("a JSON array", b'["tests.test_sizes.One", 50.0]'),
    ("a JSON string", b'"tests.test_sizes.One"'),
    ("not UTF-8", b'\xff\xfe{"tests.test_sizes.One": 50.0}'),
    ("unreadable (a directory)", DIRECTORY),
    ("none of the collected classes", {"tests.test_gone.Gone": 50.0, "tests.other.One": 9.0}),
    # round 1 F2: json reads this as an int that no float can hold
    ("an integer too large for a float", b'{"tests.test_sizes.One": 1' + b"0" * 400 + b"}"),
    (
        "values that are not seconds",
        b'{"tests.test_sizes.One": "50", "tests.test_sizes.Five": -1,'
        b' "tests.test_sizes.Three": true, "tests.test_sizes.TwoA": null,'
        b' "tests.test_sizes.TwoB": NaN, "tests.test_other.Solo": Infinity,'
        b' "tests.test_other.Pair": [50.0]}',
    ),
)


class Fallback(unittest.TestCase):
    """AC-2: a file that gives no weights leaves the split the runner made before, and the
    run neither fails nor reports the file as a test error."""

    @classmethod
    def setUpClass(cls):
        cls.tree = Tree(test_sizes=module(SIZES), test_other=module(OTHER))
        cls.addClassCleanup(rmtree_force, cls.tree.root)
        cls.everything = cls.tree.listed()
        cls.before = count_split(cls.everything, 3)

    def test_a_usable_file_does_change_the_split_of_this_tree(self):
        # the control of the cases below: here the file decides, so "unchanged" means ignored
        seconds = {cid("test_sizes", name): 1.0 for name, _ in SIZES}
        seconds.update({cid("test_other", "Pair"): 1.0})
        seconds.update({cid("test_sizes", "One"): 50.0, cid("test_other", "Solo"): 40.0})
        self.tree.durations(seconds)
        alone = [{cid("test_sizes", "One")}, {cid("test_other", "Solo")}]
        got = partition(self.tree.parts(3))
        self.assertEqual(len(got), 3)
        for group in alone:
            self.assertIn(frozenset(group), got)
        self.assertNotEqual(got, partition(self.before))

    def test_a_file_that_gives_no_weights_leaves_the_split_by_test_count(self):
        self.assertEqual(len(self.everything), 16)
        for what, data in BAD_FILES:
            with self.subTest(file=what):
                self.tree.durations(data)
                self.assertEqual(self.tree.parts(3), self.before)

    def test_a_bad_file_fails_nothing_and_is_not_a_test_error(self):
        for what, data in BAD_FILES:
            if data is None or data == b"":
                continue
            with self.subTest(file=what):
                self.tree.durations(data)
                code, out, ran = self.tree.run("-j", "2")
                self.assertEqual(code, 0, out)
                ran_line, verdict = summary(out)
                self.assertRegex(ran_line, r"^Ran 16 tests in \d+\.\d+s$")
                self.assertEqual(verdict, "OK")
                self.assertNotIn("Traceback", out)
                self.assertEqual(sorted(ran), sorted(self.everything))
                self.assertEqual({len(pids) for pids in ran.values()}, {1})


class EveryTestOnce(unittest.TestCase):
    """AC-3: with weights, the shards of N still hold every collected test exactly once, in
    whole classes, and the same ids and file give the same split."""

    @classmethod
    def setUpClass(cls):
        cls.tree = Tree(test_sizes=module(SIZES), test_other=module(OTHER))
        cls.addClassCleanup(rmtree_force, cls.tree.root)
        cls.everything = cls.tree.listed()
        # listed, listed with 0 seconds, and unlisted classes together
        cls.tree.durations(
            {
                cid("test_sizes", "Five"): 0.5,
                cid("test_sizes", "Three"): 30.0,
                cid("test_sizes", "TwoA"): 0.0,
                cid("test_other", "Pair"): 12.0,
            }
        )

    def test_the_shards_of_n_hold_every_test_once_in_whole_classes(self):
        self.assertEqual(len(self.everything), 16)
        for n in (1, 2, 3, 6, 8):  # 8: more shards than the 7 classes
            with self.subTest(n=n):
                parts = self.tree.parts(n)
                flat = [tid for part in parts for tid in part]
                self.assertEqual(sorted(flat), sorted(self.everything))  # all, none twice
                partition(parts)  # raises when a class is in two shards

    def test_the_same_ids_and_file_give_the_same_split(self):
        first = self.tree.parts(3)
        self.assertEqual(self.tree.parts(3), first)
        self.assertNotEqual(first, count_split(self.everything, 3))  # the file is in use

    def test_running_the_shards_runs_every_test_once(self):
        parts = self.tree.parts(3)
        seen = Counter()
        for k, part in enumerate(parts, 1):
            code, out, ran = self.tree.run("-j", "2", "--shard", "%d/3" % k)
            with self.subTest(shard=k):
                self.assertEqual(code, 0, out)
                self.assertEqual(sorted(ran), sorted(part))
                self.assertIn("(shard %d of 3)" % k, summary(out)[0])
            for tid, pids in ran.items():
                seen[tid] += len(pids)
        self.assertEqual(seen, Counter(self.everything))


TIMED = (("FastA", 5), ("FastB", 5), ("Slow", 2))
SLEEP = 0.3

# a class whose only test ends its worker before the worker writes its result
CRASH = (
    "import os\n"
    "import unittest\n"
    "\n"
    "\n"
    "class Dies(unittest.TestCase):\n"
    "    def test_dies(self):\n"
    "        os._exit(3)\n"
)


class DurationsOut(TreeCase):
    """AC-4: `--durations-out FILE` writes the seconds of each class the run ran."""

    def setUp(self):
        source = module(TIMED, sleep={"Slow": SLEEP}, record=False)
        self.t = self.tree(test_t=source)
        self.names = sorted(cid("test_t", name) for name, _ in TIMED)
        self.slow = cid("test_t", "Slow")

    def load(self, path):
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        self.assertIsInstance(data, dict)
        for name, value in data.items():
            self.assertNotIsInstance(value, bool, name)
            self.assertIsInstance(value, (int, float), name)
            self.assertTrue(math.isfinite(value) and value >= 0, "%s: %r" % (name, value))
        return data

    def test_the_file_holds_every_class_of_the_run_merged_over_its_workers(self):
        out_file = os.path.join(self.t.root, "measured.json")
        began = time.monotonic()
        code, out, _ = self.t.run("-j", "2", "--durations-out", out_file)
        wall = time.monotonic() - began
        self.assertEqual(code, 0, out)
        self.assertEqual(summary(out)[1], "OK")
        self.assertRegex(out, r"12 tests in 2 worker\(s\)")  # so the file is a merge
        seconds = self.load(out_file)
        self.assertEqual(sorted(seconds), self.names)
        # two tests of 0.3 s each: from the first one's start to the last one's end
        self.assertGreaterEqual(seconds[self.slow], 2 * SLEEP - 0.1)
        for name in self.names:
            self.assertLessEqual(seconds[name], wall + 0.1, name)
            if name != self.slow:  # five tests that do nothing: the class's own time
                self.assertLess(seconds[name], seconds[self.slow], name)

    def test_a_shard_writes_only_its_own_classes(self):
        seen = []
        for k in (1, 2):
            mine = sorted({class_of(tid) for tid in self.t.listed("--shard", "%d/2" % k)})
            self.assertTrue(mine, "shard %d of 2 has no class" % k)
            out_file = os.path.join(self.t.root, "part-%d.json" % k)
            args = ("-j", "2", "--shard", "%d/2" % k, "--durations-out", out_file)
            code, out, _ = self.t.run(*args)
            self.assertEqual(code, 0, out)
            self.assertEqual(sorted(self.load(out_file)), mine)
            seen.extend(mine)
        self.assertEqual(sorted(seen), self.names)

    def test_the_written_file_is_read_back_as_the_weights(self):
        # by test count: FastA and FastB open the two shards, Slow joins the first
        by_count = split_of("test_t", ("FastA", "Slow"), ("FastB",))
        by_time = split_of("test_t", ("Slow",), ("FastA", "FastB"))
        self.assertEqual(partition(self.t.parts(2)), by_count)
        # the refresh the docstring gives: the run writes the file it reads its weights from
        code, out, _ = self.t.run("-j", "2", "--durations-out", self.t.file)
        self.assertEqual(code, 0, out)
        self.assertEqual(sorted(self.load(self.t.file)), self.names)
        self.assertEqual(partition(self.t.parts(2)), by_time)
        # and again, now that the file is there
        code, out, _ = self.t.run("-j", "2", "--durations-out", self.t.file)
        self.assertEqual(code, 0, out)
        self.assertEqual(sorted(self.load(self.t.file)), self.names)
        self.assertEqual(partition(self.t.parts(2)), by_time)

    def test_a_worker_that_leaves_no_result_keeps_the_file_that_was_there(self):
        # round 1 F3: the refresh writes over the file it reads; a run that lost a worker
        # does not know that worker's classes, so it must not replace the file with fewer
        tree = self.tree(test_t=module(TIMED, record=False), test_crash=CRASH)
        before = json.dumps({cid("test_t", "Slow"): 9.0}).encode("utf-8")
        tree.durations(before)
        code, out, _ = tree.run("-j", "2", "--durations-out", tree.file)
        self.assertEqual(code, 1, out)
        self.assertIn("FAILED (errors=1)", out)  # the lost worker, counted as it was before
        with open(tree.file, "rb") as fh:
            self.assertEqual(fh.read(), before)


def matrix_legs():
    """The `include:` entries of the workflow's matrix, as dicts of strings."""
    with open(WORKFLOW, "r", encoding="utf-8") as fh:
        lines = fh.read().splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip() == "include:")
    indent = len(lines[start]) - len(lines[start].lstrip())
    legs = []
    for line in lines[start + 1 :]:
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        if len(line) - len(line.lstrip()) <= indent:
            break
        if text.startswith("- "):
            legs.append({})
            text = text[2:].strip()
        key, _, value = text.partition(":")
        legs[-1][key.strip()] = value.split(" #")[0].strip().strip("\"'")
    return legs


def repo_list(shard_value=None):
    """The ids this repository's suite lists, as a CI job with that shard value would."""
    env = clean_env()
    if shard_value:
        env[VAR] = shard_value
    code, out, err = call(["--list"], ROOT, env)
    if code != 0:
        raise AssertionError("--list (%s): exit %d\n%s" % (shard_value, code, err))
    return lines_of(out)


class Committed(unittest.TestCase):
    """AC-3, AC-5, AC-6: the committed file and the shards CI runs on Windows."""

    @classmethod
    def setUpClass(cls):
        cls.legs = matrix_legs()
        cls.windows = [leg for leg in cls.legs if leg.get("os", "").startswith("windows")]
        cls.values = sorted("%s/%s" % (leg.get("shard"), leg.get("shards")) for leg in cls.windows)
        cls.everything = repo_list()
        cls.parts = [repo_list(value) for value in cls.values]

    def recorded(self):
        with open(COMMITTED, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        self.assertIsInstance(data, dict)
        return data

    def test_the_windows_leg_is_six_shard_jobs_and_ubuntu_stays(self):
        self.assertEqual(self.values, ["%d/6" % k for k in range(1, 7)])
        for leg in self.windows:
            self.assertEqual((leg["os"], leg["python"]), ("windows-latest", "3.x"))
        # macos-ci AC-4 (author's change): the two macOS shard jobs, apart from "others"
        macos = [leg for leg in self.legs if leg.get("os", "").startswith("macos")]
        self.assertEqual(
            sorted(macos, key=lambda leg: leg["shard"]),
            [
                {"os": "macos-latest", "python": "3.x", "shard": "1", "shards": "2"},
                {"os": "macos-latest", "python": "3.x", "shard": "2", "shards": "2"},
            ],
        )
        others = [leg for leg in self.legs if leg not in self.windows and leg not in macos]
        self.assertEqual(
            sorted(others, key=lambda leg: leg["python"]),
            [
                {"os": "ubuntu-latest", "python": "3.11"},
                {"os": "ubuntu-latest", "python": "3.x", "lint": "true"},
            ],
        )

    def test_the_ci_shards_hold_this_suite_once_and_none_is_idle(self):
        self.assertGreater(len(self.everything), 1000)
        flat = [tid for part in self.parts for tid in part]
        self.assertEqual(sorted(flat), sorted(self.everything))
        self.assertEqual([k for k, part in enumerate(self.parts, 1) if not part], [])

    def test_the_committed_file_holds_seconds_for_this_suite(self):
        data = self.recorded()
        for name, value in data.items():
            self.assertRegex(name, r"^tests\.\w+\.\w+$")
            self.assertNotIsInstance(value, bool, name)
            self.assertIsInstance(value, (int, float), name)
            self.assertTrue(math.isfinite(value) and value >= 0, "%s: %r" % (name, value))
        collected = {class_of(tid) for tid in self.everything}
        listed = collected & set(data)
        # a full run of the suite: far more than half of what is collected today
        self.assertGreater(len(listed), len(collected) / 2)
        self.assertGreater(sum(data[name] for name in listed), 0)

    def test_the_ci_shards_are_balanced_by_the_recorded_seconds(self):
        data = self.recorded()
        counts = Counter(class_of(tid) for tid in self.everything)
        listed = {name: float(data[name]) for name in counts if name in data}
        self.assertTrue(listed)
        per_test = statistics.median(listed[name] / counts[name] for name in listed)
        weight = {name: listed.get(name, counts[name] * per_test) for name in counts}
        loads = [sum(weight[name] for name in {class_of(t) for t in part}) for part in self.parts]
        self.assertEqual(len(loads), 6)
        # each class went onto the least loaded shard, so no shard is ahead of another by
        # more than one class; and with this many classes the heaviest is close to the mean
        self.assertLessEqual(max(loads) - min(loads), max(weight.values()) + 1e-6, loads)
        self.assertLessEqual(max(loads), 1.1 * sum(loads) / len(loads), loads)


if __name__ == "__main__":
    unittest.main()
