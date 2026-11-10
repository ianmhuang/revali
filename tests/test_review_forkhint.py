"""Review tests for fix/sandbox-nproc-limit: what revali says when a sandbox runs out of
processes.

AC-6: a step that exits non-zero (not by timeout) with `Resource temporarily unavailable`,
      `can't start new thread` or `EAGAIN` in its log while `max_procs` > 0 gets one line
      starting `revali:` at the end of its log, naming `max_procs` and its value; the exit
      code and the result are unchanged, and the "broken before review" message shows the
      line. Other failures, passing steps, timeouts and `max_procs = 0` get no line.
AC-8: the script's own stderr goes to `<label>-script.log` in the logs directory (also on
      the ssh runner), and a session that ended before a step finished names `max_procs`,
      its value and that file when the file holds one of the three strings and
      `max_procs` > 0; otherwise the message is what it was.

The sessions run the real WslRunner / SshRunner against the repository's stand-ins for
wsl.exe, ssh and scp. The script's own stderr is produced by the script itself: the session
is given an extra file where the checked-out tree has a directory, so the script's `cp`
complains, naming that path, and the directory's name (`EAGAIN`) is one of the strings. A
session that ends early is the stand-in losing result rows after the script ran, or (for
the earlier session's script log) not running the script at all. The limits
the sessions set are large on purpose: on Linux they are really set, and a small one would
count this user's other processes against the test.
"""

import os
import re
import shutil
import sys
import tempfile
import unittest

from revali import EXIT_ERROR
from revali.config import PlatformCfg
from revali.runners import RunnerError, SessionEndedEarly, SshRunner, WslRunner, report_from_results
from tests.fixtures.make_sample_repo import PY, toml_str
from tests.helpers import FAKE_BIN, RepoCase, _quote, claude_entry, git, run_cli

HAVE_BASH = os.name == "nt" or os.path.exists("/bin/bash")
LABEL = "validate-r1"
WSL_STUB = os.path.join(FAKE_BIN, "wsl_stub.py")
SSH_STUB = os.path.join(FAKE_BIN, "ssh_stub.py")
SCP_STUB = os.path.join(FAKE_BIN, "scp_stub.py")
FORK_FAIL = "bash: fork: retry: Resource temporarily unavailable"
SCRIPT_LOG = LABEL + "-script.log"

# wsl_stub, then (when FORKHINT_KEEP_ROWS is set) only the first rows of the session's results
# file are kept and the call fails: what is on disk when the script died part way
CUT_STUB = r"""import os
import sys

sys.path.insert(0, os.environ["FORKHINT_FAKE_BIN"])

import wsl_stub


def main(argv):
    cmd = argv[3:] if argv[:1] == ["-d"] else argv
    is_script = cmd[:1] == ["bash"] and cmd[1:2] != ["-c"]
    if is_script and os.environ.get("FORKHINT_SKIP_SCRIPT"):
        return 0  # the session ended before the script ran a single line
    code = wsl_stub.main(argv)
    keep = os.environ.get("FORKHINT_KEEP_ROWS")
    if keep is None or not is_script:
        return code
    path = os.environ["FORKHINT_RESULTS"]
    with open(path, "r", encoding="utf-8", newline="") as fh:
        rows = [line for line in fh if line.strip()]
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.writelines(rows[: int(keep)])
    return 254


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
"""


def wsl_plat(max_procs):
    return PlatformCfg(
        name="linux",
        runner="wsl",
        distro="Ubuntu",
        command_timeout_min=1,
        sandbox_dir="~/.revali/sandbox",
        max_procs=max_procs,
    )


def ssh_plat(max_procs):
    return PlatformCfg(
        name="linux",
        runner="ssh",
        host="box",
        command_timeout_min=1,
        connect_timeout_s=5,
        transfer_timeout_min=1,
        sandbox_dir="~/.revali/sandbox",
        max_procs=max_procs,
    )


