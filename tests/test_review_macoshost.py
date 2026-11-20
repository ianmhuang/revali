"""macos-ci AC-3, AC-5: two things a macOS host needs from the suite and the CI log.

AC-5: `.github/runner_facts.py` prints the CPU model `sysctl -n machdep.cpu.brand_string`
gives on macOS, and falls back as on the other platforms (`platform.processor()`, then
`unknown`, the reason in parentheses) when sysctl cannot be asked. The script's `main()`
runs here as on a macOS host: `sys.platform` says darwin and `subprocess.run` is a fake
sysctl, so the test runs on any host.

AC-3: `StepLogHint.test_own_value_named` sets its process limit for real on the host's bash,
so the limit must leave room for the processes the user already runs. No test can give a
host more than 300 of them, so the limit itself is read while the test runs: it is not the
default the other tests of the class set for real, and it is not below that default, which
makes the test pass wherever its neighbours do.
"""

import contextlib
import importlib.util
import inspect
import io
import os
import shlex
import subprocess
import sys
import unittest
from unittest import mock

from tests.helpers import ROOT

SCRIPT = os.path.join(ROOT, ".github", "runner_facts.py")
SYSCTL = ["-n", "machdep.cpu.brand_string"]


def load_script():
    spec = importlib.util.spec_from_file_location("runner_facts_on_macos", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Sysctl:
    """Stands in for `subprocess.run`: answers as sysctl would, or fails as told."""

    def __init__(self, answer="", returncode=0, raises=None):
        self.answer = answer
        self.returncode = returncode
        self.raises = raises
        self.calls = []

    def __call__(self, cmd, *args, **kwargs):
        argv = shlex.split(cmd) if isinstance(cmd, str) else [str(a) for a in cmd]
        self.calls.append(argv)
        if self.raises is not None:
            raise self.raises
        as_text = any(kwargs.get(k) for k in ("text", "universal_newlines", "encoding"))
        out = self.answer if as_text else self.answer.encode("utf-8")
        err = "" if as_text else b""
        if self.returncode and kwargs.get("check"):
            raise subprocess.CalledProcessError(self.returncode, cmd, output=out, stderr=err)
        return subprocess.CompletedProcess(cmd, self.returncode, stdout=out, stderr=err)


class CpuModelOnMacos(unittest.TestCase):
    def printed(self, sysctl, processor="arm"):
        """(exit code, the line) of the script's main() on a host that says it is macOS."""
        module = load_script()
        out = io.StringIO()
        with (
            mock.patch.object(sys, "platform", "darwin"),
            mock.patch("subprocess.run", sysctl),
            mock.patch("platform.processor", return_value=processor),
            mock.patch("platform.platform", return_value="macOS-15.0-arm64-arm-64bit"),
            contextlib.redirect_stdout(out),
        ):
            code = module.main()
        return code, out.getvalue().strip()

    def test_the_model_sysctl_gives_is_printed(self):
        sysctl = Sysctl("Apple M2 Pro (Virtual)\n")
        code, line = self.printed(sysctl)
        self.assertEqual(code, 0)
        self.assertTrue(line.startswith("cpu: Apple M2 Pro (Virtual);"), line)
        self.assertIn("logical processors: %s" % (os.cpu_count() or "unknown"), line)
        self.assertEqual(len(sysctl.calls), 1, sysctl.calls)
        argv = sysctl.calls[0]
        self.assertEqual(os.path.basename(argv[0]), "sysctl")
        self.assertEqual(argv[1:], SYSCTL)

    def test_no_sysctl_falls_back_and_gives_the_reason(self):
        missing = FileNotFoundError(2, "No such file or directory: 'sysctl'")
        code, line = self.printed(Sysctl(raises=missing))
        self.assertEqual(code, 0)
        self.assertRegex(line, r"^cpu: arm \(FileNotFoundError: .+\); logical processors: ")

    def test_no_sysctl_and_no_fallback_is_unknown_with_the_reason(self):
        code, line = self.printed(Sysctl(raises=OSError("gone")), processor="")
        self.assertEqual(code, 0)
        self.assertTrue(line.startswith("cpu: unknown (OSError: gone);"), line)

    def test_a_failing_sysctl_falls_back(self):
        code, line = self.printed(Sysctl("", returncode=1))
        self.assertEqual(code, 0)
        self.assertRegex(line, r"^cpu: arm( \(.+\))?; logical processors: ")

    def test_a_sysctl_that_hangs_falls_back(self):
        hung = subprocess.TimeoutExpired(["sysctl"] + SYSCTL, 30)
        code, line = self.printed(Sysctl(raises=hung))
        self.assertEqual(code, 0)
        self.assertRegex(line, r"^cpu: arm \(TimeoutExpired: .+\); logical processors: ")

    def test_an_empty_answer_falls_back_without_a_reason(self):
        code, line = self.printed(Sysctl("\n"))
        self.assertEqual(code, 0)
        self.assertTrue(line.startswith("cpu: arm; logical processors: "), line)
        code, line = self.printed(Sysctl("\n"), processor="")
        self.assertEqual(code, 0)
        self.assertTrue(line.startswith("cpu: unknown; logical processors: "), line)


class OwnValueLeavesRoom(unittest.TestCase):
    NAME = "tests.test_sandbox_nproc.StepLogHint.test_own_value_named"

    def test_the_limit_set_for_real_is_not_below_the_default(self):
        import tests.test_sandbox_nproc as nproc

        default = inspect.signature(nproc.StepLogHint.run_step).parameters["max_procs"].default
        self.assertIsInstance(default, int)
        seen = []

        class Recording(nproc.WslRunner):
            def __init__(self, platform_cfg, *args, **kwargs):
                seen.append(platform_cfg.max_procs)
                super().__init__(platform_cfg, *args, **kwargs)

        suite = unittest.defaultTestLoader.loadTestsFromName(self.NAME)
        result = unittest.TestResult()
        with mock.patch.object(nproc, "WslRunner", Recording):
            suite.run(result)
        if result.skipped:
            self.skipTest(result.skipped[0][1])
        problems = [text for _test, text in result.failures + result.errors]
        self.assertEqual(problems, [])
        self.assertEqual(result.testsRun, 1)
        self.assertEqual(len(seen), 1, seen)
        # a value of its own, so the hint is shown to name what was configured
        self.assertNotEqual(seen[0], default)
        # and no tighter than the limit every other test of the class already runs under
        self.assertGreaterEqual(seen[0], default)


if __name__ == "__main__":
    unittest.main()
