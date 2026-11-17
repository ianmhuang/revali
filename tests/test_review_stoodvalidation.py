"""Review of fix/merge-after-refusal: a validation that passed stays mergeable when a later
run stops before its PR stage (AC-1), that stop says so (AC-2), a run that starts its PR
stage or a new commit ends it (AC-3, AC-4), and a state from before the change reads as it
did (AC-5)."""

import json
import os
import re
import subprocess
import sys

from revali import EXIT_ACTION, EXIT_ERROR, EXIT_OK, STATE_VERSION
from revali.state import State, lock_path, tree_lock_path, write_json_atomic
from tests.fixtures.make_sample_repo import PY
from tests.helpers import RepoCase, approve_response, claude_entry, git, run_cli

LINT_SWITCH = "REVALI_TEST_LINT_FAIL"
LINT = "lint = " + json.dumps(
    "%s -c \"import os, sys; sys.exit(3 if os.environ.get('%s') else 0)\"" % (PY, LINT_SWITCH)
)
NOTE = re.compile(r"validation (\d+) still stands for HEAD ([0-9a-f]{7,40})")
PASSED = re.compile(r"validation (\d+) passed")
CHANGES_REQUESTED = approve_response(
    verdict="CHANGES_REQUESTED",
    findings=[
        {
            "id": "F1",
            "file": "src/calc.py",
            "line": 1,
            "severity": "high",
            "kind": "correctness",
            "text": "mul is wrong",
            "suggestion": "",
        }
    ],
)


