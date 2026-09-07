"""Acceptance tests for feature/reviewer-lint, black-box through the CLI: `[project] lint`
runs over the reviewer's new test files before the smoke run and the commit (AC-1), a red
result sends the reviewer back once with the command and the output tail (AC-2), a second
red ends the run with exit 1 and no commit (AC-3), lint shares one bounce with the other
checks (AC-4), a NEEDS_INFO round is linted too and a green round still commits with the
trailer (AC-5), the log has one lint line per attempt (AC-6), the prompt names the command
(AC-7), preflight notes an empty line (AC-8), and the docs follow (AC-9)."""

import json
import os
import unittest

from revali import EXIT_ACTION, EXIT_ERROR, EXIT_OK, PROMPT_VERSION
from tests.fixtures.make_sample_repo import PY
from tests.helpers import TEST_REVIEW_MUL, RepoCase, approve_response, claude_entry, git, run_cli

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FILE = "tests/test_review_mul.py"
MARKER = "# unformatted"

# A stand-in for `black --check`: red when any reviewer file carries the marker, one
# "would reformat" line per offending file, exit 1; green otherwise.
FAKE_LINT = """import glob
import sys

bad = []
for path in sorted(glob.glob("tests/test_review_*.py")):
    with open(path, encoding="utf-8") as fh:
        if "# unformatted" in fh.read():
            bad.append(path.replace("\\\\", "/"))
for path in bad:
    print("would reformat " + path)
sys.exit(1 if bad else 0)
"""
BAD_FILE = TEST_REVIEW_MUL.replace("import unittest", "import unittest  " + MARKER)


def writes(text, data=None):
    """A reviewer session that answers `data` (a full approval by default) and writes FILE."""
    entry = claude_entry(data, write_tests=False)
    entry["write_files"] = {FILE: text}
    return entry


def needs_info(text):
    return writes(
        text, approve_response(verdict="NEEDS_INFO", questions=["Which rounding?"], tests=[])
    )


def stage_lines(log, prefix):
    return [line for line in log.splitlines() if "] review: " + prefix in line]


