"""fix/merge-after-refusal (M3.12): a validation that passed stays mergeable when a later run
stops before its PR stage, and stops counting once a run starts that stage or HEAD moves."""

import json
import os
from unittest import mock

from revali import EXIT_ACTION, EXIT_ERROR, EXIT_OK, STATE_VERSION
from revali.preflight import Stop
from revali.state import State
from tests.fixtures.make_sample_repo import PY
from tests.helpers import RepoCase, approve_response, claude_entry, git, run_cli

FAILING_LINT = (
    'lint = "%s -c \\"import os, sys; '
    "sys.exit(3 if os.environ.get('REVALI_TEST_LINT_FAIL') else 0)\\\"\"" % PY
)
CR = approve_response(
    verdict="CHANGES_REQUESTED",
    findings=[
        {
            "id": "F1",
            "file": "src/calc.py",
            "line": 1,
            "severity": "high",
            "kind": "correctness",
            "text": "t",
            "suggestion": "",
        }
    ],
)


class AfterReady(RepoCase):
    def setUp(self):
        super().setUp()
        os.environ["REVALI_POLL_SECONDS"] = "0.01"
        self.write("revali.toml", self.read("revali.toml").replace('lint = ""', FAILING_LINT))
        self.commit_all("lint that a variable can fail")
        self.claude(claude_entry())
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        self.validated = git(["rev-parse", "HEAD"], self.repo).strip()

    def validated_sha(self):
        with open(State.path(self.rdir()), encoding="utf-8") as fh:
            return json.load(fh).get("validated_sha")

    def test_ready_records_the_validated_head(self):
        self.assertEqual(self.validated_sha(), self.validated)

    def note(self):
        return "validation 1 still stands for HEAD %s: `revali merge` merges it" % (
            self.validated[:10]
        )

    def assert_stopped_with_note(self, out, stages=("error", "needs_action")):
        """AC-2: the stop's own result, plus the note, in the output and in the state."""
        state = State.load(self.rdir())
        self.assertIn(state.stage, stages)
        self.assertNotEqual(state.last_exit, EXIT_OK)
        self.assertIn(self.note(), out)
        self.assertIn(self.note(), state.message)
        code, status = run_cli(["status"])
        self.assertIn("stage: %s" % state.stage, status)

    def assert_merges(self):
        code, out = run_cli(["merge"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("MERGED: PR #7 into main", out)

    def assert_refused(self, words="not ready to merge"):
        code, out = run_cli(["merge"])
        self.assertNotEqual(code, EXIT_OK, out)
        self.assertIn(words, out)
        self.assertFalse(any(c["argv"][:2] == ["pr", "merge"] for c in self.fake_calls("gh")))

    # AC-1, AC-2 -----------------------------------------------------------------------------

    def test_the_refusal_before_detaching(self):
        self.write("stray.txt", "not committed\n")
        code, out = run_cli(["run"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assert_stopped_with_note(out, ("error",))
        os.remove(os.path.join(self.repo, "stray.txt"))
        self.assert_merges()

    def test_a_lint_failure(self):
        os.environ["REVALI_TEST_LINT_FAIL"] = "1"
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ACTION, out)
        self.assertIn("lint failed", out)
        self.assert_stopped_with_note(out)
        del os.environ["REVALI_TEST_LINT_FAIL"]
        self.assert_merges()

    def test_a_draft_change_md(self):
        with open(self.change_md(), encoding="utf-8", newline="") as fh:
            approved = fh.read()
        draft = approved.replace(
            "author_model: fixture\n", "author_model: fixture\nstatus: draft\n"
        )
        self.assertNotEqual(draft, approved)
        with open(self.change_md(), "w", encoding="utf-8", newline="") as fh:
            fh.write(draft)
        code, out = run_cli(["run", "--foreground"])
        self.assertNotEqual(code, EXIT_OK, out)
        self.assertIn("draft", out)
        self.assert_stopped_with_note(out)
        with open(self.change_md(), "w", encoding="utf-8", newline="") as fh:
            fh.write(approved)
        self.assert_merges()

    def test_a_stop_in_the_rerun_checks(self):
        mode = Stop(EXIT_ACTION, '[review] tests is "archive" but the 1 review round(s) ...')
        with mock.patch("revali.pipeline._rerun_bookkeeping", side_effect=mode):
            code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ACTION, out)
        self.assert_stopped_with_note(out, ("needs_action",))
        self.assert_merges()

    def test_revali_stop_after_a_run_died_in_preflight(self):
        state = State.load(self.rdir())
        state.last_exit = -1  # what a run that died before recording anything leaves
        state.save(self.rdir())
        code, out = run_cli(["stop"])
        self.assertEqual(code, EXIT_OK, out)
        state = State.load(self.rdir())
        self.assertEqual(state.stage, "stopped")
        self.assertIn(self.note(), state.message)
        self.assert_merges()

    def test_an_unexpected_error_in_preflight(self):
        with mock.patch("revali.pipeline.preflight", side_effect=RuntimeError("boom")):
            code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assert_stopped_with_note(out, ("error",))
        self.assert_merges()

    # AC-3 -----------------------------------------------------------------------------------

    def test_a_run_that_starts_its_pr_stage_ends_it(self):
        with mock.patch("revali.pr.ensure_pr", side_effect=Stop(EXIT_ERROR, "gh is down")):
            code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertNotIn("still stands", out)
        state = State.load(self.rdir())
        self.assertEqual(self.validated_sha(), "")
        self.assertNotIn("still stands", state.message)
        self.assert_refused()

    def test_a_new_review_ends_it(self):
        self.claude(claude_entry(CR, write_tests=False))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ACTION, out)
        self.assert_refused()

    def test_a_new_ready_records_its_own_head(self):
        self.claude(claude_entry(approve_response(), write_tests=False))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertEqual(self.validated_sha(), self.validated)
        self.assert_merges()

    def test_a_dry_run_leaves_it(self):
        code, out = run_cli(["run", "--dry-run"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertEqual(self.validated_sha(), self.validated)
        self.assert_merges()

    # AC-4 -----------------------------------------------------------------------------------

    def test_a_commit_after_ready(self):
        self.write("later.txt", "after validation\n")
        self.commit_all("after validation")
        moved = git(["rev-parse", "HEAD"], self.repo).strip()
        self.assert_refused(
            "HEAD moved since validation (%s -> %s)" % (self.validated[:10], moved[:10])
        )

    def test_a_commit_then_a_stopped_run(self):
        self.write("later.txt", "after validation\n")
        self.commit_all("after validation")
        os.environ["REVALI_TEST_LINT_FAIL"] = "1"
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ACTION, out)
        self.assertNotIn("still stands", out)
        self.assert_refused("HEAD moved since validation")

    # AC-5 -----------------------------------------------------------------------------------

    def test_a_state_from_before_the_field_still_merges(self):
        self.assertEqual(STATE_VERSION, 11)
        path = State.path(self.rdir())
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        data.pop("validated_sha")
        data["version"] = 10
        with open(path, "w", encoding="utf-8", newline="") as fh:
            json.dump(data, fh)
        self.assert_merges()

    def test_merged_or_closed_is_refused(self):
        for stage in ("merged", "closed"):
            with self.subTest(stage=stage):
                state = State.load(self.rdir())
                state.stage = stage
                state.save(self.rdir())
                self.assertEqual(self.validated_sha(), self.validated)
                self.assert_refused("stage: %s" % stage)

    def test_the_stage_table_says_when_merge_may_run(self):
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(here, "docs", "files.md"), encoding="utf-8") as fh:
            row = [line for line in fh if line.startswith("| `ready_to_merge` |")][0]
        self.assertIn("stopped before its `pr` stage", row)
        self.assertIn("validation <n> still stands for HEAD <sha>", row)
