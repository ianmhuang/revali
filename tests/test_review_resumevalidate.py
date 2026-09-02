"""AC-4, AC-5 and AC-6 of fix/state-write-race: after a run recorded an APPROVE and died
before validating it, the next run at the same HEAD validates that round without a new
reviewer session (PR reused, no review comment, no round added, no review cost); a
different HEAD, a CHANGES_REQUESTED round, or a round that already has a validation gets a
normal new round; README, defaults.toml and the user template carry the new behaviour."""
import contextlib
import io
import os
import unittest
from unittest import mock

from tests.helpers import ROOT, RepoCase, approve_response, claude_entry, git, run_cli
from revali import EXIT_ACTION, EXIT_ERROR, EXIT_OK
from revali.state import State, read_history

PR_URL = "https://github.example/me/sample/pull/7"
DIAGNOSIS = {"summary": "add regressed.", "cause": "code",
             "failures": [{"test": "tests/test_calc.py::test_add", "cause": "code", "note": "7 != 12"}],
             "recommendation": "fix add"}


class ResumeCase(RepoCase):
    def approve_then_die_before_validation(self):
        """Round 1 approves and commits its tests; the process dies as validation starts."""
        self.claude(claude_entry())
        with mock.patch("revali.validate.run_validation", side_effect=RuntimeError("power cut")):
            with contextlib.redirect_stderr(io.StringIO()):
                code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR, out)
        state = State.load(self.rdir())
        self.assertEqual(state.stage, "error")
        self.assertEqual([r["verdict"] for r in state.rounds], ["APPROVE"])
        self.assertEqual(state.validations, [])
        self.assertEqual(state.head_sha, git(["rev-parse", "HEAD"], self.repo).strip())
        self.assertEqual(state.head_sha, state.test_commits[0])       # HEAD is the reviewer's test commit
        return state

    def rerun(self, *claude_entries):
        """The PR opened by the first run is now open on GitHub; the reviewer has nothing to say."""
        self.scenario({"prs_open": [{"number": 7, "url": PR_URL, "isDraft": True, "title": "Add mul to calc"}]})
        self.claude(*claude_entries)
        return run_cli(["run", "--foreground"])

    def gh(self, *prefix):
        return [c["argv"] for c in self.fake_calls("gh") if c["argv"][:len(prefix)] == list(prefix)]

    def comment_names(self):
        return [os.path.basename(argv[argv.index("--body-file") + 1]) for argv in self.gh("pr", "comment")]

    def revali_log(self):
        with open(os.path.join(self.rdir(), "logs", "revali.log"), "r", encoding="utf-8", newline="") as fh:
            return fh.read()


class ResumeAtValidation(ResumeCase):
    def test_the_rerun_validates_the_approved_round_without_a_new_review(self):
        self.approve_then_die_before_validation()
        self.assertEqual(self.comment_names(), ["comment-review-1.md"])
        code, out = self.rerun()
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("READY TO MERGE", out)                                                    # AC-4: the normal outcome
        self.assertIn("before validation", out)                                                 # AC-4: says why
        self.assertIn("round 1", out)                                                           # AC-4: names the round
        self.assertIn("before validation", self.revali_log())
        state = State.load(self.rdir())
        self.assertEqual(state.stage, "ready_to_merge")
        self.assertEqual(len(state.rounds), 1)                                                  # AC-4: rounds do not grow
        self.assertAlmostEqual(state.cost_usd, 0.5)                                             # AC-4: review not charged again
        self.assertEqual([v["round"] for v in state.validations], [1])
        self.assertEqual(state.validations[0]["result"], "PASS")
        self.assertEqual(state.last_verdict, "PASS")
        self.assertEqual(len(self.fake_calls("claude")), 1)                                     # AC-4: no reviewer session
        self.assertEqual([r["label"] for r in self.fake_calls("runner")],
                         ["baseline", "smoke-r1-1", "validate-r1"])
        self.assertEqual(len(self.gh("pr", "create")), 1)                                       # AC-4: the PR is reused
        self.assertIn("reusing open PR #7", out)
        self.assertEqual(state.pr_number, 7)
        self.assertEqual(self.comment_names(), ["comment-review-1.md", "comment-validate-1.md"])  # AC-4: no new review comment
        self.assertEqual(len(self.gh("pr", "ready")), 1)                                        # AC-4: marked ready
        rows = read_history(os.path.join(self.home, "history.jsonl"))
        self.assertEqual([r["exit"] for r in rows], [EXIT_ERROR, EXIT_OK])
        self.assertEqual(rows[-1]["rounds"], 1)

    def test_a_resumed_validation_that_fails_is_action_needed(self):
        self.approve_then_die_before_validation()
        self.runner_scenario({"default": 0, "results": {"validate-r1": {"test": 1}},
                              "outputs": {"validate-r1": {"test": "FAIL: test_add"}}})
        code, out = self.rerun(claude_entry(DIAGNOSIS, write_tests=False, model="claude-opus-5", cost=0.2))
        self.assertEqual(code, EXIT_ACTION, out)
        self.assertIn("ACTION NEEDED", out)                                                     # AC-4: as after a fresh APPROVE
        self.assertIn("before validation", out)
        state = State.load(self.rdir())
        self.assertEqual(state.stage, "needs_action")
        self.assertEqual(len(state.rounds), 1)
        self.assertEqual(state.validations[0]["result"], "FAIL")
        self.assertEqual(state.validations[0]["round"], 1)
        self.assertEqual(state.last_verdict, "FAIL")
        self.assertAlmostEqual(state.cost_usd, 0.7)                                             # review 0.5 + diagnosis 0.2
        self.assertEqual(len(self.fake_calls("claude")), 2)                                     # reviewer once, diagnoser once
        self.assertEqual(self.comment_names(), ["comment-review-1.md", "comment-validate-1.md"])


