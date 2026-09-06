"""AC-6 of feature/reviewer-lint, white-box: the lint run over the reviewer's files ends the
run with exit 1 (a pipeline error, as preflight's lint does) when the command cannot start
or exceeds `[review] timeout_min`; the timeout is the configured one; nothing runs when
the reviewer wrote no file or the line is empty (AC-1)."""

import unittest
from unittest import mock

from revali import EXIT_ERROR
from revali.preflight import Stop, preflight
from revali.procs import ExeNotFound, ProcTimeout, Result
from revali.review import lint_check
from tests.fixtures.make_sample_repo import PY
from tests.helpers import RepoCase

FILE = "tests/test_review_mul.py"


class WithLint(RepoCase):
    def setUp(self):
        super().setUp()
        self.write(
            "revali.toml",
            self.read("revali.toml").replace('lint = ""', 'lint = "%s -c pass"' % PY),
        )
        self.commit_all("lint line")
        self.ctx = preflight(self.repo)

    def test_a_timeout_is_a_pipeline_error(self):
        with mock.patch("revali.review.run_shell", side_effect=ProcTimeout("timed out")):
            with self.assertRaises(Stop) as cm:
                lint_check(self.ctx, [FILE], None)
        self.assertEqual(cm.exception.exit_code, EXIT_ERROR)  # AC-6
        self.assertIn("lint", cm.exception.message)

    def test_a_command_that_cannot_start_is_a_pipeline_error(self):
        with mock.patch("revali.review.run_shell", side_effect=ExeNotFound("no such exe")):
            with self.assertRaises(Stop) as cm:
                lint_check(self.ctx, [FILE], None)
        self.assertEqual(cm.exception.exit_code, EXIT_ERROR)  # AC-6
        self.assertIn("lint", cm.exception.message)

    def test_the_lint_runs_in_the_repo_root_with_the_review_timeout(self):
        green = Result(cmd=["x"], returncode=0, stdout="", stderr="", duration=0.1)
        with mock.patch("revali.review.run_shell", return_value=green) as rs:
            self.assertIsNone(lint_check(self.ctx, [FILE], None))
        rs.assert_called_once()
        kwargs = rs.call_args.kwargs
        self.assertEqual(kwargs["cwd"], self.ctx.repo_root)  # AC-1: in the repository root
        self.assertEqual(kwargs["timeout"], self.ctx.cfg.review.timeout_min * 60)  # AC-6

    def test_a_red_result_is_reported_with_the_output_tail(self):
        red = Result(
            cmd=["x"], returncode=1, stdout="would reformat a.py\n", stderr="", duration=0.1
        )
        with mock.patch("revali.review.run_shell", return_value=red):
            problem = lint_check(self.ctx, [FILE], None)
        self.assertIsNotNone(problem)
        self.assertIn("would reformat a.py", problem)  # AC-2: the output tail
        self.assertIn("-c pass", problem)  # AC-2: the command

    def test_no_file_means_no_run(self):
        with mock.patch("revali.review.run_shell") as rs:
            self.assertIsNone(lint_check(self.ctx, [], None))  # AC-1
        rs.assert_not_called()


class EmptyLine(RepoCase):
    def test_an_empty_line_means_no_run(self):
        ctx = preflight(self.repo)
        with mock.patch("revali.review.run_shell") as rs:
            self.assertIsNone(lint_check(ctx, [FILE], None))  # AC-1
        rs.assert_not_called()


if __name__ == "__main__":
    unittest.main()
