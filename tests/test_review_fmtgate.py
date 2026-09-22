"""Acceptance tests for fix/reviewer-format-cost-encoding, the `[project] format` half,
black-box through the CLI: after each reviewer attempt revali runs the format line on the
host over that attempt's new test files and then lint, so a file that only needed formatting
passes without a model turn, with one log line per attempt naming the command and its exit
(AC-1); preflight refuses a format line without `{files}` with exit 2, and a format line that
exits non-zero or times out is logged while lint decides (AC-2); the prompt names the format
command only when one is set, also with `lint` empty (AC-3); PROMPT_VERSION moved (AC-6, the
code half). Round 2 adds the no-lint case and the timeout line's shape (round-1 F1 and F2)."""

import json
import os
import unittest
from unittest import mock

from revali import EXIT_ACTION, EXIT_ERROR, EXIT_OK, PROMPT_VERSION
from revali.procs import ProcTimeout
from tests.fixtures.make_sample_repo import PY
from tests.helpers import TEST_REVIEW_MUL, RepoCase, approve_response, claude_entry, git, run_cli

FILE = "tests/test_review_mul.py"
MARKER = "# needs-format"
LOG_ENV = "REVALI_FMTGATE_LOG"

# Stands in for `black --check`: red when any reviewer file still carries the marker.
LINT_STUB = """import glob
import sys

bad = []
for path in sorted(glob.glob("tests/test_review_*.py")):
    with open(path, encoding="utf-8") as fh:
        if "# needs-format" in fh.read():
            bad.append(path.replace("\\\\", "/"))
for path in bad:
    print("would reformat " + path)
sys.exit(1 if bad else 0)
"""
# Stands in for `black <files>`: strips the marker from every file named on its command line
# (unless REVALI_FMTGATE_NOOP is set), appends its argument list to the file LOG_ENV names,
# exits REVALI_FMTGATE_EXIT (default 0).
FORMAT_STUB = """import json
import os
import sys

for path in sys.argv[1:]:
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    if not os.environ.get("REVALI_FMTGATE_NOOP"):
        text = text.replace("  # needs-format", "")
    with open(path, "w", encoding="utf-8", newline="\\n") as fh:
        fh.write(text)
with open(os.environ["REVALI_FMTGATE_LOG"], "a", encoding="utf-8") as fh:
    fh.write(json.dumps(sys.argv[1:]) + "\\n")
sys.exit(int(os.environ.get("REVALI_FMTGATE_EXIT", "0")))
"""
UNFORMATTED = TEST_REVIEW_MUL.replace("import unittest", "import unittest  " + MARKER)


def writes(text, data=None):
    """A reviewer session answering `data` (a full approval by default) that writes FILE."""
    entry = claude_entry(data, write_tests=False)
    entry["write_files"] = {FILE: text}
    return entry


def review_lines(log, needle):
    return [line for line in log.splitlines() if "] review: " in line and needle in line]


def fmt_lines(log):
    """Review-stage lines that report a format run: the command and an exit code."""
    return [line for line in review_lines(log, "fmt_stub.py") if "exit " in line]


def lint_lines(log):
    return review_lines(log, "] review: lint")


class FormatRepo(RepoCase):
    """The fixture repo with the lint stub and, when `format_line` is set, the format stub."""

    format_line = "%s fmt_stub.py {files}" % PY
    lint_line = "%s lint_stub.py" % PY  # "" leaves the fixture's empty lint line in place

    def setUp(self):
        super().setUp()
        self.fmt_log = os.path.join(self.tmp, "fmt_calls.jsonl")
        os.environ[LOG_ENV] = self.fmt_log
        os.environ.pop("REVALI_FMTGATE_EXIT", None)
        os.environ.pop("REVALI_FMTGATE_NOOP", None)
        self.write("lint_stub.py", LINT_STUB)
        toml = self.read("revali.toml")
        if self.lint_line:
            toml = toml.replace('lint = ""', 'lint = "%s"' % self.lint_line)
        if self.format_line:
            # the stub is only in the tree (and so in the diff the prompt carries) when used
            self.write("fmt_stub.py", FORMAT_STUB)
            toml = toml.replace('lint = "', 'format = "%s"\nlint = "' % self.format_line, 1)
        self.write("revali.toml", toml)
        self.commit_all("lint and format lines")

    def run_log(self):
        return self.read(".revali/feature__mul/logs/revali.log")

    def fmt_calls(self):
        if not os.path.isfile(self.fmt_log):
            return []
        with open(self.fmt_log, encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]


