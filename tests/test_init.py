"""`revali init` writes a project's setup files from the templates (feature/init, M3.27)."""

import json
import os
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


def template(name):
    with open(os.path.join(TEMPLATES, name), "r", encoding="utf-8", newline="") as fh:
        return fh.read().replace("\r\n", "\n")


def snippet_block():
    text = template("CLAUDE-snippet.md")
    return text[text.index("-->") + 3 :].lstrip("\n")


class InitCase(unittest.TestCase):
    """A git repository with one commit and no revali files; fake gh / claude / runner and a
    private REVALI_HOME, as in RepoCase."""

    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="revali init "))
        self.repo = os.path.join(self.tmp, "proj")
        os.makedirs(self.repo)
        git(["init", "-q", "-b", "main"], self.repo)
        for key, value in (
            ("user.email", "fixture@example.invalid"),
            ("user.name", "Fixture"),
            ("commit.gpgsign", "false"),
            ("core.autocrlf", "false"),
            # the machine's global excludes (~/.config/git/ignore may hold .revali/) must not
            # decide what these tests see
            ("core.excludesFile", os.path.join(self.tmp, "no-excludes")),
        ):
            git(["config", key, value], self.repo)
        self.write("README.md", "# proj\n")
        git(["add", "-A"], self.repo)
        git(["commit", "-q", "-m", "Initial"], self.repo)
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(self.home)
        self.fake_log = os.path.join(self.tmp, "fake.log")
        self.scenario_path = os.path.join(self.tmp, "scenario.json")
        with open(self.scenario_path, "w", encoding="utf-8") as fh:
            json.dump({"name": "proj"}, fh)
        self.runner_path = os.path.join(self.tmp, "runner.json")
        with open(self.runner_path, "w", encoding="utf-8") as fh:
            json.dump({"default": 0}, fh)
        env = {
            "REVALI_HOME": self.home,
            "REVALI_GH_CMD": "%s %s" % (_quote(sys.executable), _quote(GH_STUB)),
            "REVALI_CLAUDE_CMD": "%s %s" % (_quote(sys.executable), _quote(CLAUDE_STUB)),
            "REVALI_FAKE_SCENARIO": self.scenario_path,
            "REVALI_FAKE_LOG": self.fake_log,
            "REVALI_FAKE_RUNNER": self.runner_path,
        }
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        cwd = os.getcwd()
        os.chdir(self.repo)
        self.addCleanup(os.chdir, cwd)
        self.addCleanup(rmtree_force, self.tmp)  # runs last: cleanups are LIFO

    def write(self, rel, text):
        path = os.path.join(self.repo, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)

    def read(self, rel):
        with open(os.path.join(self.repo, rel), "r", encoding="utf-8", newline="") as fh:
            return fh.read()

    def exists(self, rel):
        return os.path.exists(os.path.join(self.repo, rel))

    def init(self):
        return run_cli(["init"])


class WritesWhatIsMissing(InitCase):
    """AC-1, AC-3, AC-4."""

    def test_an_empty_repository_gets_every_file(self):
        code, out = self.init()
        self.assertEqual(code, EXIT_OK, out)
        self.assertEqual(self.read("revali.toml"), template("revali.toml"))
        self.assertEqual(self.read("CONVENTIONS.md"), template("CONVENTIONS.md"))
        self.assertEqual(self.read(".github/release.yml"), template("release.yml"))
        self.assertEqual(self.read("CLAUDE.md"), snippet_block())
        self.assertTrue(self.read("CLAUDE.md").startswith(HEADING))
        self.assertIn(".revali/", self.read(".gitignore").splitlines())
        lines = out.splitlines()
        for line in (
            "wrote revali.toml",
            "wrote CONVENTIONS.md",
            "wrote .github/release.yml",
            "wrote CLAUDE.md",
            "wrote .gitignore",
        ):
            self.assertIn(line, lines)

    def test_files_are_lf(self):
        self.init()
        for rel in ("revali.toml", "CONVENTIONS.md", ".github/release.yml", "CLAUDE.md"):
            with open(os.path.join(self.repo, rel), "rb") as fh:
                self.assertNotIn(b"\r", fh.read(), rel)

    def test_next_steps(self):
        _, out = self.init()
        self.assertIn("[validate.<platform>]", out)
        self.assertIn("<revali dir>", out)
        self.assertIn("commit", out)

    def test_an_existing_gitignore_gets_the_line_appended(self):
        self.write(".gitignore", "build/\n")
        _, out = self.init()
        self.assertEqual(self.read(".gitignore"), "build/\n.revali/\n")
        self.assertIn("appended .gitignore", out.splitlines())

    def test_ignored_through_info_exclude_leaves_gitignore_alone(self):
        with open(os.path.join(self.repo, ".git", "info", "exclude"), "a", encoding="utf-8") as fh:
            fh.write(".revali/\n")
        code, out = self.init()
        self.assertEqual(code, EXIT_OK, out)
        self.assertFalse(self.exists(".gitignore"))
        # round 1 F4: no line about a .gitignore that does not exist
        self.assertIn("kept .revali/ (already ignored by git)", out.splitlines())
        self.assertNotIn(".gitignore", out)

    def test_a_gitignore_that_has_the_rule_is_kept(self):
        self.write(".gitignore", ".revali/\n")
        _, out = self.init()
        self.assertIn("kept .gitignore", out.splitlines())

    def test_from_a_subdirectory_writes_at_the_root(self):
        os.makedirs(os.path.join(self.repo, "src"))
        os.chdir(os.path.join(self.repo, "src"))
        code, out = self.init()
        self.assertEqual(code, EXIT_OK, out)
        self.assertTrue(self.exists("revali.toml"))
        self.assertFalse(os.path.exists(os.path.join(self.repo, "src", "revali.toml")))


