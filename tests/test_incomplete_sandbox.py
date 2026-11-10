"""A sandbox session that ends before its steps did is a sandbox error, never a pass
(fix/incomplete-sandbox-session, M3.4.5). Before this, `report_from_results` read whatever
rows the results file held, so a session killed after `setup` passed came back as PASS."""

import json
import os
import shutil
import sys
import tempfile
import unittest

from revali import EXIT_ERROR, EXIT_OK
from revali.runners import RunnerError, report_from_results
from revali.state import State
from tests.fixtures.make_sample_repo import LOCAL_NEW_TEST, LOCAL_TEST, PY, toml_str
from tests.helpers import FAKE_BIN, RepoCase, _quote, claude_entry, run_cli
from tests.test_ssh_runner import HAVE_BASH

STEPS = [("setup", "s"), ("build", ""), ("test", "t"), ("new_test", "n")]  # build: no command
TERMINATED = "The Windows Subsystem for Linux instance has terminated."


class Rows(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="revali results ")
        self.addCleanup(shutil.rmtree, self.dir, True)

    def report(self, *rows, steps=STEPS):
        path = os.path.join(self.dir, "validate-r1.results")
        with open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write("".join("%s\t%s\t0\n" % r for r in rows))
        return report_from_results(self.dir, "validate-r1", steps)

    def refused(self, *rows):
        with self.assertRaises(RunnerError) as ctx:
            self.report(*rows)
        return str(ctx.exception)

    # --- AC-1 -----------------------------------------------------------------------------

    def test_rows_that_stop_after_a_pass_are_an_error(self):
        text = self.refused(("setup", 0))
        self.assertIn("ended before `test`", text)
        self.assertIn("wsl --shutdown", text)
        self.assertIn("validate-r1.results", text)

    def test_the_last_step_missing_is_an_error_too(self):
        self.assertIn("ended before `new_test`", self.refused(("setup", 0), ("test", 0)))

    # --- AC-2 -----------------------------------------------------------------------------

    def test_every_step_present(self):
        r = self.report(("setup", 0), ("test", 0), ("new_test", 0))
        self.assertEqual([s.name for s in r.steps], ["setup", "test", "new_test"])
        self.assertTrue(r.ok)

    def test_a_failing_step_ends_the_rows_by_design(self):
        r = self.report(("setup", 0), ("test", 1))
        self.assertEqual(r.failed.name, "test")
        self.assertEqual(r.failed.returncode, 1)

    def test_a_timed_out_step_ends_them_too(self):
        r = self.report(("setup", 0), ("test", 124))
        self.assertEqual(r.failed.name, "test")

    def test_steps_without_a_command_are_not_expected(self):
        r = self.report(("setup", 0), ("test", 0), ("new_test", 0))
        self.assertNotIn("build", [s.name for s in r.steps])

    def test_no_rows_names_the_first_step(self):  # PR #57 F2
        text = self.refused()
        self.assertIn("ended before `setup` finished", text)
        self.assertIn("wsl --shutdown", text)
        self.assertIn("validate-r1-setup.log", text)
        self.assertIn("validate-r1-clone.log", text)

    def test_no_rows_when_no_step_has_a_command_is_the_old_message(self):
        with self.assertRaises(RunnerError) as ctx:
            self.report(steps=[("setup", ""), ("test", " ")])
        self.assertIn("produced no results", str(ctx.exception))

    def test_a_row_cut_off_while_written_is_refused(self):  # PR #57 follow-up
        for line in ("setup\n", "setup\t\n", "setup\t0\n", "setup\tx\t0\n", "setup\t0\t\n"):
            with self.subTest(line=line):
                path = os.path.join(self.dir, "validate-r1.results")
                with open(path, "w", encoding="utf-8", newline="") as fh:
                    fh.write(line)
                with self.assertRaises(RunnerError) as ctx:
                    report_from_results(self.dir, "validate-r1", STEPS)
                self.assertIn("not a complete result", str(ctx.exception))

    def test_a_negative_exit_code_is_a_complete_row(self):
        r = self.report(("setup", -9))
        self.assertEqual(r.failed.returncode, -9)

    # --- AC-3 -----------------------------------------------------------------------------

    def test_an_unknown_step_name(self):
        self.assertIn("do not match", self.refused(("setup", 0), ("deploy", 0)))

    def test_a_repeated_step(self):
        self.assertIn("do not match", self.refused(("setup", 0), ("setup", 0)))

    def test_steps_out_of_order(self):
        self.assertIn("do not match", self.refused(("test", 0), ("setup", 0)))

    def test_rows_after_a_failure(self):
        self.assertIn("do not match", self.refused(("setup", 1), ("test", 0)))


