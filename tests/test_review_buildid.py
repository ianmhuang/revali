"""Reviewer acceptance tests for feature/revali-version-in-logs (AC-1 to AC-5).

AC-1: the build is `<VERSION> (<git describe --tags --always --dirty>)` of the directory revali
was loaded from, `<VERSION>` alone when that directory is not a git checkout, git is missing or
the command fails; git runs at most once per process. AC-2: preflight's first line ends with
`, revali <build>`, printed and written to revali.log. AC-3: the detached `started revali run`
line ends with `; revali <build>`. AC-4: a new history row carries `revali_build` next to
`revali_version`, older rows stay as they are, `revali stats` reads both. AC-5: the identity
line, `status`'s version line and `revali version` / `--version` are what they were.

Black-box: every test starts a new process on a copy of revali that sits in a git checkout of
its own, with a tag the test chose, and reads what the command printed and wrote. git is
watched through `REVALI_GIT_CMD`. Only "git is missing" asks `revali.build.build()` directly:
no command gets as far as its build line without git.
"""

import json
import os
import re
import shutil
import subprocess
import sys

from revali import NAME, VERSION, gitops
from tests.helpers import ROOT, RepoCase, _quote, git

PARTS = ("revali.py", "defaults.toml", "revali", "prompts", "schemas", "checklists", "templates")
TAG = "v9.9.9"
NO_PID = 999999999

# git as revali sees it: every call is noted in REVIEW_GIT_LOG, `describe` fails on request
GIT_STUB = """import json, os, subprocess, sys
args = sys.argv[1:]
with open(os.environ["REVIEW_GIT_LOG"], "a", encoding="utf-8") as fh:
    print(json.dumps(args), file=fh)
if "describe" in args and os.environ.get("REVIEW_GIT_DESCRIBE_FAILS"):
    print("fatal: describe refused by the test", file=sys.stderr)
    sys.exit(128)
sys.exit(subprocess.run([os.environ["REVIEW_REAL_GIT"]] + args).returncode)
"""

# `revali run` with the spawn replaced, so nothing is left running behind the test
DETACHED = """import sys
sys.path.insert(0, sys.argv[1])
from unittest import mock
from revali.cli import main
with mock.patch("revali.pipeline.spawn_detached", return_value=int(sys.argv[2])):
    sys.exit(main(["run"]))
"""

ASK = """import sys
sys.path.insert(0, sys.argv[1])
from revali.build import build
print(build())
print(build())
"""

OLD_ROW = {
    "at": "2026-01-01T00:00:00+0000",
    "repo": "o/old",
    "branch": "feature/old",
    "base": "main",
    "stage": "merged",
    "exit": 0,
    "rounds": 1,
    "fixes": 0,
    "last_verdict": "PASS",
    "cost_usd": 0.5,
    "models": ["m"],
    "fallback": False,
    "pr": 7,
    "issues": 0,
    "revali_version": "0.2.0",
    "prompt_version": "8",
}


