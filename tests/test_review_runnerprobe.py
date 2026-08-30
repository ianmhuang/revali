"""AC-3: preflight probes the configured runner before anything is pushed. An ssh host
that is unreachable stops the run with exit 1 naming the host; a WSL distro that does
not start stops the run the same way; `--dry-run` skips both probes.
"""
import os
import sys
import unittest

from tests.helpers import FAKE_BIN, RepoCase, _quote, claude_entry, run_cli
from revali import EXIT_ERROR, EXIT_OK

SSH_STUB = os.path.join(FAKE_BIN, "ssh_stub.py")
SCP_STUB = os.path.join(FAKE_BIN, "scp_stub.py")
# a wsl.exe that never starts anything
FAILING_WSL = '%s -c "raise SystemExit(3)"' % _quote(sys.executable)


class SshProbe(RepoCase):
    runner = "wsl"

    def setUp(self):
        super().setUp()
        self.use_real_local_runner()
        remote = os.path.join(self.tmp, "remote home")
        os.makedirs(remote)
        os.environ["REVALI_FAKE_REMOTE"] = remote
        os.environ["REVALI_SSH_CMD"] = "%s %s" % (_quote(sys.executable), _quote(SSH_STUB))
        os.environ["REVALI_SCP_CMD"] = "%s %s" % (_quote(sys.executable), _quote(SCP_STUB))
        os.environ.pop("REVALI_FAKE_SSH_DOWN", None)
        before = self.read("revali.toml")
        after = before.replace('runner = "wsl"\ndistro = "Ubuntu"\n', 'runner = "ssh"\nhost = "box"\n')
        self.assertNotEqual(before, after, "fixture platform table not found")
        self.write("revali.toml", after)
        self.commit_all("ssh runner")

    def test_unreachable_host_stops_with_exit_1_before_anything_is_pushed(self):
        os.environ["REVALI_FAKE_SSH_DOWN"] = "1"
        self.claude(claude_entry())
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertIn("box", out)
        gh = [c["argv"] for c in self.fake_calls("gh")]
        self.assertFalse(any(a[:2] == ["pr", "create"] for a in gh), gh)
        self.assertEqual(self.fake_calls("claude"), [])
        self.assertEqual(self.fake_calls("scp"), [])

    def test_probe_asks_the_host_for_git_and_timeout(self):
        self.claude(claude_entry())
        run_cli(["run", "--foreground"])   # the outcome is not the point; the first ssh call is
        ssh = self.fake_calls("ssh")
        self.assertTrue(ssh, "preflight made no ssh call")
        first = ssh[0]["argv"]
        self.assertIn("box", first)
        self.assertIn("git", first)
        self.assertIn("timeout", first)
        self.assertIn("BatchMode=yes", first)
        self.assertNotIn("bash", first)

    def test_dry_run_skips_the_ssh_probe(self):
        os.environ["REVALI_FAKE_SSH_DOWN"] = "1"
        code, out = run_cli(["run", "--dry-run"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertEqual(self.fake_calls("ssh"), [])
        self.assertEqual(self.fake_calls("scp"), [])


class WslProbe(RepoCase):
    runner = "wsl"

    def setUp(self):
        super().setUp()
        self.use_real_local_runner()
        os.environ["REVALI_WSL_CMD"] = FAILING_WSL

    def test_distro_that_does_not_start_stops_with_exit_1_in_preflight(self):
        self.claude(claude_entry())
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertIn("Ubuntu", out)
        self.assertNotIn("baseline", out)   # preflight, not the sandbox run
        self.assertFalse(any(c["argv"][:2] == ["pr", "create"] for c in self.fake_calls("gh")))
        self.assertEqual(self.fake_calls("claude"), [])

    def test_dry_run_skips_the_wsl_probe(self):
        code, out = run_cli(["run", "--dry-run"])
        self.assertEqual(code, EXIT_OK, out)


if __name__ == "__main__":
    unittest.main()
