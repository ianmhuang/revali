"""A wsl.exe call that fails with a transient WSL service error is retried
(fix/wsl-transient-retry). The flaky stub prints the error the way wsl.exe does, UTF-16
with NUL bytes between the letters, so matching after removing NULs is exercised too.
"""

import json
import os
import sys
import unittest
from unittest import mock

from revali import EXIT_ERROR
from revali.config import ConfigError, PlatformCfg, load_project_config
from revali.runners import RunnerError, WslRunner, probe_runner
from tests.helpers import FAKE_BIN, RepoCase, _quote, claude_entry, git, run_cli

FLAKY_WSL = os.path.join(FAKE_BIN, "wsl_flaky_stub.py")
TIMEOUT = "Wsl/Service/WSAETIMEDOUT"
NOT_FOUND = (
    "There is no distribution with the supplied name.\r\n"
    "Error code: Wsl/Service/WSL_E_DISTRO_NOT_FOUND\r\n"
)
HAS_BASH = os.name == "nt" or os.path.exists("/bin/bash")


class FlakyWsl(RepoCase):
    runner = "wsl"

    def setUp(self):
        super().setUp()
        self.use_real_local_runner()
        os.environ["REVALI_WSL_CMD"] = "%s %s" % (_quote(sys.executable), _quote(FLAKY_WSL))
        self.plan_path = os.path.join(self.tmp, "wsl-plan.json")
        self.calls_path = os.path.join(self.tmp, "wsl-calls.txt")
        os.environ["REVALI_FAKE_WSL_PLAN"] = self.plan_path
        os.environ["REVALI_FAKE_WSL_CALLS"] = self.calls_path
        self.lines = []

    def plan(self, **kw):
        with open(self.plan_path, "w", encoding="utf-8") as fh:
            json.dump(kw, fh)

    def calls(self, kind=None):
        if not os.path.isfile(self.calls_path):
            return []
        with open(self.calls_path, "r", encoding="utf-8") as fh:
            kinds = [ln.strip() for ln in fh if ln.strip()]
        return [k for k in kinds if kind is None or k == kind]

    def plat(self, retries=2, wait=0, patterns=(TIMEOUT,)):
        return PlatformCfg(
            name="linux",
            runner="wsl",
            distro="Ubuntu",
            command_timeout_min=1,
            sandbox_dir="~/.revali/sandbox",
            wsl_retries=retries,
            wsl_retry_wait_s=wait,
            wsl_transient_errors=list(patterns),
        )

    def retry_lines(self):
        # only the runner's own lines: the `$ cmd` lines carry paths, and a sandbox under a
        # branch like fix/wsl-transient-retry has both words in them
        return [ln for ln in self.lines if ln.startswith("[wsl]") and " retry " in ln]

    def sandbox_run(self, runner, steps, label="validate-r1"):
        head = git(["rev-parse", "HEAD"], self.repo).strip()
        logs = os.path.join(self.rdir(), "logs")
        report = runner.run(self.repo, head, steps, {}, logs, label, log=self.lines.append)
        return report, logs


class RetrySucceeds(FlakyWsl):
    """AC-1: one transient failure, then success, for each of the three call sites."""

    def test_wslpath(self):
        self.plan(wslpath=1)
        out = WslRunner(self.plat()).wslpath(r"C:\x y\repo", log=self.lines.append)
        self.assertTrue(out.endswith("/x y/repo"), out)
        self.assertEqual(self.calls("wslpath"), ["wslpath", "wslpath"])
        retries = self.retry_lines()
        self.assertEqual(len(retries), 1, self.lines)
        self.assertIn(TIMEOUT, retries[0])
        self.assertIn("1 of 2", retries[0])
        self.assertNotIn("\x00", retries[0])

    def test_probe(self):
        self.plan(probe=1)
        self.assertEqual(probe_runner(self.plat(), log=self.lines.append), "")
        self.assertEqual(self.calls("probe"), ["probe", "probe"])
        self.assertEqual(len(self.retry_lines()), 1, self.lines)

    @unittest.skipUnless(HAS_BASH, "needs bash")
    def test_sandbox_script(self):
        self.plan(script=1)
        report, _ = self.sandbox_run(WslRunner(self.plat()), [("test", "true")])
        self.assertTrue(report.ok, [(s.name, s.returncode) for s in report.steps])
        self.assertEqual(self.calls("script"), ["script", "script"])
        self.assertEqual(len(self.retry_lines()), 1, self.lines)

    def test_retry_line_reaches_run_log(self):
        # the probe through a real run: preflight's detail lines go to revali.log
        cfg = self.read("revali.toml").replace(
            'distro = "Ubuntu"\n', 'distro = "Ubuntu"\nwsl_retry_wait_s = 0\n', 1
        )
        self.write("revali.toml", cfg)
        self.commit_all("no wait between wsl retries")
        self.plan(probe=1, wslpath=99)  # the first sandbox session after preflight stops it
        self.claude(claude_entry())
        run_cli(["run", "--foreground"])
        with open(os.path.join(self.rdir(), "logs", "revali.log"), "r", encoding="utf-8") as fh:
            log = fh.read()
        self.assertEqual(self.calls("probe"), ["probe", "probe"])
        self.assertIn(TIMEOUT, log)
        self.assertRegex(log, r"retry 1 of 2")


