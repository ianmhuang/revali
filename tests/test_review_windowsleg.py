"""ci/windows-job, AC-1 to AC-3: the Windows CI leg and the merge wait that outlasts it.

The workflow is read from its text with the standard library (no YAML library): its shape
is small and fixed. Expectations come from the acceptance criteria, pyproject.toml (the
pinned interpreter) and the config loader (what `revali merge` gets), not from the workflow.
"""

import importlib
import os
import re
import tomllib
import unittest
from unittest import mock

from revali.config import load_defaults, parse_project_config
from tests.helpers import ROOT

WORKFLOW = (".github", "workflows", "test.yml")
OWN_WAIT_MIN = 75  # AC-2: revali's own revali.toml
OTHERS_WAIT_MIN = 30  # AC-2: defaults.toml and templates/revali.toml
LEG_TESTS = (
    ("tests.test_ci_workflow", "Workflow.test_matrix_legs"),
    ("tests.test_review_ciworkflow", "Matrix.test_three_legs"),
)


def read(*parts):
    with open(os.path.join(ROOT, *parts), "r", encoding="utf-8", newline="") as fh:
        return fh.read()


def indent(line):
    return len(line) - len(line.lstrip())


def code_lines(text):
    return [ln for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]


def under(lines, key):
    """The lines indented deeper than the first `key:` line."""
    for i, ln in enumerate(lines):
        if ln.strip() == key + ":" or ln.strip().startswith(key + ": "):
            out = []
            for nxt in lines[i + 1 :]:
                if indent(nxt) <= indent(ln):
                    break
                out.append(nxt)
            return out
    raise AssertionError("no %r in the workflow" % key)


def scalars(lines):
    """`key: value` pairs at the first level of a mapping block."""
    base = min(indent(ln) for ln in lines)
    out = {}
    for ln in lines:
        m = re.match(r"([\w-]+):\s*(.*)$", ln.strip())
        if m and indent(ln) == base:
            out[m.group(1)] = m.group(2).strip().strip("\"'")
    return out


def items(lines):
    """A sequence block as a list of dicts of each item's `key: value` lines."""
    out = []
    first = None
    for ln in lines:
        stripped = ln.lstrip()
        if first is None and stripped.startswith("- "):
            first = indent(ln)
        if first is not None and indent(ln) == first and stripped.startswith("- "):
            out.append({})
            stripped = stripped[2:]
        m = re.match(r"([\w-]+):\s*(.*)$", stripped)
        if out and m:
            out[-1][m.group(1)] = m.group(2).strip().strip("\"'")
    return out


def without_windows_leg(text):
    """The workflow text with the matrix item that starts `- os: windows-...` removed."""
    out = []
    dropping = None
    for ln in text.splitlines(keepends=True):
        if dropping is not None:
            if not ln.strip() or indent(ln) > dropping:
                continue
            dropping = None
        if re.match(r"-\s+os:\s*[\"']?windows", ln.lstrip()):
            dropping = indent(ln)
            continue
        out.append(ln)
    return "".join(out)


class WorkflowCase(unittest.TestCase):
    def setUp(self):
        self.lines = code_lines(read(*WORKFLOW))
        self.job = under(under(self.lines, "jobs"), "test")
        self.legs = items(under(self.job, "include"))
        self.steps = items(under(self.job, "steps"))
        self.windows = [leg for leg in self.legs if leg.get("os", "").startswith("windows")]

    def suite_steps(self):
        return [s for s in self.steps if s.get("run", "").endswith("tests/run_parallel.py")]


