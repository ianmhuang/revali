"""Reviewer acceptance tests for `{files}` in new_test: the placeholder is replaced by the
reviewer's test files (this round's in the smoke run, every reviewer file on the branch in
validation), repository-relative with forward slashes, quoted when a path has whitespace,
and the expanded command is what the step log records (AC-4); a command without the
placeholder runs as written (AC-5); `{files}` with nothing to name skips the step with a
stage-level log line and does not fail the run (AC-6); revali's own config, the template,
defaults.toml and the docs describe the placeholder and the timing fields (AC-7)."""

import json
import os
import re
import tomllib
import unittest

from revali import EXIT_ACTION, EXIT_OK
from revali.state import State
from tests.fixtures.make_sample_repo import LOCAL_NEW_TEST, LOCAL_TEST
from tests.helpers import TEST_REVIEW_MUL, RepoCase, approve_response, claude_entry, run_cli

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOGS = ".revali/feature__mul/logs"
REVALI_LOG = LOGS + "/revali.log"

TEST_REVIEW_ZERO = """import unittest

from src.calc import mul


class ZeroTests(unittest.TestCase):
    def test_zero_left(self):
        self.assertEqual(mul(0, 9), 0)
"""


def finding():
    return {
        "id": "F1",
        "file": "src/calc.py",
        "line": 12,
        "severity": "high",
        "kind": "correctness",
        "text": "mul ignores negative numbers",
        "suggestion": "handle the sign",
    }


class FilesCase(RepoCase):
    def set_toml(self, key, value):
        """Rewrite the one `key = ...` line of the fixture's revali.toml and commit it."""
        toml = self.read("revali.toml")
        new, n = re.subn(
            r"^%s = .*$" % re.escape(key), "%s = %s" % (key, json.dumps(value)), toml, flags=re.M
        )
        self.assertEqual(n, 1, toml)
        self.write("revali.toml", new)
        self.commit_all("configure %s" % key)

    def step_cmd(self, label, step):
        """The command the step log records for one sandbox step (its `$ ` line)."""
        first = self.read("%s/%s-%s.log" % (LOGS, label, step)).splitlines()[0]
        self.assertTrue(first.startswith("$ "), first)
        return first[2:]

    def step_files(self, label, step, prefix):
        cmd = self.step_cmd(label, step)
        self.assertTrue(cmd.startswith(prefix + " "), cmd)
        return cmd[len(prefix) + 1 :].split(" ")


