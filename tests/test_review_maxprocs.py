"""Review tests for fix/sandbox-nproc-limit: the `max_procs` key and the limits the sandbox
script sets.

AC-1: without the key in any layer the platform's `max_procs` is 4096 and the script of a
      wsl and of an ssh session runs `ulimit -u 4096`.
AC-2: `max_procs = N` in `[validate.<name>]` or `[validate.platform]` of the project or the
      user config makes the script run `ulimit -u N`.
AC-3: `max_procs = 0` leaves no `ulimit -u` command in the script; `ulimit -f 4000000` stays.
AC-4: a negative value is a config problem naming `validate.<name>.max_procs`; a float, a
      bool and a string are "must be an integer".
AC-5: the file-size limit is a command of its own, and (Linux, not root) a step still sees
      `ulimit -f` = 4000000 when the hard process limit is below `max_procs`.

The script is read from `<logs>/<label>.sh`, where the runner writes it before starting it;
the session itself runs against the repository's stand-ins for wsl.exe, ssh and scp.
"""

import dataclasses
import os
import re
import sys
import unittest

from revali.config import ConfigError, load_project_config
from revali.runners import RunnerError, get_runner
from tests.helpers import FAKE_BIN, RepoCase, _quote, git

HAVE_BASH = os.name == "nt" or os.path.exists("/bin/bash")
LABEL = "validate-r1"
WSL_STUB = os.path.join(FAKE_BIN, "wsl_stub.py")
SSH_STUB = os.path.join(FAKE_BIN, "ssh_stub.py")
SCP_STUB = os.path.join(FAKE_BIN, "scp_stub.py")

# wsl_stub with the hard process limit of the session lowered to MAXPROCS_HARD first: the
# host of AC-5, whose hard limit is below max_procs
LOWERING_STUB = r"""import os
import resource
import sys

sys.path.insert(0, os.environ["MAXPROCS_FAKE_BIN"])

import wsl_stub


def main(argv):
    hard = int(os.environ["MAXPROCS_HARD"])
    soft = resource.getrlimit(resource.RLIMIT_NPROC)[0]
    if soft == resource.RLIM_INFINITY or soft > hard:
        soft = hard
    resource.setrlimit(resource.RLIMIT_NPROC, (soft, hard))
    return wsl_stub.main(argv)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
"""


def commands(script):
    """The script's lines that are not comments, stripped."""
    lines = [ln.strip() for ln in script.splitlines()]
    return [ln for ln in lines if ln and not ln.startswith("#")]


def process_limits(script):
    """Every command of the script that sets a process limit."""
    return [ln for ln in commands(script) if re.search(r"\bulimit\b.*\s-\w*u\b", ln)]


class LimitCase(RepoCase):
    runner = "wsl"

    def setUp(self):
        super().setUp()
        self.use_real_local_runner()
        remote = os.path.join(self.tmp, "remote home")
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
        ):
            os.environ.pop(knob, None)

    def project(self, line, table="[validate.linux]"):
        """Put `line` into `table` of the project's revali.toml (a new table at the end when
        the file has none of that name)."""
        toml = self.read("revali.toml")
        if table + "\n" in toml:
            toml = toml.replace(table + "\n", "%s\n%s\n" % (table, line), 1)
        else:
            toml = "%s\n%s\n%s\n" % (toml, table, line)
        self.write("revali.toml", toml)

    def user(self, text):
        with open(os.path.join(self.home, "config.toml"), "w", encoding="utf-8", newline="") as fh:
            fh.write(text)

    def plat(self):
        return load_project_config(self.repo).validate.platforms["linux"]

    def session(self, plat, cmd="true"):
        """(report, script) of one session of `plat`; without a bash on this host the session
        cannot start, and the script the runner wrote is still read."""
        logs = os.path.join(self.tmp, "session-logs", plat.runner)
        head = git(["rev-parse", "HEAD"], self.repo).strip()
        report = None
        try:
            report = get_runner(plat).run(self.repo, head, [("test", cmd)], {}, logs, LABEL)
        except RunnerError:
            if HAVE_BASH:
                raise
        with open(os.path.join(logs, LABEL + ".sh"), "r", encoding="utf-8") as fh:
            return report, fh.read()

    def scripts(self):
        """(runner name, script) for the configured platform run by wsl and by ssh."""
        plat = self.plat()
        for name in ("wsl", "ssh"):
            yield name, self.session(dataclasses.replace(plat, runner=name, host="box"))[1]

    def assert_process_limit(self, value):
        for name, script in self.scripts():
            with self.subTest(runner=name):
                limits = process_limits(script)
                self.assertEqual(len(limits), 1, limits)
                self.assertRegex(limits[0], r"^ulimit\s+-u\s+%d\b" % value)


class DefaultValue(LimitCase):
    """AC-1"""

    def test_the_key_defaults_to_4096(self):
        self.assertEqual(self.plat().max_procs, 4096)

    def test_the_script_sets_4096_when_no_layer_names_the_key(self):
        self.assert_process_limit(4096)


class OwnValue(LimitCase):
    """AC-2. The values are large on purpose: on Linux the sessions really set them, and a
    small one would count this user's other processes against the test."""

    def test_the_projects_platform_table(self):
        self.project("max_procs = 5000")
        self.assertEqual(self.plat().max_procs, 5000)
        self.assert_process_limit(5000)

    def test_the_projects_table_for_every_platform(self):
        self.project("max_procs = 6000", table="[validate.platform]")
        self.assertEqual(self.plat().max_procs, 6000)
        self.assert_process_limit(6000)

    def test_the_users_table_for_every_platform(self):
        self.user("[validate.platform]\nmax_procs = 7000\n")
        self.assertEqual(self.plat().max_procs, 7000)
        self.assert_process_limit(7000)

    def test_the_users_platform_table(self):
        self.user("[validate.linux]\nmax_procs = 8000\n")
        self.assertEqual(self.plat().max_procs, 8000)
        self.assert_process_limit(8000)

    def test_the_project_wins_over_the_user(self):
        self.user("[validate.platform]\nmax_procs = 7000\n")
        self.project("max_procs = 5000")
        self.assertEqual(self.plat().max_procs, 5000)
        self.assert_process_limit(5000)


