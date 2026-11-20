"""Acceptance tests: a TOML file revali reads that is not UTF-8 (fix/config-not-utf8).

AC-1: `preflight` and `run` stop with exit 1 and a line naming revali.toml and UTF-8.
AC-2: `status`, `wait` and `merge` behave as with a revali.toml that is not valid TOML.
AC-3: the same when the user's config.toml is the file, and the line names that file.
AC-4: a defaults.toml that is not UTF-8 is one ConfigError problem naming it.

Every command runs in-process through revali.cli.main with stdout and stderr captured; an
exception that escapes main is what a user sees as a traceback, and it fails the test.
"""

import contextlib
import io
import os

from revali import config
from revali.state import State
from tests.helpers import RepoCase

NOT_TOML = b"= not toml\n"
PATHS_BOX = b'\n[paths]\nstate_dir = ".box"\n'


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


def not_utf8_variants(original):
    """Ways a Windows editor leaves a file that is not UTF-8, as (name, bytes)."""
    return (
        ("a Latin-1 byte in a trailing comment", original + b"# caf\xe9\n"),
        ("the whole file saved as UTF-16", original.decode("utf-8").encode("utf-16")),
        ("a multi-byte sequence cut off at the end", original + b"# caf\xc3"),
    )


class Case(RepoCase):
    def project_file(self):
        return os.path.join(self.repo, "revali.toml")

    def user_file(self):
        return os.path.join(self.home, "config.toml")

    def default_rdir(self):
        return os.path.join(self.repo, ".revali", "feature__mul")

    def original(self):
        with open(self.project_file(), "rb") as fh:
            return fh.read()

    def put(self, path, data, commit=False):
        with open(path, "wb") as fh:
            fh.write(data)
        if commit:
            self.commit_all("rewrite %s" % os.path.basename(path))

    def assert_reported(self, out, name):
        """One line of `out` names `name` and says UTF-8; nothing reads as a traceback."""
        self.assertNotIn("Traceback", out)
        self.assertNotIn("UnicodeDecodeError", out)
        lines = [ln for ln in out.splitlines() if "UTF-8" in ln and name in ln]
        self.assertTrue(lines, "no line names %s and UTF-8:\n%s" % (name, out))

    def needs_action_state(self):
        State(stage="needs_action", branch="feature/mul", message="fix F1 first", last_exit=2).save(
            self.default_rdir()
        )

    def ready_state(self):
        State(stage="ready_to_merge", branch="feature/mul", pr_number=7, last_exit=0).save(
            self.default_rdir()
        )


class ProjectFile(Case):
    """AC-1 and AC-2: the project's revali.toml."""

    def test_preflight_stops_with_exit_1_and_names_the_file(self):
        original = self.original()
        self.put(self.project_file(), NOT_TOML + original)
        toml_code, toml_out = cli(["preflight"])
        self.assertEqual(toml_code, 1, toml_out)
        for name, data in not_utf8_variants(original):
            with self.subTest(name):
                self.put(self.project_file(), data)
                code, out = cli(["preflight"])
                self.assertEqual(code, toml_code, out)
                self.assertIn("ERROR", out)
                self.assert_reported(out, "revali.toml")

    def test_run_stops_with_exit_1_and_names_the_file(self):
        self.put(self.project_file(), self.original() + b"# caf\xe9\n", commit=True)
        code, out = cli(["run", "--foreground"])
        self.assertEqual(code, 1, out)
        self.assertIn("ERROR", out)
        self.assert_reported(out, "revali.toml")
        # recorded like any other stop: `wait` reports it with the same exit code
        code, out = cli(["wait"])
        self.assertEqual(code, 1, out)
        self.assert_reported(out, "revali.toml")

    def test_status_and_wait_use_the_default_state_directory(self):
        # the file names another state directory, but it cannot be read: as with a file that
        # is not valid TOML, the state under the default directory is the one reported
        original = self.original()
        self.needs_action_state()
        self.put(self.project_file(), NOT_TOML + original + PATHS_BOX)
        expected = {cmd: cli([cmd]) for cmd in ("status", "wait")}
        self.assertEqual(expected["status"][0], 0, expected["status"][1])
        self.assertEqual(expected["wait"][0], 2, expected["wait"][1])
        for name, data in not_utf8_variants(original + PATHS_BOX):
            self.put(self.project_file(), data)
            for cmd in ("status", "wait"):
                with self.subTest(name, cmd=cmd):
                    code, out = cli([cmd])
                    self.assertEqual((code, out), expected[cmd])
                    self.assertNotIn("Traceback", out)
        self.assertIn("stage: needs_action", expected["status"][1])
        self.assertIn("needs_action: fix F1 first", expected["wait"][1])
        self.assertFalse(self.exists(".box"))

    def test_merge_of_a_ready_branch_stops_as_with_a_file_that_is_not_toml(self):
        original = self.original()
        self.ready_state()
        self.put(self.project_file(), NOT_TOML + original, commit=True)
        toml_code, toml_out = cli(["merge"])
        self.assertEqual(toml_code, 1, toml_out)
        self.assertNotIn("Traceback", toml_out)
        self.put(self.project_file(), original + b"# caf\xe9\n", commit=True)
        code, out = cli(["merge"])
        self.assertEqual(code, toml_code, out)
        self.assertIn("ERROR", out)
        self.assert_reported(out, "revali.toml")
        # nothing was merged and the branch directory is still there
        self.assertTrue(os.path.isfile(State.path(self.default_rdir())))
        self.assertEqual(State.load(self.default_rdir()).stage, "ready_to_merge")


