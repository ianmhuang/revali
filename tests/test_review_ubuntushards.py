"""ubuntu-shards AC-1, AC-2: each Ubuntu leg of the CI workflow as two shard jobs.

The workflow is read from its text with the standard library (no YAML library).

AC-1: two `ubuntu-latest` jobs for Python 3.11 and two for 3.x, whose `RUN_PARALLEL_SHARD`
is 1/2 and 2/2. Each version's two values are handed to `tests/run_parallel.py --list` on a
throwaway tree: no list is empty, no test is in both, and together they are the whole tree.
Lint is on the 3.x shard 1 job and on no other. The Windows and macOS legs are the six and
three shard jobs they were, thirteen jobs in all.

AC-2: `tests/test_ci_workflow.py` passes on the workflow as committed and reports a failure
on copies whose Ubuntu jobs are another layout (a leg back on the whole suite, a shard
missing or doubled, lint elsewhere); the reviewer tests that read the matrix report a
failure on a copy with the two unsharded jobs of before. `tests/test_review_shardtime.py`
is left out of that: its class lists this repository's suite seven times before its first
test.
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
VERSIONS = ("3.11", "3.x")
ENTRY = re.compile(r"(?m)^ *- os: ubuntu-latest\n(?: +(?!- )\w+: [^\n]+\n)+")
# (python, RUN_PARALLEL_SHARD, lints) of the four Ubuntu jobs
FOUR = [
    ("3.11", "1/2", False),
    ("3.11", "2/2", False),
    ("3.x", "1/2", True),
    ("3.x", "2/2", False),
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


def ubuntu_jobs(text):
    """(python, RUN_PARALLEL_SHARD, lints) of every Ubuntu job, sorted."""
    return sorted(
        (leg.get("python", ""), shard_value(text, leg), leg.get("lint") == "true")
        for leg in on(legs(text), "ubuntu")
    )


def values_of(text, python):
    return sorted(
        shard_value(text, leg) for leg in on(legs(text), "ubuntu") if leg.get("python") == python
    )


class Tree:
    """A throwaway test tree: five modules of two classes, 40 tests in all."""

    TESTS = 40

    def __init__(self, case):
        self.root = os.path.realpath(tempfile.mkdtemp(prefix="revali ubuntu shards "))
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
        """The test ids the suite step's runner collects with this RUN_PARALLEL_SHARD."""
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


class FourUbuntuJobs(unittest.TestCase):
    """AC-1."""

    def setUp(self):
        self.text = workflow()
        self.legs = legs(self.text)
        self.ubuntu = on(self.legs, "ubuntu")

    def test_two_jobs_for_each_python_version_one_shard_each(self):
        self.assertEqual(len(self.ubuntu), 4, self.ubuntu)
        for python in VERSIONS:
            with self.subTest(python=python):
                these = [leg for leg in self.ubuntu if leg.get("python") == python]
                self.assertEqual(
                    sorted((leg.get("shard"), leg.get("shards")) for leg in these),
                    [("1", "2"), ("2", "2")],
                )
                self.assertEqual({leg.get("os") for leg in these}, {"ubuntu-latest"})
                self.assertEqual(values_of(self.text, python), ["1/2", "2/2"])
        self.assertEqual(ubuntu_jobs(self.text), FOUR)

    def test_the_two_jobs_of_each_version_run_every_test_once(self):
        tree = Tree(self)
        whole = tree.listed("")
        self.assertEqual(len(whole), Tree.TESTS, whole)
        self.assertEqual(len(set(whole)), Tree.TESTS)
        for python in VERSIONS:
            with self.subTest(python=python):
                values = values_of(self.text, python)
                self.assertEqual(values, ["1/2", "2/2"])
                parts = [tree.listed(value) for value in values]
                for value, part in zip(values, parts, strict=True):
                    self.assertTrue(part, "the job with %s has nothing to run" % value)
                    self.assertLess(len(part), len(whole), "the job with %s runs it all" % value)
                flat = [tid for part in parts for tid in part]
                self.assertEqual(len(flat), len(set(flat)), "a test is in both jobs")
                self.assertEqual(sorted(flat), sorted(whole))
        # the one suite step runs on every leg and takes its shard from the environment
        suite = [step for step in steps(self.text) if any(SUITE in ln for ln in step)]
        self.assertEqual(len(suite), 1, suite)
        self.assertEqual([ln for ln in suite[0] if ln.startswith("if:")], [])
        self.assertNotIn("--shard", "\n".join(suite[0]))

    def test_lint_runs_on_the_first_3x_job_and_no_other(self):
        lint = [leg for leg in self.legs if "lint" in leg]
        self.assertEqual(
            lint,
            [{"os": "ubuntu-latest", "python": "3.x", "lint": "true", "shard": "1", "shards": "2"}],
        )
        # that job is still one of the two that run the 3.x tests
        self.assertEqual(shard_value(self.text, lint[0]), "1/2")
        self.assertEqual(values_of(self.text, "3.x"), ["1/2", "2/2"])
        lint_steps = [
            step
            for step in steps(self.text)
            if any(re.search(r"\b(ruff|black)\b", ln) for ln in step)
        ]
        self.assertTrue(lint_steps)
        for step in lint_steps:
            with self.subTest(step=step[0]):
                self.assertIn("if: matrix.lint", step)

    def test_the_windows_and_macos_legs_are_what_they_were(self):
        windows = on(self.legs, "windows")
        macos = on(self.legs, "macos")
        self.assertEqual(
            sorted(windows, key=lambda leg: int(leg["shard"])),
            [
                {"os": "windows-latest", "python": "3.x", "shard": str(k), "shards": "6"}
                for k in range(1, 7)
            ],
        )
        self.assertEqual(
            sorted(macos, key=lambda leg: int(leg["shard"])),
            [
                {"os": "macos-latest", "python": "3.x", "shard": str(k), "shards": "3"}
                for k in (1, 2, 3)
            ],
        )
        self.assertEqual(
            sorted(shard_value(self.text, leg) for leg in windows),
            ["%d/6" % k for k in range(1, 7)],
        )
        self.assertEqual(
            sorted(shard_value(self.text, leg) for leg in macos), ["1/3", "2/3", "3/3"]
        )
        # four Ubuntu, six Windows and three macOS jobs, and no other
        self.assertEqual(len(self.ubuntu), 4, self.ubuntu)
        self.assertEqual(len(self.legs), 13, self.legs)


