"""Acceptance tests: `revali init` and a config file that is UTF-8 but not valid TOML
(fix/init-invalid-toml).

AC-1: when the project's revali.toml or the user's config.toml is not valid TOML, init exits
      1 with no traceback and one ERROR line `<path>: not valid TOML: ...` naming that file,
      the line `preflight` gives. `.gitignore` is left as it was; the files init wrote before
      it stopped stay and are reported.
AC-2: a file that parses but holds a key revali does not know does not stop init: it
      completes, the file's `[paths]` takes effect, and `preflight` reports the key.

The last class runs `status` on what docs/configuration.md says of a damaged file (AC-3): the
other file's `[paths]` still applies.

Every command runs in-process through revali.cli.main with stdout and stderr captured; an
exception that escapes main is what a user sees as a traceback, and it fails the test.
"""

import contextlib
import io
import os
import re

from revali.state import State
from tests.helpers import RepoCase

PATHS_BOX = b'\n[paths]\nstate_dir = ".box"\n'
PATHS_UBOX = b'[paths]\nstate_dir = ".ubox"\n'
# without the default state directory, so an init that goes on has a rule to add
GITIGNORE = b".venv/\n__pycache__/\n"
NOT_TOML_LINE = re.compile(r"^ERROR: (.+): not valid TOML: (.+)$")
ACTIONS = ("wrote", "appended", "kept")


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


def reported(out):
    """{path: action} of the per-file lines init printed."""
    pairs = {}
    for line in out.splitlines():
        words = line.split()
        if len(words) >= 2 and words[0] in ACTIONS:
            pairs[words[1].replace("\\", "/")] = words[0]
    return pairs


class Case(RepoCase):
    def setUp(self):
        super().setUp()
        self.put(self.gitignore(), GITIGNORE)

    def project_file(self):
        return os.path.join(self.repo, "revali.toml")

    def user_file(self):
        return os.path.join(self.home, "config.toml")

    def gitignore(self):
        return os.path.join(self.repo, ".gitignore")

    def original(self):
        with open(self.project_file(), "rb") as fh:
            return fh.read()

    def put(self, path, data):
        with open(path, "wb") as fh:
            fh.write(data)

    def gitignore_bytes(self):
        if not os.path.isfile(self.gitignore()):
            return None
        with open(self.gitignore(), "rb") as fh:
            return fh.read()

    def gitignore_lines(self):
        return self.gitignore_bytes().decode("utf-8").splitlines()

    def error_lines(self, out):
        return [ln for ln in out.splitlines() if ln.startswith("ERROR")]

    def assert_one_not_toml_line(self, out, path):
        """The ERROR line of `out`, which must be the only one, names `path` and says it is
        not valid TOML; returns the problem text after "ERROR: "."""
        self.assertNotIn("Traceback", out)
        errors = self.error_lines(out)
        self.assertEqual(len(errors), 1, out)
        match = NOT_TOML_LINE.match(errors[0])
        self.assertIsNotNone(match, "not the line preflight prints: %r" % errors[0])
        self.assertTrue(os.path.samefile(match.group(1), path), errors[0])
        return errors[0][len("ERROR: ") :]


class ProjectFileNotToml(Case):
    """AC-1: the project's revali.toml."""

    def variants(self):
        original = self.original()
        return (
            ("a bad line after [paths] names a state directory", original + PATHS_BOX + b"= x\n"),
            ("a bad first line", b"= not toml\n" + original + PATHS_BOX),
            ("a string that is not closed", original + b'\n[paths]\nstate_dir = ".box\n'),
            ("a table declared twice", original + PATHS_BOX + b"\n[project]\n"),
        )

    def test_init_stops_with_the_line_preflight_gives(self):
        for name, data in self.variants():
            with self.subTest(name):
                self.put(self.project_file(), data)
                self.put(self.gitignore(), GITIGNORE)
                code, out = cli(["init"])
                self.assertEqual(code, 1, out)
                problem = self.assert_one_not_toml_line(out, self.project_file())
                # neither the default state directory nor the one the file names went in
                self.assertEqual(self.gitignore_bytes(), GITIGNORE)
                self.assertNotIn(".gitignore", reported(out))
                # the words preflight uses for the same file
                code, preflight_out = cli(["preflight"])
                self.assertEqual(code, 1, preflight_out)
                self.assertIn(problem, preflight_out)

    def test_no_gitignore_is_written_where_there_was_none(self):
        os.remove(self.gitignore())
        self.put(self.project_file(), self.original() + b"= not toml\n")
        code, out = cli(["init"])
        self.assertEqual(code, 1, out)
        self.assert_one_not_toml_line(out, self.project_file())
        self.assertIsNone(self.gitignore_bytes())

    def test_what_init_wrote_before_it_stopped_stays_and_is_reported(self):
        # the fixture has revali.toml and CONVENTIONS.md, and neither of the other two
        for rel in (".github/release.yml", "CLAUDE.md"):
            self.assertFalse(self.exists(rel), rel)
        self.put(self.project_file(), self.original() + b"= not toml\n")
        code, out = cli(["init"])
        self.assertEqual(code, 1, out)
        self.assert_one_not_toml_line(out, self.project_file())
        done = reported(out)
        self.assertEqual(done.get("revali.toml"), "kept", out)
        self.assertEqual(done.get("CONVENTIONS.md"), "kept", out)
        self.assertEqual(done.get(".github/release.yml"), "wrote", out)
        self.assertEqual(done.get("CLAUDE.md"), "wrote", out)
        self.assertTrue(self.exists(".github/release.yml"))
        self.assertTrue(self.exists("CLAUDE.md"))
        # reported before the stop: the ERROR line is the last of them
        lines = [ln for ln in out.splitlines() if ln.strip()]
        self.assertTrue(lines[-1].startswith("ERROR"), out)