class Legs(WorkflowCase):
    def test_linux_on_the_target_and_latest_windows_on_latest(self):
        # AC-1: exactly three legs; the pinned one is pyproject's target
        target = tomllib.loads(read("pyproject.toml"))["tool"]["black"]["target-version"]
        self.assertEqual(len(target), 1)
        m = re.fullmatch(r"py(\d)(\d+)", target[0])
        pinned = "%s.%s" % (m.group(1), m.group(2))
        self.assertEqual(pinned, "3.11")
        self.assertCountEqual(
            [(leg.get("os"), leg.get("python")) for leg in self.legs],
            [("ubuntu-latest", pinned), ("ubuntu-latest", "3.x"), ("windows-latest", "3.x")],
        )

    def test_lint_stays_on_the_one_ubuntu_leg(self):
        # AC-1: the Windows leg carries no lint marker; the steps that install or run the
        # linters are guarded by a marker only ubuntu / 3.x has
        self.assertEqual(len(self.windows), 1, "one Windows leg")
        self.assertEqual(sorted(self.windows[0]), ["os", "python"])
        markers = {k for leg in self.legs for k in leg if k not in ("os", "python")}
        self.assertEqual(len(markers), 1, "one marker picks the lint leg: %r" % markers)
        flag = markers.pop()
        marked = [leg for leg in self.legs if leg.get(flag) not in (None, "", "false")]
        self.assertEqual([(leg["os"], leg["python"]) for leg in marked], [("ubuntu-latest", "3.x")])
        lint_steps = [s for s in self.steps if re.search(r"\b(ruff|black)\b", s.get("run", ""))]
        self.assertGreaterEqual(len(lint_steps), 1)
        for step in lint_steps:
            with self.subTest(step=step.get("name")):
                self.assertEqual(step.get("if"), "matrix.%s" % flag)

    def test_the_windows_leg_runs_the_suite_and_can_fail_the_run(self):
        # AC-1: one unconditional suite step shared by every leg, nothing that skips a leg
        # or lets it fail quietly, and fail-fast stays false
        self.assertEqual(len(self.windows), 1, "one Windows leg")
        suite = self.suite_steps()
        self.assertEqual(len(suite), 1, "one suite step")
        self.assertNotIn("if", suite[0])
        self.assertNotIn("if", scalars(self.job))
        code = "\n".join(self.lines)
        self.assertNotIn("continue-on-error", code)
        self.assertNotIn("exclude:", code)
        self.assertIn("matrix.os", scalars(self.job).get("runs-on", ""))
        self.assertEqual(scalars(under(self.job, "strategy")).get("fail-fast"), "false")


class Timeouts(WorkflowCase):
    def test_a_hung_leg_is_cancelled_before_merge_stops_waiting(self):
        # AC-2: timeout-minutes on the job (so on every leg), below the checks_timeout_min
        # the loader hands `revali merge` for revali itself, which is 75
        value = scalars(self.job).get("timeout-minutes", "")
        self.assertRegex(value, r"^\d+$", "the test job sets timeout-minutes")
        wait = parse_project_config(read("revali.toml")).merge.checks_timeout_min
        self.assertEqual(wait, OWN_WAIT_MIN)
        self.assertGreater(int(value), 0)
        self.assertLess(int(value), wait)

    def test_no_step_sets_a_longer_timeout_than_the_job(self):
        # AC-2: the job's limit is the one that ends a hung leg
        job = int(scalars(self.job).get("timeout-minutes", "0"))
        self.assertGreater(job, 0, "the test job sets timeout-minutes")
        for step in self.steps:
            if "timeout-minutes" in step:
                with self.subTest(step=step.get("name")):
                    self.assertLessEqual(int(step["timeout-minutes"]), job)

    def test_other_projects_keep_the_30_minute_wait(self):
        # AC-2: only revali's own revali.toml is raised
        self.assertEqual(
            tomllib.loads(read("revali.toml"))["merge"]["checks_timeout_min"], OWN_WAIT_MIN
        )
        self.assertEqual(load_defaults()["merge"]["checks_timeout_min"], OTHERS_WAIT_MIN)
        template = tomllib.loads(read("templates", "revali.toml"))
        self.assertEqual(template["merge"]["checks_timeout_min"], OTHERS_WAIT_MIN)


class LegTests(unittest.TestCase):
    """AC-3: the two tests that pin the legs pass on three legs and fail on two."""

    def run_named(self, module, name):
        suite = unittest.TestLoader().loadTestsFromName("%s.%s" % (module, name))
        result = unittest.TestResult()
        suite.run(result)
        return result

    def test_they_pass_on_the_workflow(self):
        for module, name in LEG_TESTS:
            with self.subTest(test=name):
                result = self.run_named(module, name)
                self.assertEqual(result.testsRun, 1)
                self.assertEqual(result.errors + result.failures, [])

    def test_they_fail_without_the_windows_leg(self):
        text = read(*WORKFLOW)
        two_legs = without_windows_leg(text)
        self.assertNotEqual(two_legs, text, "the workflow has a `- os: windows-...` leg")
        self.assertIn("ubuntu-latest", two_legs)
        for module, name in LEG_TESTS:
            with self.subTest(test=name):
                mod = importlib.import_module(module)
                real = mod.read

                def fake(*parts, _real=real):
                    out = _real(*parts)
                    return two_legs if out == text else out

                with mock.patch.object(mod, "read", fake):
                    result = self.run_named(module, name)
                self.assertEqual(result.testsRun, 1)
                self.assertEqual(result.errors, [])
                self.assertEqual(len(result.failures), 1, "it accepts a two-leg workflow")


if __name__ == "__main__":
    unittest.main()