class FormatThenLint(FormatRepo):
    def test_a_file_that_only_needs_formatting_costs_no_model_turn(self):
        head = git(["rev-parse", "HEAD"], self.repo).strip()
        self.claude(writes(UNFORMATTED), writes(UNFORMATTED))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("READY TO MERGE", out)
        self.assertEqual(len(self.fake_calls("claude")), 1, out)  # AC-1: not sent back
        self.assertEqual(self.fmt_calls(), [[FILE]])  # AC-1: the attempt's files, nothing else
        # the formatted file is what got committed
        self.assertNotEqual(git(["rev-parse", "HEAD"], self.repo).strip(), head)
        self.assertNotIn(MARKER, git(["show", "HEAD:" + FILE], self.repo))
        # AC-1: one log line naming the command and its exit, before the lint line
        log = self.run_log()
        fmt = fmt_lines(log)
        self.assertEqual(len(fmt), 1, log)
        self.assertIn("exit 0", fmt[0])
        lint = lint_lines(log)
        self.assertEqual(len(lint), 1, log)
        self.assertIn("exit 0", lint[0])
        lines = log.splitlines()
        self.assertLess(lines.index(fmt[0]), lines.index(lint[0]))

    def test_format_runs_after_every_attempt(self):
        partial = approve_response(
            tests=[{"path": FILE, "purpose": "product", "covers": ["AC-1"], "expected": "12"}]
        )
        self.claude(writes(UNFORMATTED, partial), writes(UNFORMATTED))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertEqual(len(self.fake_calls("claude")), 2, out)  # the AC gap still bounces
        self.assertEqual(self.fmt_calls(), [[FILE], [FILE]])  # AC-1: once per attempt
        self.assertEqual(len(fmt_lines(self.run_log())), 2)
        # the bounce was for the gap, not for formatting
        retry = self.fake_calls("claude")[1]["prompt"]
        self.assertIn("AC-2", retry)
        self.assertNotIn("would reformat " + FILE, retry)

    def test_no_new_file_means_no_format_run(self):
        answer = approve_response(
            tests=[],
            not_testable=[
                {"ac": "AC-1", "reason": "checked by hand"},
                {"ac": "AC-2", "reason": "checked by hand"},
            ],
        )
        self.claude(claude_entry(answer, write_tests=False))
        run_cli(["run", "--foreground"])
        self.assertEqual(self.fmt_calls(), [])  # AC-1: only when the attempt left files
        self.assertEqual(fmt_lines(self.run_log()), [])


class FormatFails(FormatRepo):
    def test_a_non_zero_format_exit_is_logged_and_lint_decides(self):
        os.environ["REVALI_FMTGATE_EXIT"] = "3"
        os.environ["REVALI_FMTGATE_NOOP"] = "1"
        self.claude(writes(UNFORMATTED), writes(TEST_REVIEW_MUL))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)  # AC-2: the format exit alone stops nothing
        self.assertIn("READY TO MERGE", out)
        calls = self.fake_calls("claude")
        self.assertEqual(len(calls), 2, out)  # lint was red on the untouched file: one bounce
        self.assertIn("would reformat " + FILE, calls[1]["prompt"])
        fmt = fmt_lines(self.run_log())
        self.assertEqual(len(fmt), 2, self.run_log())
        self.assertIn("exit 3", fmt[0])  # AC-2: logged with its exit code
        self.assertIn("exit 3", fmt[1])

    def test_a_format_timeout_is_logged_and_lint_decides(self):
        from revali import procs

        real = procs.run_shell

        def slow_formatter(cmd, *args, **kwargs):
            if "fmt_stub.py" in cmd:
                raise ProcTimeout("timed out after 1s: " + cmd)
            return real(cmd, *args, **kwargs)

        self.claude(writes(UNFORMATTED), writes(TEST_REVIEW_MUL))
        with mock.patch("revali.review.run_shell", side_effect=slow_formatter):
            code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)  # AC-2: a timed-out format line stops nothing
        self.assertIn("READY TO MERGE", out)
        self.assertEqual(len(self.fake_calls("claude")), 2, out)  # lint red once, then green
        log = self.run_log()
        timed = review_lines(log, "timed out")
        self.assertEqual(len(timed), 2, log)  # AC-2: logged, once per attempt
        for line in timed:
            # AC-1: the same shape as the exit line, so one pattern finds every format run:
            # the file count, then the command in backquotes, then what happened
            self.assertIn("format of 1 new test file(s) with `", line)
            self.assertIn("fmt_stub.py " + FILE + "`: timed out", line)
        self.assertEqual(len(lint_lines(log)), 2, log)  # lint still ran each time