class NoLimit(LimitCase):
    """AC-3, and the shape AC-5 asks for"""

    def assert_file_size_limit_alone(self, script):
        sets = [ln for ln in commands(script) if re.match(r"ulimit\s+-f\s+4000000\b", ln)]
        self.assertEqual(len(sets), 1, [ln for ln in commands(script) if "ulimit" in ln])
        # a command of its own: no other option that could fail before or after it
        self.assertRegex(sets[0], r"^ulimit\s+-f\s+4000000(\s+2>\s*/dev/null)?$")

    def test_zero_leaves_no_process_limit_and_keeps_the_file_size_limit(self):
        self.project("max_procs = 0")
        self.assertEqual(self.plat().max_procs, 0)
        for name, script in self.scripts():
            with self.subTest(runner=name):
                self.assertEqual(process_limits(script), [])
                self.assertNotRegex("\n".join(commands(script)), r"ulimit\s+-u")
                self.assert_file_size_limit_alone(script)

    def test_zero_from_the_user_layer(self):
        self.user("[validate.platform]\nmax_procs = 0\n")
        for name, script in self.scripts():
            with self.subTest(runner=name):
                self.assertEqual(process_limits(script), [])

    def test_the_file_size_limit_is_its_own_command_with_a_process_limit_too(self):
        for name, script in self.scripts():
            with self.subTest(runner=name):
                self.assert_file_size_limit_alone(script)

    @unittest.skipUnless(HAVE_BASH, "needs bash")
    def test_a_session_without_a_process_limit_still_runs_its_step(self):
        self.project("max_procs = 0")
        report, _script = self.session(self.plat(), cmd="echo ran")
        self.assertTrue(report.ok, [(s.name, s.returncode, s.text) for s in report.steps])
        self.assertIn("ran", report.steps[0].text)


class BadValues(LimitCase):
    """AC-4"""

    def problems(self):
        with self.assertRaises(ConfigError) as cm:
            load_project_config(self.repo)
        return str(cm.exception)

    def test_a_negative_value_in_the_platform_table(self):
        self.project("max_procs = -1")
        self.assertIn("validate.linux.max_procs", self.problems())

    def test_a_negative_value_for_every_platform(self):
        self.project("max_procs = -5", table="[validate.platform]")
        self.assertIn("validate.linux.max_procs", self.problems())

    def test_a_negative_value_from_the_user_layer(self):
        self.user("[validate.platform]\nmax_procs = -1\n")
        self.assertIn("validate.linux.max_procs", self.problems())

    def test_a_float_a_bool_and_a_string_must_be_an_integer(self):
        original = self.read("revali.toml")
        for value in ("1.5", "true", '"4096"'):
            with self.subTest(value=value):
                self.write("revali.toml", original)
                self.project("max_procs = %s" % value)
                text = self.problems()
                self.assertRegex(text, r"validate\.linux\.max_procs must be an integer")

    def test_zero_and_a_positive_value_load(self):
        original = self.read("revali.toml")
        for value in (0, 1, 100000):
            with self.subTest(value=value):
                self.write("revali.toml", original)
                self.project("max_procs = %d" % value)
                self.assertEqual(self.plat().max_procs, value)


@unittest.skipUnless(sys.platform.startswith("linux"), "ulimit -u and -f need Linux bash")
class HardLimitBelowMaxProcs(LimitCase):
    """AC-5: the script's `ulimit -u` fails, the file-size limit holds and the step runs."""

    def test_the_file_size_limit_holds_when_the_process_limit_cannot_be_set(self):
        import resource

        if os.geteuid() == 0:
            self.skipTest("root may raise any limit")
        fsize = resource.getrlimit(resource.RLIMIT_FSIZE)[1]
        if fsize != resource.RLIM_INFINITY and fsize < 4000000 * 1024:
            self.skipTest("this host's hard file-size limit is below the sandbox's")
        asked = resource.getrlimit(resource.RLIMIT_NPROC)[1]
        if asked == resource.RLIM_INFINITY:
            asked = 1000000
        lowered = asked - 1  # far above what this user runs, unless the host's limit is tiny
        if lowered < 64:
            self.skipTest("this host's hard process limit is too low to lower it further")
        stub = os.path.join(self.tmp, "maxprocs_wsl.py")
        with open(stub, "w", encoding="utf-8", newline="") as fh:
            fh.write(LOWERING_STUB)
        os.environ["REVALI_WSL_CMD"] = "%s %s" % (_quote(sys.executable), _quote(stub))
        os.environ["MAXPROCS_FAKE_BIN"] = FAKE_BIN
        os.environ["MAXPROCS_HARD"] = str(lowered)
        self.project("max_procs = %d" % asked)
        report, script = self.session(self.plat(), cmd='echo "f=$(ulimit -f) hu=$(ulimit -Hu)"')
        self.assertEqual(len(process_limits(script)), 1, script)
        self.assertEqual([s.name for s in report.steps], ["test"])
        out = report.steps[0].text
        self.assertTrue(report.ok, out)  # the step still runs
        if "hu=%d" % lowered not in out:
            self.skipTest("this user could raise its hard process limit: %s" % out.strip())
        self.assertIn("f=4000000", out)


if __name__ == "__main__":
    unittest.main()
