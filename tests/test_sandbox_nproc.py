"""The sandbox process limit (fix/sandbox-nproc-limit, M3.4.6).

`ulimit -u` counts every process and thread of the user in the distro or on the host, so a
fixed 512 was shared by every sandbox and the user's own programs. It is now
`[validate.<platform>] max_procs` (default 4096, 0 = no limit), the file-size limit no longer
depends on it, and a step that fails to start a process gets a line in its log saying so.
When the script itself cannot start one (AC-8), the "session ended" error says so.
"""

import os
import re
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from revali import EXIT_ERROR
from revali.config import ConfigError, PlatformCfg, load_project_config
from revali.runners import (
    RunnerError,
    SessionEndedEarly,
    SshRunner,
    WslRunner,
    render_script,
    stage_inputs,
)
from tests.fixtures.make_sample_repo import PY, toml_str
from tests.helpers import FAKE_BIN, RepoCase, _quote, claude_entry, git, rmtree_force, run_cli

WSL_STUB = os.path.join(FAKE_BIN, "wsl_stub.py")
HAS_BASH = os.name == "nt" or os.path.exists("/bin/bash")
HINT = "revali: "
FORK_FAIL = "bash: fork: retry: Resource temporarily unavailable"


def plat(runner="wsl", max_procs=4096):
    return PlatformCfg(
        name="linux",
        runner=runner,
        distro="Ubuntu",
        host="box",
        command_timeout_min=1,
        connect_timeout_s=5,
        transfer_timeout_min=1,
        sandbox_dir="~/.revali/sandbox",
        max_procs=max_procs,
    )


def script_for(runner_cls, p):
    runner = runner_cls(p)
    return runner.script("/src", "/logs", "/extra", "abc", [("test", "x")], "v", "/sb", 60)


def limit_lines(text):
    return [ln.strip() for ln in text.splitlines() if ln.strip().startswith("ulimit")]


class GeneratedScript(unittest.TestCase):
    """AC-1 to AC-3 and the shape behind AC-5, on the script both remote runners generate."""

    def test_default_value(self):
        for cls in (WslRunner, SshRunner):
            with self.subTest(runner=cls.__name__):
                lines = limit_lines(script_for(cls, plat()))
                self.assertIn("ulimit -u 4096 2>/dev/null", lines)

    def test_own_value(self):
        for cls in (WslRunner, SshRunner):
            with self.subTest(runner=cls.__name__):
                lines = limit_lines(script_for(cls, plat(max_procs=77)))
                self.assertIn("ulimit -u 77 2>/dev/null", lines)
                self.assertFalse(any("4096" in ln for ln in lines), lines)

    def test_zero_sets_no_process_limit(self):
        for cls in (WslRunner, SshRunner):
            with self.subTest(runner=cls.__name__):
                text = script_for(cls, plat(max_procs=0))
                self.assertNotIn("ulimit -u", text)
                self.assertIn("ulimit -f 4000000 2>/dev/null", limit_lines(text))

    def test_file_size_limit_on_its_own_line(self):
        # bash skips the options after one that fails: `ulimit -u N -f M` loses -f when the
        # host's hard process limit is below N
        for value in (0, 4096):
            lines = limit_lines(script_for(WslRunner, plat(max_procs=value)))
            self.assertIn("ulimit -f 4000000 2>/dev/null", lines)
            for ln in lines:
                self.assertFalse("-u" in ln and "-f" in ln, ln)

    def test_no_placeholder_left(self):
        for value in (0, 4096):
            text = script_for(WslRunner, plat(max_procs=value))
            self.assertIsNone(re.search(r"__[A-Z_]+__", text), text)

    def test_render_script_without_the_argument_sets_no_process_limit(self):
        # callers that predate the key (tests among them) keep a working script
        text = render_script("src", "/l", "/e", "abc", [("test", "x")], "v", "/sb", 60)
        self.assertNotIn("ulimit -u", text)
        self.assertIn("ulimit -f 4000000 2>/dev/null", limit_lines(text))


class StaleScriptLog(unittest.TestCase):
    """PR #59 F2: a session must not be judged by an earlier session's script log."""

    def test_stage_inputs_removes_it(self):
        logs = tempfile.mkdtemp(prefix="revali stale ")
        self.addCleanup(rmtree_force, logs)
        stale = os.path.join(logs, "v-script.log")
        other = os.path.join(logs, "w-script.log")
        for path in (stale, other):
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(FORK_FAIL + "\n")
        stage_inputs(logs, "v", [("test", "true")], {})
        self.assertFalse(os.path.exists(stale))
        self.assertTrue(os.path.exists(other))  # another label's file is not this session's


