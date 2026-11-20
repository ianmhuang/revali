"""Acceptance tests for `revali init` (feature/init), written from the acceptance criteria.

AC-1 the missing setup files are written at the repository root from the templates, LF, UTF-8
AC-2 an existing file is never overwritten; CLAUDE.md gets the block appended unless it has
     the heading line
AC-3 the state directory is made ignored unless git already ignores it
AC-4 one `wrote` / `appended` / `kept` line per file, then the next steps; exit 0
AC-5 `<revali dir>` stays a placeholder, no path of the revali checkout is written
AC-6 outside a git working tree, an unreadable template, an unwritable file: one ERROR, exit 1
AC-7 no commit, no GitHub call, git only asked for the root and the ignore check
AC-8 `revali preflight` passes on a repository set up by init

Everything goes through the command line (`run_cli(["init"])`). On the base branch `init` is
not a subcommand, so every test fails there on the exit code.
"""

import builtins
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from revali import EXIT_ERROR, EXIT_OK
from tests.fixtures.make_sample_repo import CHANGE_MD
from tests.helpers import CLAUDE_STUB, GH_STUB, ROOT, _quote, git, rmtree_force, run_cli

TEMPLATES = os.path.join(ROOT, "templates")
HEADING = "## Changes go through revali"
# path in the project -> template it is a copy of
COPIED = {
    "revali.toml": "revali.toml",
    "CONVENTIONS.md": "CONVENTIONS.md",
    ".github/release.yml": "release.yml",
}
SETUP_FILES = tuple(COPIED) + ("CLAUDE.md",)
ACTIONS = ("wrote", "appended", "kept")

# A stand-in for `git` (through REVALI_GIT_CMD) that records every call and hands it to the
# real git, so the test sees which git commands init runs.
GIT_SPY = """import json, subprocess, sys
args = sys.argv[1:]
with open(%(log)r, "a", encoding="utf-8") as fh:
    fh.write(json.dumps(args) + "\\n")
sys.exit(subprocess.call([%(git)r] + args))
"""


def template(name):
    """A template's text with LF line endings, whatever the checkout's are."""
    with open(os.path.join(TEMPLATES, name), "rb") as fh:
        return fh.read().replace(b"\r\n", b"\n").decode("utf-8")


def snippet_block():
    """What AC-1 puts into CLAUDE.md: the text below the snippet's first HTML comment."""
    text = template("CLAUDE-snippet.md")
    return text[text.index("-->") + 3 :].strip("\n") + "\n"


def file_lines(out):
    """The per-file output lines as (action, path) pairs, in output order. The path is the
    word after the action; a line may say more after it."""
    pairs = []
    for line in out.splitlines():
        words = line.split()
        if len(words) >= 2 and words[0].rstrip(":") in ACTIONS:
            pairs.append((words[0].rstrip(":"), words[1].replace("\\", "/")))
    return pairs


def reported(out):
    return {path: action for action, path in file_lines(out)}


def error_lines(out):
    return [line for line in out.splitlines() if line.startswith("ERROR")]


def git_subcommand(argv):
    """The subcommand of one git call: the first word that is not an option (or its value)."""
    skip = False
    for tok in argv:
        if skip:
            skip = False
        elif tok in ("-C", "-c"):
            skip = True
        elif not tok.startswith("-"):
            return tok
    return ""


