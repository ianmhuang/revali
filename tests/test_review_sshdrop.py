"""Review tests for fix/sandbox-key-collision, AC-7: on the ssh runner, when the ssh call that
runs the sandbox script ends with exit 255 (the connection went away) and the session is a
sandbox error, the message adds that the script may still be running on the host until its
steps time out. A session that is not a sandbox error, another exit code, and a host that was
never reached (the script never started) get no such sentence.

The real SshRunner runs against the repository's stand-ins for ssh and scp, wrapped by two
stubs written here. The ssh wrapper lets the script run, then plays the dropped connection:
it keeps the first rows of the results file on the host, exits with the planned code and, when
asked, leaves the host unreachable for every later ssh and scp call.
"""

import json
import os
import sys
import unittest

from revali.runners import RunnerError, SshRunner
from tests.helpers import FAKE_BIN, _quote, git
from tests.test_ssh_runner import HAVE_BASH, SshCase, plat

LABEL = "validate-r1"

# The plan is {"exit": code, "keep": rows kept (absent: all), "stay_gone": bool}; an empty plan
# lets every call through untouched.
SSH_WRAP = r"""import json
import os
import sys

sys.path.insert(0, os.environ["SSHDROP_FAKE_BIN"])

import ssh_stub as inner


def main(argv):
    if os.path.exists(os.environ["SSHDROP_GONE"]):
        print("ssh: connect to host box port 22: Connection timed out", file=sys.stderr)
        return 255
    code = inner.main(argv)
    with open(os.environ["SSHDROP_PLAN"], "r", encoding="utf-8") as fh:
        plan = json.load(fh)
    if "exit" not in plan or not argv or not argv[-1].startswith("bash "):
        return code
    if plan.get("keep") is not None:
        for root, _dirs, files in os.walk(os.environ["REVALI_FAKE_REMOTE"]):
            for name in files:
                if name.endswith(".results"):
                    path = os.path.join(root, name)
                    with open(path, "r", encoding="utf-8", newline="") as fh:
                        rows = [line for line in fh if line.strip()]
                    with open(path, "w", encoding="utf-8", newline="") as fh:
                        fh.writelines(rows[: int(plan["keep"])])
    if plan.get("stay_gone"):
        with open(os.environ["SSHDROP_GONE"], "w", encoding="utf-8", newline="") as fh:
            fh.write("gone\n")
    print("Connection to box closed by remote host.", file=sys.stderr)
    return int(plan["exit"])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
"""

SCP_WRAP = r"""import os
import sys

sys.path.insert(0, os.environ["SSHDROP_FAKE_BIN"])

import scp_stub as inner


def main(argv):
    if os.path.exists(os.environ["SSHDROP_GONE"]):
        print("ssh: connect to host box port 22: Connection timed out", file=sys.stderr)
        return 255
    return inner.main(argv)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
"""


class DroppedConnection(SshCase):
    steps = [("setup", "true"), ("test", "true")]

    def setUp(self):
        super().setUp()
        for name, text in (("ssh", SSH_WRAP), ("scp", SCP_WRAP)):
            stub = os.path.join(self.tmp, "sshdrop_%s.py" % name)
            with open(stub, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(text)
            os.environ["REVALI_%s_CMD" % name.upper()] = "%s %s" % (
                _quote(sys.executable),
                _quote(stub),
            )
        os.environ["SSHDROP_FAKE_BIN"] = FAKE_BIN
        os.environ["SSHDROP_PLAN"] = os.path.join(self.tmp, "sshdrop-plan.json")
        os.environ["SSHDROP_GONE"] = os.path.join(self.tmp, "sshdrop-gone")
        os.environ.pop("REVALI_FAKE_SSH_SCRIPT_DROP", None)
        self.plan()
        self.head = git(["rev-parse", "HEAD"], self.repo).strip()
        self.logs = os.path.join(self.tmp, "session-logs")

    def plan(self, **plan):
        with open(os.environ["SSHDROP_PLAN"], "w", encoding="utf-8", newline="") as fh:
            json.dump(plan, fh)

    def session(self, steps=None):
        return SshRunner(plat()).run(
            self.repo, self.head, steps or self.steps, {}, self.logs, LABEL, scope="feature__mul"
        )

    def refused(self):
        with self.assertRaises(RunnerError) as cm:
            self.session()
        return str(cm.exception)

    def assert_says_the_script_may_still_run(self, text):
        self.assertRegex(text, r"(?i)may still be running")
        self.assertRegex(text, r"(?i)time[ -]?out")  # ... until its steps time out
        self.assertIn("box", text)  # the host to look at
        self.assertIn("255", text)

    def assert_says_nothing_about_a_running_script(self, text):
        self.assertNotRegex(text, r"(?i)still (be )?running")

    # --- exit 255 and a sandbox error: the sentence is added --------------------------------

    @unittest.skipUnless(HAVE_BASH, "needs bash")
    def test_rows_lost_with_the_connection(self):
        self.plan(exit=255, keep=1)
        text = self.refused()
        self.assertRegex(text, r"`test`")  # the sandbox error itself is still there
        self.assert_says_the_script_may_still_run(text)

    @unittest.skipUnless(HAVE_BASH, "needs bash")
    def test_no_row_arrived_before_the_connection_went(self):
        self.plan(exit=255, keep=0)
        text = self.refused()
        self.assertRegex(text, r"`setup`")
        self.assert_says_the_script_may_still_run(text)

    @unittest.skipUnless(HAVE_BASH, "needs bash")
    def test_the_logs_cannot_be_fetched_because_the_host_stays_away(self):
        # what a dropped connection usually looks like: the copy back fails as well
        self.plan(exit=255, stay_gone=True)
        text = self.refused()
        self.assert_says_the_script_may_still_run(text)

    # --- no sandbox error, another exit code, a script that never started: nothing added ----

    @unittest.skipUnless(HAVE_BASH, "needs bash")
    def test_a_complete_session_is_read_whatever_ssh_returned(self):
        self.plan(exit=255)
        report = self.session()
        self.assertTrue(report.ok, [(s.name, s.returncode) for s in report.steps])
        self.assertEqual([s.name for s in report.steps], ["setup", "test"])

    @unittest.skipUnless(HAVE_BASH, "needs bash")
    def test_a_failing_step_is_a_result_not_a_sandbox_error(self):
        self.plan(exit=255)
        report = self.session([("setup", "true"), ("test", "exit 3")])
        self.assertEqual(report.failed.name, "test")
        self.assertEqual(report.failed.returncode, 3)

    @unittest.skipUnless(HAVE_BASH, "needs bash")
    def test_another_exit_code_adds_nothing(self):
        self.plan(exit=1, keep=1)
        text = self.refused()
        self.assertRegex(text, r"`test`")
        self.assert_says_nothing_about_a_running_script(text)

    def test_a_host_that_was_never_reached_adds_nothing(self):
        # exit 255 too, but from the first call: the script was never started
        os.environ["REVALI_FAKE_SSH_DOWN"] = "1"
        text = self.refused()
        self.assertIn("255", text)
        self.assert_says_nothing_about_a_running_script(text)


if __name__ == "__main__":
    unittest.main()