def use_stubs(case):
    case.use_real_local_runner()
    remote = os.path.join(case.tmp, "remote home")
    os.makedirs(remote)
    os.environ["REVALI_FAKE_REMOTE"] = remote
    os.environ["REVALI_WSL_CMD"] = "%s %s" % (_quote(sys.executable), _quote(WSL_STUB))
    os.environ["REVALI_SSH_CMD"] = "%s %s" % (_quote(sys.executable), _quote(SSH_STUB))
    os.environ["REVALI_SCP_CMD"] = "%s %s" % (_quote(sys.executable), _quote(SCP_STUB))
    for knob in (
        "REVALI_FAKE_SSH_DOWN",
        "REVALI_FAKE_SSH_BASH_FAILS",
        "REVALI_FAKE_SSH_RM_FAILS",
        "REVALI_FAKE_SSH_SCRIPT_DROP",
        "FORKHINT_KEEP_ROWS",
        "FORKHINT_SKIP_SCRIPT",
    ):
        os.environ.pop(knob, None)


def hint_lines(text):
    return [ln for ln in text.splitlines() if ln.startswith("revali:")]


@unittest.skipUnless(HAVE_BASH, "needs bash")
class StepLine(RepoCase):
    """AC-6 on one sandbox session."""

    runner = "wsl"

    def setUp(self):
        super().setUp()
        use_stubs(self)
        self.head = git(["rev-parse", "HEAD"], self.repo).strip()
        self.logs = os.path.join(self.tmp, "session-logs")

    def step(self, cmd, max_procs=5000, runner=WslRunner, plat=wsl_plat):
        report = runner(plat(max_procs)).run(
            self.repo, self.head, [("test", cmd)], {}, self.logs, LABEL
        )
        self.assertEqual([s.name for s in report.steps], ["test"])
        return report.steps[0]

    def assert_names_the_limit(self, step, value):
        lines = hint_lines(step.text)
        self.assertEqual(len(lines), 1, step.text)
        line = lines[0]
        self.assertIn("max_procs", line)
        self.assertRegex(line, r"\b%d\b" % value)
        for word in ("process", "thread", "user", "distro", "host", "raise"):
            self.assertRegex(line, "(?i)" + word)
        self.assertRegex(line, r"\b0\b")  # how to turn the limit off
        # appended: the step's own output is kept and the line is the last one
        self.assertEqual(step.text.strip().splitlines()[-1], line)
        with open(step.log_path, "r", encoding="utf-8") as fh:
            self.assertIn(line, fh.read())

    def test_a_failed_fork_gets_the_line_and_keeps_its_exit_code(self):
        step = self.step("echo '%s' >&2; exit 1" % FORK_FAIL)
        self.assertEqual(step.returncode, 1)
        self.assertFalse(step.ok)
        self.assertFalse(step.timed_out)
        self.assertIn(FORK_FAIL, step.text)
        self.assert_names_the_limit(step, 5000)

    def test_the_line_names_the_configured_value(self):
        step = self.step("echo '%s' >&2; exit 1" % FORK_FAIL, max_procs=4096)
        self.assert_names_the_limit(step, 4096)

    def test_a_thread_that_could_not_start_and_eagain(self):
        cases = (("RuntimeError: can't start new thread", 2), ("Error: spawn EAGAIN", 3))
        for text, code in cases:
            with self.subTest(text=text):
                step = self.step('echo "%s"; exit %d' % (text, code))
                self.assertEqual(step.returncode, code)
                self.assertIn(text, step.text)
                self.assert_names_the_limit(step, 5000)

    def test_the_string_anywhere_in_a_longer_log(self):
        step = self.step("echo before; echo '%s'; echo after; exit 7" % FORK_FAIL)
        self.assertEqual(step.returncode, 7)
        self.assertIn("after", step.text)
        self.assert_names_the_limit(step, 5000)

    def test_another_failure_gets_no_line(self):
        step = self.step("echo 'FAIL: test_add'; exit 1")
        self.assertEqual(step.returncode, 1)
        self.assertEqual(hint_lines(step.text), [])
        self.assertNotIn("max_procs", step.text)

    def test_a_passing_step_gets_no_line(self):
        step = self.step("echo '%s'; exit 0" % FORK_FAIL)
        self.assertTrue(step.ok)
        self.assertEqual(hint_lines(step.text), [])

    def test_a_timed_out_step_gets_no_line(self):
        # 124 is what `timeout` returns for a step it stopped
        step = self.step("echo '%s'; exit 124" % FORK_FAIL)
        self.assertTrue(step.timed_out)
        self.assertEqual(hint_lines(step.text), [])

    def test_no_limit_gets_no_line(self):
        step = self.step("echo '%s' >&2; exit 1" % FORK_FAIL, max_procs=0)
        self.assertEqual(step.returncode, 1)
        self.assertIn(FORK_FAIL, step.text)
        self.assertEqual(hint_lines(step.text), [])
        self.assertNotIn("max_procs", step.text)

    def test_the_line_comes_back_from_an_ssh_host(self):
        step = self.step(
            "echo '%s' >&2; exit 1" % FORK_FAIL, max_procs=6000, runner=SshRunner, plat=ssh_plat
        )
        self.assertEqual(step.returncode, 1)
        self.assert_names_the_limit(step, 6000)

    def test_a_later_step_does_not_run_after_the_failed_one(self):
        report = WslRunner(wsl_plat(5000)).run(
            self.repo,
            self.head,
            [("setup", "true"), ("test", "echo '%s'; exit 1" % FORK_FAIL), ("new_test", "true")],
            {},
            self.logs,
            LABEL,
        )
        self.assertEqual([s.name for s in report.steps], ["setup", "test"])
        self.assertEqual(hint_lines(report.steps[0].text), [])
        self.assertEqual(report.failed.name, "test")
        self.assertEqual(len(hint_lines(report.failed.text)), 1)


