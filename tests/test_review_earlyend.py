"""Review tests for fix/incomplete-sandbox-session, at the level of one sandbox session.

AC-1: results whose rows stop after a passing step while the session had more steps are a
      RunnerError that names the first step without a result and the likely causes, on the
      wsl and on the ssh runner.
AC-2: a row for every step, and rows that end with a failing step, read as before.
AC-3: a row that is not the next step the session was given (unknown, repeated, out of
      order) is a RunnerError saying the results do not match the session's steps.

Round 2 adds the two cases the fix round brought in, both sessions stopped from outside that
must not read as a pass: no row at all (the session ended in its first step) names that
step and the causes, and a row cut off while it was written is a RunnerError.

`ResultRows` feeds results files to `report_from_results`. The session classes run the real
WslRunner / SshRunner against the repository's stand-ins for wsl.exe and ssh, wrapped by a
stub written here that lets the sandbox script run, then drops the end of its results file
and exits 1: what is left on disk when the session is stopped from outside part way.
"""

import json
import os
import shutil
import sys
import tempfile
import unittest

from revali.config import PlatformCfg
from revali.runners import RunnerError, SshRunner, WslRunner, report_from_results
from tests.helpers import FAKE_BIN, RepoCase, _quote, git

HAVE_BASH = os.name == "nt" or os.path.exists("/bin/bash")
LABEL = "validate-r1"
SCP_STUB = os.path.join(FAKE_BIN, "scp_stub.py")

# Hands every call to the repository's stand-in (__INNER__), then, for a session whose label
# the plan names, keeps only the first rows of every <label>.results under EARLYEND_ROOT and
# exits 1. The plan is {"<label>": n}; rows[:n], so -1 drops the last row.
STUB = r"""import json
import os
import re
import sys

sys.path.insert(0, os.environ["EARLYEND_FAKE_BIN"])

import __INNER__ as inner


def command(argv):
    return argv[3:] if argv[:1] == ["-d"] else argv


def session_label(argv):
    cmd = command(argv)
    if not cmd or cmd[0] == "wslpath":
        return ""
    found = re.search(r"(?:^|\s)bash\s.*?([\w-]+)\.sh['\"]?$", " ".join(cmd))
    return found.group(1) if found else ""


def main(argv):
    if command(argv)[:2] == ["bash", "-c"]:
        return 0  # the preflight probe; the host has git, bash and timeout or it does not
    code = inner.main(argv)
    label = session_label(argv)
    with open(os.environ["EARLYEND_PLAN"], "r", encoding="utf-8") as fh:
        keep = json.load(fh).get(label)
    if not label or keep is None:
        return code
    for root, _dirs, files in os.walk(os.environ["EARLYEND_ROOT"]):
        if label + ".results" in files:
            path = os.path.join(root, label + ".results")
            with open(path, "r", encoding="utf-8", newline="") as fh:
                rows = [line for line in fh if line.strip()]
            with open(path, "w", encoding="utf-8", newline="") as fh:
                fh.writelines(rows[: int(keep)])
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
"""


def lose_rows(case, plan=None):
    """`plan` is {"<label>": rows kept}; no plan lets every session through untouched."""
    with open(os.environ["EARLYEND_PLAN"], "w", encoding="utf-8", newline="") as fh:
        json.dump(plan or {}, fh)