# --- AC-2: copies of the workflow whose Ubuntu jobs are another layout -------------------

A = ("3.11", "1")
B = ("3.11", "2")
C = ("3.x", "1")  # the lint job
D = ("3.x", "2")


def value_of(entry, key):
    m = re.search(r"(?m)^ +%s: ([^\n]+)$" % key, entry)
    return m.group(1).strip().strip("\"'") if m else None


def without(entry, *keys):
    out = entry
    for key in keys:
        out, n = re.subn(r"(?m)^ +%s: [^\n]+\n" % key, "", out)
        if n != 1:
            raise AssertionError("no `%s:` in %r" % (key, entry))
    return out


def whole(entry):
    """The entry as a job on the whole suite: no shard."""
    return without(entry, "shard", "shards")


def with_number(entry, key, value):
    out, n = re.subn(r"(?m)^( +%s: )\d+$" % key, r"\g<1>%d" % value, entry)
    if n != 1:
        raise AssertionError("no `%s:` in %r" % (key, entry))
    return out


def with_lint(entry):
    if value_of(entry, "lint") is not None:
        raise AssertionError("already a lint job: %r" % entry)
    out, n = re.subn(r"(?m)^( +)(python: [^\n]+\n)", r"\g<1>\g<2>\g<1>lint: true\n", entry)
    if n != 1:
        raise AssertionError("no `python:` in %r" % entry)
    return out


def rewritten(text, change):
    """`text` with its Ubuntu matrix entries, by (python, shard), replaced by
    `change(entries)`."""
    found = list(ENTRY.finditer(text))
    if not found:
        raise AssertionError("no Ubuntu entries in the matrix")
    for before, after in zip(found, found[1:], strict=False):
        if before.end() != after.start():
            raise AssertionError("the Ubuntu entries are not next to each other")
    entries = {}
    for m in found:
        key = (value_of(m.group(0), "python"), value_of(m.group(0), "shard"))
        if key in entries:
            raise AssertionError("two Ubuntu entries for %r" % (key,))
        entries[key] = m.group(0)
    return text[: found[0].start()] + "".join(change(entries)) + text[found[-1].end() :]


def the_two_jobs_of_before(e):
    return [whole(e[A]), whole(e[C])]