@unittest.skipUnless(HAVE_BASH, "needs bash")
class RunMessage(RepoCase):
    """AC-6: the message that quotes the failed step's log tail shows the line."""

    runner = "wsl"

    def test_broken_before_review_shows_the_line(self):
        use_stubs(self)
        self.sequential_baseline()
        cfg = self.read("revali.toml")
        new = cfg.replace(
            'setup = "python3 -m venv .venv && .venv/bin/pip install -q -r requirements.txt"',
            'setup = "%s --version"' % PY,
        )
        new = new.replace(
            'test = ".venv/bin/python -m pytest -q"',
            "test = %s" % toml_str("echo '%s' >&2; exit 1" % FORK_FAIL),
        )
        self.assertNotEqual(cfg, new, "the fixture's platform table changed")
        self.write("revali.toml", new)
        self.commit_all("a suite that cannot start a process")
        self.claude(claude_entry())
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertIn("broken before review", out)
        self.assertIn(FORK_FAIL, out)
        self.assertRegex(out, r"revali:[^\n]*max_procs[^\n]*\b4096\b")
        self.assertEqual(self.fake_calls("claude"), [])


class EarlyEndMessage(unittest.TestCase):
    """AC-8 on the files a session leaves: the results rows and `<label>-script.log`."""

    STEPS = [("setup", "make deps"), ("test", "make check")]

    def setUp(self):
        self.logs = tempfile.mkdtemp(prefix="rv-forkhint-")
        self.addCleanup(shutil.rmtree, self.logs, ignore_errors=True)

    def put(self, name, text):
        with open(os.path.join(self.logs, name), "w", encoding="utf-8", newline="") as fh:
            fh.write(text)

    def ended_early(self, rows, script_log, max_procs):
        """The error's text for these result rows and this script log (None = no file)."""
        self.put(LABEL + ".results", rows)
        path = os.path.join(self.logs, SCRIPT_LOG)
        if os.path.isfile(path):
            os.remove(path)
        if script_log is not None:
            self.put(SCRIPT_LOG, script_log)
        with self.assertRaises(SessionEndedEarly) as cm:
            report_from_results(self.logs, LABEL, list(self.STEPS), max_procs=max_procs)
        return str(cm.exception)

    def assert_names_the_limit(self, text, value):
        self.assertIn("max_procs", text)
        self.assertRegex(text, r"\b%d\b" % value)
        self.assertIn(SCRIPT_LOG, text)

    def test_no_row_and_a_failed_fork_in_the_script_log(self):
        for line in (
            "/x/validate-r1.sh: fork: Resource temporarily unavailable",
            "RuntimeError: can't start new thread",
            "spawn EAGAIN",
        ):
            with self.subTest(line=line):
                text = self.ended_early("", "something before\n%s\n" % line, 300)
                self.assertIn("`setup`", text)
                self.assert_names_the_limit(text, 300)

    def test_rows_that_stop_after_a_passing_step(self):
        text = self.ended_early("setup\t0\t0\n", FORK_FAIL + "\n", 4096)
        self.assertIn("`test`", text)
        self.assert_names_the_limit(text, 4096)

    def test_the_message_is_what_it_was_without_the_strings(self):
        for rows in ("", "setup\t0\t0\n"):
            with self.subTest(rows=rows):
                plain = self.ended_early(rows, None, 300)
                self.assertNotIn("max_procs", plain)
                self.assertNotIn(SCRIPT_LOG, plain)
                self.assertEqual(self.ended_early(rows, "", 300), plain)
                # fix/ssh-session-dirs AC-6 (changes this PR's AC-8): a script log that is not
                # empty is named and quoted whatever it says; max_procs only for a fork string
                killed = self.ended_early(rows, "Killed\n", 300)
                self.assertTrue(killed.startswith(plain), killed)
                self.assertIn(SCRIPT_LOG, killed)
                self.assertIn("`Killed`", killed)
                self.assertNotIn("max_procs", killed)

    def test_the_message_is_what_it_was_without_a_limit(self):
        for rows in ("", "setup\t0\t0\n"):
            with self.subTest(rows=rows):
                plain = self.ended_early(rows, None, 300)
                # fix/ssh-session-dirs AC-6: named and quoted, but no max_procs without a limit
                text = self.ended_early(rows, FORK_FAIL + "\n", 0)
                self.assertTrue(text.startswith(plain), text)
                self.assertIn(SCRIPT_LOG, text)
                self.assertIn("`%s`" % FORK_FAIL, text)
                self.assertNotIn("max_procs", text)

    def test_a_session_that_reached_its_end_is_read_as_before(self):
        self.put(SCRIPT_LOG, FORK_FAIL + "\n")
        self.put(LABEL + ".results", "setup\t0\t0\ntest\t0\t0\n")
        report = report_from_results(self.logs, LABEL, list(self.STEPS), max_procs=5000)
        self.assertTrue(report.ok)
        self.put(LABEL + ".results", "setup\t0\t0\ntest\t1\t0\n")
        report = report_from_results(self.logs, LABEL, list(self.STEPS), max_procs=5000)
        self.assertEqual(report.failed.name, "test")
        self.assertEqual(report.failed.returncode, 1)