class InitRepo(unittest.TestCase):
    """A git repository with one commit and none of revali's files, a private REVALI_HOME,
    a fake gh and claude that log every call, and no ignore rule from the machine."""

    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="revali initsetup "))
        self.addCleanup(rmtree_force, self.tmp)  # runs last: cleanups are LIFO
        self.repo = os.path.join(self.tmp, "proj")
        os.makedirs(self.repo)
        git(["init", "-q", "-b", "main"], self.repo)
        for key, value in (
            ("user.email", "fixture@example.invalid"),
            ("user.name", "Fixture"),
            ("commit.gpgsign", "false"),
            ("core.autocrlf", "false"),
            # the machine's global excludes file must not decide what these tests see
            ("core.excludesFile", os.path.join(self.tmp, "no-excludes")),
        ):
            git(["config", key, value], self.repo)
        self.exclude = os.path.join(self.repo, ".git", "info", "exclude")
        os.makedirs(os.path.dirname(self.exclude), exist_ok=True)
        with open(self.exclude, "wb"):
            pass
        self.write("README.md", "# proj\n")
        git(["add", "-A"], self.repo)
        git(["commit", "-q", "-m", "Initial"], self.repo)

        self.home = os.path.join(self.tmp, "home")
        os.makedirs(self.home)
        self.fake_log = os.path.join(self.tmp, "fake.log")
        scenario = os.path.join(self.tmp, "scenario.json")
        with open(scenario, "w", encoding="utf-8") as fh:
            json.dump({}, fh)
        runner = os.path.join(self.tmp, "runner.json")
        with open(runner, "w", encoding="utf-8") as fh:
            json.dump({"default": 0}, fh)
        patcher = mock.patch.dict(
            os.environ,
            {
                "REVALI_HOME": self.home,
                "REVALI_GH_CMD": "%s %s" % (_quote(sys.executable), _quote(GH_STUB)),
                "REVALI_CLAUDE_CMD": "%s %s" % (_quote(sys.executable), _quote(CLAUDE_STUB)),
                "REVALI_FAKE_SCENARIO": scenario,
                "REVALI_FAKE_LOG": self.fake_log,
                "REVALI_FAKE_RUNNER": runner,
            },
        )
        patcher.start()
        self.addCleanup(patcher.stop)  # restores the whole environment
        for name in ("REVALI_DISABLE", "REVALI_GIT_CMD"):
            os.environ.pop(name, None)
        cwd = os.getcwd()
        os.chdir(self.repo)
        self.addCleanup(os.chdir, cwd)

    def path(self, rel):
        return os.path.join(self.repo, *rel.split("/"))

    def write(self, rel, text):
        os.makedirs(os.path.dirname(self.path(rel)), exist_ok=True)
        with open(self.path(rel), "wb") as fh:
            fh.write(text.encode("utf-8"))

    def raw(self, rel):
        with open(self.path(rel), "rb") as fh:
            return fh.read()

    def read(self, rel):
        return self.raw(rel).decode("utf-8")

    def exists(self, rel):
        return os.path.lexists(self.path(rel))

    def init(self):
        return run_cli(["init"])

    def ignored(self):
        """Whether git ignores the state directory, by any rule."""
        res = subprocess.run(
            ["git", "check-ignore", "-q", "--no-index", "--", ".revali/"],
            cwd=self.repo,
            capture_output=True,
        )
        return res.returncode == 0


class FreshRepository(InitRepo):
    """AC-1, AC-3, AC-4, AC-5: a repository that has none of the files."""

    def test_every_missing_file_is_written_from_its_template(self):
        code, out = self.init()
        self.assertEqual(code, EXIT_OK, out)
        for rel, name in COPIED.items():
            self.assertEqual(self.read(rel), template(name), rel)
        claude = self.read("CLAUDE.md")
        self.assertEqual(claude.strip("\n"), snippet_block().strip("\n"))
        # the paste instruction above the block is not part of it
        self.assertNotIn("Paste the block below", claude)
        self.assertIn(HEADING, claude.splitlines())

    def test_written_files_are_lf_and_utf8(self):
        code, out = self.init()
        self.assertEqual(code, EXIT_OK, out)
        for rel in SETUP_FILES:
            data = self.raw(rel)
            self.assertNotIn(b"\r", data, rel)
            data.decode("utf-8")  # raises when it is not UTF-8

    def test_from_a_subdirectory_the_files_land_at_the_repository_root(self):
        sub = os.path.join(self.repo, "src", "pkg")
        os.makedirs(sub)
        os.chdir(sub)
        code, out = self.init()
        self.assertEqual(code, EXIT_OK, out)
        for rel in SETUP_FILES:
            self.assertTrue(self.exists(rel), rel)
        self.assertEqual(os.listdir(sub), [])
        self.assertEqual(os.listdir(os.path.dirname(sub)), ["pkg"])

    def test_the_state_directory_becomes_ignored(self):
        self.assertFalse(self.ignored(), "the fixture must start without an ignore rule")
        code, out = self.init()
        self.assertEqual(code, EXIT_OK, out)
        self.assertTrue(self.ignored())
        self.assertIn(".revali/", self.read(".gitignore").splitlines())

    def test_one_line_per_file_then_the_next_steps(self):
        code, out = self.init()
        self.assertEqual(code, EXIT_OK, out)
        pairs = file_lines(out)
        for rel in SETUP_FILES:
            self.assertEqual(
                [a for a, p in pairs if p == rel], ["wrote"], "%s in:\n%s" % (rel, out)
            )
        lines = out.splitlines()
        last_file_line = max(
            i
            for i, line in enumerate(lines)
            if line.split() and line.split()[0].rstrip(":") in ACTIONS
        )
        steps = "\n".join(lines[last_file_line + 1 :])
        # what the user still has to do: the commands, the placeholder, the commit
        self.assertIn("[validate.", steps)
        self.assertIn("revali.toml", steps)
        self.assertIn("<revali dir>", steps)
        self.assertIn("CLAUDE.md", steps)
        self.assertIn("commit", steps.lower())

    def test_the_revali_checkout_is_not_named_in_any_file(self):
        code, out = self.init()
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("<revali dir>", self.read("CLAUDE.md"))
        for rel in SETUP_FILES + (".gitignore",):
            text = self.read(rel)
            for spelling in (ROOT, ROOT.replace("\\", "/"), os.path.realpath(ROOT)):
                self.assertNotIn(spelling, text, rel)