class ConfigKey(RepoCase):
    """AC-1, AC-2, AC-4: the key in defaults.toml, in the project and user layers, bad values."""

    runner = "wsl"

    def set_project(self, line, table="[validate.linux]\n"):
        cfg = self.read("revali.toml")
        new = cfg.replace(table, table + line + "\n", 1)
        self.assertNotEqual(cfg, new, "table %r not found" % table)
        self.write("revali.toml", new)

    def test_default(self):
        self.assertEqual(load_project_config(self.repo).validate.platforms["linux"].max_procs, 4096)

    def test_project_platform_table(self):
        self.set_project("max_procs = 1000")
        self.assertEqual(load_project_config(self.repo).validate.platforms["linux"].max_procs, 1000)

    def test_project_zero(self):
        self.set_project("max_procs = 0")
        self.assertEqual(load_project_config(self.repo).validate.platforms["linux"].max_procs, 0)

    def test_user_platform_defaults(self):
        with open(os.path.join(self.home, "config.toml"), "w", encoding="utf-8") as fh:
            fh.write("[validate.platform]\nmax_procs = 2048\n")
        self.assertEqual(load_project_config(self.repo).validate.platforms["linux"].max_procs, 2048)

    def test_negative(self):
        self.set_project("max_procs = -1")
        with self.assertRaises(ConfigError) as cm:
            load_project_config(self.repo)
        self.assertIn("validate.linux.max_procs", str(cm.exception))

    def test_wrong_types(self):
        for value in ("1.5", "true", '"4096"'):
            with self.subTest(value=value):
                self.set_project("max_procs = %s" % value)
                with self.assertRaises(ConfigError) as cm:
                    load_project_config(self.repo)
                self.assertIn("max_procs must be an integer", str(cm.exception))
                git(["checkout", "--", "revali.toml"], self.repo)


@unittest.skipUnless(HAS_BASH, "needs bash")
class StepLogHint(RepoCase):
    """AC-6: the line appended to a failed step's log, run through the wsl runner with the
    host's bash standing in for the distro."""

    runner = "wsl"

    def setUp(self):
        super().setUp()
        self.use_real_local_runner()
        os.environ["REVALI_WSL_CMD"] = "%s %s" % (_quote(sys.executable), _quote(WSL_STUB))

    def run_step(self, cmd, max_procs=4096):
        head = git(["rev-parse", "HEAD"], self.repo).strip()
        logs = os.path.join(self.rdir(), "logs")
        report = WslRunner(plat(max_procs=max_procs)).run(
            self.repo, head, [("test", cmd)], {}, logs, "validate-r1"
        )
        self.assertEqual(len(report.steps), 1)
        return report.steps[0]

    def test_fork_failure(self):
        step = self.run_step("echo '%s' >&2; exit 1" % FORK_FAIL)
        self.assertEqual(step.returncode, 1)
        self.assertFalse(step.ok)
        lines = [ln for ln in step.text.splitlines() if ln.startswith(HINT)]
        self.assertEqual(len(lines), 1, step.text)
        self.assertIn("max_procs = 4096", lines[0])
        self.assertIn("every process and thread of this user", lines[0])
        self.assertIn("0 for no limit", lines[0])
        self.assertTrue(step.text.rstrip("\n").endswith(lines[0]), step.text)

    def test_own_value_named(self):
        step = self.run_step("echo '%s' >&2; exit 1" % FORK_FAIL, max_procs=300)
        self.assertIn("max_procs = 300", step.text)

    def test_other_patterns(self):
        for text in ("RuntimeError: can't start new thread", "Error: spawn EAGAIN"):
            with self.subTest(text=text):
                step = self.run_step('echo "%s"; exit 2' % text)
                self.assertEqual(step.returncode, 2)
                self.assertIn(HINT, step.text)

    def test_plain_failure_gets_no_line(self):
        step = self.run_step("echo 'FAIL: test_add'; exit 1")
        self.assertEqual(step.returncode, 1)
        self.assertNotIn(HINT, step.text)

    def test_passing_step_gets_no_line(self):
        step = self.run_step("echo '%s'; exit 0" % FORK_FAIL)
        self.assertTrue(step.ok)
        self.assertNotIn(HINT, step.text)

    def test_timeout_gets_no_line(self):
        # 124 is what `timeout` returns; the runner reads it as a timed-out step
        step = self.run_step("echo '%s'; exit 124" % FORK_FAIL)
        self.assertTrue(step.timed_out)
        self.assertNotIn(HINT, step.text)

    def test_no_limit_gets_no_line(self):
        step = self.run_step("echo '%s' >&2; exit 1" % FORK_FAIL, max_procs=0)
        self.assertEqual(step.returncode, 1)
        self.assertNotIn(HINT, step.text)

    def test_line_reaches_the_baseline_message(self):
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
        self.assertNotEqual(cfg, new, "fixture platform table not found")
        self.write("revali.toml", new)
        self.commit_all("a suite that cannot fork")
        self.claude(claude_entry())
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertIn("broken before review", out)
        self.assertIn("max_procs = 4096", out)
        self.assertEqual(self.fake_calls("claude"), [])