class NeverOverwrites(InitCase):
    """AC-2, AC-4."""

    def test_existing_files_are_kept(self):
        self.write("revali.toml", "[project]\nmine = 1\n")
        self.write("CONVENTIONS.md", "# mine\n")
        self.write(".github/release.yml", "changelog: {}\n")
        code, out = self.init()
        self.assertEqual(code, EXIT_OK, out)
        self.assertEqual(self.read("revali.toml"), "[project]\nmine = 1\n")
        self.assertEqual(self.read("CONVENTIONS.md"), "# mine\n")
        self.assertEqual(self.read(".github/release.yml"), "changelog: {}\n")
        for line in ("kept revali.toml", "kept CONVENTIONS.md", "kept .github/release.yml"):
            self.assertIn(line, out.splitlines())

    def test_claude_md_gets_the_block_appended(self):
        self.write("CLAUDE.md", "# proj\n\nOur rules.")  # no final newline
        code, out = self.init()
        self.assertEqual(code, EXIT_OK, out)
        self.assertEqual(self.read("CLAUDE.md"), "# proj\n\nOur rules.\n\n" + snippet_block())
        self.assertIn("appended CLAUDE.md", out.splitlines())

    def test_a_crlf_claude_md_gets_a_crlf_block(self):
        # round 1 F3: no mixed line endings
        self.write("CLAUDE.md", "")
        with open(os.path.join(self.repo, "CLAUDE.md"), "wb") as fh:
            fh.write(b"# proj\r\n\r\nOur rules.\r\n")
        self.init()
        with open(os.path.join(self.repo, "CLAUDE.md"), "rb") as fh:
            data = fh.read()
        expected = "# proj\r\n\r\nOur rules.\r\n\r\n" + snippet_block().replace("\n", "\r\n")
        self.assertEqual(data, expected.encode("utf-8"))
        self.assertNotIn(b"\n", data.replace(b"\r\n", b""))

    def test_the_heading_comes_from_the_template(self):
        # round 1 follow-up: a reworded template heading is the one init looks for
        from revali.project_init import snippet_heading

        self.assertEqual(snippet_heading(snippet_block()), HEADING)
        other = os.path.join(self.tmp, "templates")
        os.makedirs(other)
        for name in os.listdir(TEMPLATES):
            text = template(name)
            if name == "CLAUDE-snippet.md":
                text = text.replace(HEADING, "## Use revali for every change")
            with open(os.path.join(other, name), "w", encoding="utf-8", newline="\n") as fh:
                fh.write(text)
        mine = "# proj\n\n## Use revali for every change\n\nOur wording.\n"
        self.write("CLAUDE.md", mine)
        with mock.patch("revali.project_init.TEMPLATES_DIR", other):
            _, out = self.init()
        self.assertEqual(self.read("CLAUDE.md"), mine)
        self.assertIn("kept CLAUDE.md", out.splitlines())

    def test_claude_md_with_the_heading_is_kept(self):
        text = "# proj\n\n%s\n\nOur own wording.\n" % HEADING
        self.write("CLAUDE.md", text)
        _, out = self.init()
        self.assertEqual(self.read("CLAUDE.md"), text)
        self.assertIn("kept CLAUDE.md", out.splitlines())

    def test_a_claude_md_with_a_bom_and_the_heading_first_is_kept(self):
        # round 2 follow-up: the BOM is not part of the heading line
        data = ("﻿%s\n\nOur own wording.\n" % HEADING).encode("utf-8")
        with open(os.path.join(self.repo, "CLAUDE.md"), "wb") as fh:
            fh.write(data)
        _, out = self.init()
        with open(os.path.join(self.repo, "CLAUDE.md"), "rb") as fh:
            self.assertEqual(fh.read(), data)
        self.assertIn("kept CLAUDE.md", out.splitlines())

    def test_a_second_run_keeps_everything(self):
        self.init()
        before = {
            rel: self.read(rel)
            for rel in ("revali.toml", "CONVENTIONS.md", ".github/release.yml", "CLAUDE.md")
        }
        before[".gitignore"] = self.read(".gitignore")
        code, out = self.init()
        self.assertEqual(code, EXIT_OK, out)
        for rel, text in before.items():
            self.assertEqual(self.read(rel), text, rel)
        self.assertEqual(
            [ln.split()[0] for ln in out.splitlines() if ln.startswith(("wrote", "kept", "app"))],
            ["kept"] * 5,
        )


