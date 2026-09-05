"""Reviewer acceptance tests for feature/skip-unchanged-suite: round-1 validation leaves the
existing suite (`test`) out when only the reviewer's test commits followed the commit the
baseline passed on (AC-2), the baseline records that commit and a failed or skipped baseline
records nothing (AC-1), a fix round, an author commit under test_dir, a rewritten history or
a state without the commit bring `test` back (AC-3), `[validate] reuse_baseline = false`
runs it every time (AC-4), and with no reviewer file nothing runs and the run says so (AC-5).
End to end through the CLI with the fake runner, real git."""

import contextlib
import io
import json
import re
import unittest
from unittest import mock

from revali import EXIT_ACTION, EXIT_ERROR, EXIT_OK
from revali.state import State
from tests.helpers import RepoCase, approve_response, claude_entry, git, run_cli

RDIR = ".revali/feature__mul"
LOGS = RDIR + "/logs"
REVALI_LOG = LOGS + "/revali.log"
NOTE = "existing suite not rerun"


def finding():
    return {
        "id": "F1",
        "file": "src/calc.py",
        "line": 3,
        "severity": "high",
        "kind": "correctness",
        "text": "mul ignores negative numbers",
        "suggestion": "handle them",
    }


def no_tests_response():
    return approve_response(
        tests=[],
        not_testable=[
            {"ac": "AC-1", "reason": "covered by the existing suite"},
            {"ac": "AC-2", "reason": "covered by the existing suite"},
        ],
    )


class ReuseCase(RepoCase):
    def steps(self, label):
        """The step names of every fake sandbox session run under `label`, in order."""
        return [c["steps"] for c in self.fake_calls("runner") if c["label"] == label]

    def head(self, ref="HEAD"):
        return git(["rev-parse", ref], self.repo).strip()

    def set_toml(self, key, value):
        toml = self.read("revali.toml")
        new, n = re.subn(
            r"^%s = .*$" % re.escape(key), "%s = %s" % (key, json.dumps(value)), toml, flags=re.M
        )
        self.assertEqual(n, 1, toml)
        self.write("revali.toml", new)
        self.commit_all("configure %s" % key)

    def set_reuse(self, value):
        """Add `reuse_baseline = <value>` to the fixture's [validate] table and commit it."""
        toml = self.read("revali.toml")
        self.assertIn("[validate]\n", toml)
        self.write(
            "revali.toml",
            toml.replace("[validate]\n", "[validate]\nreuse_baseline = %s\n" % value),
        )
        self.commit_all("set reuse_baseline")

    def validation_section(self, number):
        text = self.read(RDIR + "/tests.md")
        marker = "## Validation %d" % number
        self.assertIn(marker, text)
        rest = text.split(marker, 1)[1]
        return rest.split("\n## ", 1)[0]

    def validate_lines(self):
        return [ln for ln in self.read(REVALI_LOG).splitlines() if "] validate: " in ln]

    def fix_round(self):
        """Round 1 asks for changes and commits its tests; the author commits a fix; round 2
        approves without new files. Returns (code, out) of the second run."""
        cr = approve_response(verdict="CHANGES_REQUESTED", findings=[finding()])
        ok = approve_response(
            previous_findings=[{"id": "F1", "status": "resolved", "note": "fixed"}]
        )
        self.claude(claude_entry(cr), claude_entry(ok, write_tests=False))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ACTION, out)
        self.write("src/calc.py", self.read("src/calc.py") + "\n# handles negatives\n")
        self.commit_all("fix negatives")
        self.write(RDIR + "/response-1.md", "- F1: fixed in the last commit\n")
        return run_cli(["run", "--foreground"])


