"""fix/ci-followups (M3.2.5): nothing under `.revali/` is tracked (AC-1, AC-2), the merge-wait
comment in revali.toml does not repeat the job timeout (AC-3), and every CI leg prints the
runner's CPU before the suite without being able to fail the job (AC-4)."""

import contextlib
import importlib.util
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from typing import List, Optional
from unittest import mock

from tests.helpers import ROOT, git


def read(*parts: str) -> str:
    with open(os.path.join(ROOT, *parts), "r", encoding="utf-8", newline="") as fh:
        return fh.read()


def tracked_under(root: str, path: str) -> Optional[List[str]]:
    """The files git tracks under `path` in the checkout at `root`, or None when `root` is
    not a git checkout (or git cannot run)."""
    try:
        inside = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
    except OSError:
        return None
    if inside.returncode != 0 or inside.stdout.strip() != "true":
        return None
    out = git(["ls-files", "-z", "--", path], root)
    return [name for name in out.split("\0") if name]


def model_here() -> Optional[str]:
    """This machine's CPU model where the test can read it itself, else None. It reads the
    same sources as the script's `_primary_model`, so it only checks that the script returns
    them; the independent check is tests/test_review_runnerfacts.py."""
    try:
        if sys.platform.startswith("linux"):
            with open("/proc/cpuinfo", "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if line.startswith("model name"):
                        return line.split(":", 1)[1].strip() or None
        elif os.name == "nt":
            import winreg

            where = r"HARDWARE\DESCRIPTION\System\CentralProcessor\0"
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, where) as key:
                return str(winreg.QueryValueEx(key, "ProcessorNameString")[0]).strip() or None
    except (ImportError, OSError):
        return None
    return None