class NoResume(ResumeCase):
    def test_a_new_head_gets_a_new_round(self):
        self.approve_then_die_before_validation()
        self.write("src/calc.py", self.read("src/calc.py") + "\n# touched after the approval\n")
        self.commit_all("touch calc")
        code, out = self.rerun(claude_entry())
        self.assertEqual(code, EXIT_OK, out)
        self.assertNotIn("before validation", out)                                              # AC-5: no resume
        state = State.load(self.rdir())
        self.assertEqual(len(state.rounds), 2)                                                  # AC-5: a new round
        self.assertEqual(len(self.fake_calls("claude")), 2)
        self.assertEqual([v["round"] for v in state.validations], [2])
        self.assertAlmostEqual(state.cost_usd, 1.0)
        self.assertEqual(self.comment_names(), ["comment-review-1.md", "comment-review-2.md", "comment-validate-1.md"])

    def test_a_round_that_already_has_a_validation_is_not_resumed_again(self):
        self.approve_then_die_before_validation()
        code, out = self.rerun()
        self.assertEqual(code, EXIT_OK, out)
        self.assertEqual([v["round"] for v in State.load(self.rdir()).validations], [1])
        # same HEAD, round 1 validated: another run is a new round, not a third validation of round 1
        self.claude(claude_entry())
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertNotIn("before validation", out)                                              # AC-5
        state = State.load(self.rdir())
        self.assertEqual(len(state.rounds), 2)
        self.assertEqual([v["round"] for v in state.validations], [1, 2])
        self.assertEqual(len(self.fake_calls("claude")), 2)

    def test_a_changes_requested_round_is_never_resumed(self):
        finding = {"id": "F1", "file": "src/calc.py", "line": 3, "severity": "high", "kind": "correctness",
                   "text": "mul ignores negative numbers", "suggestion": "handle them"}
        self.claude(claude_entry(approve_response(verdict="CHANGES_REQUESTED", findings=[finding])))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ACTION, out)
        # make the state look like a run that died after that round, at the same HEAD
        state = State.load(self.rdir())
        state.stage, state.message, state.last_exit = "validate", "", -1
        state.save(self.rdir())
        code, out = self.rerun(claude_entry())
        self.assertEqual(code, EXIT_OK, out)
        self.assertNotIn("before validation", out)                                              # AC-5: verdict rules it out
        state = State.load(self.rdir())
        self.assertEqual([r["verdict"] for r in state.rounds], ["CHANGES_REQUESTED", "APPROVE"])
        self.assertEqual([v["round"] for v in state.validations], [2])
        self.assertEqual(len(self.fake_calls("claude")), 2)


class Documented(unittest.TestCase):
    def read(self, *parts):
        with open(os.path.join(ROOT, *parts), "r", encoding="utf-8", newline="") as fh:
            return fh.read()

    def test_readme_defaults_and_template_carry_the_change(self):
        readme = self.read("README.md")
        self.assertIn("without a result", readme)                                               # AC-6: the crash message
        self.assertIn("died at stage", readme)
        self.assertIn("validation", readme.split("without a result", 1)[1][:1500])              # AC-6: the resume, nearby
        self.assertIn("write_retry_s", readme)
        self.assertIn("write_retry_s", self.read("defaults.toml"))                              # AC-6: the constant
        self.assertIn("write_retry_s", self.read("templates", "user-config.toml"))


if __name__ == "__main__":
    unittest.main()
