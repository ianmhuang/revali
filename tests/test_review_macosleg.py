"""macos-ci AC-2, AC-4: the macOS leg of the CI workflow, and the workflow tests' hold on it.

The workflow is read from its text with the standard library (no YAML library).

AC-2: two `macos-latest` / Python `3.x` jobs, shard 1 of 2 and 2 of 2 (macos-three-shards
AC-1, author's change: three jobs, 1 of 3 to 3 of 3). The values the job's
`RUN_PARALLEL_SHARD` takes on them are handed to `tests/run_parallel.py --list` on a
throwaway tree, where the lists must be the whole tree with nothing twice. One step,
on macOS only and before the suite, installs coreutils with Homebrew and adds its `gnubin`
to the PATH of the later steps. The Linux and Windows legs are what they were.

AC-4: `tests/test_ci_workflow.py` passes on the workflow as committed and fails on a copy
whose macOS leg is broken (no macOS jobs, every job on one shard, coreutils after the suite,
coreutils on every system).
"""

import os
import re
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from tests.helpers import ROOT, rmtree_force

WORKFLOW = (".github", "workflows", "test.yml")
RUNNER = os.path.join(ROOT, "tests", "run_parallel.py")
SUITE = "tests/run_parallel.py"
SHARD_ENV = "RUN_PARALLEL_SHARD"


def read(*parts):
    with open(os.path.join(ROOT, *parts), "r", encoding="utf-8", newline="") as fh:
        return fh.read().replace("\r\n", "\n")


def indent(line):
    return len(line) - len(line.lstrip())


def section(text, key):
    """The lines under the first `key:` line that are indented deeper than it."""
    lines = text.split("\n")
    start = next(i for i, ln in enumerate(lines) if ln.strip() == key + ":")
    out = []
    for ln in lines[start + 1 :]:
        if not ln.strip():
            continue
        if indent(ln) <= indent(lines[start]):
            break
        out.append(ln)
    return out


def items(lines):
    """A YAML list of flat mappings as dicts of strings; a block scalar is joined by \\n."""
    out, key, key_indent = [], None, 0
    for ln in lines:
        if key is not None and indent(ln) > key_indent:
            out[-1][key] = (out[-1][key] + "\n" + ln.strip()).strip()
            continue
        key = None
        body = ln.strip()
        if body.startswith("#"):
            continue
        here = indent(ln)
        if body.startswith("- "):
            out.append({})
            body = body[2:].strip()
            here += 2
        name, _, value = body.partition(":")
        name, value = name.strip(), value.strip()
        if value in ("", "|", "|-", ">", ">-"):
            key, key_indent, value = name, here, ""
        out[-1][name] = value.strip("\"'") if name != "if" else value
    return out


def matrix_legs(text):
    return items(section(text, "include"))


def steps(text):
    return items(section(text, "steps"))


def on(legs, system):
    return [leg for leg in legs if leg.get("os", "").startswith(system)]


def shard_value(text, leg):
    """What the job's RUN_PARALLEL_SHARD expression gives this leg."""
    m = re.search(r"^\s*%s:\s*\$\{\{(.+?)\}\}\s*$" % SHARD_ENV, text, re.M)
    if not m:
        raise AssertionError("the job sets no %s from an expression" % SHARD_ENV)
    expr = m.group(1).strip()
    shape = (
        r"matrix\.shard\s*&&\s*format\('([^']*)',\s*matrix\.(\w+),\s*matrix\.(\w+)\)"
        r"\s*\|\|\s*''"
    )
    m = re.fullmatch(shape, expr)
    if not m:
        raise AssertionError("an expression this test cannot evaluate: %r" % expr)
    if not leg.get("shard"):
        return ""
    return m.group(1).replace("{0}", leg[m.group(2)]).replace("{1}", leg[m.group(3)])


class Tree:
    """A throwaway test tree: four modules of three classes, 42 tests in all."""

    def __init__(self, case):
        self.root = os.path.realpath(tempfile.mkdtemp(prefix="revali macos leg "))
        case.addCleanup(rmtree_force, self.root)
        self.start = os.path.join(self.root, "suite")
        os.makedirs(self.start)
        self.write("__init__.py", "")
        for m in range(4):
            body = "import unittest\n"
            for c in range(3):
                body += "\n\nclass C%d(unittest.TestCase):\n" % c
                for t in range(m + c + 1):
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


