"""Acceptance tests: config files with a UTF-8 BOM, and init's message for a config file
that is not UTF-8 (fix/config-read-messages).

AC-1: `revali init` stops with exit 1, no traceback and one ERROR line of the form
      `<path>: not UTF-8 (line N: byte 0xNN); save it as UTF-8`, naming the file, when the
      project's revali.toml or the user's config.toml is not UTF-8.
AC-2: a revali.toml, user config.toml or defaults.toml that starts with a UTF-8 BOM is read
      as if the BOM were not there by `preflight`, `run`, `status`, `wait`, `merge`, `init`
      and the `[paths]` fallback; a U+FEFF anywhere else is still the TOML parser's business.

Every command runs in-process through revali.cli.main with stdout and stderr captured; an
exception that escapes main is what a user sees as a traceback, and it fails the test.
"""

import contextlib
import io
import os
import re
from unittest import mock

from revali import config
from revali.state import State
from tests.helpers import RepoCase

BOM = b"\xef\xbb\xbf"
NOT_TOML = b"= not toml\n"
PATHS_BOX = b'\n[paths]\nstate_dir = ".box"\n'
NOT_UTF8_LINE = re.compile(
    r"^ERROR: (.+): not UTF-8 \(line (\d+): byte (0x[0-9a-f]{2})\); save it as UTF-8$"
)


def cli(argv):
    """(exit code, stdout + stderr) of one revali command."""
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

    def original(self):
        with open(self.project_file(), "rb") as fh:
            return fh.read()

    def put(self, path, data, commit=False):
        with open(path, "wb") as fh:
            fh.write(data)
        if commit:
            self.commit_all("rewrite %s" % os.path.basename(path))

    def needs_action_state_in(self, state_dir):
        """A finished run's state for the checked-out branch under `state_dir`."""
        rdir = os.path.join(self.repo, state_dir, "feature__mul")
        State(stage="needs_action", branch="feature/mul", message="fix F1 first", last_exit=2).save(
            rdir
        )

    def error_lines(self, out):
        return [ln for ln in out.splitlines() if ln.startswith("ERROR")]

    def assert_status_wait_and_merge_see_the_state(self):
        """The three commands that only need the state directory report the state saved by
        needs_action_state_in, which they find only when they read the right directory."""
        code, out = cli(["status"])
        self.assertEqual(code, 0, out)
        self.assertIn("stage: needs_action", out.splitlines())
        self.assertNotIn("not valid TOML", out)
        code, out = cli(["wait"])
        self.assertEqual(code, 2, out)
        self.assertIn("needs_action: fix F1 first", out.splitlines())
        self.assertNotIn("not valid TOML", out)
        # not ready, so nothing is merged; the stage in the refusal is the one that was read
        code, out = cli(["merge"])
        self.assertEqual(code, 1, out)
        self.assertTrue(
            [ln for ln in self.error_lines(out) if "stage: needs_action" in ln],
            "merge did not read the state:\n%s" % out,
        )
        self.assertNotIn("not valid TOML", out)


class ProjectFileWithBom(Case):
    """AC-2: the project's revali.toml."""

    def test_preflight_passes(self):
        self.put(self.project_file(), BOM + self.original(), commit=True)
        code, out = cli(["preflight"])
        self.assertEqual(code, 0, out)
        self.assertIn("preflight OK", out.splitlines())
        self.assertNotIn("not valid TOML", out)

    def test_run_gets_past_its_preflight(self):
        # a dry run is `run` up to the end of preflight: it starts no reviewer and no PR
        self.put(self.project_file(), BOM + self.original(), commit=True)
        code, out = cli(["run", "--foreground", "--dry-run"])
        self.assertEqual(code, 0, out)
        self.assertIn("DRY RUN OK", out)
        self.assertNotIn("not valid TOML", out)

    def test_status_wait_and_merge_use_the_state_directory_it_names(self):
        # the key takes effect: the state is under .box, where only this file points
        self.put(self.project_file(), BOM + self.original() + PATHS_BOX)
        self.needs_action_state_in(".box")
        self.assert_status_wait_and_merge_see_the_state()

    def test_the_paths_fallback_reads_it(self):
        # an unknown key keeps the config from loading, so the commands that only need the
        # state directory take [paths] from the file as written
        self.put(self.project_file(), BOM + self.original() + PATHS_BOX + b"no_such_key = 1\n")
        self.needs_action_state_in(".box")
        code, out = cli(["preflight"])
        self.assertEqual(code, 1, out)  # the config does not load, and not because of the BOM
        self.assertNotIn("not valid TOML", out)
        self.assertIn("no_such_key", out)
        self.assert_status_wait_and_merge_see_the_state()

    def test_init_ignores_the_state_directory_it_names(self):
        self.put(self.project_file(), BOM + self.original() + PATHS_BOX)
        code, out = cli(["init"])
        self.assertEqual(code, 0, out)
        self.assertEqual(self.error_lines(out), [], out)
        self.assertIn(".box/", self.read(".gitignore").splitlines())

    def test_a_bom_that_is_not_at_the_very_start_is_still_not_valid_toml(self):
        original = self.original()
        variants = (
            ("a second BOM after the first", BOM + BOM + original),
            ("a BOM at the start of a later line", original + b"\n" + BOM + b"x = 1\n"),
        )
        for name, data in variants:
            with self.subTest(name):
                self.put(self.project_file(), data)
                code, out = cli(["preflight"])
                self.assertEqual(code, 1, out)
                named = [ln for ln in out.splitlines() if "revali.toml" in ln]
                self.assertTrue(any("not valid TOML" in ln for ln in named), out)


