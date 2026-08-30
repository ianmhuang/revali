"""AC-1: `runner = "ssh"` with `host` is accepted; `ssh` without `host` and any runner
outside wsl | local | ssh are config errors that name the allowed runners.
AC-5: README, templates/revali.toml and defaults.toml document the ssh runner and the
empty code fence in the README Sandbox section is gone.
"""
import os
import unittest

from tests.helpers import ROOT, RepoCase, run_cli
from revali import EXIT_ERROR
from revali.config import ConfigError, load_project_config


class SshConfigTests(RepoCase):
    runner = "wsl"

    def platform_table(self, body):
        """Replace the fixture's wsl lines of [validate.linux] with `body` and commit."""
        before = self.read("revali.toml")
        after = before.replace('runner = "wsl"\ndistro = "Ubuntu"\n', body)
        self.assertNotEqual(before, after, "fixture platform table not found")
        self.write("revali.toml", after)
        self.commit_all("platform table")

    def test_ssh_with_host_is_accepted(self):
        self.platform_table('runner = "ssh"\nhost = "user@box"\n')
        cfg = load_project_config(self.repo)
        plat = cfg.validate.platforms["linux"]
        self.assertEqual(plat.runner, "ssh")
        self.assertEqual(plat.host, "user@box")

    def test_ssh_without_host_is_a_config_error(self):
        self.platform_table('runner = "ssh"\n')
        with self.assertRaises(ConfigError) as cm:
            load_project_config(self.repo)
        text = "\n".join(cm.exception.problems)
        self.assertIn("validate.linux.host", text)

    def test_unknown_runner_names_the_allowed_runners(self):
        self.platform_table('runner = "docker"\n')
        with self.assertRaises(ConfigError) as cm:
            load_project_config(self.repo)
        text = "\n".join(cm.exception.problems)
        self.assertIn("validate.linux.runner", text)
        for name in ("wsl", "local", "ssh"):
            self.assertIn(name, text)

    def test_local_and_wsl_still_load(self):
        self.assertEqual(load_project_config(self.repo).validate.platforms["linux"].runner, "wsl")
        self.platform_table('runner = "local"\n')
        self.assertEqual(load_project_config(self.repo).validate.platforms["linux"].runner, "local")

    def test_ssh_config_error_stops_the_cli_with_exit_1(self):
        self.platform_table('runner = "ssh"\n')
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertIn("validate.linux.host", out)


class DocsTests(unittest.TestCase):
    def read(self, *parts):
        with open(os.path.join(ROOT, *parts), "r", encoding="utf-8", newline="") as fh:
            return fh.read()

    def test_readme_documents_the_ssh_runner(self):
        text = self.read("README.md")
        self.assertIn('runner = "ssh"', text)
        self.assertIn("host = ", text)
        self.assertIn("BatchMode", text)
        self.assertNotIn("```\n```", text, "empty code fence still in README")
        self.assertNotIn("```\r\n```", text, "empty code fence still in README")

    def test_template_documents_the_host_key(self):
        text = self.read("templates", "revali.toml")
        self.assertIn("ssh", text)
        self.assertIn('host = "', text)

    def test_defaults_carry_the_host_key(self):
        text = self.read("defaults.toml")
        self.assertRegex(text, r"(?m)^host\s*=")


if __name__ == "__main__":
    unittest.main()