class PlaceholderNamesTheFiles(FilesCase):
    def test_smoke_run_and_validation_run_the_reviewer_file(self):
        self.set_toml("new_test", "run-new {files}")
        self.claude(claude_entry())
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        smoke = self.step_cmd("smoke-r1-1", "new_test")
        self.assertEqual(smoke, "run-new tests/test_review_mul.py")  # AC-4: this round's file
        validate = self.step_cmd("validate-r1", "new_test")
        self.assertEqual(validate, "run-new tests/test_review_mul.py")  # AC-4: the branch's
        self.assertNotIn("\\", smoke + validate)  # AC-4: forward slashes
        self.assertEqual(self.step_cmd("baseline", "test"), LOCAL_TEST)  # other steps untouched
        # Since the baseline-reuse change (feature/skip-unchanged-suite) round-1 validation
        # leaves `test` out: only the reviewer's test commit followed the baseline.
        self.assertFalse(self.exists(LOGS + "/validate-r1-test.log"))
        for name in os.listdir(os.path.join(self.repo, LOGS)):
            if name.endswith(".log") and name != "revali.log":
                self.assertNotIn("{files}", self.read(LOGS + "/" + name), name)  # AC-4
        self.assertNotIn("skipped", self.read(REVALI_LOG))  # AC-6: nothing was skipped

    def test_validation_names_every_reviewer_file_on_the_branch(self):
        """Round 1 lands test_review_mul.py, round 2 adds test_review_zero.py: the smoke run of
        round 2 names only the new file, the validation names both."""
        self.set_toml("new_test", "run-new {files}")
        cr = approve_response(verdict="CHANGES_REQUESTED", findings=[finding()])
        ok = approve_response(
            previous_findings=[{"id": "F1", "status": "resolved", "note": "fixed"}],
            tests=[
                {
                    "path": "tests/test_review_zero.py",
                    "purpose": "zero on the left",
                    "covers": ["AC-1", "AC-2"],
                    "expected": "mul(0,9)=0 per AC-2",
                }
            ],
        )
        self.claude(
            claude_entry(cr),
            claude_entry(
                ok, write_tests=False, write_files={"tests/test_review_zero.py": TEST_REVIEW_ZERO}
            ),
        )
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ACTION, out)
        self.assertEqual(
            self.step_files("smoke-r1-1", "new_test", "run-new"), ["tests/test_review_mul.py"]
        )
        self.write("src/calc.py", self.read("src/calc.py") + "\n# handles negatives\n")
        self.commit_all("fix negatives")
        self.write(".revali/feature__mul/response-1.md", "- F1: fixed in the last commit\n")
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        state = State.load(self.rdir())
        self.assertEqual(
            sorted(state.test_files), ["tests/test_review_mul.py", "tests/test_review_zero.py"]
        )
        self.assertEqual(
            self.step_files("smoke-r2-1", "new_test", "run-new"), ["tests/test_review_zero.py"]
        )  # AC-4: the smoke run names the files the reviewer wrote this round
        self.assertEqual(
            sorted(self.step_files("validate-r2", "new_test", "run-new")),
            ["tests/test_review_mul.py", "tests/test_review_zero.py"],
        )  # AC-4: validation names every reviewer file on the branch

    def test_a_path_with_whitespace_is_quoted(self):
        self.set_toml("test_dir", "my tests")
        self.set_toml("new_test", "run-new {files}")
        entry = claude_entry(
            approve_response(
                tests=[
                    {
                        "path": "my tests/test_review_mul.py",
                        "purpose": "product and zero",
                        "covers": ["AC-1", "AC-2"],
                        "expected": "mul(3,4)=12; mul(9,0)=0",
                    }
                ]
            ),
            write_tests=False,
            write_files={"my tests/test_review_mul.py": TEST_REVIEW_MUL},
        )
        self.claude(entry)
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertEqual(
            self.step_cmd("smoke-r1-1", "new_test"), 'run-new "my tests/test_review_mul.py"'
        )  # AC-4: quoted because of the space, forward slash kept
        self.assertEqual(
            self.step_cmd("validate-r1", "new_test"), 'run-new "my tests/test_review_mul.py"'
        )

    def test_a_quoted_path_escapes_what_the_shell_reads_inside_double_quotes(self):
        """A whitespace path is double-quoted (AC-4); a `$` inside it would still be expanded
        by the sandbox shells, so the expansion escapes it and the command names the file as
        it is on disk."""
        self.set_toml("test_dir", "my $dir")
        self.set_toml("new_test", "run-new {files}")
        entry = claude_entry(
            approve_response(
                tests=[
                    {
                        "path": "my $dir/test_review_mul.py",
                        "purpose": "product and zero",
                        "covers": ["AC-1", "AC-2"],
                        "expected": "mul(3,4)=12; mul(9,0)=0",
                    }
                ]
            ),
            write_tests=False,
            write_files={"my $dir/test_review_mul.py": TEST_REVIEW_MUL},
        )
        self.claude(entry)
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        expected = 'run-new "my \\$dir/test_review_mul.py"'
        self.assertEqual(self.step_cmd("smoke-r1-1", "new_test"), expected)  # AC-4
        self.assertEqual(self.step_cmd("validate-r1", "new_test"), expected)  # AC-4


class WithoutThePlaceholder(FilesCase):
    def test_the_command_runs_as_written(self):
        self.claude(claude_entry())
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertEqual(self.step_cmd("smoke-r1-1", "new_test"), LOCAL_NEW_TEST)  # AC-5
        self.assertEqual(self.step_cmd("validate-r1", "new_test"), LOCAL_NEW_TEST)  # AC-5
        self.assertNotIn("skipped", self.read(REVALI_LOG))

    def test_a_placeholder_free_command_still_runs_with_no_reviewer_file(self):
        """Without `{files}` the skip of AC-6 does not apply: the command runs as before even
        when the reviewer wrote no test file."""
        self.claude(
            claude_entry(
                approve_response(
                    tests=[],
                    not_testable=[
                        {"ac": "AC-1", "reason": "covered by the existing suite"},
                        {"ac": "AC-2", "reason": "covered by the existing suite"},
                    ],
                ),
                write_tests=False,
            )
        )
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertEqual(self.step_cmd("validate-r1", "new_test"), LOCAL_NEW_TEST)  # AC-5
        self.assertNotIn("skipped", self.read(REVALI_LOG))


