"""AC-1 and AC-2 of feature/windows-platform: `project.platforms` takes `linux` and
`windows`, any other name is refused with a message listing both, and a `windows` table is
refused with `runner = "wsl"` (WSL runs Linux) or with `runner = "local"` on a host that is
not Windows, while `runner = "ssh"` loads on any host and `linux` keeps its rules.

AC-2 words the host rule as `os.name != "nt"`, so the host is chosen by patching `os.name`
around the parse; every other case goes through the parser or the CLI unpatched.
"""

import os
import unittest
from unittest import mock

from revali import EXIT_ERROR, EXIT_OK
from revali.config import ConfigError, parse_project_config
from tests.helpers import RepoCase, run_cli

LINUX_WSL = '\n[validate.linux]\nrunner = "wsl"\nnew_test = "pytest tests"\n'
LINUX_LOCAL = '\n[validate.linux]\nrunner = "local"\nnew_test = "pytest tests"\n'
WINDOWS_SSH = '\n[validate.windows]\nrunner = "ssh"\nhost = "winbox"\nnew_test = "pytest tests"\n'
WINDOWS_LOCAL = '\n[validate.windows]\nrunner = "local"\nnew_test = "pytest tests"\n'
WINDOWS_WSL = '\n[validate.windows]\nrunner = "wsl"\nnew_test = "pytest tests"\n'
WINDOWS_BARE = '\n[validate.windows]\nnew_test = "pytest tests"\n'


def toml(platforms, *tables):
    names = ", ".join('"%s"' % name for name in platforms)
    return "[project]\nconfig_version = 1\nplatforms = [%s]\n%s" % (names, "".join(tables))


def parse(text, host=None):
    """(config, problems) of `text`; `host` is the `os.name` the parser sees, the real one
    when None."""
    try:
        if host is None:
            return parse_project_config(text), []
        with mock.patch("os.name", host):
            return parse_project_config(text), []
    except ConfigError as exc:
        return None, list(exc.problems)


class PlatformNames(unittest.TestCase):
    """AC-1"""

    def test_windows_alone_loads(self):
        cfg, problems = parse(toml(["windows"], WINDOWS_SSH))
        self.assertEqual(problems, [])
        self.assertEqual(cfg.project.platforms, ["windows"])
        self.assertEqual(cfg.validate.platforms["windows"].runner, "ssh")
        self.assertEqual(cfg.validate.platforms["windows"].host, "winbox")

    def test_linux_then_windows_loads_in_that_order(self):
        cfg, problems = parse(toml(["linux", "windows"], LINUX_WSL, WINDOWS_SSH))
        self.assertEqual(problems, [])
        self.assertEqual(cfg.project.platforms, ["linux", "windows"])

    def test_windows_then_linux_loads_in_that_order(self):
        cfg, problems = parse(toml(["windows", "linux"], LINUX_WSL, WINDOWS_SSH))
        self.assertEqual(problems, [])
        self.assertEqual(cfg.project.platforms, ["windows", "linux"])

    def test_another_name_is_refused_and_the_message_lists_both_names(self):
        for name in ("macos", "freebsd"):
            with self.subTest(name=name):
                cfg, problems = parse(toml(["linux", name], LINUX_WSL))
                self.assertIsNone(cfg)
                named = [p for p in problems if "'%s'" % name in p]
                self.assertEqual(len(named), 1, problems)
                self.assertIn("not supported", named[0])
                self.assertIn("linux", named[0])
                self.assertIn("windows", named[0])

    def test_another_name_beside_windows_is_the_only_problem(self):
        cfg, problems = parse(toml(["windows", "macos"], WINDOWS_SSH))
        self.assertIsNone(cfg)
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("macos", problems[0])

    def test_windows_listed_without_its_table_is_refused(self):
        cfg, problems = parse(toml(["linux", "windows"], LINUX_WSL))
        self.assertIsNone(cfg)
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("validate.windows", problems[0])
        self.assertNotIn("not supported", problems[0])


