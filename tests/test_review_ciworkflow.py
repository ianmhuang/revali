"""feature/ci, AC-1 to AC-4 and AC-6: the GitHub Actions workflow, read from its text.

No YAML library: the file is small and its shape is fixed, so top-level blocks, sequence
items and `key: value` lines are read with the standard library. Every expectation comes
from the acceptance criteria, pyproject.toml (the 3.11 leg), revali.toml (the lint line)
and README's Development section (the suite command), not from the workflow itself.
"""

import os
import re
import tomllib
import unittest

from tests.helpers import ROOT

WORKFLOW = os.path.join(".github", "workflows", "test.yml")


def read(*parts):
    with open(os.path.join(ROOT, *parts), "r", encoding="utf-8", newline="") as fh:
        return fh.read()


def code_lines(text):
    """The workflow without comment lines."""
    return [ln for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]


def indent(line):
    return len(line) - len(line.lstrip())


def top_block(lines, key):
    """The indented lines under a column-0 `key:`."""
    out = None
    for ln in lines:
        if indent(ln) == 0:
            if out is not None:
                break
            if ln.rstrip() == key + ":" or ln.startswith(key + ": "):
                out = []
            continue
        if out is not None:
            out.append(ln)
    if out is None:
        raise AssertionError("no top-level %r in %s" % (key, WORKFLOW))
    return out


def sub_block(lines, key):
    """The lines indented deeper than the first line that is `key:` (any depth)."""
    for i, ln in enumerate(lines):
        if ln.strip() == key + ":" or ln.strip().startswith(key + ": "):
            base = indent(ln)
            out = []
            for nxt in lines[i + 1 :]:
                if indent(nxt) <= base:
                    break
                out.append(nxt)
            return out
    raise AssertionError("no %r in %s" % (key, WORKFLOW))


def items(lines):
    """A YAML sequence as a list of dicts: every `key: value` line of each `- ` item,
    nested keys included (the depth of this workflow needs no more)."""
    out = []
    first = None
    for ln in lines:
        stripped = ln.lstrip()
        if first is None and stripped.startswith("- "):
            first = indent(ln)
        if first is not None and indent(ln) == first and stripped.startswith("- "):
            out.append({})
            stripped = stripped[2:]
        if out:
            m = re.match(r"([\w-]+):\s*(.*)$", stripped)
            if m:
                out[-1][m.group(1)] = m.group(2).strip().strip("\"'")
    return out


def scalars(lines):
    """`key: value` pairs of a mapping block, the first level only."""
    base = min(indent(ln) for ln in lines)
    out = {}
    for ln in lines:
        if indent(ln) != base:
            continue
        m = re.match(r"([\w-]+):\s*(.*)$", ln.strip())
        if m:
            out[m.group(1)] = m.group(2).strip().strip("\"'")
    return out


def readme_suite_command():
    """The first command of README's Development code block, its comment dropped."""
    section = read("README.md").split("\n## Development\n", 1)[1]
    fence = section.split("```", 2)[1]
    first = [ln for ln in fence.splitlines() if ln.strip()][0]
    return first.split("#", 1)[0].strip()


class WorkflowCase(unittest.TestCase):
    def setUp(self):
        self.text = read(WORKFLOW)
        self.lines = code_lines(self.text)
        self.steps = items(sub_block(self.lines, "steps"))
        self.legs = items(sub_block(self.lines, "include"))

    def run_steps(self):
        return [s for s in self.steps if "run" in s]


class Trigger(WorkflowCase):
    def test_push_is_the_only_trigger(self):
        # AC-1: `on` has one key, push; nothing else can start a run
        on = top_block(self.lines, "on")
        self.assertEqual(list(scalars(on)), ["push"])
        self.assertNotIn("pull_request", "\n".join(self.lines))

    def test_token_is_read_only(self):
        # AC-1: permissions is exactly `contents: read`
        self.assertEqual(scalars(top_block(self.lines, "permissions")), {"contents": "read"})

    def test_superseded_run_is_cancelled(self):
        # AC-1: one concurrency group per ref, cancel-in-progress
        concurrency = scalars(top_block(self.lines, "concurrency"))
        self.assertIn("github.ref", concurrency.get("group", ""))
        self.assertEqual(concurrency.get("cancel-in-progress"), "true")