BROKEN = {
    "the two unsharded jobs of before": (
        the_two_jobs_of_before,
        [("3.11", "", False), ("3.x", "", True)],
    ),
    "the 3.11 leg as one unsharded job": (
        lambda e: [whole(e[A]), e[C], e[D]],
        [("3.11", "", False), ("3.x", "1/2", True), ("3.x", "2/2", False)],
    ),
    "the 3.x leg as one unsharded job": (
        lambda e: [e[A], e[B], whole(e[C])],
        [("3.11", "1/2", False), ("3.11", "2/2", False), ("3.x", "", True)],
    ),
    "the second 3.11 job missing": (
        lambda e: [e[A], e[C], e[D]],
        [("3.11", "1/2", False), ("3.x", "1/2", True), ("3.x", "2/2", False)],
    ),
    "the second 3.x job missing": (
        lambda e: [e[A], e[B], e[C]],
        [("3.11", "1/2", False), ("3.11", "2/2", False), ("3.x", "1/2", True)],
    ),
    "the second 3.11 job on the whole suite": (
        lambda e: [e[A], whole(e[B]), e[C], e[D]],
        [("3.11", "", False), ("3.11", "1/2", False), ("3.x", "1/2", True), ("3.x", "2/2", False)],
    ),
    "both 3.x jobs on shard 1": (
        lambda e: [e[A], e[B], e[C], with_number(e[D], "shard", 1)],
        [
            ("3.11", "1/2", False),
            ("3.11", "2/2", False),
            ("3.x", "1/2", False),
            ("3.x", "1/2", True),
        ],
    ),
    # shards 1, 1, 2, 2 over the four jobs, but neither version runs both halves
    "both 3.11 jobs on shard 2 and both 3.x jobs on shard 1": (
        lambda e: [with_number(e[A], "shard", 2), e[B], e[C], with_number(e[D], "shard", 1)],
        [
            ("3.11", "2/2", False),
            ("3.11", "2/2", False),
            ("3.x", "1/2", False),
            ("3.x", "1/2", True),
        ],
    ),
    "the 3.11 jobs as two of three shards": (
        lambda e: [with_number(e[A], "shards", 3), with_number(e[B], "shards", 3), e[C], e[D]],
        [
            ("3.11", "1/3", False),
            ("3.11", "2/3", False),
            ("3.x", "1/2", True),
            ("3.x", "2/2", False),
        ],
    ),
    "the second 3.x job twice": (
        lambda e: [e[A], e[B], e[C], e[D], e[D]],
        FOUR + [("3.x", "2/2", False)],
    ),
    "lint on both 3.x jobs": (
        lambda e: [e[A], e[B], e[C], with_lint(e[D])],
        [
            ("3.11", "1/2", False),
            ("3.11", "2/2", False),
            ("3.x", "1/2", True),
            ("3.x", "2/2", True),
        ],
    ),
    "lint on the second 3.x job only": (
        lambda e: [e[A], e[B], without(e[C], "lint"), with_lint(e[D])],
        [
            ("3.11", "1/2", False),
            ("3.11", "2/2", False),
            ("3.x", "1/2", False),
            ("3.x", "2/2", True),
        ],
    ),
    "lint on the first 3.11 job": (
        lambda e: [with_lint(e[A]), e[B], without(e[C], "lint"), e[D]],
        [
            ("3.11", "1/2", True),
            ("3.11", "2/2", False),
            ("3.x", "1/2", False),
            ("3.x", "2/2", False),
        ],
    ),
    "no lint job": (
        lambda e: [e[A], e[B], without(e[C], "lint"), e[D]],
        [
            ("3.11", "1/2", False),
            ("3.11", "2/2", False),
            ("3.x", "1/2", False),
            ("3.x", "2/2", False),
        ],
    ),
}
READERS = (
    ("tests.test_review_cishards", "Wiring"),
    ("tests.test_review_ciworkflow", "Matrix"),
    ("tests.test_review_windowsleg", "Legs"),
    ("tests.test_review_macosleg", "MacosLeg"),
    ("tests.test_review_macosthree", "ThreeMacosJobs"),
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


def not_ubuntu(text):
    return [leg for leg in legs(text) if not leg.get("os", "").startswith("ubuntu")]


class WorkflowTestsCheckTheFourJobs(unittest.TestCase):
    """AC-2."""

    def setUp(self):
        self.good = workflow()
        self.assertEqual(ubuntu_jobs(self.good), FOUR)

    def test_the_authors_tests_accept_the_four_jobs_and_no_other_layout(self):
        ran, failed, hits = run_on("tests.test_ci_workflow", "Workflow", self.good)
        self.assertGreater(ran, 0)
        self.assertGreater(hits, 0, "the workflow tests read the workflow through open()")
        self.assertEqual(failed, [], "they pass on the workflow as committed")
        for what, (change, jobs) in BROKEN.items():
            with self.subTest(what=what):
                text = rewritten(self.good, change)
                self.assertNotEqual(text, self.good)
                self.assertEqual(ubuntu_jobs(text), sorted(jobs))
                # only the Ubuntu entries differ
                self.assertEqual(not_ubuntu(text), not_ubuntu(self.good))
                ran, failed, hits = run_on("tests.test_ci_workflow", "Workflow", text)
                self.assertGreater(hits, 0)
                self.assertTrue(failed, "no workflow test noticed: %s" % what)

    def test_the_reviewer_tests_refuse_the_two_unsharded_jobs_of_before(self):
        text = rewritten(self.good, the_two_jobs_of_before)
        self.assertEqual(ubuntu_jobs(text), [("3.11", "", False), ("3.x", "", True)])
        self.assertEqual(not_ubuntu(text), not_ubuntu(self.good))
        for module_name, class_name in READERS:
            with self.subTest(module=module_name):
                ran, failed, hits = run_on(module_name, class_name, text)
                self.assertGreater(ran, 0)
                self.assertGreater(hits, 0, "it reads the workflow through open()")
                self.assertTrue(failed, "%s accepts two unsharded Ubuntu jobs" % class_name)


if __name__ == "__main__":
    unittest.main()