@unittest.skipUnless(HAVE_BASH, "needs bash")
class DiedPartWay(RepoCase):
    """AC-4: the whole pipeline on the wsl runner, with a wsl.exe that loses the end of one
    session (the stub keeps only the first rows and fails as WSL does when it is shut down)."""

    runner = "wsl"

    def setUp(self):
        super().setUp()
        self.use_real_local_runner()
        stub = os.path.join(FAKE_BIN, "wsl_flaky_stub.py")
        os.environ["REVALI_WSL_CMD"] = "%s %s" % (_quote(sys.executable), _quote(stub))
        self.plan_path = os.path.join(self.tmp, "wsl-plan.json")
        os.environ["REVALI_FAKE_WSL_PLAN"] = self.plan_path
        cfg = self.read("revali.toml")
        new = cfg.replace(
            'setup = "python3 -m venv .venv && .venv/bin/pip install -q -r requirements.txt"',
            'setup = "%s --version"' % PY,
        )
        new = new.replace(
            'test = ".venv/bin/python -m pytest -q"', "test = %s" % toml_str(LOCAL_TEST)
        )
        new = new.replace(
            'new_test = ".venv/bin/python -m pytest -q tests"',
            "new_test = %s" % toml_str(LOCAL_NEW_TEST),
        )
        self.assertNotEqual(cfg, new, "fixture platform table not found")
        self.write("revali.toml", new)
        self.commit_all("host commands for the wsl stub")

    def lose_rows_of(self, label, keep=1):
        with open(self.plan_path, "w", encoding="utf-8") as fh:
            json.dump({"keep_rows": {label: keep}, "message": TERMINATED, "exit": 1}, fh)

    def run_it(self):
        self.claude(claude_entry())
        return run_cli(["run", "--foreground"])

    def assert_sandbox_error(self, code, out, label, missing):
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertIn("ended before `%s`" % missing, out)
        self.assertNotIn("READY TO MERGE", out)
        state = State.load(self.rdir())
        self.assertEqual(state.stage, "error")
        self.assertEqual([v for v in state.validations if v.get("result") == "PASS"], [])

    def test_the_sandbox_itself_works_through_the_stub(self):
        code, out = self.run_it()
        self.assertEqual(code, EXIT_OK, out)

    def test_validation(self):
        self.lose_rows_of("validate-r1")
        code, out = self.run_it()
        self.assert_sandbox_error(code, out, "validate-r1", "new_test")

    def test_baseline(self):
        self.lose_rows_of("baseline")
        code, out = self.run_it()
        self.assert_sandbox_error(code, out, "baseline", "test")
        self.assertIn("sandbox failed during baseline", out)

    def test_smoke_run(self):
        self.lose_rows_of("smoke-r1-1")
        code, out = self.run_it()
        self.assert_sandbox_error(code, out, "smoke-r1-1", "new_test")


class Docs(unittest.TestCase):
    def test_sandbox_md(self):  # AC-5
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(here, "docs", "sandbox.md"), encoding="utf-8") as fh:
            text = " ".join(fh.read().split())
        self.assertIn("ended before", text)
        self.assertIn("never a pass", text)


if __name__ == "__main__":
    unittest.main()
