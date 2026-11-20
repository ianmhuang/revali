"""Config files with a UTF-8 BOM, and init's message for one that is not UTF-8 (M3.36).

A leading BOM (EF BB BF), which some Windows editors write, is UTF-8: revali reads the file
as if it were not there. `revali init` names a config file that is not UTF-8 in the words
every other command uses since M3.32.
"""

import os
import unittest
from unittest import mock

from revali import EXIT_OK, config
from revali.state import State
from tests.helpers import RepoCase
from tests.test_config_not_utf8 import NOT_UTF8, run

BOM = b"\xef\xbb\xbf"


class Case(RepoCase):
    def project_file(self):
        return os.path.join(self.repo, "revali.toml")

    def user_file(self):
        return os.path.join(self.home, "config.toml")

    def write_bytes(self, path, data):
        with open(path, "wb") as fh:
            fh.write(data)
        if path.startswith(self.repo):
            self.commit_all("write %s" % os.path.basename(path))

    def project_bytes(self):
        with open(self.project_file(), "rb") as fh:
            return fh.read()

    def error_lines(self, out):
        return [ln for ln in out.splitlines() if ln.startswith("ERROR")]


class ProjectFileWithBom(Case):
    """AC-2: the project's revali.toml."""

    def test_preflight_and_run_read_it(self):
        self.write_bytes(self.project_file(), BOM + self.project_bytes())
        code, out = run(["preflight"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("preflight OK", out)
        self.assertNotIn("not valid TOML", out)

    def test_its_keys_take_effect(self):
        self.write_bytes(
            self.project_file(), BOM + self.project_bytes() + b'\n[paths]\nstate_dir = ".rv"\n'
        )
        self.assertEqual(config.load_project_config(self.repo).paths.state_dir, ".rv")
        self.assertEqual(config.paths_for(self.repo).state_dir, ".rv")

    def test_the_paths_fallback_reads_it(self):
        # an unknown key keeps the whole config from loading; status, wait and merge then
        # take [paths] from the file as written, BOM or not
        self.write_bytes(
            self.project_file(),
            BOM + self.project_bytes() + b'\n[paths]\nstate_dir = ".rv"\nno_such_key = 1\n',
        )
        with self.assertRaises(config.ConfigError):
            config.load_project_config(self.repo)
        self.assertEqual(config.paths_for(self.repo).state_dir, ".rv")

    def test_status_finds_the_state_directory_it_names(self):
        self.write_bytes(
            self.project_file(), BOM + self.project_bytes() + b'\n[paths]\nstate_dir = ".rv"\n'
        )
        rdir = os.path.join(self.repo, ".rv", "feature__mul")
        os.makedirs(rdir)
        State(stage="ready_to_merge").save(rdir)
        code, out = run(["status"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("stage: ready_to_merge", out.splitlines())
        self.assertNotIn("not valid TOML", out)

    def test_init_takes_the_state_directory_from_it(self):
        self.write_bytes(
            self.project_file(), BOM + self.project_bytes() + b'\n[paths]\nstate_dir = ".rv"\n'
        )
        code, out = run(["init"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn(".rv/", self.read(".gitignore").splitlines())

    def test_a_bom_after_the_start_is_still_not_valid_toml(self):
        # only a leading BOM is dropped; U+FEFF elsewhere is the TOML parser's business
        self.write_bytes(self.project_file(), self.project_bytes() + b"\n" + BOM + b"x = 1\n")
        code, out = run(["preflight"])
        self.assertEqual(code, 1, out)
        named = [ln for ln in out.splitlines() if self.project_file() in ln]
        self.assertTrue(any("not valid TOML" in ln for ln in named), out)

    def test_a_bad_byte_after_a_bom_is_named_with_its_line(self):
        self.write_bytes(self.project_file(), BOM + b"[project]\n" + NOT_UTF8)
        code, out = run(["preflight"])
        self.assertEqual(code, 1, out)
        self.assertIn("%s: not UTF-8 (line 2: byte 0xe9)" % self.project_file(), out)


class UserFileWithBom(Case):
    """AC-2: the user's config.toml."""

    def test_preflight_reads_it_and_its_keys_take_effect(self):
        self.write_bytes(self.user_file(), BOM + b'history_path = "h.jsonl"\n')
        self.assertEqual(config.load_user_config().history_path, "h.jsonl")
        code, out = run(["preflight"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertNotIn("not valid TOML", out)


class DefaultsWithBom(Case):
    """AC-2: revali's own defaults.toml."""

    def test_it_loads_and_commands_run(self):
        path = os.path.join(self.tmp, "defaults.toml")
        with open(config.DEFAULTS_FILE, "rb") as fh:
            body = fh.read()
        with open(path, "wb") as fh:
            fh.write(BOM + body)
        config._DEFAULTS_CACHE.pop(path, None)
        self.addCleanup(config._DEFAULTS_CACHE.pop, path, None)
        self.assertEqual(config.load_defaults(path), config.load_defaults())
        with mock.patch.object(config.load_defaults, "__defaults__", (path,)):
            for argv in (["status"], ["preflight"]):
                with self.subTest(argv=argv):
                    code, out = run(argv)
                    self.assertEqual(code, EXIT_OK, out)
                    self.assertNotIn("not valid TOML", out)


class InitNamesNotUtf8(Case):
    """AC-1: init's message for a config file that is not UTF-8 is the one other commands
    give."""

    def test_the_projects_file(self):
        self.write_bytes(self.project_file(), b"[project]\n" + NOT_UTF8)
        code, out = run(["init"])
        self.assertEqual(code, 1, out)
        self.assertNotIn("Traceback", out)
        errors = self.error_lines(out)
        self.assertEqual(len(errors), 1, out)
        self.assertEqual(
            errors[0],
            "ERROR: %s: not UTF-8 (line 2: byte 0xe9); save it as UTF-8" % self.project_file(),
        )

    def test_the_users_file(self):
        self.write_bytes(self.user_file(), NOT_UTF8)
        code, out = run(["init"])
        self.assertEqual(code, 1, out)
        self.assertNotIn("Traceback", out)
        errors = self.error_lines(out)
        self.assertEqual(len(errors), 1, out)
        self.assertEqual(
            errors[0],
            "ERROR: %s: not UTF-8 (line 1: byte 0xe9); save it as UTF-8" % self.user_file(),
        )


if __name__ == "__main__":
    unittest.main()