class FormatWithoutPlaceholder(FormatRepo):
    format_line = "%s fmt_stub.py" % PY

    def test_preflight_stops_with_exit_2(self):
        code, out = run_cli(["preflight"])
        self.assertEqual(code, EXIT_ACTION, out)  # AC-2
        self.assertIn("{files}", out)
        self.assertIn("format", out)

    def test_a_run_stops_before_the_reviewer(self):
        self.claude(writes(TEST_REVIEW_MUL))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ACTION, out)  # AC-2
        self.assertIn("{files}", out)
        self.assertEqual(self.fake_calls("claude"), [])
        self.assertEqual(self.fmt_calls(), [])


class Prompt(FormatRepo):
    def test_prompt_names_the_format_command(self):
        self.claude(writes(TEST_REVIEW_MUL))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        prompt = self.fake_calls("claude")[0]["prompt"]
        # AC-3: the statement that names the command (in backquotes, unlike the revali.toml
        # line in the diff) says the script formats the files
        named = [line for line in prompt.splitlines() if "`%s`" % self.format_line in line]
        self.assertEqual(len(named), 1, prompt)
        self.assertRegex(named[0], r"(?i)format")
        # the lint statement is still there
        self.assertRegex(prompt, r"lint command `[^`\n]*lint_stub\.py` must pass")

    def test_prompt_version_moved_past_8(self):
        self.assertGreaterEqual(int(PROMPT_VERSION), 9)  # AC-6


class FormatWithoutLint(FormatRepo):
    """`format` set, `lint` empty (round 1, F1): the formatter still rewrites the reviewer's
    files, so the prompt still names it (AC-3) and preflight's empty-lint note says so."""

    lint_line = ""

    def test_prompt_still_names_the_format_command(self):
        self.claude(writes(UNFORMATTED))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        prompt = self.fake_calls("claude")[0]["prompt"]
        named = [line for line in prompt.splitlines() if "`%s`" % self.format_line in line]
        self.assertEqual(len(named), 1, prompt)  # AC-3: format is set, so the prompt speaks
        self.assertRegex(named[0], r"(?i)format")
        self.assertNotIn("lint command", prompt)  # and claims no lint gate that does not run
        # the file was formatted and committed although nothing checked it
        self.assertEqual(self.fmt_calls(), [[FILE]])
        self.assertNotIn(MARKER, git(["show", "HEAD:" + FILE], self.repo))
        self.assertEqual(lint_lines(self.run_log()), [])

    def test_preflight_note_says_the_format_line_still_runs(self):
        code, out = run_cli(["preflight"])
        self.assertEqual(code, EXIT_OK, out)
        notes = [line for line in out.splitlines() if "preflight: note: lint is empty" in line]
        self.assertEqual(len(notes), 1, out)
        self.assertRegex(notes[0], r"(?i)format line")
        self.assertIn("format: %s" % self.format_line, out)


class NoFormatLine(FormatRepo):
    format_line = ""

    def test_prompt_and_run_are_unchanged(self):
        self.claude(writes(UNFORMATTED), writes(TEST_REVIEW_MUL))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        calls = self.fake_calls("claude")
        self.assertEqual(len(calls), 2, out)  # the old lint bounce stands
        self.assertNotIn("fmt_stub", calls[0]["prompt"])  # AC-3: nothing about formatting
        self.assertNotRegex(calls[0]["prompt"], r"(?i)formats your")
        self.assertRegex(calls[0]["prompt"], r"lint command `[^`\n]*lint_stub\.py` must pass")
        self.assertEqual(self.fmt_calls(), [])
        self.assertEqual(review_lines(self.run_log(), "fmt_stub"), [])

    def test_lint_red_twice_still_ends_the_run(self):
        self.claude(writes(UNFORMATTED), writes(UNFORMATTED))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertEqual(len(self.fake_calls("claude")), 2)


if __name__ == "__main__":
    unittest.main()
