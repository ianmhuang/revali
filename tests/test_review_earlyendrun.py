"""Review tests for fix/incomplete-sandbox-session, through the pipeline.

AC-4: a sandbox session that ended early ends `revali run` with exit 1 and the message,
      never with PASS, whether it was the baseline, a smoke run or a validation; a base
      rerun records it as unavailable and does not stop anything.

The fixture repository runs on the wsl runner; wsl.exe is the wrapper from
tests/test_review_earlyend.py, which lets a session's script run, drops the end of its
results file and exits 1.
"""

import os
import types
import unittest

from revali import EXIT_ERROR, EXIT_OK
from revali.config import PlatformCfg
from revali.state import State
from revali.validate import rerun_on_base
from tests.fixtures.make_sample_repo import LOCAL_NEW_TEST, LOCAL_TEST, PY, toml_str
from tests.helpers import TEST_REVIEW_MUL, RepoCase, claude_entry, git, run_cli
from tests.test_review_earlyend import HAVE_BASH, install, lose_rows

REVIEWER_FILE = "tests/test_review_mul.py"
# the fixture's sandbox commands (a venv and pytest) and ones the host's bash can run
HOST_COMMANDS = (
    (
        'setup = "python3 -m venv .venv && .venv/bin/pip install -q -r requirements.txt"',
        'setup = "%s --version"' % PY,
    ),
    ('test = ".venv/bin/python -m pytest -q"', "test = %s" % toml_str(LOCAL_TEST)),
    (
        'new_test = ".venv/bin/python -m pytest -q tests"',
        "new_test = %s" % toml_str(LOCAL_NEW_TEST),
    ),
)


class WslCase(RepoCase):
    runner = "wsl"

    def setUp(self):
        super().setUp()
        self.use_real_local_runner()
        install(self, "wsl_stub", "REVALI_WSL_CMD")


class Run(WslCase):
    """`revali run --foreground` with commands the host's bash can run."""

    def setUp(self):
        super().setUp()
        cfg = self.read("revali.toml")
        for old, new in HOST_COMMANDS:
            self.assertIn(old, cfg, "fixture platform table not found")
            cfg = cfg.replace(old, new)
        self.write("revali.toml", cfg)
        self.commit_all("review: host commands for the wsl stand-in")

    def run_it(self):
        self.claude(claude_entry())
        return run_cli(["run", "--foreground"])

    def assert_sandbox_error(self, code, out):
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertIn("wsl --shutdown", out)
        self.assertNotIn("READY TO MERGE", out)
        state = State.load(self.rdir())
        self.assertEqual([v for v in state.validations if v.get("result") == "PASS"], [])
        self.assertNotEqual(state.last_verdict, "PASS")

    @unittest.skipUnless(HAVE_BASH, "needs bash")
    def test_an_untouched_run_ends_ready_to_merge(self):
        code, out = self.run_it()
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("READY TO MERGE", out)

    @unittest.skipUnless(HAVE_BASH, "needs bash")
    def test_a_validation_that_lost_its_last_step_is_exit_1_not_pass(self):
        lose_rows(self, {"validate-r1": -1})
        code, out = self.run_it()
        self.assert_sandbox_error(code, out)

    @unittest.skipUnless(HAVE_BASH, "needs bash")
    def test_a_validation_that_lost_every_row_is_exit_1_not_pass(self):
        lose_rows(self, {"validate-r1": 0})
        code, out = self.run_it()
        self.assert_sandbox_error(code, out)

    @unittest.skipUnless(HAVE_BASH, "needs bash")
    def test_a_baseline_that_ended_after_setup_is_exit_1(self):
        lose_rows(self, {"baseline": 1})
        code, out = self.run_it()
        self.assert_sandbox_error(code, out)

    @unittest.skipUnless(HAVE_BASH, "needs bash")
    def test_a_smoke_run_that_ended_after_setup_is_exit_1(self):
        lose_rows(self, {"smoke-r1-1": 1})
        code, out = self.run_it()
        self.assert_sandbox_error(code, out)


class BaseRerun(WslCase):
    """`rerun_on_base` on the wsl runner: on the base tip `mul` does not exist, so the
    reviewer's file fails there in `new_test` after `setup` passed."""

    def setUp(self):
        super().setUp()
        self.write(REVIEWER_FILE, TEST_REVIEW_MUL)
        self.state = State()
        self.state.test_files = [REVIEWER_FILE]

    def ctx(self):
        plat = PlatformCfg(
            name="linux",
            runner="wsl",
            distro="Ubuntu",
            command_timeout_min=1,
            sandbox_dir="~/.revali/sandbox",
            setup="%s --version" % PY,
            new_test=LOCAL_NEW_TEST,
        )
        return types.SimpleNamespace(
            cfg=types.SimpleNamespace(
                validate=types.SimpleNamespace(rerun_on_base=True, platforms={"linux": plat}),
                project=types.SimpleNamespace(platforms=["linux"], test_dir="tests"),
            ),
            base="main",
            base_sha=git(["rev-parse", "main"], self.repo).strip(),
            branch="feature/mul",
            repo_root=self.repo,
            logs=os.path.join(self.tmp, "rerun-logs"),
        )

    def rerun(self):
        failed = types.SimpleNamespace(name="new_test")
        return rerun_on_base(self.ctx(), self.state, failed, 1, None)

    @unittest.skipUnless(HAVE_BASH, "needs bash")
    def test_a_complete_base_rerun_is_available(self):
        r = self.rerun()
        self.assertTrue(r.ran)
        self.assertTrue(r.available, r.reason)
        self.assertNotEqual(r.new_test.returncode, 0)

    @unittest.skipUnless(HAVE_BASH, "needs bash")
    def test_a_base_rerun_that_ended_early_is_unavailable_with_the_message(self):
        lose_rows(self, {"base-r1": 1})
        r = self.rerun()  # a reason on the result, never an exception
        self.assertTrue(r.ran)
        self.assertFalse(r.available)
        self.assertIn("wsl --shutdown", r.reason)
        self.assertIn("new_test", r.reason)
        record = r.as_dict()
        self.assertIsNone(record["exit"])
        self.assertIn("wsl --shutdown", record["reason"])


if __name__ == "__main__":
    unittest.main()
