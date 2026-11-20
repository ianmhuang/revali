"""A config file that is not UTF-8 is reported like one that is not valid TOML (M3.32).

Each case puts a Latin-1 byte (0xE9, "é") in a comment of a file revali reads as UTF-8, and
compares with the same file made invalid TOML instead: `preflight` and `run` stop with one
ERROR line naming the file, `status`, `wait` and `merge` fall back to the defaults.
"""

import contextlib
import io
import os
import tempfile
import unittest
from unittest import mock

from revali import config
from tests.helpers import RepoCase, rmtree_force

NOT_UTF8 = b"# caf\xe9 (Latin-1)\n"
NOT_TOML = b"= not toml\n"


def run(argv):
    """(exit code, stdout + stderr) of revali.cli.main run in-process. An exception, which
    is what a user sees as a traceback, propagates and fails the test."""
    from revali.cli import main

    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = main(argv)
        except SystemExit as exc:
            code = int(exc.code or 0)
    return code, out.getvalue() + err.getvalue()


class Case(RepoCase):
    def project_file(self):
        return os.path.join(self.repo, "revali.toml")

    def user_file(self):
        return os.path.join(self.home, "config.toml")

    def spoil(self, path, head):
        """Put `head` before the file's text (or make it the file), and commit it when it is
        in the repository, so `run` does not stop at a dirty tree first."""
        body = b""
        if os.path.isfile(path):
            with open(path, "rb") as fh:
                body = fh.read()
        with open(path, "wb") as fh:
            fh.write(head + body)
        if path.startswith(self.repo):
            self.commit_all("spoil %s" % os.path.basename(path))

    def error_lines(self, out):
        return [ln for ln in out.splitlines() if "ERROR" in ln or ln.startswith("  - ")]

    def assert_names_not_utf8(self, out, path):
        # review round 1 F3: the line naming the file is one of the ERROR or problem lines
        named = [ln for ln in self.error_lines(out) if path in ln]
        self.assertTrue(named, "no ERROR or problem line names %s:\n%s" % (path, out))
        self.assertTrue(any("not UTF-8" in ln for ln in named), "no line says not UTF-8:\n%s" % out)
        self.assertNotIn("Traceback", out)
        self.assertNotIn("UnicodeDecodeError", out)


class ProjectFile(Case):
    """AC-1, AC-2: the project's revali.toml."""

    def test_preflight_names_the_file(self):
        self.spoil(self.project_file(), NOT_UTF8)
        code, out = run(["preflight"])
        self.assertEqual(code, 1, out)
        self.assert_names_not_utf8(out, self.project_file())

    def test_run_names_the_file(self):
        self.spoil(self.project_file(), NOT_UTF8)
        code, out = run(["run", "--foreground"])
        self.assertEqual(code, 1, out)
        self.assert_names_not_utf8(out, self.project_file())

    def test_status_wait_and_merge_go_on_as_with_a_file_that_is_not_toml(self):
        with open(self.project_file(), "rb") as fh:
            original = fh.read()
        self.spoil(self.project_file(), NOT_TOML)
        expected = {cmd: run([cmd]) for cmd in ("status", "wait", "merge")}
        # review round 1 F2: from the original text, so the file is not UTF-8 and nothing else
        with open(self.project_file(), "wb") as fh:
            fh.write(original)
        self.spoil(self.project_file(), NOT_UTF8)
        for cmd, (code, out) in expected.items():
            with self.subTest(cmd=cmd):
                self.assertEqual(run([cmd]), (code, out))
                self.assertNotIn("Traceback", out)


class DamagedDefaults(Case):
    """Review round 1 F1 and the AC-4 scope note: a defaults.toml that is not UTF-8 (or not
    valid TOML) is one ERROR line and exit 1 at the command line, from any command, `init`
    included, where it was a traceback."""

    def damaged_defaults(self, head):
        """A copy of defaults.toml with `head` before its text, which load_defaults reads
        by default while the returned patch is active."""
        path = os.path.join(self.tmp, "defaults.toml")
        with open(config.DEFAULTS_FILE, "rb") as fh:
            body = fh.read()
        with open(path, "wb") as fh:
            fh.write(head + body)
        config._DEFAULTS_CACHE.pop(path, None)
        self.addCleanup(config._DEFAULTS_CACHE.pop, path, None)
        return path, mock.patch.object(config.load_defaults, "__defaults__", (path,))

    def test_every_command_reports_it_in_one_line(self):
        for head, words in ((NOT_UTF8, "not UTF-8"), (NOT_TOML, "not valid TOML")):
            path, patch = self.damaged_defaults(head)
            with patch:
                for argv in (["status"], ["wait"], ["merge"], ["preflight"], ["init"]):
                    with self.subTest(argv=argv, words=words):
                        code, out = run(argv)
                        self.assertEqual(code, 1, out)
                        self.assertNotIn("Traceback", out)
                        named = [ln for ln in self.error_lines(out) if path in ln and words in ln]
                        self.assertTrue(named, out)


class UserFile(Case):
    """AC-3: the user's config.toml, with the project's revali.toml as it is."""

    def test_preflight_names_the_users_file_not_the_projects(self):
        self.spoil(self.user_file(), NOT_UTF8)
        code, out = run(["preflight"])
        self.assertEqual(code, 1, out)
        self.assert_names_not_utf8(out, self.user_file())
        self.assertFalse(
            [ln for ln in out.splitlines() if self.project_file() in ln and "UTF-8" in ln], out
        )

    def test_run_names_the_users_file(self):
        self.spoil(self.user_file(), NOT_UTF8)
        code, out = run(["run", "--foreground"])
        self.assertEqual(code, 1, out)
        self.assert_names_not_utf8(out, self.user_file())

    def test_status_wait_and_merge_go_on_as_with_a_file_that_is_not_toml(self):
        self.spoil(self.user_file(), NOT_TOML)
        expected = {cmd: run([cmd]) for cmd in ("status", "wait", "merge")}
        os.remove(self.user_file())
        self.spoil(self.user_file(), NOT_UTF8)
        for cmd, (code, out) in expected.items():
            with self.subTest(cmd=cmd):
                self.assertEqual(run([cmd]), (code, out))
                self.assertNotIn("Traceback", out)


class DefaultsFile(unittest.TestCase):
    """AC-4: revali's own defaults.toml, read through load_defaults."""

    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="revali defaults "))
        self.addCleanup(rmtree_force, self.tmp)

    def load(self, head):
        path = os.path.join(self.tmp, "defaults.toml")
        with open(config.DEFAULTS_FILE, "rb") as fh:
            body = fh.read()
        with open(path, "wb") as fh:
            fh.write(head + body)
        self.addCleanup(config._DEFAULTS_CACHE.pop, path, None)
        with self.assertRaises(config.ConfigError) as ctx:
            config.load_defaults(path)
        return path, ctx.exception.problems

    def test_a_defaults_file_that_is_not_utf8_is_one_problem_naming_it(self):
        path, problems = self.load(NOT_UTF8)
        self.assertEqual(len(problems), 1, problems)
        self.assertIn(path, problems[0])
        self.assertIn("not UTF-8", problems[0])
        # the same path as a file that is not valid TOML
        _, toml_problems = self.load(NOT_TOML)
        self.assertEqual(len(toml_problems), 1)
        self.assertTrue(toml_problems[0].startswith(path + ": "))
        self.assertTrue(problems[0].startswith(path + ": "))


if __name__ == "__main__":
    unittest.main()