class MacosLeg(unittest.TestCase):
    def setUp(self):
        self.text = read(*WORKFLOW)
        self.legs = matrix_legs(self.text)
        self.steps = steps(self.text)
        self.macos = on(self.legs, "macos")

    # macos-three-shards AC-2 (author's change): renamed from
    # test_two_jobs_on_the_latest_macos_and_python
    def test_three_jobs_on_the_latest_macos_and_python(self):
        # macos-three-shards AC-2 (author's change): was 2
        self.assertEqual(len(self.macos), 3, self.legs)
        for leg in self.macos:
            with self.subTest(leg=leg):
                self.assertEqual((leg.get("os"), leg.get("python")), ("macos-latest", "3.x"))
                self.assertNotIn("lint", leg)
        self.assertEqual(
            sorted((leg.get("shard"), leg.get("shards")) for leg in self.macos),
            # macos-three-shards AC-2 (author's change): was 1 and 2 of 2
            [("1", "3"), ("2", "3"), ("3", "3")],
        )

    # macos-three-shards AC-2 (author's change): renamed from
    # test_the_two_jobs_run_every_test_once
    def test_the_three_jobs_run_every_test_once(self):
        values = sorted(shard_value(self.text, leg) for leg in self.macos)
        # macos-three-shards AC-2 (author's change): was ["1/2", "2/2"]
        self.assertEqual(values, ["1/3", "2/3", "3/3"])
        tree = Tree(self)
        whole = tree.listed("")
        self.assertEqual(len(whole), 42, whole)
        self.assertEqual(len(set(whole)), 42)
        parts = [tree.listed(value) for value in values]
        for value, part in zip(values, parts, strict=True):
            self.assertTrue(part, "the job with %s has nothing to run" % value)
        # macos-three-shards AC-2 (author's change): every part, not the first two
        self.assertEqual(sorted(sum(parts, [])), sorted(whole))
        # the suite step reads the value from the environment and runs on every leg
        suite = [step for step in self.steps if SUITE in step.get("run", "")]
        self.assertEqual(len(suite), 1, self.steps)
        self.assertNotIn("if", suite[0])
        self.assertNotIn("--shard", suite[0]["run"])

    def test_coreutils_on_macos_only_and_on_path_before_the_suite(self):
        brew = [
            i
            for i, step in enumerate(self.steps)
            if re.search(r"\bbrew\s+install\b[^\n]*\bcoreutils\b", step.get("run", ""))
        ]
        self.assertEqual(len(brew), 1, self.steps)
        step = self.steps[brew[0]]
        suite = [i for i, other in enumerate(self.steps) if SUITE in other.get("run", "")]
        self.assertEqual(len(suite), 1, self.steps)
        self.assertLess(brew[0], suite[0])
        # only the macOS jobs run it
        condition = step.get("if", "")
        self.assertRegex(condition, r"(?i)\bmacos\b")
        self.assertNotIn("!", condition)
        self.assertNotIn("||", condition)
        # a later step sees what a step appends to $GITHUB_PATH, not what it exports
        self.assertRegex(step["run"], r"gnubin[^\n]*>>\s*[\"']?\$\{?GITHUB_PATH\b")
        self.assertRegex(step["run"], r"brew --prefix[^\n]*coreutils|/opt/[^\n]*coreutils")
        # nothing else in the workflow uses Homebrew
        self.assertEqual(len([s for s in self.steps if "brew " in s.get("run", "")]), 1)

    def test_the_linux_and_windows_legs_are_what_they_were(self):
        ubuntu = on(self.legs, "ubuntu")
        windows = on(self.legs, "windows")
        # ubuntu-shards AC-2 (author's change): two shard jobs per Python version (was one
        # job each, unsharded)
        self.assertEqual(
            sorted(ubuntu, key=lambda leg: (leg["python"], leg["shard"])),
            [
                {"os": "ubuntu-latest", "python": "3.11", "shard": "1", "shards": "2"},
                {"os": "ubuntu-latest", "python": "3.11", "shard": "2", "shards": "2"},
                {
                    "os": "ubuntu-latest",
                    "python": "3.x",
                    "lint": "true",
                    "shard": "1",
                    "shards": "2",
                },
                {"os": "ubuntu-latest", "python": "3.x", "shard": "2", "shards": "2"},
            ],
        )
        self.assertEqual(
            sorted(windows, key=lambda leg: int(leg["shard"])),
            [
                {"os": "windows-latest", "python": "3.x", "shard": str(k), "shards": "6"}
                for k in range(1, 7)
            ],
        )
        # ubuntu-shards AC-2 (author's change): was ["", ""]
        self.assertEqual(
            [shard_value(self.text, leg) for leg in ubuntu], ["1/2", "2/2", "1/2", "2/2"]
        )
        self.assertEqual(
            sorted(shard_value(self.text, leg) for leg in windows),
            ["%d/6" % k for k in range(1, 7)],
        )
        # and the macOS jobs are the only other ones
        self.assertEqual(len(self.legs), len(ubuntu) + len(windows) + len(self.macos))
        # macos-three-shards AC-2 (author's change): was 10;
        # ubuntu-shards AC-2 (author's change): was 11
        self.assertEqual(len(self.legs), 13)

    def test_lint_stays_on_the_one_ubuntu_leg(self):
        self.assertEqual(len(self.macos), 3)  # macos-three-shards AC-2 (author's change)
        lint = [leg for leg in self.legs if "lint" in leg]
        self.assertEqual(
            [(leg["os"], leg["python"], leg["lint"]) for leg in lint],
            [("ubuntu-latest", "3.x", "true")],
        )
        lint_steps = [
            step for step in self.steps if re.search(r"\b(ruff|black)\b", step.get("run", ""))
        ]
        self.assertTrue(lint_steps)
        for step in lint_steps:
            self.assertEqual(step.get("if"), "matrix.lint", step)


