"""AC-2: the ssh runner delivers the branch as a git bundle, runs the shared sandbox
script on the host, brings the per-step logs back under the WSL runner's file names,
removes its staging and log directories on the host on every path (and says so in the
run log when it cannot; round 1, F4), copes with a repository directory name that
contains a space (round 1, F1), and passes BatchMode=yes to every ssh / scp call.

The host is a directory served by tests/fixtures/fake_bin/ssh_stub.py and scp_stub.py;
the sandbox script runs with the host's bash, as the WSL runner tests do.
"""

import os
import sys
import unittest

from revali.config import PlatformCfg
from revali.runners import RunnerError, SshRunner, WslRunner
from tests.fixtures.make_sample_repo import LOCAL_NEW_TEST, LOCAL_TEST, PY
from tests.helpers import FAKE_BIN, RepoCase, _quote, git

SSH_STUB = os.path.join(FAKE_BIN, "ssh_stub.py")
SCP_STUB = os.path.join(FAKE_BIN, "scp_stub.py")
WSL_STUB = os.path.join(FAKE_BIN, "wsl_stub.py")
HAVE_BASH = os.name == "nt" or os.path.exists("/bin/bash")
NEW_TEST = (
    "import unittest\nfrom src.calc import mul\n\n\nclass T(unittest.TestCase):\n"
    "    def test_m(self):\n        self.assertEqual(mul(2, 3), 6)\n"
)


def ssh_plat(**kw):
    base = dict(runner="ssh", host="box", command_timeout_min=1, sandbox_dir="~/.revali/sandbox")
    base.update(kw)
    return PlatformCfg(**base)


class SshTransportCase(RepoCase):
    runner = "wsl"

    def setUp(self):
        super().setUp()
        self.use_real_local_runner()
        self.remote = os.path.join(self.tmp, "remote home")
        os.makedirs(self.remote)
        os.environ["REVALI_FAKE_REMOTE"] = self.remote
        os.environ["REVALI_SSH_CMD"] = "%s %s" % (_quote(sys.executable), _quote(SSH_STUB))
        os.environ["REVALI_SCP_CMD"] = "%s %s" % (_quote(sys.executable), _quote(SCP_STUB))
        os.environ["REVALI_WSL_CMD"] = "%s %s" % (_quote(sys.executable), _quote(WSL_STUB))
        for knob in (
            "REVALI_FAKE_SSH_DOWN",
            "REVALI_FAKE_SSH_BASH_FAILS",
            "REVALI_FAKE_SSH_RM_FAILS",
        ):
            os.environ.pop(knob, None)
        self.head = git(["rev-parse", "HEAD"], self.repo).strip()

    def remote_files(self):
        found = []
        for root, _dirs, files in os.walk(self.remote):
            for f in files:
                found.append(os.path.relpath(os.path.join(root, f), self.remote))
        return sorted(found)

    def transport_calls(self):
        return [(c["exe"], c["argv"]) for c in self.fake_calls() if c["exe"] in ("ssh", "scp")]

    def run_ssh(self, steps, extra, label, repo=None, log=None):
        logs = os.path.join(self.tmp, "logs-" + label)
        report = SshRunner(ssh_plat()).run(
            repo or self.repo, self.head, steps, extra, logs, label, log=log
        )
        return report, logs

    def assert_host_clean_and_rm_last(self):
        self.assertEqual(self.remote_files(), [])
        calls = self.transport_calls()
        self.assertTrue(calls, "no ssh / scp call recorded")
        self.assertEqual(calls[-1][0], "ssh", calls[-1])
        # the cleanup is one shell line handed to the remote shell
        self.assertTrue(calls[-1][1][-1].startswith("rm -rf "), calls[-1])


