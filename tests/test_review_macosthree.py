"""macos-three-shards AC-1, AC-2: the macOS leg of the CI workflow as three shard jobs.

The workflow is read from its text with the standard library (no YAML library).

AC-1: three `macos-latest` / Python `3.x` jobs whose `RUN_PARALLEL_SHARD` is 1/3, 2/3 and
3/3. The three values are handed to `tests/run_parallel.py --list` on a throwaway tree: no
list is empty, no test is in two lists, and together they are the whole tree. The Linux and
Windows legs and the coreutils step are what they were before the change.

AC-2: `tests/test_ci_workflow.py` passes on the workflow as committed and reports a failure
on a copy whose macOS leg is not shards 1..3 of 3; the reviewer tests that read the matrix
report a failure on a copy with the two jobs of before. `tests/test_review_shardtime.py` is
left out of that: its class lists this repository's suite seven times before its first test.
"""

import importlib
import io
import os
import re
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from tests.helpers import ROOT, rmtree_force

WORKFLOW = os.path.join(ROOT, ".github", "workflows", "test.yml")
RUNNER = os.path.join(ROOT, "tests", "run_parallel.py")
SUITE = "tests/run_parallel.py"
SHARD_ENV = "RUN_PARALLEL_SHARD"
ENTRY = re.compile(r"(?m)^ *- os: macos-latest\n(?: +(?!- )\w+: [^\n]+\n)+")
COREUTILS = [
    "- name: GNU coreutils",
    "if: runner.os == 'macOS'",
    "run: |",
    "brew install coreutils",
    'echo "$(brew --prefix coreutils)/libexec/gnubin" >> "$GITHUB_PATH"',
]


def workflow():
    with open(WORKFLOW, "r", encoding="utf-8", newline="") as fh:
        return fh.read().replace("\r\n", "\n")


def indent(line):
    return len(line) - len(line.lstrip())


def under(text, key):
    """The lines below the first `key:` line that are indented deeper than it, no comments."""
    lines = text.split("\n")
    start = next((i for i, ln in enumerate(lines) if ln.strip() == key + ":"), None)
    if start is None:
        raise AssertionError("no `%s:` in the workflow" % key)
    out = []
    for ln in lines[start + 1 :]:
        if not ln.strip() or ln.strip().startswith("#"):
            continue
        if indent(ln) <= indent(lines[start]):
            break
        out.append(ln)
    return out


def legs(text):
    """The matrix `include` entries as dicts of strings."""
    out = []
    for ln in under(text, "include"):
        body = ln.strip()
        if body.startswith("- "):
            out.append({})
            body = body[2:].strip()
        if not out:
            raise AssertionError("`include:` does not start with a list item: %r" % ln)
        key, _, value = body.partition(":")
        out[-1][key.strip()] = value.split(" #")[0].strip().strip("\"'")
    return out


def steps(text):
    """The job's steps, each as its stripped lines."""
    lines = under(text, "steps")
    dash = indent(lines[0])
    out = []
    for ln in lines:
        if indent(ln) == dash and ln.strip().startswith("- "):
            out.append([])
        out[-1].append(ln.strip())
    return out


def on(some, system):
    return [leg for leg in some if leg.get("os", "").startswith(system)]


def shard_value(text, leg):
    """What the job's RUN_PARALLEL_SHARD expression gives this leg."""
    m = re.search(r"(?m)^\s*%s:\s*\$\{\{\s*(.+?)\s*\}\}\s*$" % SHARD_ENV, text)
    if not m:
        raise AssertionError("the job sets no %s from an expression" % SHARD_ENV)
    shape = (
        r"matrix\.shard\s*&&\s*format\('\{0\}/\{1\}',\s*matrix\.shard,\s*matrix\.shards\)"
        r"\s*\|\|\s*''"
    )
    if not re.fullmatch(shape, m.group(1)):
        raise AssertionError("an expression this test cannot evaluate: %r" % m.group(1))
    if not leg.get("shard"):
        return ""
    return "%s/%s" % (leg["shard"], leg.get("shards", ""))


def macos_values(text):
    return sorted(shard_value(text, leg) for leg in on(legs(text), "macos"))