class RetryGivesUp(FlakyWsl):
    """AC-2: a service that stays down ends the run with exit 1 and says what to do."""

    def test_wslpath_three_attempts_then_runner_error(self):
        self.plan(wslpath=9)
        with self.assertRaises(RunnerError) as cm:
            WslRunner(self.plat()).wslpath(r"C:\repo", log=self.lines.append)
        msg = str(cm.exception)
        self.assertEqual(len(self.calls("wslpath")), 3)
        self.assertEqual(len(self.retry_lines()), 2, self.lines)
        self.assertNotIn("\x00", msg)
        self.assertIn(TIMEOUT, msg)
        self.assertIn("wsl --shutdown", msg)
        self.assertIn("every running distro", msg)

    def test_probe_in_a_run_exit_1(self):
        cfg = self.read("revali.toml").replace(
            'distro = "Ubuntu"\n', 'distro = "Ubuntu"\nwsl_retry_wait_s = 0\n', 1
        )
        self.write("revali.toml", cfg)
        self.commit_all("no wait between wsl retries")
        self.plan(probe=9)
        self.claude(claude_entry())
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertEqual(len(self.calls("probe")), 3)
        self.assertNotIn("\x00", out)
        self.assertIn(TIMEOUT, out)
        self.assertIn("wsl --shutdown", out)
        self.assertIn("every running distro", out)
        self.assertFalse(any(c["argv"][:2] == ["pr", "create"] for c in self.fake_calls("gh")))
        self.assertEqual(self.fake_calls("claude"), [])

    @unittest.skipUnless(HAS_BASH, "needs bash")
    def test_sandbox_script_three_attempts(self):
        self.plan(script=9)
        with self.assertRaises(RunnerError) as cm:
            self.sandbox_run(WslRunner(self.plat()), [("test", "true")])
        self.assertEqual(len(self.calls("script")), 3)
        self.assertIn("wsl --shutdown", str(cm.exception))
        self.assertNotIn("\x00", str(cm.exception))