class Delivery(SshTransportCase):
    @unittest.skipUnless(HAVE_BASH, "needs bash")
    def test_branch_arrives_as_a_bundle_and_the_steps_run_on_the_host(self):
        steps = [("setup", PY + " --version"), ("test", LOCAL_TEST), ("new_test", LOCAL_NEW_TEST)]
        report, logs = self.run_ssh(steps, {"tests/test_review_mul.py": NEW_TEST}, "validate-r1")
        self.assertTrue(report.ok, [(s.name, s.returncode, s.stdout[-300:]) for s in report.steps])
        self.assertEqual([s.name for s in report.steps], ["setup", "test", "new_test"])
        self.assertIn("Ran 1 test", report.step("new_test").stdout)
        # one upload carried a bundle; the script clones from it, not from the source repo
        uploads = [
            argv
            for exe, argv in self.transport_calls()
            if exe == "scp" and argv[-1].startswith("box:")
        ]
        self.assertEqual(len(uploads), 1, self.transport_calls())
        self.assertTrue(any(a.endswith("validate-r1.bundle") for a in uploads[0]), uploads[0])
        with open(os.path.join(logs, "validate-r1.sh"), "r", encoding="utf-8") as fh:
            script = fh.read()
        self.assertIn("validate-r1.bundle", script)
        self.assertNotIn(self.repo.replace("\\", "/"), script.replace("\\", "/"))

    @unittest.skipUnless(HAVE_BASH, "needs bash")
    def test_logs_come_back_under_the_wsl_runners_file_names(self):
        steps = [("setup", "true"), ("test", LOCAL_TEST), ("new_test", LOCAL_NEW_TEST)]
        extra = {"tests/test_review_mul.py": NEW_TEST}
        wsl_logs = os.path.join(self.tmp, "logs-wsl")
        wsl = WslRunner(
            PlatformCfg(
                runner="wsl",
                distro="Ubuntu",
                command_timeout_min=1,
                sandbox_dir="~/.revali/sandbox",
            )
        )
        wsl_report = wsl.run(self.repo, self.head, steps, extra, wsl_logs, "validate-r1")
        self.assertTrue(
            wsl_report.ok, [(s.name, s.returncode, s.stdout[-300:]) for s in wsl_report.steps]
        )
        ssh_report, ssh_logs = self.run_ssh(steps, extra, "validate-r1")
        self.assertTrue(
            ssh_report.ok, [(s.name, s.returncode, s.stdout[-300:]) for s in ssh_report.steps]
        )
        self.assertEqual(sorted(os.listdir(ssh_logs)), sorted(os.listdir(wsl_logs)))
        for name in (
            "validate-r1.results",
            "validate-r1-clone.log",
            "validate-r1-setup.log",
            "validate-r1-test.log",
            "validate-r1-new_test.log",
        ):
            self.assertIn(name, os.listdir(ssh_logs))
        self.assertNotIn("validate-r1.bundle", os.listdir(ssh_logs))
        self.assertNotIn("validate-r1-extra", os.listdir(ssh_logs))
        self.assertEqual(
            [s.log_path for s in ssh_report.steps],
            [
                os.path.join(ssh_logs, "validate-r1-%s.log" % n)
                for n in ("setup", "test", "new_test")
            ],
        )

    @unittest.skipUnless(HAVE_BASH, "needs bash")
    def test_repository_directory_with_a_space_runs_and_leaves_the_host_clean(self):
        # round 1, F1: the remote directories carry the repository's directory name; a
        # name with a space must neither split the remote command lines nor leak files
        clone = os.path.join(self.tmp, "my project")
        git(["clone", "-q", self.repo, clone], self.tmp)
        self.assertEqual(git(["rev-parse", "HEAD"], clone).strip(), self.head)
        report, _ = self.run_ssh([("test", LOCAL_TEST)], {}, "validate-r5", repo=clone)
        self.assertTrue(report.ok, [(s.name, s.returncode, s.stdout[-300:]) for s in report.steps])
        self.assertEqual([s.name for s in report.steps], ["test"])
        self.assert_host_clean_and_rm_last()
        for exe, argv in self.transport_calls():
            remote_specs = [a for a in argv if a.startswith("box:")]
            for spec in remote_specs:
                self.assertFalse(any(ch.isspace() for ch in spec), (exe, argv))
        # nothing landed outside the sandbox root on the host, e.g. a stray "project/..."
        self.assertFalse(
            os.path.exists(os.path.join(self.remote, "project")), os.listdir(self.remote)
        )
        self.assertFalse(
            os.path.exists(os.path.join(self.remote, ".revali", "sandbox", "my")),
            (
                os.listdir(os.path.join(self.remote, ".revali", "sandbox"))
                if os.path.isdir(os.path.join(self.remote, ".revali", "sandbox"))
                else []
            ),
        )


