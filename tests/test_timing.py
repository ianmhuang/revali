"""revali.timing (stage and sandbox clocks) and the {files} expansion of new_test."""

import unittest

from revali.config import PlatformCfg
from revali.runners import files_argument, steps_for, steps_with_files, wants_files
from revali.timing import Timing, fmt_duration


class FakeClock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


class FormatDuration(unittest.TestCase):
    def test_units(self):
        self.assertEqual(fmt_duration(0), "0s")
        self.assertEqual(fmt_duration(0.4), "0s")
        self.assertEqual(fmt_duration(42), "42s")
        self.assertEqual(fmt_duration(500), "8m20s")
        self.assertEqual(fmt_duration(3723), "1h2m3s")
        self.assertEqual(fmt_duration(-5), "0s")


class StageClock(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.t = Timing(clock=self.clock)

    def advance(self, secs):
        self.clock.now += secs

    def test_stages_in_order_with_their_durations(self):
        self.t.stage("preflight")
        self.advance(500)
        self.t.stage("pr")
        self.advance(8)
        self.t.stage("review")
        self.advance(361)
        self.t.stage("validate")
        self.advance(708)
        self.t.stage("ready_to_merge")  # terminal: closes validate, starts nothing
        self.advance(99)
        self.assertEqual(
            self.t.as_dict()["stage_s"],
            {"preflight": 500.0, "pr": 8.0, "review": 361.0, "validate": 708.0},
        )
        self.assertEqual(list(self.t.stages), ["preflight", "pr", "review", "validate"])
        self.assertEqual(self.t.summary(), "preflight 8m20s, pr 8s, review 6m1s, validate 11m48s")

    def test_reentering_the_running_stage_keeps_its_clock(self):
        self.t.stage("preflight")
        self.advance(10)
        self.t.stage("preflight")  # the dry run sets preflight twice
        self.advance(5)
        self.t.close()
        self.assertEqual(self.t.as_dict()["stage_s"], {"preflight": 15.0})

    def test_a_stage_visited_twice_accumulates(self):
        self.t.stage("review")
        self.advance(10)
        self.t.stage("validate")
        self.advance(1)
        self.t.stage("review")
        self.advance(5)
        self.t.close()
        self.assertEqual(self.t.as_dict()["stage_s"], {"review": 15.0, "validate": 1.0})

    def test_close_is_idempotent_and_terminal_without_a_stage_is_harmless(self):
        self.t.stage("error")
        self.t.close()
        self.t.close()
        self.assertEqual(self.t.as_dict(), {"stage_s": {}, "sandbox_s": {}})
        self.assertEqual(self.t.summary(), "no stage ran")

    def test_sandbox_sessions_in_the_summary_and_the_dict(self):
        self.t.stage("preflight")
        self.t.sandbox("baseline", 499.4)
        self.advance(1)
        self.t.stage("review")
        self.t.sandbox("smoke-r1-1", 262.06)
        self.t.stage("needs_action")
        d = self.t.as_dict()
        self.assertEqual(d["sandbox_s"], {"baseline": 499.4, "smoke-r1-1": 262.1})
        self.assertEqual(
            self.t.summary(),
            "preflight 1s, review 0s; sandbox baseline 8m19s, smoke-r1-1 4m22s",
        )


def _plat(new_test, test="run-all"):
    return PlatformCfg(name="linux", setup="", build="", test=test, new_test=new_test)


class NewTestFiles(unittest.TestCase):
    def test_wants_files(self):
        self.assertTrue(wants_files("pytest -q {files}"))
        self.assertFalse(wants_files("pytest -q tests"))

    def test_files_argument_quotes_only_whitespace_and_uses_forward_slashes(self):
        self.assertEqual(
            files_argument(["tests/test_review_a.py", "tests\\sub\\test_review_b.py"]),
            "tests/test_review_a.py tests/sub/test_review_b.py",
        )
        self.assertEqual(
            files_argument(["my tests/test_review_a.py"]), '"my tests/test_review_a.py"'
        )
        self.assertEqual(files_argument([]), "")

    def test_a_quoted_path_escapes_what_a_posix_shell_reads_inside_double_quotes(self):
        self.assertEqual(
            files_argument(['my $dir/te"st`/test_review_a.py']),
            '"my \\$dir/te\\"st\\`/test_review_a.py"',
        )
        # no whitespace: no quotes, nothing escaped
        self.assertEqual(files_argument(["my$dir/test_review_a.py"]), "my$dir/test_review_a.py")

    def test_steps_for_expands_only_new_test(self):
        plat = _plat("pytest -q {files}", test="pytest -q {files}")
        steps = dict(steps_for(plat, ["test", "new_test"], files=["tests/t.py"]))
        self.assertEqual(steps["new_test"], "pytest -q tests/t.py")
        self.assertEqual(steps["test"], "pytest -q {files}")  # only new_test knows the files

    def test_steps_for_without_files_argument_leaves_the_command_alone(self):
        plat = _plat("pytest -q {files}")
        self.assertEqual(dict(steps_for(plat, ["new_test"]))["new_test"], "pytest -q {files}")

    def test_steps_for_without_placeholder_is_unchanged(self):
        plat = _plat("pytest -q tests")
        self.assertEqual(
            dict(steps_for(plat, ["new_test"], files=["tests/t.py"]))["new_test"],
            "pytest -q tests",
        )

    def test_steps_with_files_skips_and_logs_when_no_file_is_named(self):
        plat = _plat("pytest -q {files}")
        lines = []
        steps = steps_with_files(
            plat, ["test", "new_test"], [], lambda st, msg: lines.append((st, msg)), "validate"
        )
        self.assertEqual([n for n, _ in steps], ["test"])
        self.assertEqual(lines, [("validate", "new_test skipped: {files} names no test file")])

    def test_steps_with_files_does_not_skip_a_plain_command(self):
        plat = _plat("pytest -q tests")
        lines = []
        steps = steps_with_files(plat, ["new_test"], [], lambda *a: lines.append(a), "review")
        self.assertEqual(steps, [("new_test", "pytest -q tests")])
        self.assertEqual(lines, [])

    def test_steps_with_files_drops_empty_commands(self):
        plat = _plat("pytest -q {files}", test="")
        steps = steps_with_files(plat, ["setup", "test", "new_test"], ["tests/t.py"], None, "x")
        self.assertEqual(steps, [("new_test", "pytest -q tests/t.py")])


if __name__ == "__main__":
    unittest.main()