class CheckoutCase(RepoCase):
    """The sample project plus copies of revali to run it with."""

    def setUp(self):
        super().setUp()
        self.root = gitops.repo_root(self.repo)
        self.history = os.path.join(self.home, "history.jsonl")
        self.git_log = os.path.join(self.tmp, "git calls.jsonl")

    def make_checkout(self, name="revali checkout", repo=True, tag=TAG):
        """A copy of revali; with `repo`, the root of a git checkout holding one commit."""
        path = os.path.join(self.tmp, name)
        os.makedirs(path)
        for part in PARTS:
            src = os.path.join(ROOT, part)
            if os.path.isdir(src):
                shutil.copytree(
                    src, os.path.join(path, part), ignore=shutil.ignore_patterns("__pycache__")
                )
            else:
                shutil.copy(src, os.path.join(path, part))
        if repo:
            git(["init", "-q"], path)
            for key, value in (
                ("user.email", "reviewer@example.invalid"),
                ("user.name", "reviewer"),
                ("core.autocrlf", "false"),
                ("commit.gpgsign", "false"),
                ("tag.gpgsign", "false"),
            ):
                git(["config", key, value], path)
            git(["add", "-A"], path)
            git(["commit", "-q", "-m", "revali"], path)
            if tag:
                git(["tag", tag], path)
        return path

    def env(self, **extra):
        env = dict(os.environ)
        env.update(PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1")
        env.update(extra)
        return env

    def watched_git(self, **extra):
        """An environment in which revali's git calls are logged to `self.git_log`."""
        stub = os.path.join(self.tmp, "git_stub.py")
        with open(stub, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(GIT_STUB)
        real = shutil.which("git")
        self.assertTrue(real, "git is needed for this test")
        return self.env(
            REVALI_GIT_CMD="%s %s" % (_quote(sys.executable), _quote(stub)),
            REVIEW_GIT_LOG=self.git_log,
            REVIEW_REAL_GIT=real,
            **extra,
        )

    def describe_calls(self):
        if not os.path.isfile(self.git_log):
            return []
        with open(self.git_log, "r", encoding="utf-8") as fh:
            calls = [json.loads(line) for line in fh if line.strip()]
        return [call for call in calls if "describe" in call]

    def python(self, argv, env=None):
        return subprocess.run(
            [sys.executable] + argv,
            cwd=self.repo,
            env=env or self.env(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=300,
        )

    def revali(self, checkout, argv, env=None):
        return self.python([os.path.join(checkout, "revali.py")] + argv, env)

    def untracked_file(self):
        """Makes the project's tree dirty: preflight stops right after its first line."""
        self.write("scratch.txt", "untracked\n")

    def build_part(self, text, where):
        """What follows `repo <root>, branch <branch>` on the first preflight line of `text`."""
        lines = [ln for ln in text.splitlines() if "] preflight: " in ln]
        self.assertTrue(lines, "no preflight line in %s:\n%s" % (where, text))
        opening = "] preflight: repo %s, branch feature/mul" % self.root
        self.assertIn(opening, lines[0], "%s:\n%s" % (where, text))
        return lines[0].split(opening, 1)[1]

    def preflight_build(self, checkout, env=None):
        self.untracked_file()
        res = self.revali(checkout, ["preflight"], env)
        return self.build_part(res.stdout, "preflight's output\n" + res.stderr)

    def history_lines(self):
        with open(self.history, "r", encoding="utf-8", newline="") as fh:
            return fh.read().split("\n")[:-1]


class BuildString(CheckoutCase):
    """AC-1, read from preflight's first line (AC-2)."""

    def test_tagged_checkout(self):
        checkout = self.make_checkout()
        self.assertEqual(self.preflight_build(checkout), ", revali %s (%s)" % (VERSION, TAG))

    def test_commit_past_the_tag(self):
        checkout = self.make_checkout()
        with open(os.path.join(checkout, "NOTES.txt"), "w", encoding="utf-8", newline="\n") as fh:
            fh.write("one more commit\n")
        git(["add", "NOTES.txt"], checkout)
        git(["commit", "-q", "-m", "notes"], checkout)
        head = git(["rev-parse", "HEAD"], checkout).strip()
        got = self.preflight_build(checkout)
        found = re.fullmatch(
            r", revali %s \(%s-1-g([0-9a-f]{4,40})\)" % (re.escape(VERSION), re.escape(TAG)), got
        )
        self.assertTrue(found, got)
        self.assertTrue(head.startswith(found.group(1)), (head, got))

    def test_uncommitted_change_is_dirty(self):
        checkout = self.make_checkout()
        with open(os.path.join(checkout, "templates", "change.md"), "ab") as fh:
            fh.write(b"a local edit\n")
        self.assertEqual(self.preflight_build(checkout), ", revali %s (%s-dirty)" % (VERSION, TAG))

    def test_no_tag_gives_the_commit(self):
        checkout = self.make_checkout(tag=None)
        head = git(["rev-parse", "HEAD"], checkout).strip()
        got = self.preflight_build(checkout)
        found = re.fullmatch(r", revali %s \(([0-9a-f]{4,40})\)" % re.escape(VERSION), got)
        self.assertTrue(found, got)
        self.assertTrue(head.startswith(found.group(1)), (head, got))

    def test_not_a_git_checkout(self):
        checkout = self.make_checkout(name="revali copy", repo=False)
        self.assertEqual(self.preflight_build(checkout), ", revali %s" % VERSION)

    def test_describe_fails(self):
        checkout = self.make_checkout()
        env = self.watched_git(REVIEW_GIT_DESCRIBE_FAILS="1")
        self.assertEqual(self.preflight_build(checkout, env), ", revali %s" % VERSION)
        self.assertEqual(len(self.describe_calls()), 1, self.describe_calls())

    def test_git_missing(self):
        checkout = self.make_checkout()
        ask = os.path.join(self.tmp, "ask.py")
        with open(ask, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(ASK)
        with_git = self.python([ask, checkout])
        self.assertEqual(
            with_git.stdout.splitlines(), ["%s (%s)" % (VERSION, TAG)] * 2, with_git.stderr
        )
        missing = self.env(REVALI_GIT_CMD=_quote(os.path.join(self.tmp, "no-such-git")))
        without = self.python([ask, checkout], missing)
        self.assertEqual(without.returncode, 0, without.stderr)
        self.assertEqual(without.stdout.splitlines(), [VERSION] * 2, without.stderr)


class BuildInARun(CheckoutCase):
    """AC-1 (once per process), AC-2, AC-4, AC-5 on a foreground run, which is what a
    detached run executes with its output in run.log."""

    def stopped_run(self, checkout, env=None):
        """`run --foreground` on a dirty tree: preflight's first line, then the stop and its
        history row."""
        self.untracked_file()
        res = self.revali(checkout, ["run", "--foreground"], env)
        self.assertEqual(res.returncode, 1, res.stdout + res.stderr)
        return res

    def test_output_log_and_history_row_name_the_build(self):
        checkout = self.make_checkout()
        old_line = json.dumps(OLD_ROW)
        with open(self.history, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(old_line + "\n")
        res = self.stopped_run(checkout)
        expected = ", revali %s (%s)" % (VERSION, TAG)
        # AC-5: the identity line is whole, without the build
        self.assertEqual(
            res.stdout.splitlines()[0], "repo: %s  branch: feature/mul" % self.root, res.stdout
        )
        # AC-2: stdout (run.log for a detached run) and revali.log
        self.assertEqual(self.build_part(res.stdout, "the run's output"), expected)
        with open(os.path.join(self.rdir(), "logs", "revali.log"), encoding="utf-8") as fh:
            self.assertEqual(self.build_part(fh.read(), "revali.log"), expected)
        # AC-4: the old row is untouched, the new one carries the build
        lines = self.history_lines()
        self.assertEqual(len(lines), 2, lines)
        self.assertEqual(lines[0], old_line)
        row = json.loads(lines[1])
        self.assertEqual(row.get("revali_build"), "%s (%s)" % (VERSION, TAG))
        self.assertEqual(row.get("revali_version"), VERSION)
        # AC-4: stats reads the file with both kinds of row
        stats = self.revali(checkout, ["stats"])
        self.assertEqual(stats.returncode, 0, stats.stdout + stats.stderr)
        self.assertIn("history rows: 2, pipelines: 2", stats.stdout)

    def test_git_describes_once_for_the_line_and_the_row(self):
        checkout = self.make_checkout()
        res = self.stopped_run(checkout, self.watched_git())
        build = "%s (%s)" % (VERSION, TAG)
        self.assertEqual(self.build_part(res.stdout, "the run's output"), ", revali " + build)
        self.assertEqual(json.loads(self.history_lines()[-1]).get("revali_build"), build)
        calls = self.describe_calls()
        self.assertEqual(len(calls), 1, calls)
        self.assertEqual(calls[0][-4:], ["describe", "--tags", "--always", "--dirty"])


class DetachedStart(CheckoutCase):
    """AC-3."""

    def test_start_line_ends_with_the_build(self):
        checkout = self.make_checkout()
        driver = os.path.join(self.tmp, "detached.py")
        with open(driver, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(DETACHED)
        res = self.python([driver, checkout, str(NO_PID)])
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        log_path = os.path.join(self.root, ".revali", "feature__mul", "logs", "run.log")
        before = "started revali run (pid %d); log: %s" % (NO_PID, log_path)
        lines = res.stdout.splitlines()
        self.assertEqual(lines[0], "repo: %s  branch: feature/mul" % self.root, res.stdout)
        self.assertEqual(lines[1], "%s; revali %s (%s)" % (before, VERSION, TAG), res.stdout)


class WholeLines(CheckoutCase):
    """AC-5: a checkout whose build has a describe part still prints the old whole lines."""

    def test_version_and_status_lines_carry_no_build(self):
        checkout = self.make_checkout()
        for argv in (["version"], ["--version"]):
            with self.subTest(argv=argv):
                res = self.revali(checkout, argv)
                self.assertEqual(res.returncode, 0, res.stderr)
                self.assertEqual(res.stdout, "%s %s\n" % (NAME, VERSION))
        status = self.revali(checkout, ["status"])
        self.assertEqual(status.returncode, 0, status.stdout + status.stderr)
        lines = status.stdout.splitlines()
        self.assertEqual(lines[0], "repo: %s  branch: feature/mul" % self.root)
        self.assertEqual(lines[1], "%s %s" % (NAME, VERSION))
        # the same checkout does have a build to show where AC-2 puts it
        self.assertEqual(self.preflight_build(checkout), ", revali %s (%s)" % (VERSION, TAG))