class RetrySettings(FlakyWsl):
    """AC-3 and AC-6: the wait, the count and the patterns come from the platform table."""

    def test_wait_between_attempts(self):
        self.plan(wslpath=2)
        with mock.patch("revali.runners.time.sleep") as sleep:
            WslRunner(self.plat(wait=7.5)).wslpath(r"C:\repo", log=self.lines.append)
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [7.5, 7.5])
        self.assertEqual(len(self.calls("wslpath")), 3)

    def test_zero_retries_makes_one_call(self):
        self.plan(wslpath=1)
        with mock.patch("revali.runners.time.sleep") as sleep:
            with self.assertRaises(RunnerError):
                WslRunner(self.plat(retries=0)).wslpath(r"C:\repo", log=self.lines.append)
        self.assertEqual(len(self.calls("wslpath")), 1)
        self.assertEqual(self.retry_lines(), [])
        sleep.assert_not_called()

    def test_defaults(self):
        plat = load_project_config(self.repo).validate.platforms["linux"]
        self.assertEqual(plat.wsl_retries, 2)
        self.assertEqual(plat.wsl_retry_wait_s, 30)
        self.assertEqual(plat.wsl_transient_errors, [TIMEOUT])

    def test_own_pattern_is_retried(self):
        self.plan(wslpath=1, message="Error code: Wsl/Service/E_SOMETHING_ELSE\r\n")
        WslRunner(self.plat(patterns=["Wsl/Service/E_SOMETHING_ELSE"])).wslpath(
            r"C:\repo", log=self.lines.append
        )
        self.assertEqual(len(self.calls("wslpath")), 2)

    def test_timeout_not_retried_when_not_listed(self):
        self.plan(wslpath=1)
        with self.assertRaises(RunnerError) as cm:
            WslRunner(self.plat(patterns=["Wsl/Service/E_SOMETHING_ELSE"])).wslpath(
                r"C:\repo", log=self.lines.append
            )
        self.assertEqual(len(self.calls("wslpath")), 1)
        self.assertNotIn("wsl --shutdown", str(cm.exception))

    def test_negative_values_are_config_errors(self):
        for key, value in (("wsl_retries", "-1"), ("wsl_retry_wait_s", "-0.5")):
            cfg = self.read("revali.toml").replace(
                'distro = "Ubuntu"\n', 'distro = "Ubuntu"\n%s = %s\n' % (key, value), 1
            )
            self.write("revali.toml", cfg)
            with self.assertRaises(ConfigError) as cm:
                load_project_config(self.repo)
            self.assertIn(key, str(cm.exception))
            git(["checkout", "--", "revali.toml"], self.repo)


class NotTransient(FlakyWsl):
    """AC-4: anything without a configured pattern gets one call and today's message."""

    def test_distro_not_found(self):
        self.plan(wslpath=1, message=NOT_FOUND)
        with self.assertRaises(RunnerError) as cm:
            WslRunner(self.plat()).wslpath(r"C:\repo", log=self.lines.append)
        self.assertEqual(len(self.calls("wslpath")), 1)
        self.assertIn("wslpath failed", str(cm.exception))
        self.assertNotIn("wsl --shutdown", str(cm.exception))

    def test_probe_exit_1(self):
        self.plan(probe=1, message="bash: git: command not found\n", exit=1)
        problem = probe_runner(self.plat(), log=self.lines.append)
        self.assertIn("did not start or lacks git", problem)
        self.assertEqual(len(self.calls("probe")), 1)
        self.assertNotIn("wsl --shutdown", problem)

    @unittest.skipUnless(HAS_BASH, "needs bash")
    def test_failing_step(self):
        report, _ = self.sandbox_run(
            WslRunner(self.plat()), [("setup", "true"), ("test", "exit 3")], "validate-r2"
        )
        self.assertEqual(report.failed.name, "test")
        self.assertEqual(report.failed.returncode, 3)
        self.assertEqual(len(self.calls("script")), 1)
        self.assertEqual(self.retry_lines(), [])


class ScriptThatRan(FlakyWsl):
    """AC-5: a sandbox script that left its results file is never run again."""

    @unittest.skipUnless(HAS_BASH, "needs bash")
    def test_results_present_no_retry(self):
        self.plan(script_after=1)
        report, logs = self.sandbox_run(WslRunner(self.plat()), [("test", "true")])
        self.assertTrue(os.path.isfile(os.path.join(logs, "validate-r1.results")))
        self.assertEqual(len(self.calls("script")), 1)
        self.assertTrue(report.ok, [(s.name, s.returncode) for s in report.steps])
        self.assertEqual(self.retry_lines(), [])


class Docs(unittest.TestCase):
    """AC-7: the three keys are described where the platform keys are."""

    KEYS = ("wsl_retries", "wsl_retry_wait_s", "wsl_transient_errors")

    def read(self, rel):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, rel), "r", encoding="utf-8") as fh:
            return fh.read()

    def test_configuration_md(self):
        text = self.read(os.path.join("docs", "configuration.md"))
        for key in self.KEYS:
            self.assertIn("`%s`" % key, text)

    def test_template(self):
        text = self.read(os.path.join("templates", "revali.toml"))
        for key in self.KEYS:
            self.assertRegex(text, r"(?m)^#?\s*%s\s*=" % key)


if __name__ == "__main__":
    unittest.main()