class RoundOneReusesTheBaseline(ReuseCase):
    def test_validation_runs_only_the_new_tests_and_says_why(self):
        self.claude(claude_entry())
        reviewed = self.head()  # the commit the baseline will run on
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        # AC-2: the suite ran once, in the baseline; validation ran the new tests only
        self.assertEqual(self.steps("baseline"), [["test"]])
        self.assertEqual(self.steps("validate-r1"), [["new_test"]])
        self.assertFalse(self.exists(LOGS + "/validate-r1-test.log"))
        self.assertTrue(self.exists(LOGS + "/validate-r1-new_test.log"))
        # AC-1: the state names the commit the baseline passed on (HEAD before the reviewer's
        # test commit)
        state = State.load(self.rdir())
        self.assertEqual(state.baseline_sha, reviewed)
        self.assertEqual(self.head("HEAD~1"), reviewed)
        self.assertEqual(state.validations[0]["result"], "PASS")
        # AC-2: a `validate:` stage line says so and names the baseline commit
        lines = [ln for ln in self.validate_lines() if NOTE in ln]
        self.assertEqual(len(lines), 1, self.validate_lines())
        self.assertIn(reviewed[:10], lines[0])
        # AC-2: the run line lists the steps that ran, without `test`
        run_line = [
            ln for ln in self.validate_lines() if "run 1:" in ln and "(validate-r1)" in ln
        ]
        self.assertEqual(len(run_line), 1, self.validate_lines())
        self.assertNotRegex(run_line[0], r"run 1: .*\btest\b, ")
        self.assertIn("new_test", run_line[0])
        # AC-2: tests.md and the PR comment carry the same note
        section = self.validation_section(1)
        self.assertIn(NOTE, section)
        self.assertIn(reviewed[:10], section)
        self.assertNotIn("| test |", section)
        self.assertIn("| new_test |", section)
        comment = self.read(LOGS + "/comment-validate-1.md")
        self.assertIn(NOTE, comment)
        self.assertNotIn("| test |", comment)
        self.assertIn("| new_test |", comment)

    def test_round_two_after_a_question_still_reuses_it(self):
        """Round 1 has questions and commits nothing; the author answers without a commit;
        round 2 approves and commits its tests. Every commit since the baseline is the
        reviewer's, so AC-2 applies to validation 1 even though it comes in the second run."""
        asks = approve_response(verdict="NEEDS_INFO", questions=["Are floats in scope?"], tests=[])
        self.claude(claude_entry(asks, write_tests=False), claude_entry())
        reviewed = self.head()
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ACTION, out)
        self.write(RDIR + "/response-1.md", "- floats: no\n")
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertEqual(self.steps("baseline"), [["test"]])  # ran once, in the first run
        self.assertEqual(self.steps("validate-r2"), [["new_test"]])
        self.assertEqual(State.load(self.rdir()).baseline_sha, reviewed)
        self.assertIn(NOTE, self.validation_section(1))

class TheBaselineRecordsItsCommit(ReuseCase):
    def test_a_failed_baseline_records_nothing(self):
        self.runner_scenario({"default": 0, "results": {"baseline": {"test": 1}}})
        self.claude(claude_entry())
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR, out)  # the branch is broken before review
        state = State.load(self.rdir())
        self.assertIsNotNone(state)
        self.assertEqual(state.baseline_sha, "")  # AC-1
        self.assertEqual(self.steps("validate-r1"), [])

    def test_no_test_command_records_nothing_and_validation_runs_the_new_tests(self):
        self.set_toml("test", "")
        self.claude(claude_entry())
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertEqual(self.steps("baseline"), [])  # skipped: nothing to run
        self.assertEqual(State.load(self.rdir()).baseline_sha, "")  # AC-1
        self.assertEqual(self.steps("validate-r1"), [["new_test"]])
        self.assertNotIn(NOTE, self.read(REVALI_LOG))  # nothing was reused

    def test_a_passed_baseline_is_recorded_before_the_review_starts(self):
        """The sha is on disk once the baseline passed, so a run that dies later still has
        it: the reviewer session fails here, after the baseline."""
        self.claude(claude_entry(is_error=True, exit=1))
        reviewed = self.head()
        code, out = run_cli(["run", "--foreground"])
        self.assertNotEqual(code, EXIT_OK, out)
        self.assertEqual(self.steps("baseline"), [["test"]])
        self.assertEqual(State.load(self.rdir()).baseline_sha, reviewed)  # AC-1