class WindowsRunnerRules(unittest.TestCase):
    """AC-2"""

    def assert_refused(self, problems, word):
        """One problem, about the windows table and `word`; not the unsupported-name one."""
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("validate.windows", problems[0])
        self.assertIn(word, problems[0])
        self.assertNotIn("not supported", problems[0])

    def test_wsl_is_refused_on_any_host(self):
        for host in ("nt", "posix"):
            with self.subTest(host=host):
                cfg, problems = parse(toml(["windows"], WINDOWS_WSL), host)
                self.assertIsNone(cfg)
                self.assert_refused(problems, "wsl")

    def test_a_table_without_a_runner_inherits_wsl_and_is_refused(self):
        cfg, problems = parse(toml(["windows"], WINDOWS_BARE), "nt")
        self.assertIsNone(cfg)
        self.assert_refused(problems, "wsl")

    def test_wsl_on_windows_does_not_blame_the_linux_table(self):
        cfg, problems = parse(toml(["linux", "windows"], LINUX_WSL, WINDOWS_WSL), "nt")
        self.assertIsNone(cfg)
        self.assert_refused(problems, "wsl")

    def test_local_loads_on_a_windows_host(self):
        cfg, problems = parse(toml(["windows"], WINDOWS_LOCAL), "nt")
        self.assertEqual(problems, [])
        self.assertEqual(cfg.validate.platforms["windows"].runner, "local")

    def test_local_is_refused_on_a_host_that_is_not_windows(self):
        cfg, problems = parse(toml(["windows"], WINDOWS_LOCAL), "posix")
        self.assertIsNone(cfg)
        self.assert_refused(problems, "local")

    def test_local_from_the_platform_defaults_follows_the_same_rule(self):
        text = toml(["windows"], '\n[validate.platform]\nrunner = "local"\n', WINDOWS_BARE)
        cfg, problems = parse(text, "nt")
        self.assertEqual(problems, [])
        self.assertEqual(cfg.validate.platforms["windows"].runner, "local")
        cfg, problems = parse(text, "posix")
        self.assertIsNone(cfg)
        self.assert_refused(problems, "local")

    def test_ssh_loads_on_any_host(self):
        for host in ("nt", "posix"):
            with self.subTest(host=host):
                cfg, problems = parse(toml(["windows"], WINDOWS_SSH), host)
                self.assertEqual(problems, [])
                self.assertEqual(cfg.validate.platforms["windows"].runner, "ssh")

    def test_ssh_still_needs_a_host(self):
        table = '\n[validate.windows]\nrunner = "ssh"\nnew_test = "pytest tests"\n'
        cfg, problems = parse(toml(["windows"], table))
        self.assertIsNone(cfg)
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("validate.windows.host", problems[0])

    def test_linux_keeps_every_runner_on_any_host(self):
        for host in ("nt", "posix"):
            for table in (LINUX_WSL, LINUX_LOCAL):
                with self.subTest(host=host, table=table):
                    cfg, problems = parse(toml(["linux"], table), host)
                    self.assertEqual(problems, [])
                    self.assertEqual(cfg.project.platforms, ["linux"])

    def test_linux_local_beside_windows_ssh_loads_on_any_host(self):
        for host in ("nt", "posix"):
            with self.subTest(host=host):
                cfg, problems = parse(toml(["linux", "windows"], LINUX_LOCAL, WINDOWS_SSH), host)
                self.assertEqual(problems, [])
                self.assertEqual(cfg.validate.platforms["linux"].runner, "local")


class ThroughTheCli(RepoCase):
    """AC-1 and AC-2 as the author meets them: `revali run --dry-run` on a repository whose
    revali.toml lists windows."""

    def list_windows(self, table):
        before = self.read("revali.toml")
        after = before.replace('platforms = ["linux"]', 'platforms = ["linux", "windows"]')
        self.assertNotEqual(before, after, "fixture platforms line not found")
        self.write("revali.toml", after + table)
        self.commit_all("list windows")

    def test_windows_over_ssh_passes_a_dry_run(self):
        self.list_windows(WINDOWS_SSH)
        code, out = run_cli(["run", "--dry-run"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertNotIn("not supported", out)

    def test_windows_on_wsl_stops_the_run_naming_the_table(self):
        self.list_windows(WINDOWS_WSL)
        code, out = run_cli(["run", "--dry-run"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertIn("validate.windows", out)
        self.assertIn("wsl", out)
        self.assertNotIn("not supported", out)

    def test_windows_local_follows_the_real_host(self):
        self.list_windows(WINDOWS_LOCAL)
        code, out = run_cli(["run", "--dry-run"])
        self.assertNotIn("not supported", out)
        if os.name == "nt":
            self.assertEqual(code, EXIT_OK, out)
        else:
            self.assertEqual(code, EXIT_ERROR, out)
            self.assertIn("validate.windows", out)
            self.assertIn("local", out)


if __name__ == "__main__":
    unittest.main()