def install(case, inner, env_name):
    """Make `env_name` (REVALI_WSL_CMD / REVALI_SSH_CMD) the wrapper around `inner`."""
    stub = os.path.join(case.tmp, "earlyend_%s.py" % inner)
    with open(stub, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(STUB.replace("__INNER__", inner))
    os.environ[env_name] = "%s %s" % (_quote(sys.executable), _quote(stub))
    os.environ["EARLYEND_FAKE_BIN"] = FAKE_BIN
    os.environ["EARLYEND_ROOT"] = case.tmp
    os.environ["EARLYEND_PLAN"] = os.path.join(case.tmp, "earlyend-plan.json")
    lose_rows(case)


class ResultRows(unittest.TestCase):
    """The rules on the rows alone: (name, exit code, timed out) per row, as the script
    writes them."""

    STEPS = [
        ("setup", "make deps"),
        ("build", "make"),
        ("test", "make check"),
        ("new_test", "make check-new"),
    ]

    def setUp(self):
        self.logs = tempfile.mkdtemp(prefix="rv-rows-")
        self.addCleanup(shutil.rmtree, self.logs, ignore_errors=True)

    def report(self, *rows, steps=None):
        path = os.path.join(self.logs, LABEL + ".results")
        with open(path, "w", encoding="utf-8", newline="") as fh:
            for name, rc, to in rows:
                fh.write("%s\t%d\t%d\n" % (name, rc, to))
        for name, _rc, _to in rows:
            log = os.path.join(self.logs, "%s-%s.log" % (LABEL, name))
            with open(log, "w", encoding="utf-8", newline="") as fh:
                fh.write("output of %s\n" % name)
        return report_from_results(self.logs, LABEL, list(steps or self.STEPS))

    def refused(self, *rows, steps=None):
        """The RunnerError's text, without the logs directory (its name is not the message)."""
        with self.assertRaises(RunnerError) as cm:
            self.report(*rows, steps=steps)
        return str(cm.exception).replace(self.logs, "<logs>")

    def refused_text(self, text, steps=None):
        """The RunnerError's text for a results file holding exactly `text`."""
        path = os.path.join(self.logs, LABEL + ".results")
        with open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
        with self.assertRaises(RunnerError) as cm:
            report_from_results(self.logs, LABEL, list(steps or self.STEPS))
        return str(cm.exception).replace(self.logs, "<logs>")

    def assert_names_the_causes(self, text):
        self.assertIn("wsl --shutdown", text)
        self.assertIn("ssh", text.lower())
        self.assertIn("memory", text.lower())

    # --- AC-1 -----------------------------------------------------------------------------

    def test_an_empty_results_file_names_the_first_step_and_the_causes(self):
        # the session ended during its first step: no row was written yet
        text = self.refused(steps=[("build", "make"), ("test", "make check")])
        self.assertIn("build", text)
        self.assert_names_the_causes(text)

    def test_a_missing_results_file_names_the_first_step_and_the_causes(self):
        with self.assertRaises(RunnerError) as cm:
            report_from_results(self.logs, LABEL, [("build", "make"), ("test", "make check")])
        text = str(cm.exception).replace(self.logs, "<logs>")
        self.assertIn("build", text)
        self.assert_names_the_causes(text)

    def test_no_rows_skip_a_first_step_without_a_command(self):
        text = self.refused(steps=[("setup", ""), ("build", "make"), ("test", "make check")])
        self.assertIn("build", text)
        self.assertNotIn("setup", text)

    def test_a_first_row_cut_off_while_it_was_written_is_never_a_pass(self):
        for torn in ("set", "setup", "setup\t", "setup\t0", "setup\t0\t"):
            for ending in ("", "\n"):
                with self.subTest(row=torn + ending):
                    self.refused_text(torn + ending)

    def test_a_cut_off_row_after_passing_rows_is_never_a_pass(self):
        for torn in ("bui", "build", "build\t", "build\t0", "build\t0\t"):
            with self.subTest(row=torn):
                self.refused_text("setup\t0\t0\n" + torn)

    def test_a_cut_off_last_row_is_never_a_pass(self):
        whole = "setup\t0\t0\nbuild\t0\t0\ntest\t0\t0\n"
        for torn in ("new_test", "new_test\t0", "new_test\t0\t"):
            with self.subTest(row=torn):
                self.refused_text(whole + torn)

    def test_rows_that_stop_after_the_first_step_name_the_second(self):
        text = self.refused(("setup", 0, 0))
        self.assertIn("build", text)
        self.assert_names_the_causes(text)

    def test_rows_that_stop_in_the_middle_name_the_next_step(self):
        text = self.refused(("setup", 0, 0), ("build", 0, 0))
        self.assertRegex(text, r"\btest\b")
        self.assert_names_the_causes(text)

    def test_only_the_last_step_missing_is_an_error_too(self):
        text = self.refused(("setup", 0, 0), ("build", 0, 0), ("test", 0, 0))
        self.assertIn("new_test", text)
        self.assert_names_the_causes(text)

    def test_two_steps_and_one_row(self):
        text = self.refused(("setup", 0, 0), steps=[("setup", "true"), ("new_test", "run")])
        self.assertIn("new_test", text)

    # --- AC-2 -----------------------------------------------------------------------------

    def test_a_row_for_every_step_reads_as_before(self):
        r = self.report(("setup", 0, 0), ("build", 0, 0), ("test", 0, 0), ("new_test", 0, 0))
        self.assertEqual([s.name for s in r.steps], ["setup", "build", "test", "new_test"])
        self.assertTrue(r.ok)
        self.assertIsNone(r.failed)
        self.assertEqual(r.label, LABEL)
        test = r.step("test")
        self.assertEqual(test.cmd, "make check")
        self.assertEqual(test.returncode, 0)
        self.assertFalse(test.timed_out)
        self.assertEqual(test.stdout, "output of test\n")
        self.assertEqual(test.log_path, os.path.join(self.logs, LABEL + "-test.log"))

    def test_rows_that_end_with_a_failing_step_read_as_before(self):
        r = self.report(("setup", 0, 0), ("build", 2, 0))
        self.assertEqual([s.name for s in r.steps], ["setup", "build"])
        self.assertFalse(r.ok)
        self.assertEqual(r.failed.name, "build")
        self.assertEqual(r.failed.returncode, 2)
        self.assertEqual(r.failed.stdout, "output of build\n")

    def test_a_failing_first_step_reads_as_before(self):
        r = self.report(("setup", 1, 0))
        self.assertEqual([s.name for s in r.steps], ["setup"])
        self.assertEqual(r.failed.name, "setup")

    def test_a_failing_last_step_reads_as_before(self):
        r = self.report(("setup", 0, 0), ("build", 0, 0), ("test", 0, 0), ("new_test", 1, 0))
        self.assertEqual(r.failed.name, "new_test")
        self.assertEqual(r.failed.returncode, 1)

    def test_a_timed_out_step_ends_the_rows_like_a_failing_one(self):
        r = self.report(("setup", 0, 0), ("build", 0, 0), ("test", 124, 1))
        self.assertEqual(r.failed.name, "test")
        self.assertTrue(r.failed.timed_out)
        self.assertEqual(r.failed.returncode, 124)

    def test_a_step_without_a_command_has_no_row_and_none_is_expected(self):
        steps = [("setup", "true"), ("build", ""), ("test", "run"), ("new_test", "run new")]
        r = self.report(("setup", 0, 0), ("test", 0, 0), ("new_test", 0, 0), steps=steps)
        self.assertEqual([s.name for s in r.steps], ["setup", "test", "new_test"])
        self.assertTrue(r.ok)

    def test_a_failed_clone_and_an_empty_file_stay_errors(self):
        self.assertIn("clone", self.refused(("clone", 128, 0)))
        self.refused()

    # --- AC-3 -----------------------------------------------------------------------------

    def assert_mismatch(self, *rows):
        self.assertRegex(self.refused(*rows), r"(?i)\b(do|does) not match\b")

    def test_an_unknown_step_name(self):
        self.assert_mismatch(("setup", 0, 0), ("deploy", 0, 0))

    def test_a_repeated_step(self):
        self.assert_mismatch(("setup", 0, 0), ("setup", 0, 0))

    def test_a_first_row_that_is_not_the_first_step(self):
        self.assert_mismatch(("build", 0, 0), ("setup", 0, 0))

    def test_a_skipped_step(self):
        self.assert_mismatch(("setup", 0, 0), ("test", 0, 0), ("new_test", 0, 0))

    def test_a_row_beyond_the_last_step(self):
        self.assert_mismatch(
            ("setup", 0, 0), ("build", 0, 0), ("test", 0, 0), ("new_test", 0, 0), ("test", 0, 0)
        )

    def test_a_mismatch_in_a_failing_row_is_not_read_as_the_failure(self):
        self.assert_mismatch(("setup", 0, 0), ("deploy", 1, 0))


class _Sessions:
    """One real runner session through the wrapper; the subclasses pick the runner."""

    runner = "wsl"
    inner = ""
    env_name = ""
    steps = [("setup", "true"), ("test", "true"), ("new_test", "true")]

    def setUp(self):
        super().setUp()
        self.use_real_local_runner()
        install(self, self.inner, self.env_name)
        self.head = git(["rev-parse", "HEAD"], self.repo).strip()
        self.logs = os.path.join(self.tmp, "session-logs")

    def session(self, steps=None):
        return self.make_runner().run(
            self.repo, self.head, steps or self.steps, {}, self.logs, LABEL
        )

    def died(self):
        with self.assertRaises(RunnerError) as cm:
            self.session()
        return str(cm.exception).replace(self.logs, "<logs>")

    def names(self, report):
        return [(s.name, s.returncode, s.stdout[-300:]) for s in report.steps]

    # --- AC-1 -----------------------------------------------------------------------------

    @unittest.skipUnless(HAVE_BASH, "needs bash")
    def test_a_session_that_lost_its_last_step_is_an_error_naming_it(self):
        lose_rows(self, {LABEL: 2})
        text = self.died()
        self.assertIn("new_test", text)
        self.assertIn("wsl --shutdown", text)

    @unittest.skipUnless(HAVE_BASH, "needs bash")
    def test_a_session_that_ended_after_setup_is_an_error_naming_the_next_step(self):
        lose_rows(self, {LABEL: 1})
        text = self.died()
        self.assertRegex(text, r"\btest\b")
        self.assertIn("wsl --shutdown", text)

    @unittest.skipUnless(HAVE_BASH, "needs bash")
    def test_a_session_that_lost_every_row_is_an_error_naming_the_first_step(self):
        lose_rows(self, {LABEL: 0})
        text = self.died()
        self.assertIn("setup", text)
        self.assertIn("wsl --shutdown", text)

    # --- AC-2 -----------------------------------------------------------------------------

    @unittest.skipUnless(HAVE_BASH, "needs bash")
    def test_a_complete_session_passes(self):
        report = self.session()
        self.assertTrue(report.ok, self.names(report))
        self.assertEqual([s.name for s in report.steps], ["setup", "test", "new_test"])

    @unittest.skipUnless(HAVE_BASH, "needs bash")
    def test_every_row_written_and_a_failing_exit_still_reads_the_rows(self):
        lose_rows(self, {LABEL: 3})  # nothing lost; only the transport reports a failure
        report = self.session()
        self.assertTrue(report.ok, self.names(report))
        self.assertEqual([s.name for s in report.steps], ["setup", "test", "new_test"])

    @unittest.skipUnless(HAVE_BASH, "needs bash")
    def test_a_failing_step_ends_the_session_and_is_reported(self):
        report = self.session([("setup", "true"), ("test", "exit 3"), ("new_test", "true")])
        self.assertEqual([s.name for s in report.steps], ["setup", "test"], self.names(report))
        self.assertEqual(report.failed.name, "test")
        self.assertEqual(report.failed.returncode, 3)


class WslSessions(_Sessions, RepoCase):
    inner = "wsl_stub"
    env_name = "REVALI_WSL_CMD"

    def make_runner(self):
        return WslRunner(
            PlatformCfg(
                runner="wsl",
                distro="Ubuntu",
                command_timeout_min=1,
                sandbox_dir="~/.revali/sandbox",
            )
        )


class SshSessions(_Sessions, RepoCase):
    inner = "ssh_stub"
    env_name = "REVALI_SSH_CMD"

    def setUp(self):
        super().setUp()
        remote = os.path.join(self.tmp, "remote home")
        os.makedirs(remote)
        os.environ["REVALI_FAKE_REMOTE"] = remote
        os.environ["REVALI_SCP_CMD"] = "%s %s" % (_quote(sys.executable), _quote(SCP_STUB))
        for knob in (
            "REVALI_FAKE_SSH_DOWN",
            "REVALI_FAKE_SSH_BASH_FAILS",
            "REVALI_FAKE_SSH_RM_FAILS",
        ):
            os.environ.pop(knob, None)

    def make_runner(self):
        return SshRunner(
            PlatformCfg(
                runner="ssh",
                host="box",
                command_timeout_min=1,
                sandbox_dir="~/.revali/sandbox",
            )
        )


if __name__ == "__main__":
    unittest.main()
