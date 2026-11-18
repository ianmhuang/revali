"""fix/format-scope-cleanup: the format call gets only files that exist (AC-1), and the
format, preflight, smoke and lint lines stop calling the reviewer's files "new" (AC-2..AC-4,
AC-6); the lint line counts only files that exist (AC-6)."""

import unittest
from unittest import mock

from revali import EXIT_ACTION, EXIT_OK
from revali.preflight import preflight
from revali.procs import ProcTimeout
from revali.review import format_files
from tests.fixtures.make_sample_repo import PY
from tests.helpers import TEST_REVIEW_MUL, run_cli
from tests.test_format_scope import TwoRounds, fmt_calls
from tests.test_tests_archive import FILE, SECOND, SECOND_TEXT, approving, requesting_changes


class Deleted(TwoRounds):
    """Round 1 writes FILE; round 2 deletes it and writes SECOND. Runs in archive mode;
    DeletedCommitMode reruns every test here in commit mode."""

    def revali_log(self):
        return self.read(".revali/feature__mul/logs/revali.log")

    def round_two_deletes(self, keep_second=True):
        self.claude(requesting_changes(**{FILE: TEST_REVIEW_MUL}))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ACTION, out)
        self.write("src/calc.py", self.read("src/calc.py") + "\n# negatives handled\n")
        self.commit_all("fix")
        entry = approving(**({SECOND: SECOND_TEXT} if keep_second else {}))
        entry["delete_files"] = [FILE]
        self.claude(entry)
        return run_cli(["run", "--foreground"])

    def format_lines(self):
        return [ln for ln in self.revali_log().splitlines() if "] review: format of" in ln]

    def lint_lines(self):
        return [ln for ln in self.revali_log().splitlines() if "] review: lint of" in ln]

    def enable_lint(self):
        toml = self.read("revali.toml")
        self.assertIn('lint = ""', toml)
        self.write("revali.toml", toml.replace('lint = ""', 'lint = "%s lintcheck.py"' % PY))
        self.commit_all("lint line")

    def test_lint_line_counts_what_exists(self):  # AC-6
        self.enable_lint()
        code, out = self.round_two_deletes()
        self.assertRegex(self.lint_lines()[-1], r"lint of 1 test file\(s\) with `.*`: exit 0$")

    def test_formats_only_what_exists(self):  # AC-1
        code, out = self.round_two_deletes()
        self.assertEqual(fmt_calls(self)[-1], [SECOND], out)
        self.assertIn("format of 1 test file(s) with `", self.format_lines()[-1])

    def test_nothing_left_runs_no_format(self):  # AC-1
        code, out = self.round_two_deletes(keep_second=False)
        self.assertEqual(fmt_calls(self), [[FILE]], out)  # round 1's call only
        self.assertEqual(len(self.format_lines()), 1, self.revali_log())


class DeletedCommitMode(Deleted):
    def setUp(self):
        super().setUp()
        self.switch_mode("commit")

    def test_commit_mode_formats_only_what_exists(self):  # AC-1
        code, out = self.round_two_deletes()
        self.assertEqual(code, EXIT_OK, out)
        self.assertEqual(fmt_calls(self)[-1], [SECOND], out)
        self.assertIn("format of 1 test file(s) with `", self.format_lines()[-1])

    def test_lint_line_with_only_a_deletion(self):  # AC-6
        self.enable_lint()
        self.round_two_deletes(keep_second=False)
        # lint still runs over the tree; the line counts no files
        self.assertRegex(self.lint_lines()[-1], r"lint of 0 test file\(s\) with `.*`: exit 0$")


class Wording(TwoRounds):
    def test_format_lines_say_test_files(self):  # AC-2
        ctx = preflight(self.repo)
        log = mock.Mock()
        with mock.patch("revali.review.run_shell") as rs:
            rs.return_value = mock.Mock(returncode=0, ok=True, text="")
            self.write(FILE, TEST_REVIEW_MUL)
            format_files(ctx, [FILE], log)
        with mock.patch("revali.review.run_shell", side_effect=ProcTimeout("timed out")):
            format_files(ctx, [FILE], log)
        lines = [c.args[1] for c in log.stage.call_args_list]
        self.assertEqual(len(lines), 2, lines)
        for line in lines:
            self.assertTrue(line.startswith("format of 1 test file(s) with `"), line)
        self.assertTrue(lines[0].endswith(": exit 0"), lines[0])
        self.assertIn("timed out", lines[1])

    def test_preflight_line(self):  # AC-3
        code, out = run_cli(["preflight"])
        self.assertEqual(code, EXIT_OK, out)
        line = [ln for ln in out.splitlines() if "preflight: format: " in ln]
        self.assertEqual(len(line), 1, out)
        self.assertTrue(
            line[0].endswith("(the reviewer's test files that differ from HEAD)"), line[0]
        )
        self.assertNotIn("new test files", out)

    def test_smoke_line(self):  # AC-4
        self.claude(approving(**{FILE: TEST_REVIEW_MUL}))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        smoke = [ln for ln in out.splitlines() if "review: smoke run of" in ln]
        self.assertEqual(len(smoke), 1, out)
        self.assertRegex(smoke[0], r"smoke run of 1 test file\(s\) on \w+$")


if __name__ == "__main__":
    unittest.main()