class _ScriptLogCase(RepoCase):
    """A repository whose tree has the directories `EAGAIN` and `plain`: an extra file of
    either name makes the sandbox script's own `cp` complain on its stderr."""

    runner = "wsl"
    STEPS = [("setup", "true"), ("test", "true")]
    FORK_LIKE = {"EAGAIN": "a file where the tree has a directory\n"}
    OTHER = {"plain": "a file where the tree has a directory\n"}

    def setUp(self):
        super().setUp()
        use_stubs(self)
        self.write("EAGAIN/keep.txt", "kept\n")
        self.write("plain/keep.txt", "kept\n")
        self.commit_all("two directories")
        self.head = git(["rev-parse", "HEAD"], self.repo).strip()
        self.logs = os.path.join(self.tmp, "session-logs")

    def script_log(self):
        path = os.path.join(self.logs, SCRIPT_LOG)
        self.assertTrue(os.path.isfile(path), sorted(os.listdir(self.logs)))
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()


@unittest.skipUnless(HAVE_BASH, "needs bash")
class WslScriptLog(_ScriptLogCase):
    """AC-8 on the wsl runner."""

    def setUp(self):
        super().setUp()
        stub = os.path.join(self.tmp, "forkhint_wsl.py")
        with open(stub, "w", encoding="utf-8", newline="") as fh:
            fh.write(CUT_STUB)
        os.environ["REVALI_WSL_CMD"] = "%s %s" % (_quote(sys.executable), _quote(stub))
        os.environ["FORKHINT_FAKE_BIN"] = FAKE_BIN
        os.environ["FORKHINT_RESULTS"] = os.path.join(self.logs, LABEL + ".results")

    def session(self, extra, max_procs=5000):
        return WslRunner(wsl_plat(max_procs)).run(
            self.repo, self.head, self.STEPS, extra, self.logs, LABEL
        )

    def ended_early(self, rows_kept, extra, max_procs=5000):
        os.environ["FORKHINT_KEEP_ROWS"] = str(rows_kept)
        with self.assertRaises(SessionEndedEarly) as cm:
            self.session(extra, max_procs)
        return str(cm.exception)

    def test_the_scripts_own_stderr_is_in_the_script_log(self):
        report = self.session(self.FORK_LIKE)
        self.assertTrue(report.ok, [(s.name, s.returncode, s.text) for s in report.steps])
        self.assertIn("EAGAIN", self.script_log())
        for step in report.steps:
            self.assertNotIn("EAGAIN", step.text)

    def test_no_row_names_max_procs_and_the_script_log(self):
        text = self.ended_early(0, self.FORK_LIKE)
        self.assertIn("`setup`", text)
        self.assertIn("max_procs", text)
        self.assertRegex(text, r"\b5000\b")
        self.assertIn(SCRIPT_LOG, text)

    def test_rows_that_stop_after_a_passing_step_name_max_procs(self):
        text = self.ended_early(1, self.FORK_LIKE, max_procs=4096)
        self.assertIn("`test`", text)
        self.assertIn("max_procs", text)
        self.assertRegex(text, r"\b4096\b")
        self.assertIn(SCRIPT_LOG, text)

    def test_other_stderr_leaves_the_message_alone(self):
        text = self.ended_early(0, self.OTHER)
        self.assertIn("`setup`", text)
        self.assertNotIn("max_procs", text)
        log = self.script_log()
        self.assertIn("plain", log)  # the script did complain, about something else
        self.assertIsNone(re.search("EAGAIN|Resource temporarily|new thread", log), log)

    def test_no_limit_leaves_the_message_alone(self):
        text = self.ended_early(0, self.FORK_LIKE, max_procs=0)
        self.assertIn("`setup`", text)
        self.assertNotIn("max_procs", text)
        self.assertIn("EAGAIN", self.script_log())

    def test_an_earlier_sessions_script_log_is_not_read_as_this_sessions(self):
        # round 1, F2: the same label and logs directory twice; the first session leaves a
        # script log holding one of the strings, the second never runs a line of its script
        self.assertTrue(self.session(self.FORK_LIKE).ok)
        self.assertIn("EAGAIN", self.script_log())
        os.environ["FORKHINT_SKIP_SCRIPT"] = "1"
        self.addCleanup(os.environ.pop, "FORKHINT_SKIP_SCRIPT", None)
        with self.assertRaises(SessionEndedEarly) as cm:
            self.session({})
        text = str(cm.exception)
        self.assertIn("`setup`", text)
        self.assertNotIn("max_procs", text)
        self.assertNotIn(SCRIPT_LOG, text)


