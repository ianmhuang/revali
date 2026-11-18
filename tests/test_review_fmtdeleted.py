"""Reviewer acceptance tests for fix/format-scope-cleanup, black-box through the CLI.

AC-1: a test file of the reviewer's own that the attempt deleted is not handed to
`[project] format`; the log line counts the files that exist, and with none left the format
line does not run.
AC-2: the format log line reads "format of N test file(s) with `<cmd>`: ..." for an exit and
for a timeout.
AC-3: preflight's line ends "(the reviewer's test files that differ from HEAD)".
AC-4: the smoke run's line reads "smoke run of N test file(s) on <runner>".
AC-6: the lint log line reads "lint of N test file(s) with `<cmd>`: exit <code>", N being the
listed files that exist on disk; lint still runs when the only listed path was deleted.
"""

import json
import os
import re
import unittest
from unittest import mock

from revali import EXIT_ACTION, EXIT_OK
from revali.procs import ProcTimeout
from tests.fixtures.make_sample_repo import PY
from tests.helpers import TEST_REVIEW_MUL, RepoCase, approve_response, claude_entry, run_cli

FIRST = "tests/test_review_mul.py"
LATER = "tests/test_review_later.py"
LATER_TEXT = TEST_REVIEW_MUL.replace("MulTests", "LaterTests")
ARGS_ENV = "REVALI_TEST_FMTDELETED_LOG"
FORMAT_LINE = "%s record_fmt.py {files}" % PY

# Stands in for a formatter: appends the paths it is given, one JSON list per call, to the
# file ARGS_ENV names (outside the repository). It opens none of them, so a path that is not
# on disk is recorded like any other.
RECORDER = """import json
import os
import sys

with open(os.environ["REVALI_TEST_FMTDELETED_LOG"], "a", encoding="utf-8") as fh:
    fh.write(json.dumps([a.replace("\\\\", "/") for a in sys.argv[1:]]) + "\\n")
"""
# Stands in for a linter that checks the whole tree: red (exit 3) while any reviewer file
# carries the marker, green otherwise.
LINT_LINE = "%s lint_stub.py" % PY
LINT_STUB = """import glob
import sys

red = []
for path in sorted(glob.glob("tests/test_review_*.py")):
    with open(path, encoding="utf-8") as fh:
        if "# LINT-RED" in fh.read():
            red.append(path)
for path in red:
    print("would reformat " + path)
sys.exit(3 if red else 0)
"""
RED_TEXT = TEST_REVIEW_MUL.replace("import unittest", "import unittest  # LINT-RED", 1)
FINDING = {
    "id": "F1",
    "file": "src/calc.py",
    "line": 3,
    "severity": "high",
    "kind": "correctness",
    "text": "mul ignores negative numbers",
    "suggestion": "handle them",
}


def answer(verdict, files, delete=()):
    """A reviewer session with `verdict` that writes `files` (path -> text) and deletes
    `delete`. Without files both fixture ACs are listed as not testable, so nothing bounces."""
    tests = [
        {"path": p, "purpose": "acceptance", "covers": ["AC-1", "AC-2"], "expected": "per AC"}
        for p in files
    ]
    data = approve_response(tests=tests)
    if not files:
        data["not_testable"] = [{"ac": ac, "reason": "checked by hand"} for ac in ("AC-1", "AC-2")]
    if verdict == "CHANGES_REQUESTED":
        data.update(verdict="CHANGES_REQUESTED", findings=[dict(FINDING)])
    entry = claude_entry(data, write_tests=False)
    entry["write_files"] = dict(files)
    entry["delete_files"] = list(delete)
    return entry


def review_lines(out, needle):
    """The review stage lines of one run's output that start with `needle`."""
    return [line for line in out.splitlines() if "] review: " + needle in line]


def format_line_pattern(path, ending):
    return r"review: format of 1 test file\(s\) with `[^`]*record_fmt\.py %s`: %s" % (
        re.escape(path),
        ending,
    )