class Cleanup(SshTransportCase):
    @unittest.skipUnless(HAVE_BASH, "needs bash")
    def test_success_leaves_nothing_on_the_host(self):
        report, _ = self.run_ssh([("test", "true")], {}, "validate-r1")
        self.assertTrue(report.ok)
        self.assert_host_clean_and_rm_last()

    @unittest.skipUnless(HAVE_BASH, "needs bash")
    def test_failing_step_is_reported_and_the_host_is_cleaned(self):
        report, _ = self.run_ssh(
            [("setup", "true"), ("test", "exit 3"), ("new_test", "true")], {}, "validate-r2"
        )
        self.assertEqual([s.name for s in report.steps], ["setup", "test"])
        self.assertEqual(report.failed.name, "test")
        self.assertEqual(report.failed.returncode, 3)
        self.assert_host_clean_and_rm_last()

    def test_remote_bash_exiting_non_zero_still_cleans_the_host(self):
        os.environ["REVALI_FAKE_SSH_BASH_FAILS"] = "1"
        with self.assertRaises(RunnerError) as cm:
            self.run_ssh([("test", "true")], {}, "validate-r3")
        self.assertIn("box", str(cm.exception))
        self.assert_host_clean_and_rm_last()
        self.assertFalse(
            os.path.exists(os.path.join(self.tmp, "logs-validate-r3", "validate-r3.bundle"))
        )

    @unittest.skipUnless(HAVE_BASH, "needs bash")
    def test_cleanup_that_fails_on_the_host_is_named_in_the_run_log(self):
        # round 1, F4: a remote rm that fails must not be swallowed; the run still
        # succeeds, and the log names the host and what was left behind
        os.environ["REVALI_FAKE_SSH_RM_FAILS"] = "1"
        lines = []
        report, _ = self.run_ssh([("test", "true")], {}, "validate-r6", log=lines.append)
        self.assertTrue(report.ok)
        hits = [
            line
            for line in lines
            if "box" in line
            and "validate-r6" in line
            and ("remove" in line.lower() or "delete" in line.lower() or "clean" in line.lower())
        ]
        self.assertTrue(hits, lines)


class NonInteractive(SshTransportCase):
    @unittest.skipUnless(HAVE_BASH, "needs bash")
    def test_every_call_is_batch_mode_and_the_transport_is_ordered(self):
        self.run_ssh([("test", "true")], {}, "validate-r1")
        calls = self.transport_calls()
        self.assertGreaterEqual(len(calls), 4, calls)
        for exe, argv in calls:
            self.assertIn("BatchMode=yes", argv, (exe, argv))
        upload = [i for i, (e, a) in enumerate(calls) if e == "scp" and a[-1].startswith("box:")]
        run = [i for i, (e, a) in enumerate(calls) if e == "ssh" and a[-1].startswith("bash ")]
        download = [i for i, (e, a) in enumerate(calls) if e == "scp" and a[-1] == "."]
        cleanup = [
            i for i, (e, a) in enumerate(calls) if e == "ssh" and a[-1].startswith("rm -rf ")
        ]
        for name, found in (
            ("upload", upload),
            ("run", run),
            ("download", download),
            ("cleanup", cleanup),
        ):
            self.assertEqual(len(found), 1, "%s: %r in %r" % (name, found, calls))
        self.assertLess(upload[0], run[0])
        self.assertLess(run[0], download[0])
        self.assertLess(download[0], cleanup[0])

    def test_unreachable_host_is_named_and_every_call_stayed_batch_mode(self):
        os.environ["REVALI_FAKE_SSH_DOWN"] = "1"
        with self.assertRaises(RunnerError) as cm:
            self.run_ssh([("test", "true")], {}, "validate-r4")
        self.assertIn("box", str(cm.exception))
        calls = self.transport_calls()
        self.assertTrue(calls)
        for exe, argv in calls:
            self.assertIn("BatchMode=yes", argv, (exe, argv))


if __name__ == "__main__":
    unittest.main()