PARENT_FORK_FAIL = "/x/validate-r1.sh: fork: Resource temporarily unavailable"
SCRIPT_HINT = "show a failed attempt to start a process; if the process limit caused it"


def dying_before(cls, step_index, stderr_line, code="exit 0"):
    """Patch `cls.script` so the generated script prints `stderr_line` to its own stderr and
    stops before its `step_index`-th step: what bash does when it cannot fork that step."""
    real = cls.script

    def script(self, *args, **kwargs):
        lines = real(self, *args, **kwargs).split("\n")
        calls = [i for i, ln in enumerate(lines) if ln.startswith("run_step ")]
        injected = "echo %s >&2" % shlex.quote(stderr_line)
        lines.insert(calls[step_index], injected + ("; " + code if code else ""))
        return "\n".join(lines)

    return mock.patch.object(cls, "script", script)


@unittest.skipUnless(HAS_BASH, "needs bash")
class ScriptCannotFork(RepoCase):
    """AC-8 on the wsl runner: the script's own stderr goes to <label>-script.log, and a
    session that ended early with a fork error there names max_procs."""

    runner = "wsl"
    STEPS = [("setup", "true"), ("test", "true")]

    def setUp(self):
        super().setUp()
        self.use_real_local_runner()
        os.environ["REVALI_WSL_CMD"] = "%s %s" % (_quote(sys.executable), _quote(WSL_STUB))
        self.logs = os.path.join(self.rdir(), "logs")

    def run_wsl(self, max_procs=4096):
        head = git(["rev-parse", "HEAD"], self.repo).strip()
        return WslRunner(plat(max_procs=max_procs)).run(
            self.repo, head, self.STEPS, {}, self.logs, "validate-r1"
        )

    def ended_early(self, step_index, line, max_procs=4096):
        with dying_before(WslRunner, step_index, line):
            with self.assertRaises(SessionEndedEarly) as cm:
                self.run_wsl(max_procs)
        return str(cm.exception)

    def test_no_row_names_max_procs(self):
        msg = self.ended_early(0, PARENT_FORK_FAIL)
        self.assertIn("ended before `setup` finished", msg)
        self.assertIn(SCRIPT_HINT, msg)
        self.assertIn("max_procs = 4096", msg)
        self.assertIn(os.path.join(self.logs, "validate-r1-script.log"), msg)

    def test_rows_stop_after_a_pass_names_max_procs(self):
        msg = self.ended_early(1, PARENT_FORK_FAIL)
        self.assertIn("ended before `test` finished", msg)
        self.assertIn("max_procs = 4096", msg)

    def test_thread_and_eagain_strings(self):
        for line in ("can't start new thread", "spawn EAGAIN"):
            with self.subTest(line=line):
                self.assertIn(SCRIPT_HINT, self.ended_early(0, line))

    def test_other_stderr_leaves_the_message_alone(self):
        msg = self.ended_early(0, "Killed")
        self.assertIn("ended before `setup` finished", msg)
        self.assertNotIn(SCRIPT_HINT, msg)
        self.assertNotIn("max_procs", msg)

    def test_no_limit_leaves_the_message_alone(self):
        msg = self.ended_early(0, PARENT_FORK_FAIL, max_procs=0)
        self.assertNotIn(SCRIPT_HINT, msg)
        self.assertNotIn("max_procs", msg)

    def test_stderr_lands_in_the_script_log(self):
        # nothing stops: the run is a normal pass, and the line is in the file, not lost
        with dying_before(WslRunner, 0, "marker from the script", code=""):
            report = self.run_wsl()
        self.assertTrue(report.ok, [(s.name, s.returncode) for s in report.steps])
        with open(os.path.join(self.logs, "validate-r1-script.log"), encoding="utf-8") as fh:
            self.assertIn("marker from the script", fh.read())