def lint_line_pattern(count, code):
    shape = r"review: lint of %d test file\(s\) with `[^`]*lint_stub\.py`: exit %d$"
    return shape % (count, code)


class FormatRepo(RepoCase):
    """The fixture repo with a recording `[project] format` line; the lint line is empty
    unless `lint` is set, which puts LINT_STUB behind it."""

    archive = False
    lint = False

    def setUp(self):
        super().setUp()
        os.environ["REVALI_POLL_SECONDS"] = "0.01"
        self.args_log = os.path.join(self.tmp, "format-args.jsonl")
        os.environ[ARGS_ENV] = self.args_log
        self.write("record_fmt.py", RECORDER)
        toml = self.read("revali.toml")
        assert "[project]\n" in toml, toml
        toml = toml.replace("[project]\n", '[project]\nformat = "%s"\n' % FORMAT_LINE, 1)
        if self.lint:
            assert 'lint = ""\n' in toml, toml
            toml = toml.replace('lint = ""\n', 'lint = "%s"\n' % LINT_LINE, 1)
            self.write("lint_stub.py", LINT_STUB)
        if self.archive:
            key = 'exclude = ["*.lock"]\n'
            assert key in toml, toml
            toml = toml.replace(key, key + 'tests = "archive"\n', 1)
        self.write("revali.toml", toml)
        self.commit_all("format line for the reviewer's tests")

    def format_calls(self):
        if not os.path.isfile(self.args_log):
            return []
        with open(self.args_log, encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def round_one(self):
        """Round 1 writes FIRST and asks for changes; the author answers with a commit."""
        self.claude(answer("CHANGES_REQUESTED", {FIRST: TEST_REVIEW_MUL}))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ACTION, out)
        self.assertEqual(self.format_calls(), [[FIRST]], out)
        self.write("src/calc.py", self.read("src/calc.py") + "\n# negatives handled\n")
        self.commit_all("fix")

    def round_two(self, files):
        """Round 2 deletes FIRST and writes `files`; returns that run's output."""
        self.claude(answer("APPROVE", files, delete=[FIRST]))
        _, out = run_cli(["run", "--foreground"])
        self.assertGreaterEqual(len(self.fake_calls("claude")), 2, out)  # the reviewer ran again
        return out


class DeletedCommittedFile(FormatRepo):
    """AC-1, commit mode: FIRST is committed by round 1, so deleting it is a difference from
    HEAD that used to reach the formatter."""

    def test_the_format_call_gets_only_the_file_that_exists(self):
        self.round_one()
        out = self.round_two({LATER: LATER_TEXT})
        self.assertEqual(self.format_calls()[1:], [[LATER]], out)  # no deleted path
        fmt = review_lines(out, "format of")
        self.assertEqual(len(fmt), 1, out)
        self.assertRegex(fmt[0], format_line_pattern(LATER, "exit 0$"))  # counts one file
        self.assertNotIn(FIRST, fmt[0])

    def test_with_every_file_deleted_the_format_line_does_not_run(self):
        self.round_one()
        out = self.round_two({})
        self.assertEqual(self.format_calls(), [[FIRST]], out)  # round 1's call only
        self.assertEqual(review_lines(out, "format of"), [], out)


class DeletedPlacedBackFile(FormatRepo):
    """AC-1, archive mode: FIRST is placed back for round 2, which deletes it."""

    archive = True

    def test_the_format_call_gets_only_the_file_that_exists(self):
        self.round_one()
        out = self.round_two({LATER: LATER_TEXT})
        self.assertEqual(self.format_calls()[1:], [[LATER]], out)
        fmt = review_lines(out, "format of")
        self.assertEqual(len(fmt), 1, out)
        self.assertRegex(fmt[0], format_line_pattern(LATER, "exit 0$"))
        self.assertNotIn(FIRST, fmt[0])


