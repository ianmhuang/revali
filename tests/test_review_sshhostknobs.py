"""Acceptance tests for feature/validate-every-platform, AC-8: each knob of the ssh stand-in
takes today's value (it acts on every host) or the hosts it acts on, so two ssh platforms on
two hosts can be told apart and one of them made to fail. The stand-ins are run as revali
runs them: as separate processes, the host first.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest

from tests.helpers import FAKE_BIN

SSH_STUB = os.path.join(FAKE_BIN, "ssh_stub.py")
SCP_STUB = os.path.join(FAKE_BIN, "scp_stub.py")
ROWS = "setup 0\nbuild 0\ntest 0\n"


class KnobCase(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="revali knobs "))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.remote = os.path.join(self.tmp, "remote")
        self.work = os.path.join(self.tmp, "work")
        os.makedirs(self.remote)
        os.makedirs(self.work)
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("REVALI_FAKE_")}
        self.env["REVALI_FAKE_REMOTE"] = self.remote

    def call(self, stub, args, knobs):
        return subprocess.run(
            [sys.executable, stub] + list(args),
            env=dict(self.env, **knobs),
            cwd=self.work,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )

    def ssh(self, host, *words, **knobs):
        return self.call(SSH_STUB, [host] + list(words), knobs)

    def scp(self, src, dst, **knobs):
        return self.call(SCP_STUB, [src, dst], knobs)

    def put(self, name, text):
        path = os.path.join(self.remote, name)
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        return path

    def need_bash(self):
        """A script the stand-in can run on the "host"; skips where it finds no bash."""
        self.put("x.sh", "exit 0\n")
        res = self.ssh("linbox", "bash", "x.sh")
        if res.returncode != 0 and "no bash for the stub" in res.stderr:
            self.skipTest("the ssh stand-in found no bash on this machine")
        self.assertEqual(res.returncode, 0, res.stderr)


class Down(KnobCase):
    def test_one_still_refuses_every_host(self):
        for host in ("winbox", "linbox"):
            self.assertEqual(self.ssh(host, "true", REVALI_FAKE_SSH_DOWN="1").returncode, 255)

    def test_a_host_name_refuses_that_host_only(self):
        down = self.ssh("winbox", "true", REVALI_FAKE_SSH_DOWN="winbox")
        self.assertEqual(down.returncode, 255)
        self.assertIn("winbox", down.stderr)
        up = self.ssh("linbox", "true", REVALI_FAKE_SSH_DOWN="winbox")
        self.assertEqual(up.returncode, 0, up.stderr)

    def test_a_list_refuses_each_host_it_names(self):
        for host in ("linbox", "winbox"):
            res = self.ssh(host, "true", REVALI_FAKE_SSH_DOWN="linbox,winbox")
            self.assertEqual(res.returncode, 255, host)
        other = self.ssh("macbox", "true", REVALI_FAKE_SSH_DOWN="linbox,winbox")
        self.assertEqual(other.returncode, 0, other.stderr)

    def test_scp_follows_the_same_hosts(self):
        with open(os.path.join(self.work, "up.txt"), "w", encoding="utf-8", newline="\n") as fh:
            fh.write("payload\n")
        down = self.scp("up.txt", "winbox:in", REVALI_FAKE_SSH_DOWN="winbox")
        self.assertEqual(down.returncode, 255)
        self.assertFalse(os.path.exists(os.path.join(self.remote, "in", "up.txt")))
        up = self.scp("up.txt", "linbox:in", REVALI_FAKE_SSH_DOWN="winbox")
        self.assertEqual(up.returncode, 0, up.stderr)
        self.assertTrue(os.path.isfile(os.path.join(self.remote, "in", "up.txt")))
        every = self.scp("up.txt", "linbox:again", REVALI_FAKE_SSH_DOWN="1")
        self.assertEqual(every.returncode, 255)

    def test_scp_from_a_named_host_is_refused_too(self):
        self.put("down.txt", "payload\n")
        down = self.scp("winbox:down.txt", ".", REVALI_FAKE_SSH_DOWN="winbox")
        self.assertEqual(down.returncode, 255)
        up = self.scp("linbox:down.txt", ".", REVALI_FAKE_SSH_DOWN="winbox")
        self.assertEqual(up.returncode, 0, up.stderr)
        self.assertTrue(os.path.isfile(os.path.join(self.work, "down.txt")))

    def test_scp_naming_no_host_is_refused_by_one_only(self):
        # round 2: today's value keeps today's reach, a call that names no remote host
        # included; a host list acts on the hosts it names, and this call names none
        with open(os.path.join(self.work, "up.txt"), "w", encoding="utf-8", newline="\n") as fh:
            fh.write("payload\n")
        every = self.scp("up.txt", "copy.txt", REVALI_FAKE_SSH_DOWN="1")
        self.assertEqual(every.returncode, 255, every.stderr)
        named = self.scp("up.txt", "copy.txt", REVALI_FAKE_SSH_DOWN="winbox")
        self.assertNotEqual(named.returncode, 255, named.stderr)
        self.assertNotIn("Connection refused", named.stderr)


class BashFails(KnobCase):
    def test_one_still_fails_bash_on_every_host(self):
        for host in ("winbox", "linbox"):
            res = self.ssh(host, "bash", "x.sh", REVALI_FAKE_SSH_BASH_FAILS="1")
            self.assertEqual(res.returncode, 127, host)
            self.assertIn("bash: cannot execute", res.stderr)

    def test_a_host_name_fails_bash_on_that_host_only(self):
        self.put("x.sh", "exit 0\n")
        named = self.ssh("winbox", "bash", "x.sh", REVALI_FAKE_SSH_BASH_FAILS="winbox")
        self.assertEqual(named.returncode, 127)
        self.assertIn("bash: cannot execute", named.stderr)
        other = self.ssh("linbox", "bash", "x.sh", REVALI_FAKE_SSH_BASH_FAILS="winbox")
        self.assertNotEqual(other.returncode, 127, other.stderr)
        self.assertNotIn("bash: cannot execute", other.stderr)


class ScriptDrop(KnobCase):
    def rows(self):
        with open(os.path.join(self.remote, "s.results"), "r", encoding="utf-8") as fh:
            return fh.read()

    def test_a_count_alone_still_drops_on_every_host(self):
        self.need_bash()
        self.put("s.results", ROWS)
        res = self.ssh("linbox", "bash", "x.sh", REVALI_FAKE_SSH_SCRIPT_DROP="2")
        self.assertEqual(res.returncode, 255, res.stderr)
        self.assertEqual(self.rows(), "setup 0\nbuild 0\n")

    def test_a_count_at_a_host_drops_on_that_host_only(self):
        self.need_bash()
        self.put("s.results", ROWS)
        other = self.ssh("linbox", "bash", "x.sh", REVALI_FAKE_SSH_SCRIPT_DROP="1@winbox")
        self.assertEqual(other.returncode, 0, other.stderr)
        self.assertEqual(self.rows(), ROWS)
        named = self.ssh("winbox", "bash", "x.sh", REVALI_FAKE_SSH_SCRIPT_DROP="1@winbox")
        self.assertEqual(named.returncode, 255, named.stderr)
        self.assertEqual(self.rows(), "setup 0\n")

    def test_zero_rows_at_a_host(self):
        self.need_bash()
        self.put("s.results", ROWS)
        named = self.ssh("winbox", "bash", "x.sh", REVALI_FAKE_SSH_SCRIPT_DROP="0@linbox,winbox")
        self.assertEqual(named.returncode, 255, named.stderr)
        self.assertEqual(self.rows(), "")


if __name__ == "__main__":
    unittest.main()