class ExistingFiles(InitRepo):
    """AC-2, AC-3, AC-4, AC-5: what the project already has stays as it is."""

    def test_existing_copies_are_kept_byte_for_byte(self):
        mine = {
            "revali.toml": "[project]\r\nmine = 1\r\n",  # CRLF: a rewrite would show
            "CONVENTIONS.md": "# ours\n",
            ".github/release.yml": "changelog: {}\n",
        }
        for rel, text in mine.items():
            self.write(rel, text)
        code, out = self.init()
        self.assertEqual(code, EXIT_OK, out)
        seen = reported(out)
        for rel, text in mine.items():
            self.assertEqual(self.raw(rel), text.encode("utf-8"), rel)
            self.assertEqual(seen.get(rel), "kept", out)
        # the one file that was missing is still written
        self.assertEqual(seen.get("CLAUDE.md"), "wrote", out)
        self.assertEqual(self.read("CLAUDE.md").strip("\n"), snippet_block().strip("\n"))

    def test_claude_md_gets_the_block_appended_after_one_blank_line(self):
        block = snippet_block()
        for original, separator in (
            ("# proj\n\nOur rules.\n", "\n"),
            ("# proj\n\nOur rules.", "\n\n"),  # no final newline
        ):
            with self.subTest(original=original):
                self.write("CLAUDE.md", original)
                code, out = self.init()
                self.assertEqual(code, EXIT_OK, out)
                result = self.read("CLAUDE.md")
                self.assertTrue(result.startswith(original), result)
                # one blank line, then the block from its heading on
                self.assertTrue(result[len(original) :].startswith(separator + HEADING), result)
                self.assertEqual(result.rstrip("\n"), (original + separator + block).rstrip("\n"))
                self.assertEqual(reported(out).get("CLAUDE.md"), "appended", out)
                # AC-5 holds for the appended block too
                self.assertIn("<revali dir>", result)
                self.assertNotIn(ROOT, result)
                self.assertNotIn(ROOT.replace("\\", "/"), result)

    def test_a_crlf_claude_md_keeps_one_line_ending(self):
        # round 1 F3: the block is appended in the file's own line ending
        original = b"# proj\r\n\r\nOur rules.\r\n"
        with open(self.path("CLAUDE.md"), "wb") as fh:
            fh.write(original)
        code, out = self.init()
        self.assertEqual(code, EXIT_OK, out)
        data = self.raw("CLAUDE.md")
        self.assertTrue(data.startswith(original), data)
        self.assertNotIn(b"\n", data.replace(b"\r\n", b""), "a bare LF in a CRLF file")
        self.assertNotIn(b"\r", data.replace(b"\r\n", b""), "a bare CR")
        as_lf = data.replace(b"\r\n", b"\n").decode("utf-8")
        expected = "# proj\n\nOur rules.\n\n" + snippet_block()
        self.assertEqual(as_lf.rstrip("\n"), expected.rstrip("\n"))
        self.assertEqual(reported(out).get("CLAUDE.md"), "appended", out)

    def test_claude_md_that_has_the_heading_line_is_kept(self):
        text = "# proj\n\n%s\n\nOur own wording of it.\n" % HEADING
        self.write("CLAUDE.md", text)
        code, out = self.init()
        self.assertEqual(code, EXIT_OK, out)
        self.assertEqual(self.raw("CLAUDE.md"), text.encode("utf-8"))
        self.assertEqual(reported(out).get("CLAUDE.md"), "kept", out)

    def test_a_second_run_keeps_every_file_and_exits_0(self):
        code, out = self.init()
        self.assertEqual(code, EXIT_OK, out)
        before = {rel: self.raw(rel) for rel in SETUP_FILES + (".gitignore",)}
        code, out = self.init()
        self.assertEqual(code, EXIT_OK, out)
        for rel, data in before.items():
            self.assertEqual(self.raw(rel), data, rel)
        pairs = file_lines(out)
        for rel in SETUP_FILES:
            self.assertEqual([a for a, p in pairs if p == rel], ["kept"], "%s in:\n%s" % (rel, out))
        self.assertEqual({action for action, _ in pairs}, {"kept"}, out)

    def test_a_gitignore_with_other_rules_gets_the_line_appended(self):
        self.write(".gitignore", "build/\n")
        code, out = self.init()
        self.assertEqual(code, EXIT_OK, out)
        self.assertEqual(self.read(".gitignore").splitlines(), ["build/", ".revali/"])
        self.assertTrue(self.ignored())

    def test_a_gitignore_that_ignores_it_already_is_unchanged(self):
        self.write(".gitignore", ".revali/\nbuild/\n")
        code, out = self.init()
        self.assertEqual(code, EXIT_OK, out)
        self.assertEqual(self.raw(".gitignore"), b".revali/\nbuild/\n")

    def test_ignored_through_info_exclude_no_gitignore_is_written(self):
        with open(self.exclude, "wb") as fh:
            fh.write(b".revali/\n")
        self.assertTrue(self.ignored())
        code, out = self.init()
        self.assertEqual(code, EXIT_OK, out)
        self.assertFalse(self.exists(".gitignore"))
        with open(self.exclude, "rb") as fh:
            self.assertEqual(fh.read(), b".revali/\n")
        # round 1 F4: no line about a .gitignore that does not exist; the line is about the
        # state directory, which is kept as git has it
        seen = reported(out)
        self.assertNotIn(".gitignore", seen, out)
        self.assertEqual(seen.get(".revali/"), "kept", out)
        for rel in SETUP_FILES:
            self.assertEqual(seen.get(rel), "wrote", out)


