"""SSH runner: config, the generated script, transport order, cleanup, preflight probe.

The remote host is a directory (REVALI_FAKE_REMOTE) served by ssh_stub / scp_stub; the
sandbox script itself runs with the host's bash, as the WSL runner tests do.
"""
import os
import sys
import unittest

from tests.helpers import FAKE_BIN, RepoCase, _quote, claude_entry, git, run_cli
from tests.fixtures.make_sample_repo import LOCAL_NEW_TEST, LOCAL_TEST, PY, toml_str
from revali import EXIT_ERROR, EXIT_OK
from revali.config import PlatformCfg
from revali.runners import RunnerError, SshRunner, sandbox_root

SSH_STUB = os.path.join(FAKE_BIN, "ssh_stub.py")
SCP_STUB = os.path.join(FAKE_BIN, "scp_stub.py")
HAVE_BASH = os.name == "nt" or os.path.exists("/bin/bash")


def plat(**kw):
    base = dict(runner="ssh", host="box", command_timeout_min=1, sandbox_dir="~/.revali/sandbox")
    base.update(kw)
    return PlatformCfg(**base)


class SshCase(RepoCase):
    runner = "wsl"   # the fixture's wsl platform table, switched to ssh below

    def setUp(self):
        super().setUp()
        self.use_real_local_runner()
        self.remote = os.path.join(self.tmp, "remote home")
        os.makedirs(self.remote)
        os.environ["REVALI_FAKE_REMOTE"] = self.remote
        os.environ["REVALI_SSH_CMD"] = "%s %s" % (_quote(sys.executable), _quote(SSH_STUB))
        os.environ["REVALI_SCP_CMD"] = "%s %s" % (_quote(sys.executable), _quote(SCP_STUB))
        for knob in ("REVALI_FAKE_SSH_DOWN", "REVALI_FAKE_SSH_BASH_FAILS"):
            os.environ.pop(knob, None)
        cfg = self.read("revali.toml")
        cfg = cfg.replace('runner = "wsl"\ndistro = "Ubuntu"\n', 'runner = "ssh"\nhost = "box"\n')
        cfg = cfg.replace('setup = "python3 -m venv .venv && .venv/bin/pip install -q -r requirements.txt"',
                          'setup = "%s --version"' % PY)
        cfg = cfg.replace('test = ".venv/bin/python -m pytest -q"', 'test = %s' % toml_str(LOCAL_TEST))
        cfg = cfg.replace('new_test = ".venv/bin/python -m pytest -q tests"', 'new_test = %s' % toml_str(LOCAL_NEW_TEST))
        self.assertIn('host = "box"', cfg)
        self.write("revali.toml", cfg)
        self.commit_all("ssh runner")

    def remote_leftovers(self):
        found = []
        for root, dirs, files in os.walk(self.remote):
            for f in files:
                found.append(os.path.relpath(os.path.join(root, f), self.remote))
        return found

    def calls(self):
        return [(c["exe"], c["argv"]) for c in self.fake_calls() if c["exe"] in ("ssh", "scp")]


class ConfigTests(SshCase):
    def test_ssh_without_host_is_a_config_error(self):
        self.write("revali.toml", self.read("revali.toml").replace('host = "box"\n', ""))
        self.commit_all("no host")
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertIn("validate.linux.host is required for the ssh runner", out)

    def test_unknown_runner_names_the_allowed_ones(self):
        self.write("revali.toml", self.read("revali.toml").replace('runner = "ssh"', 'runner = "docker"'))
        self.commit_all("docker")
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertIn("validate.linux.runner must be one of wsl, local, ssh", out)

    def test_sandbox_root_forms(self):
        self.assertEqual(sandbox_root(plat(sandbox_dir="~/.revali/sandbox")), ("$HOME/.revali/sandbox", ".revali/sandbox"))
        self.assertEqual(sandbox_root(plat(sandbox_dir="/srv/revali/")), ("/srv/revali", "/srv/revali"))
        self.assertEqual(sandbox_root(plat(sandbox_dir="~")), ("$HOME", "."))


class ScriptTests(unittest.TestCase):
    def test_clone_source_is_the_bundle(self):
        r = SshRunner(plat())
        text = r.script("$HOME/.revali/sandbox/sample/validate-r1-in/validate-r1.bundle",
                        "$HOME/.revali/sandbox/sample/validate-r1-logs",
                        "$HOME/.revali/sandbox/sample/validate-r1-in/validate-r1-extra", "abc123",
                        [("setup", "true"), ("build", ""), ("test", "pytest -q")],
                        "validate-r1", "$HOME/.revali/sandbox/sample/validate-r1", 60)
        self.assertIn('HOST="$HOME/.revali/sandbox/sample/validate-r1-in/validate-r1.bundle"', text)
        self.assertIn('LOGS="$HOME/.revali/sandbox/sample/validate-r1-logs"', text)
        self.assertIn("run_step setup ||", text)
        self.assertIn("run_step test ||", text)
        self.assertNotIn("run_step build", text)
        self.assertIn("STEP_TIMEOUT=60", text)
        self.assertNotIn("\r", text)


