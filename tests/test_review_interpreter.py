"""Review tests for fix/suite-on-linux: the fixture picks a Python interpreter
that exists on the host, and the test files no longer hard-code `python`."""
import os
import re
import shutil
import subprocess
import tomllib
import unittest

from tests.helpers import ROOT, RepoCase
from tests.fixtures import make_sample_repo


class InterpreterChoice(unittest.TestCase):
    """AC-1: the interpreter name is chosen at run time and exposed."""

    def test_fixture_exposes_interpreter_names(self):
        for name in ("PY", "LOCAL_TEST", "LOCAL_NEW_TEST"):
            self.assertTrue(hasattr(make_sample_repo, name), name)
        py = make_sample_repo.PY
        self.assertIn(py, ("python", "python3"))
        self.assertIsNotNone(shutil.which(py), "%s is not on PATH" % py)
        if shutil.which("python"):
            self.assertEqual(py, "python")
        self.assertTrue(make_sample_repo.LOCAL_TEST.startswith(py + " -m unittest "))
        self.assertTrue(make_sample_repo.LOCAL_NEW_TEST.startswith(py + " -m unittest "))
        self.assertIn("test_calc*.py", make_sample_repo.LOCAL_TEST)
        self.assertIn("test_review_*.py", make_sample_repo.LOCAL_NEW_TEST)


class LocalFixtureCommands(RepoCase):
    """AC-1: the generated revali.toml carries those commands and they run here."""
    runner = "local"

    def _platform(self):
        with open(os.path.join(self.repo, "revali.toml"), "rb") as fh:
            return tomllib.load(fh)["validate"]["linux"]

    def test_toml_commands_match_exposed_strings(self):
        plat = self._platform()
        self.assertEqual(plat["test"], make_sample_repo.LOCAL_TEST)
        self.assertEqual(plat["new_test"], make_sample_repo.LOCAL_NEW_TEST)
        for cmd in (plat["test"], plat["new_test"]):
            self.assertIsNotNone(shutil.which(cmd.split()[0]), cmd)

    def test_existing_suite_command_runs_on_this_host(self):
        plat = self._platform()
        proc = subprocess.run(plat["test"], shell=True, cwd=self.repo, capture_output=True,
                              text=True, encoding="utf-8", errors="replace", timeout=120)
        out = proc.stdout + proc.stderr
        self.assertEqual(proc.returncode, 0, out[-500:])
        self.assertIn("Ran 2 tests", out)


class NoLiteralInterpreter(unittest.TestCase):
    """AC-2: lint cases and command rewrites do not spell the interpreter as `python`."""

    def test_no_bare_python_command_in_test_files(self):
        pattern = re.compile(r'''["']python -''')
        for rel in ("tests/test_preflight.py", "tests/test_validate.py"):
            with open(os.path.join(ROOT, rel), "r", encoding="utf-8") as fh:
                text = fh.read()
            hits = [i + 1 for i, line in enumerate(text.splitlines()) if pattern.search(line)]
            self.assertEqual(hits, [], "%s hard-codes `python` on lines %s" % (rel, hits))


if __name__ == "__main__":
    unittest.main()
