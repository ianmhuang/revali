"""AC-1..AC-9 of feature/reviewer-lint: the reviewer's test files must pass `[project] lint`
before they are committed; preflight says so when the line is empty."""

import json
import os
import unittest
from unittest import mock

from revali import EXIT_ACTION, EXIT_ERROR, EXIT_OK, PROMPT_VERSION
from revali.preflight import Stop, preflight
from revali.procs import ExeNotFound, ProcTimeout
from revali.review import build_prompt, lint_check
from revali.state import State
from tests.fixtures.make_sample_repo import PY
from tests.helpers import TEST_REVIEW_MUL, RepoCase, approve_response, claude_entry, git, run_cli

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Fails when any reviewer file carries the marker, the way black --check fails on a file
# it would reformat; prints one line per offending file.
LINTCHECK = """import glob
import sys

bad = []
for path in sorted(glob.glob("tests/test_review_*.py")):
    with open(path, encoding="utf-8") as fh:
        if "# BAD" in fh.read():
            bad.append(path.replace("\\\\", "/"))
for path in bad:
    print("would reformat " + path)
sys.exit(1 if bad else 0)
"""
BAD_TEST = TEST_REVIEW_MUL.replace("import unittest", "import unittest  # BAD")
FILE = "tests/test_review_mul.py"


def entry(text, data=None):
    e = claude_entry(data, write_tests=False)
    e["write_files"] = {FILE: text}
    return e


def asking(text):
    return entry(text, approve_response(verdict="NEEDS_INFO", questions=["Which?"], tests=[]))


def stage_lines(log, stage):
    return [line for line in log.splitlines() if "] %s: " % stage in line]


def lint_lines(log):
    return [line for line in stage_lines(log, "review") if "] review: lint" in line]


def read_repo_file(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


class LintCase(RepoCase):
    def setUp(self):
        super().setUp()
        self.write("lintcheck.py", LINTCHECK)
        self.write(
            "revali.toml",
            self.read("revali.toml").replace('lint = ""', 'lint = "%s lintcheck.py"' % PY),
        )
        self.commit_all("lint line")

    def log(self):
        return self.read(".revali/feature__mul/logs/revali.log")


class Bounce(LintCase):
    def test_bad_file_bounces_once_then_the_clean_file_is_committed(self):
        self.claude(entry(BAD_TEST), entry(TEST_REVIEW_MUL))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("READY TO MERGE", out)
        cl = self.fake_calls("claude")
        self.assertEqual(len(cl), 2)  # AC-2: sent back once
        self.assertIn("Corrections required", cl[1]["prompt"])
        self.assertIn("lintcheck.py", cl[1]["prompt"])  # names the command
        self.assertIn("would reformat tests/test_review_mul.py", cl[1]["prompt"])  # output tail
        self.assertEqual(json.loads(self.read(".revali/feature__mul/review-1.json"))["bounces"], 1)
        # AC-1: lint ran before the smoke run, once per attempt, logged with command and exit
        log = self.log()
        review = stage_lines(log, "review")
        lint_idx = [i for i, line in enumerate(review) if "] review: lint" in line]
        smoke_idx = [i for i, line in enumerate(review) if "smoke run of" in line]
        self.assertEqual(len(lint_idx), 2, log)
        self.assertEqual(len(smoke_idx), 2, log)
        self.assertLess(lint_idx[0], smoke_idx[0])
        self.assertLess(lint_idx[1], smoke_idx[1])
        self.assertIn("lintcheck.py", review[lint_idx[0]])
        self.assertIn("exit 1", review[lint_idx[0]])  # AC-6: exit code in the line
        self.assertIn("exit 0", review[lint_idx[1]])
        # AC-5: the committed file is the clean one, with the trailer
        self.assertNotIn("# BAD", git(["show", "HEAD:" + FILE], self.repo))
        self.assertIn("Revali-Round: 1", git(["log", "-1", "--format=%B"], self.repo))

    def test_lint_red_twice_ends_the_run_without_a_commit(self):
        head = git(["rev-parse", "HEAD"], self.repo).strip()
        self.claude(entry(BAD_TEST), entry(BAD_TEST))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR, out)  # AC-3
        self.assertIn("still", out)
        self.assertIn("lint", out)
        self.assertIn("would reformat tests/test_review_mul.py", out)
        self.assertEqual(git(["rev-parse", "HEAD"], self.repo).strip(), head)
        self.assertNotIn(FILE, git(["status", "--porcelain"], self.repo))
        self.assertEqual(len(self.fake_calls("claude")), 2)

    def test_lint_and_ac_gap_share_one_bounce(self):
        partial = approve_response(
            tests=[{"path": FILE, "purpose": "p", "covers": ["AC-1"], "expected": "e"}]
        )
        self.claude(entry(BAD_TEST, partial), entry(TEST_REVIEW_MUL))
        code, out = run_cli(["run", "--foreground"])
        self.assertIn("READY TO MERGE", out)
        cl = self.fake_calls("claude")
        self.assertEqual(len(cl), 2)  # AC-4
        self.assertIn("AC-2", cl[1]["prompt"])
        self.assertIn("lintcheck.py", cl[1]["prompt"])

    def test_lint_and_smoke_problem_share_one_bounce(self):
        self.runner_scenario(
            {
                "default": 0,
                "results": {"smoke-r1-1": {"new_test": 2}},
                "outputs": {"smoke-r1-1": {"new_test": "ImportError: no module named nothing"}},
            }
        )
        self.claude(entry(BAD_TEST), entry(TEST_REVIEW_MUL))
        code, out = run_cli(["run", "--foreground"])
        self.assertIn("READY TO MERGE", out)
        cl = self.fake_calls("claude")
        self.assertEqual(len(cl), 2)  # AC-4: the smoke run still happened on the same attempt
        self.assertIn("could not run", cl[1]["prompt"])
        self.assertIn("ImportError", cl[1]["prompt"])
        self.assertIn("lintcheck.py", cl[1]["prompt"])

    def test_a_needs_info_round_is_linted_too(self):
        self.claude(asking(BAD_TEST), asking(TEST_REVIEW_MUL))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ACTION, out)  # NEEDS_INFO still stops for the author
        cl = self.fake_calls("claude")
        self.assertEqual(len(cl), 2)  # AC-5
        self.assertIn("lintcheck.py", cl[1]["prompt"])
        self.assertNotIn("# BAD", self.read(FILE))  # the pending file is the clean one
        self.assertEqual(State.load(self.rdir()).pending_test_files, [FILE])

    def test_a_clean_file_passes_without_a_bounce(self):
        self.claude(entry(TEST_REVIEW_MUL))
        code, out = run_cli(["run", "--foreground"])
        self.assertIn("READY TO MERGE", out)
        self.assertEqual(len(self.fake_calls("claude")), 1)
        self.assertEqual(len(lint_lines(self.log())), 1)