class Failures(InitRepo):
    """AC-6: one ERROR line and exit 1."""

    def test_outside_a_git_working_tree_nothing_is_written(self):
        plain = os.path.join(self.tmp, "plain")
        os.makedirs(plain)
        os.chdir(plain)
        # a repository above the temporary directory must not be found
        with mock.patch.dict(os.environ, {"GIT_CEILING_DIRECTORIES": self.tmp}):
            code, out = self.init()
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertEqual(len(error_lines(out)), 1, out)
        self.assertEqual(os.listdir(plain), [])
        self.assertEqual(file_lines(out), [], out)

    def test_a_file_that_cannot_be_written(self):
        blocker = "a file where the directory would go\n"
        self.write(".github", blocker)  # .github/release.yml cannot be created
        code, out = self.init()
        self.assertEqual(code, EXIT_ERROR, out)
        errors = error_lines(out)
        self.assertEqual(len(errors), 1, out)
        self.assertIn(".github", errors[0])  # the line names the path
        self.assertEqual(self.read(".github"), blocker)
        seen = reported(out)
        self.assertNotEqual(seen.get(".github/release.yml"), "wrote", out)
        # whatever was reported as written before the failure is there, complete
        for rel, action in seen.items():
            if action == "wrote" and rel in COPIED:
                self.assertEqual(self.read(rel), template(COPIED[rel]), rel)
            elif action == "wrote":
                self.assertTrue(self.exists(rel), rel)

    def test_a_gitignore_that_is_not_utf8_is_one_error_line(self):
        # round 1 F1: Latin-1 bytes git parses and a UTF-8 read rejects; the rule is missing,
        # so init has to read the file to append the line
        mine = "# café\nbuild/\n".encode("latin-1")
        with open(self.path(".gitignore"), "wb") as fh:
            fh.write(mine)
        code, out = self.init()  # an uncaught decode error would end the test here
        self.assertEqual(code, EXIT_ERROR, out)
        errors = error_lines(out)
        self.assertEqual(len(errors), 1, out)
        self.assertIn(".gitignore", errors[0])  # the line names the path
        self.assertNotIn("Traceback", out)
        self.assertEqual(self.raw(".gitignore"), mine)
        # files already written stay, and were reported
        seen = reported(out)
        for rel, name in COPIED.items():
            self.assertEqual(self.read(rel), template(name), rel)
            self.assertEqual(seen.get(rel), "wrote", out)
        self.assertTrue(self.exists("CLAUDE.md"))

    def test_a_kept_revali_toml_that_is_not_utf8_is_one_error_line(self):
        # round 1 F1: init keeps the file, then needs its [paths] state_dir for the ignore rule
        mine = b"[project]\n# caf\xe9\n"
        with open(self.path("revali.toml"), "wb") as fh:
            fh.write(mine)
        code, out = self.init()
        self.assertEqual(code, EXIT_ERROR, out)
        errors = error_lines(out)
        self.assertEqual(len(errors), 1, out)
        self.assertIn("revali.toml", errors[0])  # the line names the path
        self.assertNotIn("Traceback", out)
        self.assertEqual(self.raw("revali.toml"), mine)
        self.assertEqual(reported(out).get("revali.toml"), "kept", out)
        self.assertEqual(self.read("CONVENTIONS.md"), template("CONVENTIONS.md"))

    def test_a_template_that_cannot_be_read(self):
        # White-box by need: the only way to make a template unreadable without touching the
        # checkout the suite runs from is to refuse the open() of that one file.
        target = os.path.normcase(os.path.realpath(os.path.join(TEMPLATES, "CLAUDE-snippet.md")))
        real_open = builtins.open
        refused = []

        def guarded_open(file, *args, **kwargs):
            if isinstance(file, str):
                if os.path.normcase(os.path.realpath(file)) == target:
                    refused.append(file)
                    raise PermissionError(13, "Permission denied", file)
            return real_open(file, *args, **kwargs)

        with mock.patch("builtins.open", guarded_open):
            code, out = self.init()
        self.assertTrue(refused, "init did not open the template through open(); out:\n" + out)
        self.assertEqual(code, EXIT_ERROR, out)
        errors = error_lines(out)
        self.assertEqual(len(errors), 1, out)
        self.assertIn("CLAUDE-snippet.md", errors[0])  # the line names the path