class LintLine(FormatRepo):
    """AC-6, commit mode: a deleted committed file is still listed for lint, and the line
    counts the listed files that are on disk."""

    lint = True

    def test_the_line_counts_only_the_file_that_exists(self):
        self.round_one()
        out = self.round_two({LATER: LATER_TEXT})  # listed: FIRST (deleted) and LATER
        lint = review_lines(out, "lint of")
        self.assertEqual(len(lint), 1, out)
        self.assertRegex(lint[0], lint_line_pattern(1, 0))

    def test_with_only_a_deletion_lint_still_runs_and_counts_no_file(self):
        self.round_one()
        out = self.round_two({})  # listed: FIRST (deleted) only
        lint = review_lines(out, "lint of")
        self.assertEqual(len(lint), 1, out)  # which rounds lint runs in is unchanged
        self.assertRegex(lint[0], lint_line_pattern(0, 0))

    def test_the_wording_for_a_red_and_a_green_exit(self):
        self.claude(
            answer("APPROVE", {FIRST: RED_TEXT}),  # red lint: sent back once
            answer("APPROVE", {FIRST: TEST_REVIEW_MUL}),
        )
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        lint = review_lines(out, "lint of")
        self.assertEqual(len(lint), 2, out)
        self.assertRegex(lint[0], lint_line_pattern(1, 3))
        self.assertRegex(lint[1], lint_line_pattern(1, 0))
        for line in lint:
            self.assertNotIn("new test file", line)


class LintLineArchive(FormatRepo):
    """AC-6, archive mode: the deleted placed-back file is not listed, LATER is."""

    archive = True
    lint = True

    def test_the_line_counts_the_file_that_exists(self):
        self.round_one()
        out = self.round_two({LATER: LATER_TEXT})
        lint = review_lines(out, "lint of")
        self.assertEqual(len(lint), 1, out)
        self.assertRegex(lint[0], lint_line_pattern(1, 0))


class Wording(FormatRepo):
    def test_the_exit_line_and_the_smoke_line(self):  # AC-2, AC-4
        self.claude(answer("APPROVE", {FIRST: TEST_REVIEW_MUL}))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        fmt = review_lines(out, "format of")
        self.assertEqual(len(fmt), 1, out)
        self.assertRegex(fmt[0], format_line_pattern(FIRST, "exit 0$"))
        smoke = review_lines(out, "smoke run of")
        self.assertEqual(len(smoke), 1, out)
        self.assertRegex(smoke[0], r"review: smoke run of 1 test file\(s\) on \S")
        for line in fmt + smoke:
            self.assertNotIn("new test file", line)

    def test_the_timeout_line(self):  # AC-2
        from revali import procs

        real = procs.run_shell

        def slow_formatter(cmd, *args, **kwargs):
            if "record_fmt.py" in cmd:
                raise ProcTimeout("timed out after 1s: " + cmd)
            return real(cmd, *args, **kwargs)

        self.claude(answer("APPROVE", {FIRST: TEST_REVIEW_MUL}))
        with mock.patch("revali.review.run_shell", side_effect=slow_formatter):
            _, out = run_cli(["run", "--foreground"])
        fmt = review_lines(out, "format of")
        self.assertEqual(len(fmt), 1, out)
        self.assertRegex(fmt[0], format_line_pattern(FIRST, "timed out"))
        self.assertEqual(self.format_calls(), [], out)  # the stand-in never ran

    def test_the_preflight_line(self):  # AC-3
        code, out = run_cli(["preflight"])
        self.assertEqual(code, EXIT_OK, out)
        lines = [line for line in out.splitlines() if "format: " + FORMAT_LINE in line]
        self.assertEqual(len(lines), 1, out)
        expected = "format: %s (the reviewer's test files that differ from HEAD)" % FORMAT_LINE
        self.assertTrue(lines[0].endswith(expected), lines[0])


if __name__ == "__main__":
    unittest.main()
