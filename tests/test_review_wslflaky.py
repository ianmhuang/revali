"""Review tests for fix/wsl-transient-retry, through the CLI (`revali run --foreground`).

A wsl.exe stand-in written by the test fails chosen calls the way the WSL service did:
its own message in UTF-16 (a NUL between the letters once read as UTF-8) and exit -1.
The retry count, the pause and the patterns are set where a user sets them, in
`[validate.linux]` of revali.toml; the pause is observed by recording `time.sleep`.

AC-1: a probe that fails once is tried again and the run goes on, with one line in
      logs/revali.log naming the error, the attempt and the wait; a run in which the
      probe, a `wslpath` and a sandbox script each fail once still ends ready to merge.
AC-2: a service that stays down ends the run with exit 1 after three attempts; the
      message is readable (no NUL) and says what `wsl --shutdown` does.
AC-3: the pause is `wsl_retry_wait_s` (30 by default), the count `wsl_retries`
      (2 by default), and 0 means one call.
AC-4: a failure without a configured pattern gets exactly one call.
AC-6: the patterns come from `wsl_transient_errors`.
"""

import json
import os
import re
import sys
import time
import unittest
from unittest import mock

from revali import EXIT_ERROR, EXIT_OK
from tests.fixtures.make_sample_repo import LOCAL_NEW_TEST, LOCAL_TEST, PY, toml_str
from tests.helpers import FAKE_BIN, RepoCase, _quote, claude_entry, run_cli

HAVE_BASH = os.name == "nt" or os.path.exists("/bin/bash")
TIMEOUT = "Wsl/Service/WSAETIMEDOUT"
TIMEOUT_TEXT = (
    "The operation timed out because a response was not received from the virtual machine "
    "or container. \r\nError code: " + TIMEOUT + "\r\n"
)
OTHER = "Wsl/Service/E_REVIEW_OTHER"
OTHER_TEXT = "Something else went wrong. \r\nError code: " + OTHER + "\r\n"
NOT_FOUND_TEXT = (
    "There is no distribution with the supplied name.\r\n"
    "Error code: Wsl/Service/WSL_E_DISTRO_NOT_FOUND\r\n"
)
# how a failing call looks: wsl.exe's own errors are UTF-16 and exit -1 (4294967295 on
# Windows, 255 elsewhere); a failure from inside the distro is plain text and a small exit
SERVICE_DOWN = {"text": TIMEOUT_TEXT, "exit": -1, "utf16": True}
PLAIN_FAILURE = {"text": "wslpath: boom\n", "exit": 1, "utf16": False}
WAIT = 7  # a pause no other part of revali asks for, so the recorded sleeps can be told apart

# Fails a planned number of calls per kind (`probe` = bash -c, `wslpath`, `script` = bash
# <file>), before running them or (`after`) once they ran; everything else is handed to
# the repository's wsl_stub.py. One line per call goes to REVIEW_WSL_CALLS.
STUB = r"""import json
import os
import sys

sys.path.insert(0, os.environ["REVIEW_FAKE_BIN"])

import wsl_stub


def kind_of(cmd):
    if cmd and cmd[0] == "wslpath":
        return "wslpath"
    if len(cmd) >= 2 and cmd[0] == "bash" and cmd[1] == "-c":
        return "probe"
    if cmd and cmd[0] == "bash":
        return "script"
    return "other"


def fail(how):
    sys.stdout.flush()
    encoding = "utf-16-le" if how.get("utf16", True) else "utf-8"
    sys.stdout.buffer.write(how["text"].encode(encoding))
    sys.stdout.buffer.flush()
    return int(how.get("exit", -1))


def main(argv):
    cmd = argv[3:] if len(argv) >= 4 and argv[0] == "-d" and argv[2] == "-e" else argv
    kind = kind_of(cmd)
    with open(os.environ["REVIEW_WSL_CALLS"], "a", encoding="utf-8", newline="") as fh:
        fh.write(kind + "\n")
    path = os.environ["REVIEW_WSL_PLAN"]
    with open(path, "r", encoding="utf-8") as fh:
        plan = json.load(fh)
    how = plan["how"].get(kind, plan["default"])
    before = plan["before"].get(kind, 0) > 0
    after = not before and plan["after"].get(kind, 0) > 0
    if before:
        plan["before"][kind] -= 1
    if after:
        plan["after"][kind] -= 1
    with open(path, "w", encoding="utf-8", newline="") as fh:
        json.dump(plan, fh)
    if before:
        return fail(how)
    code = 0 if kind == "probe" else wsl_stub.main(argv)
    if after:
        return fail(how)
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
"""


