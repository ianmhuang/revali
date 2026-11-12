"""AC-3 and AC-4 of feature/windows-platform, through `revali run`: preflight probes the
runner of every listed platform in list order, stops at the first that cannot start with
`validate.<name>: ` in front of the reason (one platform: the reason alone, as before), and
a config listing more than one platform gets a note naming the platforms after the first.

The linux table uses the wsl runner and the windows table the ssh runner, so the two probes
are told apart by the executable they call. Both are stand-ins that write to the fake log:
the repository's ssh stub, and a wsl stand-in written here that answers the probe.
"""

import os
import sys
import unittest

from revali import EXIT_ERROR, EXIT_OK
from tests.helpers import FAKE_BIN, RepoCase, _quote, claude_entry, run_cli

SSH_STUB = os.path.join(FAKE_BIN, "ssh_stub.py")
SCP_STUB = os.path.join(FAKE_BIN, "scp_stub.py")

# wsl.exe for the probe only: records the call, then starts (exit 0) or does not (exit 3)
WSL_PROBE_STUB = """import json
import os
import sys

log = os.environ.get("REVALI_FAKE_LOG")
if log:
    with open(log, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"exe": "wsl", "argv": sys.argv[1:]}) + "\\n")
if os.environ.get("REVIEW_FAKE_WSL_DOWN"):
    sys.stderr.write("the distribution could not be started\\n")
    sys.exit(3)
print("git version 2.43.0 (fake)")
"""

WINDOWS_SSH = '\n[validate.windows]\nrunner = "ssh"\nhost = "winbox"\nnew_test = "pytest tests"\n'


class ProbeCase(RepoCase):
    runner = "wsl"

    def setUp(self):
        super().setUp()
        self.use_real_local_runner()  # the real probe, against the stand-ins below
        stub = os.path.join(self.tmp, "wsl_probe_stub.py")
        with open(stub, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(WSL_PROBE_STUB)
        os.environ["REVALI_WSL_CMD"] = "%s %s" % (_quote(sys.executable), _quote(stub))
        os.environ["REVALI_SSH_CMD"] = "%s %s" % (_quote(sys.executable), _quote(SSH_STUB))
        os.environ["REVALI_SCP_CMD"] = "%s %s" % (_quote(sys.executable), _quote(SCP_STUB))
        os.environ["REVALI_FAKE_REMOTE"] = os.path.join(self.tmp, "remote home")
        os.makedirs(os.environ["REVALI_FAKE_REMOTE"])
        os.environ.pop("REVALI_FAKE_SSH_DOWN", None)
        os.environ.pop("REVIEW_FAKE_WSL_DOWN", None)

    def platforms(self, names):
        before = self.read("revali.toml")
        after = before.replace(
            'platforms = ["linux"]', "platforms = [%s]" % ", ".join('"%s"' % n for n in names)
        )
        self.assertNotEqual(before, after, "fixture platforms line not found")
        self.write("revali.toml", after + WINDOWS_SSH)
        self.commit_all("list %s" % " and ".join(names))

    def probes(self):
        """The executables the probes called, in call order."""
        return [c["exe"] for c in self.fake_calls() if c.get("exe") in ("wsl", "ssh")]

    def assert_nothing_pushed(self):
        gh = [c["argv"] for c in self.fake_calls("gh")]
        self.assertFalse(any(a[:2] == ["pr", "create"] for a in gh), gh)
        self.assertEqual(self.fake_calls("claude"), [])
        self.assertEqual(self.fake_calls("scp"), [])

    def run_stopped(self):
        self.claude(claude_entry())
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertNotIn("Traceback", out)
        self.assert_nothing_pushed()
        return out


class EveryPlatformIsProbed(ProbeCase):
    """AC-3"""

    def test_second_platform_fails_after_the_first_was_probed(self):
        self.platforms(["linux", "windows"])
        os.environ["REVALI_FAKE_SSH_DOWN"] = "1"
        out = self.run_stopped()
        self.assertEqual(self.probes(), ["wsl", "ssh"])
        self.assertIn("validate.windows: ", out)
        self.assertIn("winbox", out)
        self.assertNotIn("validate.linux: ", out)

    def test_reversed_list_probes_windows_first(self):
        self.platforms(["windows", "linux"])
        os.environ["REVIEW_FAKE_WSL_DOWN"] = "1"
        out = self.run_stopped()
        self.assertEqual(self.probes(), ["ssh", "wsl"])
        self.assertIn("validate.linux: ", out)
        self.assertIn("Ubuntu", out)
        self.assertNotIn("validate.windows: ", out)
        ssh = self.fake_calls("ssh")[0]["argv"]
        self.assertIn("winbox", ssh)

    def test_first_platform_fails_and_the_second_is_not_probed(self):
        self.platforms(["linux", "windows"])
        os.environ["REVIEW_FAKE_WSL_DOWN"] = "1"
        os.environ["REVALI_FAKE_SSH_DOWN"] = "1"
        out = self.run_stopped()
        self.assertEqual(self.probes(), ["wsl"])
        self.assertIn("validate.linux: ", out)
        self.assertNotIn("validate.windows: ", out)

    def test_first_platform_fails_in_the_reversed_list_too(self):
        self.platforms(["windows", "linux"])
        os.environ["REVIEW_FAKE_WSL_DOWN"] = "1"
        os.environ["REVALI_FAKE_SSH_DOWN"] = "1"
        out = self.run_stopped()
        self.assertEqual(self.probes(), ["ssh"])
        self.assertIn("validate.windows: ", out)
        self.assertNotIn("validate.linux: ", out)

    def test_one_platform_keeps_the_message_without_a_prefix(self):
        os.environ["REVIEW_FAKE_WSL_DOWN"] = "1"
        out = self.run_stopped()
        self.assertEqual(self.probes(), ["wsl"])
        self.assertIn("WSL distro 'Ubuntu'", out)
        self.assertNotIn("validate.linux: ", out)

    def test_a_dry_run_probes_no_platform(self):
        self.platforms(["linux", "windows"])
        os.environ["REVIEW_FAKE_WSL_DOWN"] = "1"
        os.environ["REVALI_FAKE_SSH_DOWN"] = "1"
        code, out = run_cli(["run", "--dry-run"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertEqual(self.probes(), [])


class OnlyTheFirstIsValidated(ProbeCase):
    """AC-4"""

    def notes(self, out):
        return [ln for ln in out.splitlines() if "note:" in ln and "not validated" in ln]

    def test_the_note_names_windows_when_it_comes_second(self):
        self.platforms(["linux", "windows"])
        code, out = run_cli(["run", "--dry-run"])
        self.assertEqual(code, EXIT_OK, out)
        notes = self.notes(out)
        self.assertEqual(len(notes), 1, out)
        self.assertIn("windows", notes[0])

    def test_the_note_names_linux_when_it_comes_second(self):
        self.platforms(["windows", "linux"])
        code, out = run_cli(["run", "--dry-run"])
        self.assertEqual(code, EXIT_OK, out)
        notes = self.notes(out)
        self.assertEqual(len(notes), 1, out)
        self.assertIn("linux", notes[0])

    def test_one_platform_gets_no_note(self):
        code, out = run_cli(["run", "--dry-run"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertEqual(self.notes(out), [])


if __name__ == "__main__":
    unittest.main()