@unittest.skipUnless(HAS_BASH, "needs bash")
class ScriptCannotForkSsh(RepoCase):
    """AC-8 on the ssh runner: the script log comes back with the other logs."""

    runner = "wsl"

    def setUp(self):
        super().setUp()
        from tests.test_ssh_runner import SCP_STUB, SSH_STUB

        self.use_real_local_runner()
        self.remote = os.path.join(self.tmp, "remote home")
        os.makedirs(self.remote)
        os.environ["REVALI_FAKE_REMOTE"] = self.remote
        os.environ["REVALI_SSH_CMD"] = "%s %s" % (_quote(sys.executable), _quote(SSH_STUB))
        os.environ["REVALI_SCP_CMD"] = "%s %s" % (_quote(sys.executable), _quote(SCP_STUB))
        for knob in ("REVALI_FAKE_SSH_DOWN", "REVALI_FAKE_SSH_BASH_FAILS"):
            os.environ.pop(knob, None)

    def test_no_row_names_max_procs(self):
        head = git(["rev-parse", "HEAD"], self.repo).strip()
        logs = os.path.join(self.rdir(), "logs")
        with dying_before(SshRunner, 0, PARENT_FORK_FAIL):
            with self.assertRaises(RunnerError) as cm:
                SshRunner(plat(runner="ssh", max_procs=300)).run(
                    self.repo, head, [("test", "true")], {}, logs, "validate-r1"
                )
        msg = str(cm.exception)
        self.assertIn("ended before `test` finished", msg)
        self.assertIn(SCRIPT_HINT, msg)
        self.assertIn("max_procs = 300", msg)


def _nproc_limit():
    try:
        import resource
    except ImportError:
        return None
    return resource.getrlimit(resource.RLIMIT_NPROC)[1]


@unittest.skipUnless(sys.platform.startswith("linux"), "ulimit -u and -f need Linux bash")
@unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root may raise any limit")
class FileSizeLimitSurvives(unittest.TestCase):
    """AC-5: a host whose hard process limit is below max_procs keeps the file-size limit."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="revali nproc ")
        self.addCleanup(rmtree_force, self.tmp)

    def test_lower_hard_limit(self):
        import resource

        hard = _nproc_limit()
        if hard == resource.RLIM_INFINITY:
            hard = 1000000
        # far above what this user runs, so the test's own processes never hit it
        lowered = hard - 1
        source = os.path.join(self.tmp, "src")
        os.makedirs(source)
        git(["init", "-q", "-b", "main"], source)
        git(
            [
                "-c",
                "user.email=t@t",
                "-c",
                "user.name=t",
                "commit",
                "-q",
                "--allow-empty",
                "-m",
                "init",
            ],
            source,
        )
        head = git(["rev-parse", "HEAD"], source).strip()
        logs = os.path.join(self.tmp, "logs")
        cmd = 'echo "f=$(ulimit -f) u=$(ulimit -Hu)"'
        steps, extra = stage_inputs(logs, "v", [("test", cmd)], {})
        script = render_script(
            source,
            logs,
            extra,
            head,
            steps,
            "v",
            os.path.join(self.tmp, "sb"),
            60,
            max_procs=hard,
        )
        path = os.path.join(self.tmp, "script.sh")
        with open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write(script)
        subprocess.run(
            ["bash", "-c", 'ulimit -u %d && exec bash "$0"' % lowered, path],
            check=True,
            timeout=60,
        )
        with open(os.path.join(logs, "v.results"), encoding="utf-8") as fh:
            self.assertEqual(fh.read().split("\t")[:2], ["test", "0"])
        with open(os.path.join(logs, "v-test.log"), encoding="utf-8") as fh:
            out = fh.read()
        self.assertIn("f=4000000", out)
        self.assertIn("u=%d" % lowered, out)  # the raise really failed


class Docs(unittest.TestCase):
    """AC-7: the key is described where the platform keys are."""

    def read(self, *rel):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, *rel), "r", encoding="utf-8") as fh:
            return fh.read()

    def test_sandbox_md(self):
        text = " ".join(self.read("docs", "sandbox.md").split())
        self.assertIn("`max_procs`", text)
        self.assertIn("4096", text)
        self.assertIn("other sandboxes", text)
        self.assertIn("local runner", text)

    def test_configuration_md(self):
        self.assertIn("`max_procs`", self.read("docs", "configuration.md"))

    def test_template(self):
        self.assertRegex(self.read("templates", "revali.toml"), r"(?m)^#\s*max_procs\s*=")

    def test_defaults(self):
        self.assertRegex(self.read("defaults.toml"), r"(?m)^max_procs = 4096\b")


if __name__ == "__main__":
    unittest.main()