class TheFullSuiteComesBack(ReuseCase):
    def test_a_fix_round_runs_test_and_new_test(self):
        code, out = self.fix_round()
        self.assertEqual(code, EXIT_OK, out)
        self.assertEqual(self.steps("baseline"), [["test"]])  # once, in the first run
        self.assertEqual(self.steps("validate-r2"), [["test", "new_test"]])  # AC-3
        self.assertNotIn(NOTE, self.read(REVALI_LOG))
        section = self.validation_section(1)
        self.assertNotIn(NOTE, section)
        self.assertIn("| test |", section)
        self.assertIn("| new_test |", section)
        self.assertNotIn(NOTE, self.read(LOGS + "/comment-validate-1.md"))
        self.assertEqual(State.load(self.rdir()).fixes, 1)

    def test_an_author_commit_under_test_dir_without_the_trailer_runs_the_suite(self):
        """The author's fix touches only tests/: the paths alone would allow the reuse, the
        missing trailer must not (AC-3: any commit since the baseline lacking the trailer)."""
        cr = approve_response(verdict="CHANGES_REQUESTED", findings=[finding()])
        ok = approve_response(
            previous_findings=[{"id": "F1", "status": "wontfix", "note": "by design"}]
        )
        self.claude(claude_entry(cr), claude_entry(ok, write_tests=False))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ACTION, out)
        self.write("tests/test_calc.py", self.read("tests/test_calc.py") + "\n# author's edit\n")
        self.commit_all("touch the existing suite")
        self.write(RDIR + "/response-1.md", "- F1: wontfix: by design\n")
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertEqual(self.steps("validate-r2"), [["test", "new_test"]])  # AC-3
        self.assertNotIn(NOTE, self.read(REVALI_LOG))

    def test_a_rewritten_history_runs_the_suite_in_the_restarted_round(self):
        """The author drops the reviewer's commit and redoes theirs: the baseline's commit is
        gone from HEAD, the review starts over, and validation runs `test` (AC-3)."""
        cr = approve_response(verdict="CHANGES_REQUESTED", findings=[finding()])
        self.claude(claude_entry(cr), claude_entry())
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ACTION, out)
        self.assertTrue(State.load(self.rdir()).baseline_sha)
        git(["reset", "-q", "--hard", "HEAD~1"], self.repo)  # drop the reviewer's test commit
        self.write("src/calc.py", self.read("src/calc.py") + "\n# redo\n")
        self.commit_all("redo")
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("starts over", out)
        self.assertEqual(self.steps("baseline"), [["test"]])  # not rerun: rounds existed
        self.assertEqual(self.steps("validate-r1"), [["test", "new_test"]])  # AC-3
        self.assertNotIn(NOTE, self.read(REVALI_LOG))

    def test_a_state_without_the_baseline_commit_runs_the_suite(self):
        """A state written before this rule (no `baseline_sha`), or one whose baseline never
        ran: the validation that resumes an approved round runs `test` (AC-3)."""
        self.claude(claude_entry())
        with mock.patch("revali.validate.run_validation", side_effect=RuntimeError("power cut")):
            with contextlib.redirect_stderr(io.StringIO()):
                code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR, out)
        path = State.path(self.rdir())
        with open(path, "r", encoding="utf-8", newline="") as fh:
            data = json.load(fh)
        self.assertTrue(data.get("baseline_sha"))  # recorded by the first run
        del data["baseline_sha"]  # an older state file layout
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(data, fh, indent=2)
        self.scenario(
            {
                "prs_open": [
                    {
                        "number": 7,
                        "url": "https://github.example/me/sample/pull/7",
                        "isDraft": True,
                        "title": "Add mul to calc",
                    }
                ]
            }
        )
        self.claude()
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertEqual(self.steps("validate-r1"), [["test", "new_test"]])  # AC-3
        self.assertNotIn(NOTE, self.read(REVALI_LOG))


class TheKeyTurnsItOff(ReuseCase):
    def test_reuse_baseline_false_runs_the_suite_in_round_one(self):
        self.set_reuse("false")
        self.claude(claude_entry())
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertEqual(self.steps("baseline"), [["test"]])
        self.assertEqual(self.steps("validate-r1"), [["test", "new_test"]])  # AC-4
        self.assertNotIn(NOTE, self.read(REVALI_LOG))
        self.assertNotIn(NOTE, self.read(RDIR + "/tests.md"))
        self.assertIn("| test |", self.validation_section(1))

    def test_reuse_baseline_true_is_accepted_and_is_the_default(self):
        self.set_reuse("true")
        self.claude(claude_entry())
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertEqual(self.steps("validate-r1"), [["new_test"]])  # AC-4: same as unset


class NothingToValidate(ReuseCase):
    def test_no_reviewer_file_and_an_unchanged_suite_runs_nothing_and_passes(self):
        self.set_toml("new_test", "run-new {files}")
        self.claude(claude_entry(no_tests_response(), write_tests=False))
        reviewed = self.head()
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)  # AC-5: passes
        self.assertIn("READY TO MERGE", out)
        self.assertEqual(self.head(), reviewed)  # nothing was committed
        self.assertEqual(self.steps("validate-r1"), [])  # AC-5: no step, no session
        self.assertFalse(self.exists(LOGS + "/validate-r1-test.log"))
        self.assertFalse(self.exists(LOGS + "/validate-r1-new_test.log"))
        state = State.load(self.rdir())
        self.assertEqual(state.stage, "ready_to_merge")
        self.assertEqual(state.validations[0]["result"], "PASS")
        self.assertEqual(state.baseline_sha, reviewed)
        # AC-5: the note says nothing changed since the baseline that passed
        section = self.validation_section(1)
        self.assertIn("nothing to run", section)
        self.assertIn(NOTE, section)
        self.assertIn(reviewed[:10], section)
        self.assertIn("no new test file", section)
        log = self.read(REVALI_LOG)
        self.assertIn("nothing to run", log)
        self.assertIn(NOTE, log)
        self.assertIn("nothing to run", self.read(LOGS + "/comment-validate-1.md"))
        self.assertIn("nothing to run", out)  # the READY TO MERGE line carries the reason

    def test_no_reviewer_file_with_the_key_off_still_runs_the_suite(self):
        self.set_toml("new_test", "run-new {files}")
        self.set_reuse("false")
        self.claude(claude_entry(no_tests_response(), write_tests=False))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertEqual(self.steps("validate-r1"), [["test"]])  # AC-4 with AC-5's input
        self.assertNotIn("nothing to run", self.read(RDIR + "/tests.md"))


if __name__ == "__main__":
    unittest.main()