class NoSideEffects(InitRepo):
    """AC-7: nothing is committed and nothing but the root and the ignore rule is asked."""

    def test_nothing_is_committed_staged_or_sent_to_github(self):
        head = git(["rev-parse", "HEAD"], self.repo)
        refs = git(["for-each-ref"], self.repo)
        code, out = self.init()
        self.assertEqual(code, EXIT_OK, out)
        self.assertEqual(git(["rev-parse", "HEAD"], self.repo), head)
        self.assertEqual(git(["for-each-ref"], self.repo), refs)
        self.assertEqual(git(["diff", "--cached", "--name-only"], self.repo).strip(), "")
        status = git(["status", "--porcelain", "--untracked-files=all"], self.repo)
        for rel in SETUP_FILES + (".gitignore",):
            self.assertIn("?? %s" % rel, status.splitlines())
        self.assertFalse(os.path.exists(self.fake_log), "gh or claude was called")

    def test_git_is_asked_only_for_the_root_and_the_ignore_rule(self):
        spy_log = os.path.join(self.tmp, "git-calls.jsonl")
        script = os.path.join(self.tmp, "git_spy.py")
        with open(script, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(GIT_SPY % {"log": spy_log, "git": shutil.which("git") or "git"})
        spy = "%s %s" % (_quote(sys.executable), _quote(script))
        with mock.patch.dict(os.environ, {"REVALI_GIT_CMD": spy}):
            code, out = self.init()
        self.assertEqual(code, EXIT_OK, out)
        with open(spy_log, "r", encoding="utf-8") as fh:
            calls = [json.loads(line) for line in fh if line.strip()]
        self.assertTrue(calls, "no git call went through REVALI_GIT_CMD")
        used = {git_subcommand(argv) for argv in calls}
        self.assertLessEqual(used, {"rev-parse", "check-ignore"}, calls)
        self.assertFalse(os.path.exists(self.fake_log), "gh or claude was called")


class PreflightAfterInit(InitRepo):
    """AC-8: what init wrote, committed as it is, is a project preflight accepts."""

    def test_preflight_passes_on_what_init_wrote(self):
        remote = os.path.join(self.tmp, "proj.git")
        subprocess.run(["git", "init", "-q", "--bare", remote], check=True, capture_output=True)
        git(["remote", "add", "origin", remote], self.repo)
        git(["push", "-q", "-u", "origin", "main"], self.repo)

        code, out = self.init()
        self.assertEqual(code, EXIT_OK, out)
        git(["add", "-A"], self.repo)
        git(["commit", "-q", "-m", "Set up revali"], self.repo)
        git(["push", "-q", "origin", "main"], self.repo)

        git(["checkout", "-q", "-b", "feature/mul"], self.repo)
        self.write("src/calc.py", "def mul(a, b):\n    return a * b\n")
        git(["add", "-A"], self.repo)
        git(["commit", "-q", "-m", "Add mul"], self.repo)
        self.write(".revali/feature__mul/change.md", CHANGE_MD)  # approved: no status line

        code, out = run_cli(["preflight"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("preflight OK", out)


if __name__ == "__main__":
    unittest.main()