class UserFileWithBom(Case):
    """AC-2: the user's config.toml under REVALI_HOME, with CRLF line endings as a Windows
    editor writes it; the project's revali.toml is as the fixture made it."""

    def test_preflight_and_run_pass(self):
        self.put(self.user_file(), BOM + b"# my machine\r\n")
        code, out = cli(["preflight"])
        self.assertEqual(code, 0, out)
        self.assertIn("preflight OK", out.splitlines())
        code, out = cli(["run", "--foreground", "--dry-run"])
        self.assertEqual(code, 0, out)
        self.assertIn("DRY RUN OK", out)
        self.assertNotIn("not valid TOML", out)

    def test_status_wait_and_merge_use_the_state_directory_it_names(self):
        self.put(self.user_file(), BOM + b'[paths]\r\nstate_dir = ".ubox"\r\n')
        self.needs_action_state_in(".ubox")
        self.assert_status_wait_and_merge_see_the_state()

    def test_init_ignores_the_state_directory_it_names(self):
        self.put(self.user_file(), BOM + b'[paths]\r\nstate_dir = ".ubox"\r\n')
        code, out = cli(["init"])
        self.assertEqual(code, 0, out)
        self.assertEqual(self.error_lines(out), [], out)
        self.assertIn(".ubox/", self.read(".gitignore").splitlines())


class DefaultsWithBom(Case):
    """AC-2: revali's own defaults.toml. The commands read a copy where they would read the
    shipped file, so the checkout the suite runs from is not touched."""

    def setUp(self):
        super().setUp()
        self.defaults = os.path.join(self.tmp, "defaults.toml")
        real = config.load_defaults
        patcher = mock.patch.object(
            config, "load_defaults", lambda path=None: real(path or self.defaults)
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(config._DEFAULTS_CACHE.pop, self.defaults, None)
        with open(config.DEFAULTS_FILE, "rb") as fh:
            self.shipped = fh.read()

    def test_commands_run_as_with_the_shipped_file(self):
        # first that the copy is the file the commands read: damaged, it stops them
        self.put(self.defaults, NOT_TOML + self.shipped)
        code, out = cli(["status"])
        self.assertEqual(code, 1, out)
        self.assertTrue([ln for ln in self.error_lines(out) if self.defaults in ln], out)
        # the same copy with nothing but a BOM before the shipped text
        self.put(self.defaults, BOM + self.shipped)
        code, out = cli(["status"])
        self.assertEqual(code, 0, out)
        self.assertEqual(self.error_lines(out), [], out)
        code, out = cli(["preflight"])
        self.assertEqual(code, 0, out)
        self.assertIn("preflight OK", out.splitlines())
        code, out = cli(["init"])
        self.assertEqual(code, 0, out)
        self.assertEqual(self.error_lines(out), [], out)


class InitNamesNotUtf8(Case):
    """AC-1: init's one ERROR line for a config file that is not UTF-8."""

    def assert_one_not_utf8_line(self, out, path, line, byte):
        """The ERROR line of `out`, which must be the only one, name `path` and give the
        line and the byte; returns the problem text after "ERROR: "."""
        self.assertNotIn("Traceback", out)
        self.assertNotIn("UnicodeDecodeError", out)
        errors = self.error_lines(out)
        self.assertEqual(len(errors), 1, out)
        match = NOT_UTF8_LINE.match(errors[0])
        self.assertIsNotNone(match, "not the line the other commands print: %r" % errors[0])
        self.assertTrue(os.path.samefile(match.group(1), path), errors[0])
        self.assertEqual((int(match.group(2)), match.group(3)), (line, byte), errors[0])
        return errors[0][len("ERROR: ") :]

    def test_the_projects_file(self):
        variants = (
            ("a Latin-1 byte on line 3", b"[project]\n# two\n# caf\xe9\n", 3, "0xe9"),
            ("saved as UTF-16", b"\xff\xfe" + "[project]\n".encode("utf-16-le"), 1, "0xff"),
            ("a bad byte on the line after a BOM", BOM + b"[project]\n# caf\xe9\n", 2, "0xe9"),
        )
        for name, data, line, byte in variants:
            with self.subTest(name):
                self.put(self.project_file(), data)
                code, out = cli(["init"])
                self.assertEqual(code, 1, out)
                problem = self.assert_one_not_utf8_line(out, self.project_file(), line, byte)
                # the words another command uses for the same file
                self.assertIn(problem, cli(["preflight"])[1])

    def test_the_users_file(self):
        variants = (
            ("a Latin-1 byte on line 2", b"# mine\n# caf\xe9\n", 2, "0xe9"),
            ("saved as UTF-16", b"\xff\xfe" + "# mine\n".encode("utf-16-le"), 1, "0xff"),
        )
        for name, data, line, byte in variants:
            with self.subTest(name):
                self.put(self.user_file(), data)
                code, out = cli(["init"])
                self.assertEqual(code, 1, out)
                problem = self.assert_one_not_utf8_line(out, self.user_file(), line, byte)
                self.assertIn(problem, cli(["preflight"])[1])