ENTRY = r"(?m)^ *- os: macos-latest\n(?: +(?!- )\w+: [^\n]+\n)+"


def without_macos_legs(text):
    return re.sub(ENTRY, "", text)


def macos_jobs_on_one_shard(text):
    return re.sub(ENTRY, lambda m: re.sub(r"(shard: )\d+", r"\g<1>1", m.group(0)), text)


def step_chunks(text):
    """(the text up to the first step, the steps as texts), which join back to `text`."""
    lines = text.split("\n")
    start = next(i for i, ln in enumerate(lines) if ln.strip() == "steps:")
    rest = lines[start + 1 :]
    dash = indent(next(ln for ln in rest if ln.strip()))
    chunks = []
    for ln in rest:
        if ln.strip().startswith("- ") and indent(ln) == dash:
            chunks.append([])
        chunks[-1].append(ln)
    return "\n".join(lines[: start + 1]) + "\n", ["\n".join(chunk) for chunk in chunks]


def with_steps(text, change):
    head, chunks = step_chunks(text)
    last = chunks[-1].rstrip("\n")
    tail = chunks[-1][len(last) :]
    chunks[-1] = last
    brew = next(i for i, chunk in enumerate(chunks) if "brew install" in chunk)
    suite = next(i for i, chunk in enumerate(chunks) if SUITE in chunk)
    return head + "\n".join(change(chunks, brew, suite)) + tail


def coreutils_after_the_suite(text):
    def change(chunks, brew, suite):
        moved = [chunk for i, chunk in enumerate(chunks) if i != brew]
        at = moved.index(chunks[suite]) + 1
        return moved[:at] + [chunks[brew]] + moved[at:]

    return with_steps(text, change)


def coreutils_on_every_system(text):
    def change(chunks, brew, _suite):
        kept = [ln for ln in chunks[brew].split("\n") if not ln.strip().startswith("if:")]
        return chunks[:brew] + ["\n".join(kept)] + chunks[brew + 1 :]

    return with_steps(text, change)


class WorkflowTestsHoldTheLeg(unittest.TestCase):
    """AC-4, on tests/test_ci_workflow.py: its `Workflow` class run on a given text."""

    def problems(self, text):
        import tests.test_ci_workflow as module

        real = module.read

        def fake(*parts):
            if parts and parts[-1] == "test.yml":
                return text
            return real(*parts)

        suite = unittest.defaultTestLoader.loadTestsFromTestCase(module.Workflow)
        result = unittest.TestResult()
        with mock.patch.object(module, "read", fake):
            suite.run(result)
        self.assertGreater(result.testsRun, 0)
        return [str(test) for test, _text in result.failures + result.errors]

    def test_the_committed_workflow_is_accepted(self):
        # macos-three-shards AC-2 (author's change): was 2
        self.assertEqual(len(on(matrix_legs(read(*WORKFLOW)), "macos")), 3)
        self.assertEqual(self.problems(read(*WORKFLOW)), [])

    def test_a_broken_macos_leg_is_caught(self):
        good = read(*WORKFLOW)
        broken = {
            "no macOS jobs": without_macos_legs,
            # macos-three-shards AC-2 (author's change): was "both macOS jobs on shard 1"
            "every macOS job on shard 1": macos_jobs_on_one_shard,
            "coreutils after the suite": coreutils_after_the_suite,
            "coreutils on every system": coreutils_on_every_system,
        }
        for what, make in broken.items():
            with self.subTest(what=what):
                text = make(good)
                self.assertNotEqual(text, good, "the text was not changed")
                self.assertIn("steps:", text)
                self.assertTrue(self.problems(text), "no workflow test noticed: %s" % what)


if __name__ == "__main__":
    unittest.main()