class FlakyCase(RepoCase):
    """The fixture repository on the wsl runner, with the planned stand-in as wsl.exe."""

    runner = "wsl"

    def setUp(self):
        super().setUp()
        self.use_real_local_runner()
        stub = os.path.join(self.tmp, "review_wsl.py")
        with open(stub, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(STUB)
        os.environ["REVALI_WSL_CMD"] = "%s %s" % (_quote(sys.executable), _quote(stub))
        os.environ["REVIEW_FAKE_BIN"] = FAKE_BIN
        self.plan_path = os.path.join(self.tmp, "review-wsl-plan.json")
        self.calls_path = os.path.join(self.tmp, "review-wsl-calls.txt")
        os.environ["REVIEW_WSL_PLAN"] = self.plan_path
        os.environ["REVIEW_WSL_CALLS"] = self.calls_path
        self.plan()

    def plan(self, before=None, after=None, how=None, default=None):
        data = {
            "before": before or {},
            "after": after or {},
            "how": how or {},
            "default": default or SERVICE_DOWN,
        }
        with open(self.plan_path, "w", encoding="utf-8", newline="") as fh:
            json.dump(data, fh)

    def calls(self, kind):
        if not os.path.isfile(self.calls_path):
            return []
        with open(self.calls_path, "r", encoding="utf-8") as fh:
            return [ln.strip() for ln in fh if ln.strip() == kind]


class CliCase(FlakyCase):
    def configure(self, *lines):
        """Add keys to [validate.linux] and make its commands ones the host's bash runs."""
        before = self.read("revali.toml")
        extra = "".join(ln + "\n" for ln in lines)
        cfg = before.replace('distro = "Ubuntu"\n', 'distro = "Ubuntu"\n' + extra, 1)
        cfg = cfg.replace(
            'setup = "python3 -m venv .venv && .venv/bin/pip install -q -r requirements.txt"',
            'setup = "%s --version"' % PY,
        )
        cfg = cfg.replace(
            'test = ".venv/bin/python -m pytest -q"', "test = %s" % toml_str(LOCAL_TEST)
        )
        cfg = cfg.replace(
            'new_test = ".venv/bin/python -m pytest -q tests"',
            "new_test = %s" % toml_str(LOCAL_NEW_TEST),
        )
        self.assertNotEqual(before, cfg, "fixture platform table not found")
        for ln in lines:
            self.assertIn(ln + "\n", cfg)
        self.write("revali.toml", cfg)
        self.commit_all("review: wsl settings")

    def run_revali(self):
        """(exit code, stdout, every pause asked of time.sleep). The retry pauses (WAIT, or
        the default 30) are recorded and skipped; any other sleep really happens."""
        waits = []
        real = time.sleep

        def fake(seconds):
            waits.append(seconds)
            if seconds not in (WAIT, 30):
                real(seconds)

        with mock.patch("time.sleep", side_effect=fake):
            code, out = run_cli(["run", "--foreground"])
        return code, out, waits

    def log_lines(self, needle):
        path = os.path.join(self.rdir(), "logs", "revali.log")
        if not os.path.isfile(path):
            return []
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return [ln.rstrip("\n") for ln in fh if needle in ln]

    def assert_nothing_pushed(self):
        gh = [c["argv"] for c in self.fake_calls("gh")]
        self.assertFalse(any(a[:2] == ["pr", "create"] for a in gh), gh)
        self.assertEqual(self.fake_calls("claude"), [])


class ProbeRecovers(CliCase):
    """AC-1 (and AC-4 for the `wslpath` that ends the run)."""

    def test_probe_fails_once_then_the_run_goes_on_and_the_log_has_one_retry_line(self):
        self.configure("wsl_retry_wait_s = %d" % WAIT)
        # the probe fails once with the service timeout; the first wslpath of the baseline
        # then fails for an unrelated reason, which ends the run cheaply past preflight
        self.plan(before={"probe": 1, "wslpath": 99}, how={"wslpath": PLAIN_FAILURE})
        self.claude(claude_entry())
        code, out, waits = self.run_revali()
        self.assertEqual(len(self.calls("probe")), 2, out)
        self.assertEqual(waits.count(WAIT), 1, waits)
        lines = self.log_lines(TIMEOUT)
        self.assertEqual(len(lines), 1, lines)
        self.assertNotIn("\x00", lines[0])
        # the line names the attempt and the wait (the timestamp's digits are not counted)
        numbers = re.findall(r"\d+", re.sub(r"^\[\d\d:\d\d:\d\d\]\s*", "", lines[0]))
        self.assertIn("1", numbers, lines[0])
        self.assertIn(str(WAIT), numbers, lines[0])
        # AC-4: a failing wslpath without a configured pattern is one call, today's message
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertEqual(len(self.calls("wslpath")), 1, out)
        self.assertIn("boom", out)
        self.assertNotIn("wsl --shutdown", out)

    @unittest.skipUnless(HAVE_BASH, "needs bash")
    def test_probe_wslpath_and_script_each_fail_once_and_the_run_ends_ready(self):
        self.configure("wsl_retry_wait_s = %d" % WAIT)
        self.plan(before={"probe": 1, "wslpath": 1, "script": 1})
        self.claude(claude_entry())
        code, out, waits = self.run_revali()
        self.assertEqual(code, EXIT_OK, out)
        self.assertEqual(len(self.calls("probe")), 2)
        self.assertEqual(waits.count(WAIT), 3, waits)
        lines = self.log_lines(TIMEOUT)
        self.assertEqual(len(lines), 3, lines)
        for line in lines:
            self.assertNotIn("\x00", line)


class ServiceStaysDown(CliCase):
    """AC-2, and the defaults of AC-3."""

    def test_default_is_three_attempts_30_s_apart_then_exit_1_with_the_hint(self):
        self.configure()  # no wsl_* key: defaults.toml decides
        self.plan(before={"probe": 99})
        self.claude(claude_entry())
        code, out, waits = self.run_revali()
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertEqual(len(self.calls("probe")), 3, out)
        self.assertEqual(waits.count(30), 2, waits)
        self.assertNotIn("\x00", out)
        self.assertIn(TIMEOUT, out)
        self.assertIn("The operation timed out", out)  # readable only without the NULs
        self.assertIn("wsl --shutdown", out)
        self.assertRegex(out, r"(?i)stop\w*\s+every\s+running\s+distro")
        self.assert_nothing_pushed()


class Settings(CliCase):
    """AC-3 and AC-6: count, pause and patterns come from the platform table."""

    def test_count_and_pause_follow_the_configuration(self):
        self.configure("wsl_retries = 4", "wsl_retry_wait_s = %d" % WAIT)
        self.plan(before={"probe": 99})
        code, out, waits = self.run_revali()
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertEqual(len(self.calls("probe")), 5, out)
        self.assertEqual(waits.count(WAIT), 4, waits)
        self.assertEqual(waits.count(30), 0, waits)

    def test_zero_retries_is_one_call_and_no_pause(self):
        self.configure("wsl_retries = 0", "wsl_retry_wait_s = %d" % WAIT)
        self.plan(before={"probe": 1})  # a second call would have succeeded
        code, out, waits = self.run_revali()
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertEqual(len(self.calls("probe")), 1, out)
        self.assertEqual(waits.count(WAIT), 0, waits)
        self.assertIn(TIMEOUT, out)
        self.assert_nothing_pushed()

    def test_a_pattern_of_ones_own_is_retried(self):
        self.configure('wsl_transient_errors = ["%s"]' % OTHER, "wsl_retry_wait_s = %d" % WAIT)
        self.plan(
            before={"probe": 2, "wslpath": 99},
            how={"wslpath": PLAIN_FAILURE},
            default={"text": OTHER_TEXT, "exit": -1, "utf16": True},
        )
        code, out, waits = self.run_revali()
        self.assertEqual(len(self.calls("probe")), 3, out)  # two retries, the third call works
        self.assertEqual(waits.count(WAIT), 2, waits)
        self.assertEqual(len(self.log_lines(OTHER)), 2)
        self.assertEqual(len(self.calls("wslpath")), 1, out)  # past preflight
        self.assertEqual(code, EXIT_ERROR, out)

    def test_the_timeout_is_not_retried_when_the_list_does_not_name_it(self):
        self.configure('wsl_transient_errors = ["%s"]' % OTHER, "wsl_retry_wait_s = %d" % WAIT)
        self.plan(before={"probe": 1})
        code, out, waits = self.run_revali()
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertEqual(len(self.calls("probe")), 1, out)
        self.assertEqual(waits.count(WAIT), 0, waits)
        self.assertNotIn("wsl --shutdown", out)


class NotTransient(CliCase):
    """AC-4: one call and today's message for anything without a configured pattern."""

    def test_another_wsl_error_is_one_call_without_the_hint(self):
        self.configure("wsl_retry_wait_s = %d" % WAIT)
        self.plan(before={"probe": 99}, default={"text": NOT_FOUND_TEXT, "exit": -1, "utf16": True})
        code, out, waits = self.run_revali()
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertEqual(len(self.calls("probe")), 1, out)
        self.assertEqual(waits.count(WAIT), 0, waits)
        self.assertIn("Ubuntu", out)  # the probe's message names the distro, as before
        self.assertNotIn("wsl --shutdown", out)
        self.assert_nothing_pushed()

    def test_a_probe_that_exits_1_is_one_call_without_the_hint(self):
        self.configure("wsl_retry_wait_s = %d" % WAIT)
        self.plan(
            before={"probe": 99},
            default={"text": "bash: git: command not found\n", "exit": 1, "utf16": False},
        )
        code, out, waits = self.run_revali()
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertEqual(len(self.calls("probe")), 1, out)
        self.assertEqual(waits.count(WAIT), 0, waits)
        self.assertIn("Ubuntu", out)
        self.assertIn("command not found", out)
        self.assertNotIn("wsl --shutdown", out)


if __name__ == "__main__":
    unittest.main()