def repo_file(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


class LintRepo(RepoCase):
    """The fixture repo with `[project] lint` set to the fake lint script."""

    def setUp(self):
        super().setUp()
        self.write("fake_lint.py", FAKE_LINT)
        self.write(
            "revali.toml",
            self.read("revali.toml").replace('lint = ""', 'lint = "%s fake_lint.py"' % PY),
        )
        self.commit_all("lint line")

    def run_log(self):
        return self.read(".revali/feature__mul/logs/revali.log")


class RedFile(LintRepo):
    def test_red_file_bounces_once_and_the_clean_file_is_committed(self):
        head = git(["rev-parse", "HEAD"], self.repo).strip()
        self.claude(writes(BAD_FILE), writes(TEST_REVIEW_MUL))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("READY TO MERGE", out)
        calls = self.fake_calls("claude")
        self.assertEqual(len(calls), 2, out)  # AC-2: sent back exactly once
        retry = calls[1]["prompt"]
        self.assertIn("Corrections required", retry)
        self.assertIn("fake_lint.py", retry)  # AC-2: the bounce names the command
        self.assertIn("would reformat tests/test_review_mul.py", retry)  # AC-2: output tail
        rec = json.loads(self.read(".revali/feature__mul/review-1.json"))
        self.assertEqual(rec["bounces"], 1)  # AC-2
        # AC-1: lint ran before the smoke run on each attempt; AC-6: one line per attempt
        # naming the command and the exit code
        log = self.run_log()
        review = [line for line in log.splitlines() if "] review: " in line]
        lint_at = [i for i, line in enumerate(review) if "] review: lint" in line]
        smoke_at = [i for i, line in enumerate(review) if "smoke run of" in line]
        self.assertEqual(len(lint_at), 2, log)
        self.assertEqual(len(smoke_at), 2, log)
        self.assertLess(lint_at[0], smoke_at[0])
        self.assertLess(lint_at[1], smoke_at[1])
        self.assertIn("fake_lint.py", review[lint_at[0]])
        self.assertIn("exit 1", review[lint_at[0]])
        self.assertIn("exit 0", review[lint_at[1]])
        # AC-5: the green attempt is committed with the trailer, and the committed content is
        # the clean file, on top of the head the run started from
        self.assertNotEqual(git(["rev-parse", "HEAD"], self.repo).strip(), head)
        self.assertNotIn(MARKER, git(["show", "HEAD:" + FILE], self.repo))
        self.assertIn("Revali-Round: 1", git(["log", "-1", "--format=%B"], self.repo))

    def test_red_twice_ends_the_run_with_exit_1_and_no_commit(self):
        head = git(["rev-parse", "HEAD"], self.repo).strip()
        self.claude(writes(BAD_FILE), writes(BAD_FILE))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR, out)  # AC-3
        self.assertIn("lint", out)
        self.assertIn("still", out)
        self.assertIn("would reformat tests/test_review_mul.py", out)  # AC-3: the tail again
        self.assertEqual(len(self.fake_calls("claude")), 2)
        self.assertEqual(git(["rev-parse", "HEAD"], self.repo).strip(), head)  # nothing committed
        self.assertNotIn("test_review_mul", git(["status", "--porcelain"], self.repo))
        self.assertFalse(self.exists(FILE))

    def test_lint_and_an_ac_gap_share_one_bounce(self):
        partial = approve_response(
            tests=[{"path": FILE, "purpose": "product", "covers": ["AC-1"], "expected": "12"}]
        )
        self.claude(writes(BAD_FILE, partial), writes(TEST_REVIEW_MUL))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("READY TO MERGE", out)
        calls = self.fake_calls("claude")
        self.assertEqual(len(calls), 2, out)  # AC-4: spawned exactly twice
        retry = calls[1]["prompt"]
        self.assertIn("AC-2", retry)  # the gap
        self.assertIn("would reformat tests/test_review_mul.py", retry)  # and the lint output
        self.assertEqual(len(stage_lines(self.run_log(), "sending the reviewer back")), 1)

    def test_lint_and_a_smoke_problem_share_one_bounce(self):
        self.runner_scenario(
            {
                "default": 0,
                "results": {"smoke-r1-1": {"new_test": 2}},
                "outputs": {"smoke-r1-1": {"new_test": "ImportError: No module named nope"}},
            }
        )
        self.claude(writes(BAD_FILE), writes(TEST_REVIEW_MUL))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        calls = self.fake_calls("claude")
        self.assertEqual(len(calls), 2, out)  # AC-4
        retry = calls[1]["prompt"]
        self.assertIn("ImportError", retry)
        self.assertIn("would reformat tests/test_review_mul.py", retry)

    def test_a_needs_info_round_is_linted_and_keeps_the_clean_file_pending(self):
        self.claude(needs_info(BAD_FILE), needs_info(TEST_REVIEW_MUL))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ACTION, out)  # NEEDS_INFO still stops for the author
        calls = self.fake_calls("claude")
        self.assertEqual(len(calls), 2, out)  # AC-5: the round was bounced on lint
        self.assertIn("would reformat tests/test_review_mul.py", calls[1]["prompt"])
        self.assertTrue(self.exists(FILE))
        self.assertNotIn(MARKER, self.read(FILE))  # what stays pending is the clean file
        self.assertIn("??", git(["status", "--porcelain", "--", FILE], self.repo))

    def test_a_needs_info_round_red_twice_ends_the_run_and_drops_the_file(self):
        head = git(["rev-parse", "HEAD"], self.repo).strip()
        self.claude(needs_info(BAD_FILE), needs_info(BAD_FILE))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR, out)  # AC-3 applies to NEEDS_INFO as well
        self.assertEqual(git(["rev-parse", "HEAD"], self.repo).strip(), head)
        self.assertFalse(self.exists(FILE))