class NothingToName(FilesCase):
    def test_new_test_is_skipped_with_a_log_line_and_the_run_passes(self):
        self.set_toml("new_test", "run-new {files}")
        self.claude(
            claude_entry(
                approve_response(
                    tests=[],
                    not_testable=[
                        {"ac": "AC-1", "reason": "covered by the existing suite"},
                        {"ac": "AC-2", "reason": "covered by the existing suite"},
                    ],
                ),
                write_tests=False,
            )
        )
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)  # AC-6: the skip does not fail the run
        self.assertIn("READY TO MERGE", out)
        self.assertEqual(State.load(self.rdir()).stage, "ready_to_merge")
        self.assertFalse(self.exists(LOGS + "/validate-r1-new_test.log"))  # AC-6: not run
        # Since the baseline-reuse change (feature/skip-unchanged-suite) the existing suite is
        # not rerun either: nothing but the baseline's own tree is there to validate, so no
        # sandbox session runs and validation records why.
        self.assertFalse(self.exists(LOGS + "/validate-r1-test.log"))
        self.assertIn("existing suite not rerun", self.read(REVALI_LOG))
        calls = [c for c in self.fake_calls("runner") if c["label"] == "validate-r1"]
        self.assertEqual(calls, [])  # AC-6: no empty-list command (no session at all)
        log = self.read(REVALI_LOG)
        skip = [
            ln
            for ln in log.splitlines()
            if "] validate: " in ln and "new_test" in ln and "skip" in ln.lower()
        ]
        self.assertEqual(len(skip), 1, log)  # AC-6: a stage-level line says so
        self.assertRegex(log, r"validate: run 1: PASS")


class ConfigAndDocsDescribeIt(unittest.TestCase):
    def read(self, *parts):
        with open(os.path.join(ROOT, *parts), "r", encoding="utf-8", newline="") as fh:
            return fh.read()

    @staticmethod
    def new_test_values(data):
        out = []
        for key, value in data.items():
            if key == "new_test" and isinstance(value, str):
                out.append(value)
            elif isinstance(value, dict):
                out.extend(ConfigAndDocsDescribeIt.new_test_values(value))
        return out

    def test_revali_own_config_uses_the_placeholder(self):
        data = tomllib.loads(self.read("revali.toml"))
        values = self.new_test_values(data)
        self.assertTrue(values, data)
        self.assertTrue(all("{files}" in v for v in values), values)  # AC-7

    def test_the_template_uses_and_explains_the_placeholder(self):
        text = self.read("templates", "revali.toml")
        values = self.new_test_values(tomllib.loads(text))
        self.assertTrue(values, text)
        self.assertTrue(any("{files}" in v for v in values), values)  # AC-7
        # the comment around new_test explains it
        idx = text.index("new_test")
        self.assertIn("{files}", text[max(0, idx - 600) : idx + 600])

    def test_defaults_toml_describes_the_placeholder(self):
        text = self.read("defaults.toml")
        lines = [ln for ln in text.splitlines() if ln.startswith("new_test")]
        self.assertEqual(len(lines), 1, text)
        self.assertIn("{files}", lines[0])  # AC-7

    def test_docs_describe_the_placeholder_and_the_timing_fields(self):
        sandbox = self.read("docs", "sandbox.md")
        self.assertIn("{files}", sandbox)  # AC-7
        self.assertIn("new_test", sandbox)
        self.assertIn("skipped", sandbox)  # the no-file case
        files = self.read("docs", "files.md")
        self.assertIn("stage_s", files)  # AC-7: the history row fields
        self.assertIn("sandbox_s", files)


if __name__ == "__main__":
    unittest.main()
