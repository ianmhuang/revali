"""AC-1: `runner = "ssh"` with `host` is accepted; `ssh` without `host` and any runner
outside wsl | local | ssh are config errors that name the allowed runners; a
`sandbox_dir` with whitespace is rejected for ssh (round 1, F1).
AC-5: README, templates/revali.toml and defaults.toml document the ssh runner and its
keys (host, connect_timeout_s, transfer_timeout_min; round 1, F5), and the empty code
fence in the README Sandbox section is gone.
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

    def problems(self):
        with self.assertRaises(ConfigError) as cm:
            load_project_config(self.repo)
        return "\n".join(cm.exception.problems)

    def test_ssh_with_host_is_accepted(self):
        self.platform_table('runner = "ssh"\nhost = "user@box"\n')
        cfg = load_project_config(self.repo)
        plat = cfg.validate.platforms["linux"]
        self.assertEqual(plat.runner, "ssh")
        self.assertEqual(plat.host, "user@box")

    def test_ssh_timeouts_come_from_defaults(self):
        # round 1, F5: the ssh timeouts are config keys, so a table that names only the
        # host gets positive values from defaults.toml
        self.platform_table('runner = "ssh"\nhost = "user@box"\n')
        plat = load_project_config(self.repo).validate.platforms["linux"]
        self.assertGreater(plat.connect_timeout_s, 0)
        self.assertGreater(plat.transfer_timeout_min, 0)
        self.platform_table('runner = "ssh"\nhost = "user@box"\nconnect_timeout_s = 3\ntransfer_timeout_min = 2\n')
        plat = load_project_config(self.repo).validate.platforms["linux"]
        self.assertEqual((plat.connect_timeout_s, plat.transfer_timeout_min), (3, 2))

    def test_ssh_without_host_is_a_config_error(self):
        self.platform_table('runner = "ssh"\n')
        self.assertIn("validate.linux.host", self.problems())

    def test_unknown_runner_names_the_allowed_runners(self):
        self.platform_table('runner = "docker"\n')
        text = self.problems()
        self.assertIn("validate.linux.runner", text)
        for name in ("wsl", "local", "ssh"):
            self.assertIn(name, text)

    def test_sandbox_dir_with_whitespace_is_rejected_for_ssh(self):
        # round 1, F1: the remote directory travels on scp and ssh command lines
        self.platform_table('runner = "ssh"\nhost = "user@box"\nsandbox_dir = "~/sand box"\n')
        self.assertIn("validate.linux.sandbox_dir", self.problems())

    def test_sandbox_dir_with_whitespace_stays_legal_for_wsl(self):
        self.platform_table('runner = "wsl"\ndistro = "Ubuntu"\nsandbox_dir = "~/sand box"\n')
        self.assertEqual(load_project_config(self.repo).validate.platforms["linux"].sandbox_dir, "~/sand box")

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

    def test_readme_documents_the_ssh_timeout_keys(self):
        text = self.read("README.md")
        self.assertIn("connect_timeout_s", text)
        self.assertIn("transfer_timeout_min", text)

    def test_template_documents_the_host_key(self):
        text = self.read("templates", "revali.toml")
        self.assertIn("ssh", text)
        self.assertIn('host = "', text)

    def test_defaults_carry_the_ssh_keys(self):
        text = self.read("defaults.toml")
        self.assertRegex(text, r"(?m)^host\s*=")
        self.assertRegex(text, r"(?m)^connect_timeout_s\s*=\s*[1-9]")
        self.assertRegex(text, r"(?m)^transfer_timeout_min\s*=\s*[1-9]")


if __name__ == "__main__":
    unittest.main()