class ReadyCase(RepoCase):
    """A branch whose run ended READY TO MERGE; its lint fails while LINT_SWITCH is set."""

    def setUp(self):
        super().setUp()
        os.environ["REVALI_POLL_SECONDS"] = "0.01"
        toml = self.read("revali.toml")
        self.assertIn('lint = ""', toml)
        self.write("revali.toml", toml.replace('lint = ""', LINT))
        self.commit_all("a lint command an environment variable can fail")
        self.claude(claude_entry())
        self.run_to_ready()

    def run_to_ready(self):
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("READY TO MERGE", out)
        state = State.load(self.rdir())
        self.assertEqual(state.stage, "ready_to_merge")
        self.passed = PASSED.search(state.message).group(1)  # the <n> a later note must name
        self.validated = self.head()

    def head(self):
        return git(["rev-parse", "HEAD"], self.repo).strip()

    def stopped_by_lint(self):
        os.environ[LINT_SWITCH] = "1"
        try:
            return run_cli(["run", "--foreground"])
        finally:
            del os.environ[LINT_SWITCH]

    def assert_note(self, text):
        found = NOTE.search(text)
        self.assertIsNotNone(found, text)
        self.assertEqual(found.group(1), self.passed, text)
        self.assertTrue(self.validated.startswith(found.group(2)), text)
        self.assertIn("revali merge", text)

    def assert_reported(self, stage, exit_code, words):
        """AC-2: the stop's own stage, message and exit code in the state, `wait` and
        `status`, with the note as the message's last line (what skill/SKILL.md tells the
        author session to look at)."""
        state = State.load(self.rdir())
        self.assertEqual(state.stage, stage)
        self.assertEqual(state.last_exit, exit_code)
        lines = state.message.split("\n")
        self.assertIn(words, lines[0])
        self.assertGreater(len(lines), 1, state.message)
        self.assert_note(lines[-1])
        code, out = run_cli(["wait", "--timeout", "1s"])
        self.assertEqual(code, exit_code, out)
        self.assertIn("%s: " % stage, out)
        self.assertIn(words, out)
        self.assert_note(out)
        code, out = run_cli(["status"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("stage: %s" % stage, out)
        self.assertIn(words, out)
        self.assert_note(out)

    def merge_calls(self):
        return [c for c in self.fake_calls("gh") if c.get("argv", [])[:2] == ["pr", "merge"]]

    def assert_merges(self):
        code, out = run_cli(["merge"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("MERGED: PR #", out)
        self.assertEqual(len(self.merge_calls()), 1, out)

    def assert_refused(self, words, exit_code=EXIT_ERROR):
        code, out = run_cli(["merge"])
        self.assertEqual(code, exit_code, out)
        self.assertIn(words, out)
        self.assertEqual(self.merge_calls(), [], out)
        return out

    def edit_state(self, **changes):
        path = State.path(self.rdir())
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        data.update(changes)
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(data, fh)

    def make_older(self, **changes):
        """The state file as the layout before this change wrote it."""
        path = State.path(self.rdir())
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        data.pop("validated_sha", None)
        data["version"] = 10
        data.update(changes)
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(data, fh)


class StopsBeforeThePrStage(ReadyCase):
    """AC-1, AC-2"""

    def test_dirty_tree_refused_before_detaching(self):
        self.write("stray.txt", "not committed\n")
        code, out = run_cli(["run"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertIn("working tree is not clean", out)
        self.assert_note(out)
        self.assert_reported("error", EXIT_ERROR, "working tree is not clean")
        # the dirty paths stay under the line that announces them; the note follows the list
        message = State.load(self.rdir()).message
        self.assertLess(message.index("stray.txt"), NOTE.search(message).start(), message)
        os.remove(os.path.join(self.repo, "stray.txt"))
        self.assert_merges()

    def test_a_refused_merge_leaves_the_branch_mergeable(self):
        code, out = self.stopped_by_lint()
        self.assertEqual(code, EXIT_ACTION, out)
        self.write("stray.txt", "not committed\n")
        self.assert_refused("working tree is not clean")
        # the merge's own refusal is what `status` shows now, and it still says what stands
        code, out = run_cli(["status"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("working tree is not clean", out)
        self.assert_note(out)
        os.remove(os.path.join(self.repo, "stray.txt"))
        self.assert_merges()

    def test_lint_failure(self):
        code, out = self.stopped_by_lint()
        self.assertEqual(code, EXIT_ACTION, out)
        self.assertIn("lint failed", out)
        self.assert_note(out)
        self.assert_reported("needs_action", EXIT_ACTION, "lint failed")
        self.assert_merges()

    def test_draft_change_md(self):
        with open(self.change_md(), encoding="utf-8", newline="") as fh:
            approved = fh.read()
        draft = approved.replace(
            "author_model: fixture\n", "author_model: fixture\nstatus: draft\n", 1
        )
        self.assertNotEqual(draft, approved)
        with open(self.change_md(), "w", encoding="utf-8", newline="") as fh:
            fh.write(draft)
        code, out = run_cli(["run", "--foreground"])
        self.assertNotEqual(code, EXIT_OK, out)
        self.assertIn("draft", out)
        self.assert_note(out)
        state = State.load(self.rdir())
        self.assertIn(state.stage, ("error", "needs_action"))
        self.assertEqual(state.last_exit, code)
        self.assert_note(state.message)
        with open(self.change_md(), "w", encoding="utf-8", newline="") as fh:
            fh.write(approved)
        self.assert_merges()

    def test_gh_logged_out(self):
        """A preflight stop with exit 1 that the run itself records (not the refusal before
        detaching)."""
        self.scenario({"auth_exit": 1})
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertIn("not logged in", out)
        self.assert_note(out)
        self.assert_reported("error", EXIT_ERROR, "not logged in")
        self.scenario({"auth_exit": 0})
        self.assert_merges()

    def test_two_stops_in_a_row(self):
        self.write("stray.txt", "not committed\n")
        code, out = run_cli(["run"])
        self.assertEqual(code, EXIT_ERROR, out)
        os.remove(os.path.join(self.repo, "stray.txt"))
        code, out = self.stopped_by_lint()
        self.assertEqual(code, EXIT_ACTION, out)
        self.assert_note(out)
        self.assert_reported("needs_action", EXIT_ACTION, "lint failed")
        self.assert_merges()

    def in_preflight(self):
        """What a later run leaves on disk while it is in preflight: no result yet, the
        stage still the earlier run's."""
        self.edit_state(last_exit=-1)

    def test_revali_stop_of_a_live_run_in_preflight(self):
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(120)"], start_new_session=True
        )
        self.addCleanup(lambda: child.poll() is None and child.kill())
        self.in_preflight()
        write_json_atomic(lock_path(self.rdir()), {"pid": child.pid, "since": "x"})
        write_json_atomic(
            tree_lock_path(self.repo, ".revali"),
            {"pid": child.pid, "branch": "feature/mul", "since": "x"},
        )
        code, out = run_cli(["stop"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("stopped pid %d" % child.pid, out)
        self.assertIsNotNone(child.wait(timeout=10))
        self.assert_reported("stopped", EXIT_ERROR, "stopped by user")
        self.assert_merges()

    def test_revali_stop_of_a_run_that_died_in_preflight(self):
        self.in_preflight()
        code, out = run_cli(["stop"])
        self.assertEqual(code, EXIT_OK, out)
        self.assert_reported("stopped", EXIT_ERROR, "found dead")
        self.assert_merges()


class WhatEndsIt(ReadyCase):
    """AC-3, AC-4"""

    def test_a_run_that_reviews_again_ends_it(self):
        self.claude(claude_entry(CHANGES_REQUESTED, write_tests=False))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ACTION, out)
        self.assertNotIn("still stands", out)
        self.assert_refused("not ready to merge")
        # a stop before the PR stage after that does not bring the validation back
        code, out = self.stopped_by_lint()
        self.assertEqual(code, EXIT_ACTION, out)
        self.assertNotIn("still stands", out)
        self.assertNotIn("still stands", State.load(self.rdir()).message)
        self.assert_refused("not ready to merge")

    def test_a_run_that_stops_in_its_pr_stage_ends_it(self):
        """AC-3, whatever that run's outcome: the run passed preflight and pushed, then found
        the PR closed (exit 1, no review). The cause going away does not bring the validation
        back."""
        self.scenario({"prs_all": [{"number": 7, "url": "u", "state": "CLOSED"}]})
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertIn("is closed", out)
        self.assertNotIn("still stands", out)
        state = State.load(self.rdir())
        self.assertEqual(state.stage, "error")
        self.assertNotIn("still stands", state.message)
        self.scenario({"prs_all": []})
        self.assert_refused("not ready to merge")
        code, out = run_cli(["status"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertNotIn("still stands", out)

    def test_a_run_that_ends_ready_again_counts_again(self):
        self.claude(claude_entry(approve_response(), write_tests=False))
        self.run_to_ready()
        code, out = self.stopped_by_lint()
        self.assertEqual(code, EXIT_ACTION, out)
        self.assert_note(out)  # names the validation of the second run
        self.assert_merges()

    def test_a_dry_run_leaves_it(self):
        code, out = run_cli(["run", "--dry-run"])
        self.assertEqual(code, EXIT_OK, out)
        self.assert_merges()

    def test_a_dry_run_against_another_base_leaves_the_merge_where_it_was(self):
        git(["branch", "release", "main"], self.repo)
        git(["push", "-q", "origin", "release"], self.repo)
        code, out = run_cli(["run", "--dry-run", "--base", "release"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("against release", out)
        code, out = run_cli(["merge"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("MERGED: PR #7 into main", out)  # the base the validation ran against
        self.assertNotIn("into release", out)
        self.assertEqual(len(self.merge_calls()), 1, out)

    def moved_words(self):
        return "HEAD moved since validation (%s -> %s)" % (self.validated[:10], self.head()[:10])

    def test_a_commit_after_ready(self):
        self.write("later.txt", "after validation\n")
        self.commit_all("after validation")
        self.assert_refused(self.moved_words(), EXIT_ACTION)

    def test_a_commit_then_a_stopped_run(self):
        self.write("later.txt", "after validation\n")
        self.commit_all("after validation")
        code, out = self.stopped_by_lint()
        self.assertEqual(code, EXIT_ACTION, out)
        self.assertIn("lint failed", out)
        self.assertNotIn("still stands", out)
        self.assertNotIn("still stands", State.load(self.rdir()).message)
        self.assert_refused(self.moved_words(), EXIT_ACTION)

    def test_a_stopped_run_then_a_commit(self):
        self.write("stray.txt", "not committed yet\n")
        code, out = run_cli(["run"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assert_note(out)
        self.commit_all("after validation")
        self.assert_refused(self.moved_words(), EXIT_ACTION)


class OlderStates(ReadyCase):
    """AC-5"""

    def test_state_version_is_11(self):
        self.assertEqual(STATE_VERSION, 11)
        with open(State.path(self.rdir()), encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["version"], 11)

    def test_an_older_ready_state_still_merges(self):
        self.make_older()
        self.assert_merges()

    def test_an_older_ready_state_refuses_a_moved_head(self):
        self.make_older()
        self.write("later.txt", "after validation\n")
        self.commit_all("after validation")
        moved = "HEAD moved since validation (%s -> %s)" % (
            self.validated[:10],
            self.head()[:10],
        )
        self.assert_refused(moved, EXIT_ACTION)

    def test_an_older_ready_state_survives_a_stopped_run(self):
        """A branch that was ready when revali was upgraded: its validation stands too."""
        self.make_older()
        code, out = self.stopped_by_lint()
        self.assertEqual(code, EXIT_ACTION, out)
        self.assert_note(out)
        self.assert_reported("needs_action", EXIT_ACTION, "lint failed")
        self.assert_merges()

    def test_an_older_state_that_was_not_ready_gains_nothing(self):
        self.make_older(stage="needs_action", last_exit=EXIT_ACTION)
        code, out = self.stopped_by_lint()
        self.assertEqual(code, EXIT_ACTION, out)
        self.assertNotIn("still stands", out)
        self.assertNotIn("still stands", State.load(self.rdir()).message)
        self.assert_refused("not ready to merge")

    def test_merged_or_closed_is_refused(self):
        for stage in ("merged", "closed"):
            with self.subTest(layout="current", stage=stage):
                self.edit_state(stage=stage)
                out = self.assert_refused("not ready to merge")
                self.assertIn("stage: %s" % stage, out)
        for stage in ("merged", "closed"):
            with self.subTest(layout="older", stage=stage):
                self.make_older(stage=stage)
                out = self.assert_refused("not ready to merge")
                self.assertIn("stage: %s" % stage, out)