class UserFileNotToml(Case):
    """AC-1: the user's config.toml under REVALI_HOME; the project's revali.toml is fine."""

    def assert_names_the_users_file_only(self, out):
        problem = self.assert_one_not_toml_line(out, self.user_file())
        self.assertNotIn("revali.toml", problem)
        return problem

    def test_init_stops_with_the_line_preflight_gives(self):
        variants = (
            ("a bad line after [paths] names a state directory", PATHS_UBOX + b"= not toml\n"),
            ("nothing but a bad line", b"= not toml\n"),
            ("a key set twice", b'checklist = "a.md"\nchecklist = "b.md"\n'),
        )
        for name, data in variants:
            with self.subTest(name):
                self.put(self.user_file(), data)
                self.put(self.gitignore(), GITIGNORE)
                code, out = cli(["init"])
                self.assertEqual(code, 1, out)
                problem = self.assert_names_the_users_file_only(out)
                self.assertEqual(self.gitignore_bytes(), GITIGNORE)
                code, preflight_out = cli(["preflight"])
                self.assertEqual(code, 1, preflight_out)
                self.assertIn(problem, preflight_out)

    def test_a_project_without_revali_toml_keeps_the_one_init_wrote(self):
        os.remove(self.project_file())
        self.put(self.user_file(), b"= not toml\n")
        code, out = cli(["init"])
        self.assertEqual(code, 1, out)
        self.assert_names_the_users_file_only(out)
        self.assertEqual(reported(out).get("revali.toml"), "wrote", out)
        self.assertTrue(os.path.isfile(self.project_file()))
        self.assertEqual(self.gitignore_bytes(), GITIGNORE)

    def test_both_files_damaged_is_still_one_error_line(self):
        self.put(self.user_file(), b"= not toml\n")
        self.put(self.project_file(), self.original() + b"= not toml\n")
        code, out = cli(["init"])
        self.assertEqual(code, 1, out)
        self.assertNotIn("Traceback", out)
        errors = self.error_lines(out)
        self.assertEqual(len(errors), 1, out)
        match = NOT_TOML_LINE.match(errors[0])
        self.assertIsNotNone(match, errors[0])
        named = match.group(1)
        self.assertTrue(
            os.path.samefile(named, self.user_file())
            or os.path.samefile(named, self.project_file()),
            errors[0],
        )
        self.assertEqual(self.gitignore_bytes(), GITIGNORE)


class UnknownKeyLetsInitThrough(Case):
    """AC-2: a file that parses, with a key revali does not know."""

    def assert_init_ignores(self, entry):
        code, out = cli(["init"])
        self.assertEqual(code, 0, out)
        self.assertNotIn("Traceback", out)
        self.assertEqual(self.error_lines(out), [], out)
        self.assertIn(entry, self.gitignore_lines())
        self.assertNotIn(".revali/", self.gitignore_lines())
        self.assertEqual(reported(out).get(".gitignore"), "appended", out)

    def assert_preflight_reports(self, key):
        code, out = cli(["preflight"])
        self.assertEqual(code, 1, out)
        self.assertNotIn("not valid TOML", out)
        self.assertIn(key, out)

    def test_an_unknown_key_in_the_projects_file(self):
        self.put(self.project_file(), self.original() + PATHS_BOX + b"no_such_key = 1\n")
        self.assert_init_ignores(".box/")
        self.assert_preflight_reports("no_such_key")

    def test_an_unknown_table_in_the_projects_file(self):
        self.put(self.project_file(), self.original() + PATHS_BOX + b"\n[no_such_table]\nx = 1\n")
        self.assert_init_ignores(".box/")
        self.assert_preflight_reports("no_such_table")

    def test_an_unknown_key_in_the_users_file(self):
        self.put(self.user_file(), b"no_such_key = 1\n" + PATHS_UBOX)
        self.assert_init_ignores(".ubox/")
        self.assert_preflight_reports("no_such_key")


class OtherFileStillApplies(Case):
    """What docs/configuration.md says of `status` and a damaged file (AC-3): that file's
    [paths] is ignored, the other file's still applies."""

    def needs_action_state_in(self, state_dir):
        rdir = os.path.join(self.repo, state_dir, "feature__mul")
        State(stage="needs_action", branch="feature/mul", message="fix F1 first", last_exit=2).save(
            rdir
        )

    def assert_status_reads_the_state(self):
        code, out = cli(["status"])
        self.assertEqual(code, 0, out)
        self.assertNotIn("Traceback", out)
        self.assertIn("stage: needs_action", out.splitlines())

    def test_the_users_state_dir_with_a_damaged_project_file(self):
        self.put(self.user_file(), PATHS_UBOX)
        self.put(self.project_file(), self.original() + PATHS_BOX + b"= not toml\n")
        self.needs_action_state_in(".ubox")
        self.assert_status_reads_the_state()

    def test_the_projects_state_dir_with_a_damaged_user_file(self):
        self.put(self.user_file(), PATHS_UBOX + b"= not toml\n")
        self.put(self.project_file(), self.original() + PATHS_BOX)
        self.needs_action_state_in(".box")
        self.assert_status_reads_the_state()