class GreenFile(LintRepo):
    def test_a_clean_file_is_linted_once_and_committed(self):
        self.claude(writes(TEST_REVIEW_MUL))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("READY TO MERGE", out)
        self.assertEqual(len(self.fake_calls("claude")), 1)  # no bounce
        lint = stage_lines(self.run_log(), "lint")
        self.assertEqual(len(lint), 1, self.run_log())  # AC-1, AC-6
        self.assertIn("exit 0", lint[0])
        self.assertIn("Revali-Round: 1", git(["log", "-1", "--format=%B"], self.repo))  # AC-5

    def test_no_file_written_means_no_lint_run(self):
        answer = approve_response(
            tests=[],
            not_testable=[
                {"ac": "AC-1", "reason": "checked by hand"},
                {"ac": "AC-2", "reason": "checked by hand"},
            ],
        )
        self.claude(claude_entry(answer, write_tests=False))
        run_cli(["run", "--foreground"])
        self.assertEqual(stage_lines(self.run_log(), "lint"), [])  # AC-1


class Prompt(LintRepo):
    def test_prompt_states_the_command_the_files_must_pass(self):
        self.claude(writes(TEST_REVIEW_MUL))
        run_cli(["run", "--foreground"])
        prompt = self.fake_calls("claude")[0]["prompt"]
        # the diff in the prompt mentions the file name too; the statement is the line that
        # names the command as the one the reviewer's files must pass
        self.assertRegex(prompt, r"lint command `[^`\n]*fake_lint\.py` must pass")  # AC-7

    def test_prompt_version_is_at_least_7(self):
        # AC-7 of PR #32 asked for a bump to 7; pinned to "7" this test failed on the next
        # prompt change (PR archive-on-merge, version 8), so it checks the bump happened
        self.assertGreaterEqual(int(PROMPT_VERSION), 7)


class EmptyLine(RepoCase):
    """The fixture leaves `[project] lint` empty."""

    def note_lines(self, out):
        return [line for line in out.splitlines() if "note: lint is empty" in line]

    def test_preflight_prints_the_note_once(self):
        code, out = run_cli(["preflight"])
        self.assertEqual(code, EXIT_OK, out)
        notes = self.note_lines(out)
        self.assertEqual(len(notes), 1, out)  # AC-8
        self.assertIn("formatting", notes[0])
        self.assertIn("style", notes[0])
        self.assertIn("reviewer", notes[0])

    def test_dry_run_prints_the_note_once(self):
        code, out = run_cli(["run", "--dry-run"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertEqual(len(self.note_lines(out)), 1, out)  # AC-8

    def test_a_run_prints_the_note_and_runs_no_lint(self):
        self.claude(claude_entry())
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("READY TO MERGE", out)
        self.assertEqual(len(self.note_lines(out)), 1, out)  # AC-8
        log = self.read(".revali/feature__mul/logs/revali.log")
        self.assertEqual(stage_lines(log, "lint"), [])  # AC-1: no lint line at all
        self.assertNotIn("lint command", self.fake_calls("claude")[0]["prompt"])  # AC-7

    def test_no_note_when_the_line_is_set(self):
        self.write(
            "revali.toml",
            self.read("revali.toml").replace('lint = ""', 'lint = "%s -c pass"' % PY),
        )
        self.commit_all("lint line")
        code, out = run_cli(["preflight"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertEqual(self.note_lines(out), [])  # AC-8


class Docs(unittest.TestCase):
    def test_side_effects_lists_the_lint_run_and_its_bounce(self):
        bullets = repo_file("docs/side-effects.md").split("\n- ")
        lint = [b for b in bullets if "[project] lint" in b]
        self.assertEqual(len(lint), 1, bullets)  # AC-9
        self.assertIn("sent back once", lint[0])
        self.assertIn("NEEDS_INFO", lint[0])

    def test_template_and_configuration_say_the_line_gates_the_reviewer(self):
        template = [
            line for line in repo_file("templates/revali.toml").splitlines() if "lint =" in line
        ]
        self.assertEqual(len(template), 1)
        self.assertIn("reviewer", template[0])  # AC-9
        config = repo_file("docs/configuration.md")
        self.assertIn("[project] lint", config)
        self.assertIn("reviewer's tests", config)  # AC-9

    def test_readme_run_summary_mentions_lint(self):
        readme = repo_file("README.md")
        paragraph = readme.split("What a run does", 1)[1].split("\n\n", 1)[0]
        self.assertIn("lint", paragraph)  # AC-9


if __name__ == "__main__":
    unittest.main()