class UserFile(Case):
    """AC-3: the user's config.toml under REVALI_HOME; the project's revali.toml is fine."""

    def assert_names_the_users_file_only(self, out):
        self.assert_reported(out, self.user_file())
        blamed = [ln for ln in out.splitlines() if "UTF-8" in ln and "revali.toml" in ln]
        self.assertFalse(blamed, out)

    def test_preflight_names_the_users_file(self):
        self.put(self.user_file(), NOT_TOML)
        toml_code, toml_out = cli(["preflight"])
        self.assertEqual(toml_code, 1, toml_out)
        for name, data in not_utf8_variants(b'checklist = "mine.md"\n'):
            with self.subTest(name):
                self.put(self.user_file(), data)
                code, out = cli(["preflight"])
                self.assertEqual(code, toml_code, out)
                self.assertIn("ERROR", out)
                self.assert_names_the_users_file_only(out)

    def test_run_names_the_users_file(self):
        self.put(self.user_file(), b"# caf\xe9\n")
        code, out = cli(["run", "--foreground"])
        self.assertEqual(code, 1, out)
        self.assertIn("ERROR", out)
        self.assert_names_the_users_file_only(out)

    def test_status_and_wait_use_the_default_state_directory(self):
        self.needs_action_state()
        body = b'[paths]\nstate_dir = ".ubox"\n'
        self.put(self.user_file(), NOT_TOML + body)
        expected = {cmd: cli([cmd]) for cmd in ("status", "wait")}
        self.assertEqual(expected["status"][0], 0, expected["status"][1])
        self.assertEqual(expected["wait"][0], 2, expected["wait"][1])
        self.put(self.user_file(), b"# caf\xe9\n" + body)
        for cmd in ("status", "wait"):
            with self.subTest(cmd=cmd):
                code, out = cli([cmd])
                self.assertEqual((code, out), expected[cmd])
                self.assertNotIn("Traceback", out)
        self.assertIn("stage: needs_action", expected["status"][1])
        self.assertFalse(self.exists(".ubox"))

    def test_merge_of_a_ready_branch_names_the_users_file(self):
        self.ready_state()
        self.put(self.user_file(), NOT_TOML)
        toml_code, toml_out = cli(["merge"])
        self.assertEqual(toml_code, 1, toml_out)
        self.put(self.user_file(), b"# caf\xe9\n")
        code, out = cli(["merge"])
        self.assertEqual(code, toml_code, out)
        self.assertIn("ERROR", out)
        self.assert_names_the_users_file_only(out)
        self.assertEqual(State.load(self.default_rdir()).stage, "ready_to_merge")


class DefaultsFile(Case):
    """AC-4: a copy of revali's defaults.toml, loaded the way revali loads its own."""

    def problems_of(self, data):
        path = os.path.join(self.tmp, "defaults.toml")
        self.put(path, data)
        with self.assertRaises(config.ConfigError) as caught:
            config.load_defaults(path)
        return path, caught.exception.problems

    def test_not_utf8_is_one_problem_naming_the_file_like_not_toml(self):
        with open(config.DEFAULTS_FILE, "rb") as fh:
            original = fh.read()
        path, toml_problems = self.problems_of(NOT_TOML + original)
        self.assertEqual(len(toml_problems), 1, toml_problems)
        self.assertIn(path, toml_problems[0])
        for name, data in not_utf8_variants(original):
            with self.subTest(name):
                path, problems = self.problems_of(data)
                self.assertEqual(len(problems), 1, problems)
                self.assertIn(path, problems[0])
                self.assertIn("UTF-8", problems[0])
                self.assertNotIn("\n", problems[0])