class NoMachinePath(InitCase):
    """AC-5."""

    def test_revali_dir_stays_a_placeholder(self):
        self.init()
        self.assertIn("<revali dir>", self.read("CLAUDE.md"))
        for rel in ("revali.toml", "CONVENTIONS.md", ".github/release.yml", "CLAUDE.md"):
            text = self.read(rel)
            self.assertNotIn(ROOT, text, rel)
            self.assertNotIn(ROOT.replace("\\", "/"), text, rel)


class Failures(InitCase):
    """AC-6."""

    def test_outside_a_git_working_tree(self):
        plain = os.path.join(self.tmp, "plain")
        os.makedirs(plain)
        os.chdir(plain)
        with mock.patch.dict(os.environ, {"GIT_CEILING_DIRECTORIES": self.tmp}):
            code, out = self.init()
        self.assertEqual(code, EXIT_ERROR)
        self.assertEqual(len([ln for ln in out.splitlines() if ln.startswith("ERROR")]), 1, out)
        self.assertEqual(os.listdir(plain), [])

    def test_a_missing_template_writes_nothing(self):
        missing = os.path.join(self.tmp, "no templates")
        with mock.patch("revali.project_init.TEMPLATES_DIR", missing):
            code, out = self.init()
        self.assertEqual(code, EXIT_ERROR)
        errors = [ln for ln in out.splitlines() if ln.startswith("ERROR")]
        self.assertEqual(len(errors), 1, out)
        self.assertIn(missing, errors[0])
        for rel in ("revali.toml", "CONVENTIONS.md", ".github", "CLAUDE.md", ".gitignore"):
            self.assertFalse(self.exists(rel), rel)

    def test_a_gitignore_that_is_not_utf8(self):
        # round 1 F1: a Latin-1 .gitignore without the rule, so revali reads it to append the
        # line, and that UTF-8 read fails. (A UTF-16 one never gets here: git 2.55 reads its
        # NUL-only second line as a pattern that ignores every path, `.revali/` included.)
        with open(os.path.join(self.repo, ".gitignore"), "wb") as fh:
            fh.write("# café\nbuild/\n".encode("latin-1"))
        code, out = self.init()
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertNotIn("Traceback", out)
        errors = [ln for ln in out.splitlines() if ln.startswith("ERROR")]
        self.assertEqual(len(errors), 1, out)
        self.assertIn(".gitignore", errors[0])
        self.assertEqual(self.read("revali.toml"), template("revali.toml"))  # written before
        self.assertIn("wrote CLAUDE.md", out.splitlines())

    def test_a_revali_toml_that_is_not_utf8(self):
        # round 1 F1: init keeps it, then reads it for [paths] state_dir
        with open(os.path.join(self.repo, "revali.toml"), "wb") as fh:
            fh.write(b"[project]\n# caf\xe9\n")
        code, out = self.init()
        self.assertEqual(code, EXIT_ERROR, out)
        errors = [ln for ln in out.splitlines() if ln.startswith("ERROR")]
        self.assertEqual(len(errors), 1, out)
        self.assertIn("revali.toml", errors[0])
        self.assertIn("kept revali.toml", out.splitlines())
        self.assertTrue(self.exists("CONVENTIONS.md"))

    def test_a_user_config_that_is_not_utf8(self):
        # round 2 F6: the ERROR names ~/.revali/config.toml, not the revali.toml init wrote
        user_config = os.path.join(self.home, "config.toml")
        with open(user_config, "wb") as fh:
            fh.write(b"# caf\xe9\n")
        code, out = self.init()
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertNotIn("Traceback", out)
        errors = [ln for ln in out.splitlines() if ln.startswith("ERROR")]
        self.assertEqual(len(errors), 1, out)
        self.assertIn(user_config, errors[0])
        self.assertNotIn(os.path.join(self.repo, "revali.toml"), errors[0])
        self.assertEqual(self.read("revali.toml"), template("revali.toml"))  # written before

    def test_a_file_that_cannot_be_written(self):
        self.write(".github", "a file where the directory would go\n")
        code, out = self.init()
        self.assertEqual(code, EXIT_ERROR)
        errors = [ln for ln in out.splitlines() if ln.startswith("ERROR")]
        self.assertEqual(len(errors), 1, out)
        self.assertIn("release.yml", errors[0])
        self.assertEqual(self.read("revali.toml"), template("revali.toml"))  # written before
        self.assertEqual(self.read(".github"), "a file where the directory would go\n")