class Tree:
    """A throwaway test tree: five modules of two classes, 40 tests in all."""

    TESTS = 40

    def __init__(self, case):
        self.root = os.path.realpath(tempfile.mkdtemp(prefix="revali macos three "))
        case.addCleanup(rmtree_force, self.root)
        self.start = os.path.join(self.root, "suite")
        os.makedirs(self.start)
        self.write("__init__.py", "")
        for m in range(5):
            body = "import unittest\n"
            for c in range(2):
                body += "\n\nclass C%d(unittest.TestCase):\n" % c
                for t in range(m + 2 * c + 1):
                    body += "    def test_%d(self):\n        pass\n\n" % t
            self.write("test_m%d.py" % m, body)

    def write(self, name, text):
        with open(os.path.join(self.start, name), "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)

    def listed(self, value):
        env = dict(os.environ)
        env.pop(SHARD_ENV, None)
        if value:
            env[SHARD_ENV] = value
        res = subprocess.run(
            [sys.executable, RUNNER, "-s", self.start, "-t", self.root, "--list"],
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=300,
        )
        if res.returncode != 0:
            raise AssertionError("--list with %r: %s%s" % (value, res.stdout, res.stderr))
        return res.stdout.split()


class ThreeMacosJobs(unittest.TestCase):
    """AC-1."""

    def setUp(self):
        self.text = workflow()
        self.legs = legs(self.text)
        self.macos = on(self.legs, "macos")

    def test_three_jobs_on_the_latest_macos_and_python_one_shard_each(self):
        self.assertEqual(len(self.macos), 3, self.legs)
        self.assertEqual(
            sorted(self.macos, key=lambda leg: leg.get("shard", "")),
            [
                {"os": "macos-latest", "python": "3.x", "shard": str(k), "shards": "3"}
                for k in (1, 2, 3)
            ],
        )
        self.assertEqual(macos_values(self.text), ["1/3", "2/3", "3/3"])

    def test_the_three_values_run_every_test_once(self):
        values = macos_values(self.text)
        self.assertEqual(values, ["1/3", "2/3", "3/3"])
        tree = Tree(self)
        whole = tree.listed("")
        self.assertEqual(len(whole), Tree.TESTS, whole)
        self.assertEqual(len(set(whole)), Tree.TESTS)
        parts = [tree.listed(value) for value in values]
        for value, part in zip(values, parts, strict=True):
            with self.subTest(value=value):
                self.assertTrue(part, "the job with %s has nothing to run" % value)
                self.assertLess(len(part), len(whole), "the job with %s runs it all" % value)
        flat = [tid for part in parts for tid in part]
        self.assertEqual(len(flat), len(set(flat)), "a test is in two jobs")
        self.assertEqual(sorted(flat), sorted(whole))
        # the one suite step runs on every leg and takes its shard from the environment
        suite = [step for step in steps(self.text) if any(SUITE in ln for ln in step)]
        self.assertEqual(len(suite), 1, suite)
        self.assertEqual([ln for ln in suite[0] if ln.startswith("if:")], [])
        self.assertNotIn("--shard", "\n".join(suite[0]))

    def test_the_other_legs_and_the_coreutils_step_are_what_they_were(self):
        self.assertEqual(len(self.macos), 3, self.legs)
        ubuntu = on(self.legs, "ubuntu")
        windows = on(self.legs, "windows")
        self.assertEqual(
            sorted(ubuntu, key=lambda leg: leg["python"]),
            [
                {"os": "ubuntu-latest", "python": "3.11"},
                {"os": "ubuntu-latest", "python": "3.x", "lint": "true"},
            ],
        )
        self.assertEqual(
            sorted(windows, key=lambda leg: int(leg["shard"])),
            [
                {"os": "windows-latest", "python": "3.x", "shard": str(k), "shards": "6"}
                for k in range(1, 7)
            ],
        )
        self.assertEqual([shard_value(self.text, leg) for leg in ubuntu], ["", ""])
        self.assertEqual(
            sorted(shard_value(self.text, leg) for leg in windows),
            ["%d/6" % k for k in range(1, 7)],
        )
        # two Ubuntu, six Windows and three macOS jobs, and no other
        self.assertEqual(len(self.legs), 11, self.legs)
        every = steps(self.text)
        brew = [i for i, step in enumerate(every) if any("brew " in ln for ln in step)]
        self.assertEqual(len(brew), 1, every)
        self.assertEqual(every[brew[0]], COREUTILS)
        suite = [i for i, step in enumerate(every) if any(SUITE in ln for ln in step)]
        self.assertEqual(len(suite), 1, every)
        self.assertLess(brew[0], suite[0], "coreutils is on PATH before the suite runs")


# --- AC-2: copies of the workflow whose macOS leg is not shards 1..3 of 3 ----------------


def shard_of(entry):
    m = re.search(r"(?m)^ +shard: (\d+)$", entry)
    if not m:
        raise AssertionError("no `shard:` in %r" % entry)
    return int(m.group(1))


def with_field(entry, key, value):
    out, n = re.subn(r"(?m)^( +%s: )\d+$" % key, r"\g<1>%d" % value, entry)
    if n != 1:
        raise AssertionError("no `%s:` in %r" % (key, entry))
    return out


def rewritten(text, change):
    """`text` with its macOS matrix entries, in shard order, replaced by `change(entries)`."""
    found = list(ENTRY.finditer(text))
    if not found:
        raise AssertionError("no macOS entries in the matrix")
    for before, after in zip(found, found[1:], strict=False):
        if before.end() != after.start():
            raise AssertionError("the macOS entries are not next to each other")
    entries = sorted((m.group(0) for m in found), key=shard_of)
    return text[: found[0].start()] + "".join(change(entries)) + text[found[-1].end() :]


def the_two_jobs_of_before(entries):
    return [with_field(entry, "shards", 2) for entry in entries[:2]]


def the_third_job_missing(entries):
    return entries[:2]


def the_third_job_on_shard_one(entries):
    return entries[:2] + [with_field(entries[2], "shard", 1)]


def three_jobs_of_four_shards(entries):
    return [with_field(entry, "shards", 4) for entry in entries]


def the_third_job_twice(entries):
    return entries + entries[2:]


BROKEN = {
    "the two jobs of before": (the_two_jobs_of_before, ["1/2", "2/2"]),
    "the third job missing": (the_third_job_missing, ["1/3", "2/3"]),
    "the third job on shard 1": (the_third_job_on_shard_one, ["1/3", "1/3", "2/3"]),
    "three jobs of four shards": (three_jobs_of_four_shards, ["1/4", "2/4", "3/4"]),
    "the third job twice": (the_third_job_twice, ["1/3", "2/3", "3/3", "3/3"]),
}
READERS = (
    ("tests.test_review_macosleg", "MacosLeg"),
    ("tests.test_review_cishards", "Wiring"),
    ("tests.test_review_ciworkflow", "Matrix"),
    ("tests.test_review_windowsleg", "Legs"),
)


def run_on(module_name, class_name, text):
    """(tests run, the failed ones, reads of the workflow) of a test class of this
    repository, the workflow file reading as `text`."""
    case = getattr(importlib.import_module(module_name), class_name, None)
    if case is None:
        raise AssertionError("no class %s in %s" % (class_name, module_name))
    target = os.path.normcase(os.path.abspath(WORKFLOW))
    real = open
    hits = []

    def fake(file, mode="r", *args, **kwargs):
        if isinstance(file, (str, os.PathLike)):
            if os.path.normcase(os.path.abspath(os.fspath(file))) == target:
                hits.append(mode)
                return io.BytesIO(text.encode("utf-8")) if "b" in mode else io.StringIO(text)
        return real(file, mode, *args, **kwargs)

    suite = unittest.defaultTestLoader.loadTestsFromTestCase(case)
    result = unittest.TestResult()
    with mock.patch("builtins.open", fake):
        suite.run(result)
    failed = [str(test) for test, _text in result.failures + result.errors]
    return result.testsRun, failed, len(hits)


class WorkflowTestsCheckTheThreeJobs(unittest.TestCase):
    """AC-2."""

    def setUp(self):
        self.good = workflow()
        self.assertEqual(macos_values(self.good), ["1/3", "2/3", "3/3"])

    def test_the_authors_tests_accept_the_three_jobs_and_nothing_else(self):
        ran, failed, hits = run_on("tests.test_ci_workflow", "Workflow", self.good)
        self.assertGreater(ran, 0)
        self.assertGreater(hits, 0, "the workflow tests read the workflow through open()")
        self.assertEqual(failed, [], "they pass on the workflow as committed")
        for what, (change, values) in BROKEN.items():
            with self.subTest(what=what):
                text = rewritten(self.good, change)
                self.assertNotEqual(text, self.good)
                self.assertEqual(macos_values(text), values)
                # only the macOS entries differ
                self.assertEqual(
                    [leg for leg in legs(text) if leg.get("os") != "macos-latest"],
                    [leg for leg in legs(self.good) if leg.get("os") != "macos-latest"],
                )
                ran, failed, hits = run_on("tests.test_ci_workflow", "Workflow", text)
                self.assertTrue(failed, "no workflow test noticed: %s" % what)

    def test_the_reviewer_tests_refuse_the_two_jobs_of_before(self):
        text = rewritten(self.good, the_two_jobs_of_before)
        self.assertEqual(macos_values(text), ["1/2", "2/2"])
        for module_name, class_name in READERS:
            with self.subTest(module=module_name):
                ran, failed, hits = run_on(module_name, class_name, text)
                self.assertGreater(ran, 0)
                self.assertGreater(hits, 0, "it reads the workflow through open()")
                self.assertTrue(failed, "%s accepts two macOS jobs" % class_name)


if __name__ == "__main__":
    unittest.main()