class Errors(LintCase):
    def test_a_lint_timeout_is_a_pipeline_error(self):
        ctx = preflight(self.repo)
        with mock.patch("revali.review.run_shell", side_effect=ProcTimeout("timed out")):
            with self.assertRaises(Stop) as cm:
                lint_check(ctx, [FILE], None)
        self.assertEqual(cm.exception.exit_code, EXIT_ERROR)  # AC-6
        self.assertIn("lint", cm.exception.message)

    def test_a_lint_that_cannot_start_is_a_pipeline_error(self):
        ctx = preflight(self.repo)
        with mock.patch("revali.review.run_shell", side_effect=ExeNotFound("no such exe")):
            with self.assertRaises(Stop) as cm:
                lint_check(ctx, [FILE], None)
        self.assertEqual(cm.exception.exit_code, EXIT_ERROR)  # AC-6
        self.assertIn("lint", cm.exception.message)

    def test_no_files_runs_no_lint(self):
        ctx = preflight(self.repo)
        with mock.patch("revali.review.run_shell") as rs:
            self.assertIsNone(lint_check(ctx, [], None))  # AC-1
        rs.assert_not_called()


class EmptyLine(RepoCase):
    """The fixture leaves `[project] lint` empty."""

    def test_nothing_runs_and_preflight_says_so(self):
        self.claude(claude_entry())
        code, out = run_cli(["run", "--foreground"])
        self.assertIn("READY TO MERGE", out)
        self.assertEqual(lint_lines(self.read(".revali/feature__mul/logs/revali.log")), [])  # AC-1
        self.assertIn("note: lint is empty", out)  # AC-8

    def test_preflight_and_dry_run_print_the_note(self):
        for argv in (["preflight"], ["run", "--dry-run"]):
            code, out = run_cli(argv)
            self.assertEqual(code, EXIT_OK, out)
            note = [line for line in out.splitlines() if "note: lint is empty" in line]
            self.assertEqual(len(note), 1, out)  # AC-8
            self.assertIn("formatting", note[0])
            self.assertIn("reviewer", note[0])

    def test_no_note_when_lint_is_set(self):
        self.write(
            "revali.toml", self.read("revali.toml").replace('lint = ""', 'lint = "%s -c pass"' % PY)
        )
        self.commit_all("lint")
        code, out = run_cli(["preflight"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertNotIn("lint is empty", out)  # AC-8

    def test_prompt_is_silent_about_lint(self):
        ctx = preflight(self.repo)
        prompt = build_prompt(ctx, State(), self.rdir(), 1)
        self.assertNotIn("lint command", prompt)  # AC-7


class Prompt(LintCase):
    def test_prompt_names_the_lint_command(self):
        ctx = preflight(self.repo)
        prompt = build_prompt(ctx, State(), self.rdir(), 1)
        self.assertIn("lint command", prompt)  # AC-7
        self.assertIn("lintcheck.py", prompt)
        self.assertGreaterEqual(int(PROMPT_VERSION), 7)


class Docs(unittest.TestCase):
    def test_docs_mention_the_reviewer_lint_gate(self):
        side = read_repo_file("docs/side-effects.md")
        bullet = [b for b in side.split("\n- ") if "lint` a second time" in b]
        self.assertEqual(len(bullet), 1)
        self.assertIn("sent back once", bullet[0])
        self.assertIn("NEEDS_INFO", bullet[0])
        template_line = [
            line
            for line in read_repo_file("templates/revali.toml").splitlines()
            if "lint =" in line
        ][0]
        self.assertIn("reviewer", template_line.lower())
        self.assertIn("reviewer's tests", read_repo_file("docs/configuration.md"))
        readme = read_repo_file("README.md").split("What a run does")[1].split("\n\n")[0]
        self.assertIn("lint", readme)  # AC-9


if __name__ == "__main__":
    unittest.main()