@unittest.skipUnless(HAVE_BASH, "needs bash")
class SshScriptLog(_ScriptLogCase):
    """AC-8 on the ssh runner: the script log comes back with the other logs."""

    def session(self, extra, max_procs=5000):
        return SshRunner(ssh_plat(max_procs)).run(
            self.repo, self.head, self.STEPS, extra, self.logs, LABEL
        )

    def test_the_script_log_is_copied_back(self):
        report = self.session(self.FORK_LIKE)
        self.assertTrue(report.ok, [(s.name, s.returncode, s.text) for s in report.steps])
        self.assertIn("EAGAIN", self.script_log())

    def test_a_session_that_lost_its_rows_names_max_procs(self):
        os.environ["REVALI_FAKE_SSH_SCRIPT_DROP"] = "0"
        with self.assertRaises(RunnerError) as cm:
            self.session(self.FORK_LIKE, max_procs=6000)
        text = str(cm.exception)
        self.assertIn("`setup`", text)
        self.assertIn("max_procs", text)
        self.assertRegex(text, r"\b6000\b")
        self.assertIn(SCRIPT_LOG, text)

    def test_other_stderr_leaves_the_message_alone(self):
        os.environ["REVALI_FAKE_SSH_SCRIPT_DROP"] = "0"
        with self.assertRaises(RunnerError) as cm:
            self.session(self.OTHER, max_procs=6000)
        text = str(cm.exception)
        self.assertIn("`setup`", text)
        self.assertNotIn("max_procs", text)


if __name__ == "__main__":
    unittest.main()