class Matrix(WorkflowCase):
    def test_three_legs(self):
        # AC-2 (amended after round 1, author's change to this reviewer test): ubuntu on
        # the pyproject target and on the latest 3.x; the windows and macos legs were dropped.
        # ci/windows-job AC-3 (author's change, renamed from test_two_legs): windows on the
        # latest 3.x is back.
        target = tomllib.loads(read("pyproject.toml"))["tool"]["black"]["target-version"]
        self.assertEqual(len(target), 1)
        m = re.fullmatch(r"py(\d)(\d+)", target[0])
        pinned = "%s.%s" % (m.group(1), m.group(2))
        self.assertCountEqual(
            [(leg["os"], leg["python"]) for leg in self.legs],
            [
                ("ubuntu-latest", pinned),
                ("ubuntu-latest", "3.x"),
                ("windows-latest", "3.x"),
            ],
        )

    def test_legs_drive_runner_and_interpreter(self):
        # AC-2: the matrix values are what the job runs on and the Python it sets up
        job = scalars(sub_block(top_block(self.lines, "jobs"), "test"))
        self.assertIn("matrix.os", job["runs-on"])
        setup = [s for s in self.steps if s.get("uses", "").startswith("actions/setup-python@")]
        self.assertEqual(len(setup), 1)
        self.assertIn("matrix.python", setup[0]["python-version"])

    def test_one_red_leg_does_not_hide_the_others(self):
        # AC-2
        strategy = scalars(sub_block(self.lines, "strategy"))
        self.assertEqual(strategy.get("fail-fast"), "false")


class Lint(WorkflowCase):
    def flag(self):
        """The one matrix key besides os / python, and the legs carrying it."""
        extra = {k for leg in self.legs for k in leg if k not in ("os", "python")}
        self.assertEqual(len(extra), 1, "one marker picks the lint leg: %r" % extra)
        flag = extra.pop()
        return flag, [leg for leg in self.legs if leg.get(flag) not in (None, "", "false")]

    def test_exactly_one_leg_is_marked_for_lint(self):
        # AC-3: ubuntu on the latest 3.x
        _, marked = self.flag()
        self.assertEqual([(leg["os"], leg["python"]) for leg in marked], [("ubuntu-latest", "3.x")])

    def test_only_the_lint_leg_installs_anything(self):
        # AC-3: one install step, guarded by the marker, installing ruff and black
        flag, _ = self.flag()
        installs = [s for s in self.run_steps() if "pip install" in s["run"]]
        self.assertEqual(len(installs), 1)
        self.assertEqual(installs[0].get("if"), "matrix.%s" % flag)
        self.assertRegex(installs[0]["run"], r"\bruff\b")
        self.assertRegex(installs[0]["run"], r"\bblack\b")
        self.assertNotIn("requirements", "\n".join(self.lines))

    def test_lint_step_runs_the_projects_lint_line(self):
        # AC-3: the same command `revali preflight` runs, guarded by the marker
        flag, _ = self.flag()
        lint = tomllib.loads(read("revali.toml"))["project"]["lint"]
        steps = [s for s in self.run_steps() if s["run"] == lint]
        self.assertEqual(len(steps), 1, "no step runs %r" % lint)
        self.assertEqual(steps[0].get("if"), "matrix.%s" % flag)


class Suite(WorkflowCase):
    def test_every_leg_runs_the_suite_as_readme_says(self):
        # AC-4: README's command, unconditional, and its exit code is the step's
        command = readme_suite_command()
        self.assertTrue(command.endswith("tests/run_parallel.py"), command)
        steps = [s for s in self.run_steps() if s["run"] == command]
        self.assertEqual(len(steps), 1, "no step runs %r" % command)
        self.assertNotIn("if", steps[0])
        self.assertNotIn("continue-on-error", "\n".join(self.lines))

    def test_suite_runs_after_checkout_and_python_setup(self):
        # AC-4: the suite needs the checkout and the matrix interpreter
        order = [s.get("uses", s.get("run", "")) for s in self.steps]
        suite = order.index(readme_suite_command())
        checkout = [i for i, u in enumerate(order) if u.startswith("actions/checkout@")]
        setup = [i for i, u in enumerate(order) if u.startswith("actions/setup-python@")]
        self.assertEqual(len(checkout), 1)
        self.assertLess(checkout[0], suite)
        self.assertLess(setup[0], suite)


class AuthorsTest(unittest.TestCase):
    def test_workflow_test_exists_and_uses_no_yaml_library(self):
        # AC-6: tests/test_ci_workflow.py collects tests and reads the workflow as text
        source = read("tests", "test_ci_workflow.py")
        yaml_import = re.compile(r"^\s*(import|from)\s+(ruamel\.)?yaml\b", re.M)
        self.assertNotRegex(source, yaml_import, "the AC asks for no YAML library")
        for anchor in ("revali.toml", "README.md", "test.yml"):
            with self.subTest(anchor=anchor):
                self.assertIn(anchor, source)
        suite = unittest.TestLoader().loadTestsFromName("tests.test_ci_workflow")
        self.assertGreaterEqual(suite.countTestCases(), 4)


if __name__ == "__main__":
    unittest.main()