def load_runner_facts():
    path = os.path.join(ROOT, ".github", "runner_facts.py")
    spec = importlib.util.spec_from_file_location("runner_facts", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class NothingTrackedUnderRevali(unittest.TestCase):
    """AC-1, AC-2"""

    def test_this_checkout_tracks_nothing_under_revali(self):
        files = tracked_under(ROOT, ".revali")
        if files is None:
            self.skipTest("the suite does not run from a git checkout")
        self.assertEqual(files, [], "tracked under .revali/ (ignored on purpose): %s" % files)

    def test_the_check_sees_a_forced_add_and_ignores_the_ignored(self):
        tmp = tempfile.mkdtemp(prefix="revali test ")
        self.addCleanup(shutil.rmtree, tmp, True)
        git(["init", "--quiet"], tmp)
        # no commit here, but the suite's rule is an identity with every git init
        git(["config", "user.email", "test@example.invalid"], tmp)
        git(["config", "user.name", "revali test"], tmp)
        with open(os.path.join(tmp, ".gitignore"), "w", encoding="utf-8", newline="") as fh:
            fh.write(".revali/\n")
        os.makedirs(os.path.join(tmp, ".revali", "feature__x"))
        for name in ("change.md", "state.json"):
            with open(os.path.join(tmp, ".revali", "feature__x", name), "w", newline="") as fh:
                fh.write("x\n")
        git(["add", ".gitignore"], tmp)
        self.assertEqual(tracked_under(tmp, ".revali"), [])
        git(["add", "-f", ".revali/feature__x/change.md"], tmp)
        self.assertEqual(tracked_under(tmp, ".revali"), [".revali/feature__x/change.md"])

    def test_outside_a_checkout_the_check_skips(self):
        tmp = tempfile.mkdtemp(prefix="revali test ")
        self.addCleanup(shutil.rmtree, tmp, True)
        with mock.patch.dict(os.environ, {"GIT_CEILING_DIRECTORIES": os.path.dirname(tmp)}):
            self.assertIsNone(tracked_under(tmp, ".revali"))

    def test_git_missing_means_skip(self):
        with mock.patch("subprocess.run", side_effect=FileNotFoundError("git")):
            self.assertIsNone(tracked_under(ROOT, ".revali"))


class MergeWaitComment(unittest.TestCase):
    """AC-3"""

    def comment(self) -> str:
        lines = read("revali.toml").splitlines()
        at = next(i for i, ln in enumerate(lines) if ln.startswith("checks_timeout_min"))
        above = []
        for ln in reversed(lines[:at]):
            if not ln.startswith("#"):
                break
            above.append(ln)
        return " ".join(ln.lstrip("# ") for ln in reversed(above))

    def test_the_job_timeout_is_not_repeated(self):
        workflow = read(".github", "workflows", "test.yml")
        job = re.search(r"^    timeout-minutes:\s*(\d+)\s*$", workflow, re.M)
        self.assertIsNotNone(job)
        self.assertNotRegex(self.comment(), r"\b%s\b" % job.group(1))

    def test_it_still_says_why_and_what_was_measured(self):
        text = self.comment()
        self.assertIn("above the job's timeout-minutes", text)
        self.assertIn(".github/workflows/test.yml", text)
        self.assertRegex(text, r"\d+, \d+ and \d+ minutes")


class RunnerFactsStep(unittest.TestCase):
    """AC-4: the workflow side"""

    def steps(self):
        text = read(".github", "workflows", "test.yml")
        return re.findall(r"^      - (?:name: (.+)|uses: (.+))\n((?:        .+\n)*)", text, re.M)

    def test_before_the_suite_and_unconditional(self):
        names = [name or uses for name, uses, _ in self.steps()]
        self.assertIn("runner facts", names)
        self.assertLess(names.index("runner facts"), names.index("suite"))
        self.assertGreater(names.index("runner facts"), names.index("actions/setup-python@v7"))
        body = dict((name, rest) for name, _, rest in self.steps() if name)["runner facts"]
        self.assertEqual(body.split(), ["run:", "python", ".github/runner_facts.py"])


class RunnerFactsScript(unittest.TestCase):
    """AC-4: the script prints the CPU and never fails"""

    def setUp(self):
        self.facts = load_runner_facts()

    def run_main(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = self.facts.main()
        return code, out.getvalue()

    def test_prints_the_model_and_the_count_here(self):
        code, out = self.run_main()
        self.assertEqual(code, 0)
        count = re.escape(str(os.cpu_count() or "unknown"))
        self.assertRegex(out, r"^cpu: .+; logical processors: %s; " % count)
        model = model_here()
        if model is not None:  # round 1 F1: only where the test itself can read one
            self.assertEqual(self.facts.cpu_model(), model)

    def test_a_model_it_cannot_read_is_unknown_with_the_reason(self):
        # round 1 F2: the reason the primary source failed reaches the log
        with (
            mock.patch("builtins.open", side_effect=PermissionError("denied")),
            mock.patch.dict(sys.modules, {"winreg": None}),
            mock.patch("platform.processor", return_value=""),
            mock.patch("platform.platform", return_value="SomeOS-1"),
        ):
            model = self.facts.cpu_model()
            code, out = self.run_main()
        if sys.platform.startswith("linux") or os.name == "nt":
            if os.name == "nt":  # round 2 F1: each platform's own reason
                self.assertRegex(model, r"^unknown \((ModuleNotFoundError|ImportError): .+\)$")
            else:
                self.assertEqual(model, "unknown (PermissionError: denied)")
        else:
            self.assertEqual(model, "unknown")
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("cpu: unknown"), out)
        self.assertTrue(out.rstrip().endswith("; SomeOS-1"), out)

    def test_no_model_line_falls_back_without_a_reason(self):
        # e.g. an ARM Linux whose /proc/cpuinfo has no "model name"
        with (
            mock.patch.object(self.facts, "_primary_model", return_value=""),
            mock.patch("platform.processor", return_value="aarch64"),
        ):
            self.assertEqual(self.facts.cpu_model(), "aarch64")
        with (
            mock.patch.object(self.facts, "_primary_model", return_value=""),
            mock.patch("platform.processor", return_value=""),
        ):
            self.assertEqual(self.facts.cpu_model(), "unknown")

    def test_a_failing_fallback_keeps_the_first_reason(self):
        with (
            mock.patch.object(self.facts, "_primary_model", side_effect=OSError("gone")),
            mock.patch("platform.processor", side_effect=RuntimeError("no uname")),
        ):
            self.assertEqual(self.facts.cpu_model(), "unknown (OSError: gone)")

    def test_an_error_in_the_facts_is_printed_not_raised(self):
        with mock.patch.object(self.facts, "cpu_model", side_effect=RuntimeError("boom")):
            code, out = self.run_main()
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), "cpu: unknown (boom)")

    def test_a_broken_stdout_still_exits_0(self):
        broken = mock.Mock()
        broken.write.side_effect = OSError("closed")
        with mock.patch.object(sys, "stdout", broken):
            self.assertEqual(self.facts.main(), 0)

    def test_as_the_workflow_runs_it(self):
        res = subprocess.run(
            [sys.executable, os.path.join(".github", "runner_facts.py")],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertTrue(res.stdout.startswith("cpu: "), res.stdout)


if __name__ == "__main__":
    unittest.main()
