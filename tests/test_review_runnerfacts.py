"""fix/ci-followups, AC-4: every CI leg prints the runner's CPU model and logical processor
count before the suite, in a step that cannot fail the job.

The workflow is read from its text with the standard library (no YAML library). The step is
found by what it does, not by its name: a `python <script in the repository>` step before
the suite whose output has this machine's logical processor count and, where the test can
read one from the OS itself, its CPU model. The same command is then run on a machine where
nothing about the CPU can be read.
"""

import os
import re
import shlex
import subprocess
import sys
import unittest

from tests.helpers import ROOT

WORKFLOW = (".github", "workflows", "test.yml")
SUITE = "tests/run_parallel.py"

# Runs argv[1:] as `python <script> ...` would, with /proc and /sys unreadable, no registry
# module, and no way to start a process.
HOSTILE = """
import builtins, io, os, runpy, subprocess, sys

real_open = builtins.open


def deny(file, *args, **kwargs):
    if isinstance(file, (str, bytes, os.PathLike)):
        name = os.fsdecode(file).replace(os.sep, "/")
        if name.startswith(("/proc/", "/sys/")):
            raise PermissionError(13, "Permission denied", name)
    return real_open(file, *args, **kwargs)


def no_process(*args, **kwargs):
    raise FileNotFoundError(2, "No such file or directory")


builtins.open = deny
io.open = deny
sys.modules["winreg"] = None
subprocess.Popen = no_process
os.popen = no_process
os.system = no_process
sys.argv = sys.argv[1:]
runpy.run_path(sys.argv[0], run_name="__main__")
"""


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


def squeeze(text):
    return " ".join(text.split())


def cpu_model_here():
    """This machine's CPU model as the OS names it, or None when the test cannot tell."""
    try:
        if sys.platform.startswith("linux"):
            with open("/proc/cpuinfo", "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if line.lower().startswith("model name") and ":" in line:
                        return squeeze(line.split(":", 1)[1]) or None
        elif os.name == "nt":
            import winreg

            where = r"HARDWARE\DESCRIPTION\System\CentralProcessor\0"
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, where) as key:
                return squeeze(str(winreg.QueryValueEx(key, "ProcessorNameString")[0])) or None
    except (ImportError, OSError):
        return None
    return None


def has_count(text, count):
    """`count` as a number of its own, not a digit of a version or a clock speed."""
    return re.search(r"(?<![\w.])%d(?!\w|\.\d)" % count, text) is not None


def run(argv):
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run(
        argv,
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )


class RunnerFacts(unittest.TestCase):
    def setUp(self):
        lines = code_lines(read(*WORKFLOW))
        self.job = under(under(lines, "jobs"), "test")
        self.steps = items(under(self.job, "steps"))
        self.count = os.cpu_count()
        if not self.count:
            self.skipTest("this machine does not report a processor count")
        self.model = cpu_model_here()

    def python_steps(self):
        """(step, argv) of each `python <script in the repository>` step before the suite."""
        at = [i for i, s in enumerate(self.steps) if s.get("run", "").endswith(SUITE)]
        self.assertEqual(len(at), 1, "one suite step")
        out = []
        for step in self.steps[: at[0]]:
            try:
                words = shlex.split(step.get("run", ""))
            except ValueError:
                continue
            if len(words) < 2 or not re.fullmatch(r"python3?", words[0]):
                continue
            if words[1].endswith(".py") and os.path.isfile(os.path.join(ROOT, words[1])):
                out.append((step, [sys.executable] + words[1:]))
        return out

    def fact_steps(self):
        """The steps before the suite that print the processor count (and the model, when
        the test knows it), each with its command and its result here."""
        found = []
        for step, argv in self.python_steps():
            res = run(argv)
            text = squeeze(res.stdout)
            if has_count(text, self.count) and (self.model is None or self.model in text):
                found.append((step, argv, res))
        self.assertTrue(
            found,
            "no step before the suite prints the CPU model (%r) and the logical processor "
            "count (%d)" % (self.model, self.count),
        )
        return found

    def test_a_step_before_the_suite_prints_the_model_and_the_count(self):
        for step, _, res in self.fact_steps():
            with self.subTest(step=step.get("name")):
                self.assertEqual(res.returncode, 0, res.stderr)

    def test_the_step_runs_on_every_leg(self):
        # no condition on the step or on the job, so no leg skips it
        unconditional = [step for step, _, _ in self.fact_steps() if "if" not in step]
        self.assertTrue(unconditional, "the step that prints the CPU is conditional")
        base = min(indent(ln) for ln in self.job)
        job_keys = [ln.strip().split(":", 1)[0] for ln in self.job if indent(ln) == base]
        self.assertNotIn("if", job_keys)

    def test_a_model_that_cannot_be_read_does_not_fail_the_step(self):
        # the step's own command, where the CPU cannot be read: it still exits 0
        for step, argv, _ in self.fact_steps():
            with self.subTest(step=step.get("name")):
                res = run([sys.executable, "-c", HOSTILE] + argv[1:])
                self.assertEqual(res.returncode, 0, res.stderr)
                self.assertNotIn("Traceback", res.stderr)
                if self.model is not None and self.model in squeeze(res.stdout):
                    self.skipTest("the model was still read, so this run shows nothing")


if __name__ == "__main__":
    unittest.main()