class NoGitSideEffects(InitCase):
    """AC-7."""

    def test_no_commit_and_no_github_call(self):
        head = git(["rev-parse", "HEAD"], self.repo)
        self.init()
        self.assertEqual(git(["rev-parse", "HEAD"], self.repo), head)
        status = git(["status", "--porcelain", "--untracked-files=all"], self.repo)
        self.assertIn("?? revali.toml", status)
        self.assertFalse(os.path.exists(self.fake_log), "gh or claude was called")


class PreflightAfterInit(InitCase):
    """AC-8."""

    def test_preflight_passes(self):
        remote = os.path.join(self.tmp, "proj.git")
        subprocess.run(["git", "init", "-q", "--bare", remote], check=True, capture_output=True)
        git(["remote", "add", "origin", remote], self.repo)
        git(["push", "-q", "-u", "origin", "main"], self.repo)
        self.assertEqual(self.init()[0], EXIT_OK)
        # round 1 F5: revali.toml exactly as init wrote it (preflight probes no runner)
        self.assertEqual(self.read("revali.toml"), template("revali.toml"))
        git(["add", "-A"], self.repo)
        git(["commit", "-q", "-m", "Set up revali"], self.repo)
        git(["push", "-q", "origin", "main"], self.repo)
        git(["checkout", "-q", "-b", "feature/mul"], self.repo)
        self.write("src/calc.py", "def mul(a, b):\n    return a * b\n")
        git(["add", "-A"], self.repo)
        git(["commit", "-q", "-m", "Add mul"], self.repo)
        self.write(".revali/feature__mul/change.md", CHANGE_MD)
        code, out = run_cli(["preflight"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("preflight OK", out)


class Docs(unittest.TestCase):
    """AC-9."""

    def read(self, *parts):
        with open(os.path.join(ROOT, *parts), "r", encoding="utf-8") as fh:
            return fh.read()

    def test_readme_usage(self):
        self.assertIn("revali.py init", self.read("README.md"))

    def test_workflow_project_setup_starts_with_init(self):
        text = self.read("docs", "workflow.md")
        section = text[text.index("## Project setup") :]
        first = section.split("\n\n")[1]
        self.assertIn("revali init", first.replace("revali.py init", "revali init"))

    def test_side_effects_lists_the_files(self):
        text = self.read("docs", "side-effects.md")
        item = text[text.index("- on `revali init`") :].split("\n- ")[0]
        for name in ("revali.toml", "CONVENTIONS.md", ".github/release.yml", "CLAUDE.md"):
            self.assertIn(name, item)

    def test_help_lists_init(self):
        _, out = run_cli(["--help"])
        self.assertIn("init", out)


if __name__ == "__main__":
    unittest.main()