class TransportTests(SshCase):
    @unittest.skipUnless(HAVE_BASH, "needs bash")
    def test_run_through_bash(self):
        r = SshRunner(plat())
        logs = os.path.join(self.rdir(), "logs")
        head = git(["rev-parse", "HEAD"], self.repo).strip()
        new_test = ("import unittest\nfrom src.calc import mul\n\nclass T(unittest.TestCase):\n"
                    "    def test_m(self):\n        self.assertEqual(mul(2, 3), 6)\n")
        report = r.run(self.repo, head, [("setup", PY + " --version"), ("test", LOCAL_TEST), ("new_test", LOCAL_NEW_TEST)],
                       {"tests/test_review_mul.py": new_test}, logs, "validate-r1")
        self.assertEqual([s.name for s in report.steps], ["setup", "test", "new_test"])
        self.assertTrue(report.ok, [(s.name, s.returncode, s.stdout[-300:]) for s in report.steps])
        self.assertIn("Ran 1 test", report.step("new_test").stdout)
        # the same files as the WSL runner leaves behind, and no bundle or extra dir
        for name in ("validate-r1.sh", "validate-r1.results", "validate-r1-clone.log",
                     "validate-r1-setup.log", "validate-r1-test.log", "validate-r1-new_test.log"):
            self.assertTrue(os.path.isfile(os.path.join(logs, name)), name)
        self.assertFalse(os.path.exists(os.path.join(logs, "validate-r1.bundle")))
        self.assertFalse(os.path.isdir(os.path.join(logs, "validate-r1-extra")))
        self.assertEqual(self.remote_leftovers(), [])
        # transport order and non-interactive flags on every call
        calls = self.calls()
        self.assertEqual([c[0] for c in calls], ["ssh", "scp", "ssh", "scp", "ssh"])
        for exe, argv in calls:
            self.assertIn("BatchMode=yes", argv, (exe, argv))
        self.assertIn("mkdir", calls[0][1])
        self.assertIn("validate-r1.bundle", calls[1][1])
        self.assertIn("validate-r1.sh", calls[1][1])
        self.assertIn("validate-r1-extra", calls[1][1])
        self.assertTrue(calls[1][1][-1].startswith("box:.revali/sandbox/sample/validate-r1-in"), calls[1][1])
        self.assertIn("bash", calls[2][1])
        self.assertEqual(calls[3][1][-2:], ["box:.revali/sandbox/sample/validate-r1-logs/.", "."])
        self.assertIn("rm", calls[4][1])

    @unittest.skipUnless(HAVE_BASH, "needs bash")
    def test_failing_step_is_reported_and_cleaned_up(self):
        r = SshRunner(plat())
        logs = os.path.join(self.rdir(), "logs")
        head = git(["rev-parse", "HEAD"], self.repo).strip()
        report = r.run(self.repo, head, [("setup", "true"), ("test", "exit 3"), ("new_test", "true")], {}, logs, "validate-r2")
        self.assertEqual([s.name for s in report.steps], ["setup", "test"])
        self.assertEqual(report.failed.name, "test")
        self.assertEqual(report.failed.returncode, 3)
        self.assertEqual(self.remote_leftovers(), [])
        self.assertEqual(self.calls()[-1][0], "ssh")
        self.assertIn("rm", self.calls()[-1][1])

    def test_remote_bash_failure_still_cleans_up(self):
        os.environ["REVALI_FAKE_SSH_BASH_FAILS"] = "1"
        r = SshRunner(plat())
        logs = os.path.join(self.rdir(), "logs")
        head = git(["rev-parse", "HEAD"], self.repo).strip()
        with self.assertRaises(RunnerError) as cm:
            r.run(self.repo, head, [("test", "true")], {}, logs, "validate-r3")
        self.assertIn("could not start (exit 127)", str(cm.exception))
        self.assertIn("box", str(cm.exception))
        self.assertEqual(self.remote_leftovers(), [])
        self.assertIn("rm", self.calls()[-1][1])
        self.assertFalse(os.path.exists(os.path.join(logs, "validate-r3.bundle")))

    def test_unreachable_host_is_named(self):
        os.environ["REVALI_FAKE_SSH_DOWN"] = "1"
        r = SshRunner(plat())
        logs = os.path.join(self.rdir(), "logs")
        head = git(["rev-parse", "HEAD"], self.repo).strip()
        with self.assertRaises(RunnerError) as cm:
            r.run(self.repo, head, [("test", "true")], {}, logs, "validate-r4")
        self.assertIn("could not reach host 'box' (exit 255)", str(cm.exception))


class PreflightProbeTests(SshCase):
    def test_unreachable_host_stops_preflight(self):
        os.environ["REVALI_FAKE_SSH_DOWN"] = "1"
        self.claude(claude_entry())
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertIn("ssh host 'box' is unreachable or lacks git / coreutils timeout", out)
        self.assertIn("run `ssh box` once by hand", out)
        self.assertFalse(any(c["argv"][:2] == ["pr", "create"] for c in self.fake_calls("gh")))
        self.assertEqual(self.fake_calls("claude"), [])

    def test_dry_run_skips_the_probe(self):
        os.environ["REVALI_FAKE_SSH_DOWN"] = "1"
        code, out = run_cli(["run", "--dry-run"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertEqual(self.calls(), [])

    @unittest.skipUnless(HAVE_BASH, "needs bash")
    def test_pipeline_end_to_end_over_ssh(self):
        self.claude(claude_entry())
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("READY TO MERGE", out)
        logs = os.path.join(self.rdir(), "logs")
        self.assertTrue(os.path.isfile(os.path.join(logs, "baseline-test.log")))
        self.assertTrue(os.path.isfile(os.path.join(logs, "validate-r1-new_test.log")))
        self.assertEqual(self.remote_leftovers(), [])
        # the probe ran once before anything was pushed
        first = self.calls()[0][1]
        self.assertEqual(first[-6:], ["git", "--version", "&&", "command", "-v", "timeout"])


if __name__ == "__main__":
    unittest.main()
