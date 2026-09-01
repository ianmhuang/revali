"""Reviewer tests for fix/needs-info-files (AC-1..AC-7): a NEEDS_INFO round's uncommitted
test files are recorded in the state, tolerated by the clean-tree check (and only they),
handed to the next round, smoke-run and committed or removed by it, and swept with the
other leftovers when that round is interrupted. Black-box through the CLI and the state
file; AC-6 pokes the module boundary because the criterion is about it."""
import importlib
import json
import os
import unittest

from tests.helpers import ROOT, RepoCase, TEST_REVIEW_MUL, approve_response, claude_entry, git, run_cli
from revali import EXIT_ACTION, EXIT_ERROR, EXIT_OK, PROMPT_VERSION, STATE_VERSION
from revali.state import State

MUL = "tests/test_review_mul.py"
SPACED = "tests/test_review_my topic.py"          # a pending path with a space: no quoting games
OTHER = "tests/test_review_other.py"
LEFT = "tests/test_review_left.py"
OTHER_TEXT = TEST_REVIEW_MUL.replace("MulTests", "OtherTests")
SPACED_TEXT = TEST_REVIEW_MUL.replace("MulTests", "SpacedTests")


def needs_info(files=None):
    """A NEEDS_INFO answer writing `files` (path -> text). None: the stub's default MUL file;
    {}: nothing."""
    data = approve_response(verdict="NEEDS_INFO", questions=["Which integers?"], tests=[])
    if files is None:
        return claude_entry(data)
    entry = claude_entry(data, write_tests=False)
    if files:
        entry["write_files"] = dict(files)
    return entry


def approving(files, delete=()):
    """An APPROVE answer that writes `files`, lists them as its tests, and deletes `delete`."""
    tests = [{"path": p, "purpose": "acceptance", "covers": ["AC-1", "AC-2"], "expected": "per AC"}
             for p in files]
    data = approve_response(tests=tests)
    if not files:
        data["not_testable"] = [{"ac": "AC-1", "reason": "covered by the existing suite"},
                                {"ac": "AC-2", "reason": "covered by the existing suite"}]
    entry = claude_entry(data, write_tests=False)
    if files:
        entry["write_files"] = dict(files)
    if delete:
        entry["delete_files"] = list(delete)
    return entry


def blocking_finding():
    return {"id": "F1", "file": "src/calc.py", "line": 3, "severity": "high", "kind": "correctness",
            "text": "mul ignores negative numbers", "suggestion": "handle them"}


def offenders(out):
    """The paths listed after the 'not clean' line of an ERROR message."""
    if "not clean" not in out:
        return ""
    return out.split("not clean", 1)[1]


class PendingCase(RepoCase):
    def needs_info_round(self, files=None):
        self.claude(needs_info(files))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ACTION, out)
        return out

    def state(self):
        return State.load(self.rdir())

    def state_json(self):
        with open(State.path(self.rdir()), "r", encoding="utf-8") as fh:
            return json.load(fh)

    def status(self):
        return git(["status", "--porcelain"], self.repo)

    def committed_in(self, sha):
        out = git(["show", "--name-only", "--format=", sha], self.repo)
        return sorted(l.strip() for l in out.splitlines() if l.strip())

    def prompts(self):
        return [c["prompt"] for c in self.fake_calls("claude")]

    def smoke_runs(self):
        return [c for c in self.fake_calls("runner") if c.get("label", "").startswith("smoke-")]


class Recorded(PendingCase):
    """AC-1: the state file lists the round's files; the ACTION NEEDED message names them."""

    def test_needs_info_records_its_files_and_tells_the_author(self):
        out = self.needs_info_round({MUL: TEST_REVIEW_MUL, SPACED: SPACED_TEXT})
        self.assertEqual(sorted(self.state().pending_test_files), sorted([SPACED, MUL]))           # AC-1: recorded
        self.assertIn("pending_test_files", self.state_json())
        self.assertIn("?? " + MUL, self.status())                                          # still uncommitted
        self.assertIn(SPACED.split("/", 1)[1], self.status())
        self.assertEqual(self.state().test_commits, [])
        action = out.split("ACTION NEEDED", 1)[1]
        self.assertIn(MUL, action)                                                         # AC-1: named
        self.assertIn(SPACED, action)
        self.assertIn("do not commit", action.lower())
        self.assertGreaterEqual(STATE_VERSION, 3)
        self.assertFalse(self.state().reviewer_running)

    def test_needs_info_that_wrote_nothing_records_an_empty_list(self):
        out = self.needs_info_round({})
        self.assertEqual(self.state().pending_test_files, [])                              # AC-1: empty list
        self.assertEqual(self.state_json()["pending_test_files"], [])
        self.assertNotIn("do not commit", out.lower())
        self.assertEqual(self.status().strip(), "")


class Tolerated(PendingCase):
    """AC-2 / AC-5: exactly the pending files may be dirty; anything else still stops the run
    and leaves the pending files and the list alone."""

    def test_preflight_dry_run_and_run_accept_the_pending_files(self):
        self.needs_info_round({MUL: TEST_REVIEW_MUL, SPACED: SPACED_TEXT})
        code, out = run_cli(["preflight"])
        self.assertEqual(code, EXIT_OK, out)                                               # AC-2: preflight
        self.assertIn("preflight OK", out)
        code, out = run_cli(["run", "--dry-run"])
        self.assertEqual(code, EXIT_OK, out)                                               # AC-2: dry run
        self.assertNotIn("removed", out)
        self.assertTrue(self.exists(MUL) and self.exists(SPACED))
        self.assertEqual(sorted(self.state().pending_test_files), sorted([SPACED, MUL]))           # untouched
        self.claude(approving({MUL: TEST_REVIEW_MUL, SPACED: SPACED_TEXT}))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)                                               # AC-2: run
        self.assertNotIn("not clean", out)

    def test_another_untracked_file_under_test_dir_still_stops_the_run(self):
        self.needs_info_round()
        self.write(OTHER, "# the author's own file, on the pattern\n")
        self.claude(approving({MUL: TEST_REVIEW_MUL}))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR, out)                                            # AC-2: other path
        self.assertIn("not clean", out)
        self.assertIn(OTHER, offenders(out))
        self.assertNotIn(MUL, offenders(out))                                              # AC-2: not an offender
        self.assertEqual(len(self.fake_calls("claude")), 1)                                # reviewer not reached
        self.assertTrue(self.exists(MUL))                                                  # AC-5: file kept
        self.assertEqual(self.state().pending_test_files, [MUL])                           # AC-5: list kept
        code, out = run_cli(["preflight"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertNotIn(MUL, offenders(out))
        os.remove(os.path.join(self.repo, OTHER))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)                                               # AC-5: tolerated again
        self.assertEqual(self.state().pending_test_files, [])

    def test_a_modified_tracked_file_outside_test_dir_still_stops_the_run(self):
        self.needs_info_round()
        self.write("src/calc.py", self.read("src/calc.py") + "# dirty\n")
        self.claude(approving({MUL: TEST_REVIEW_MUL}))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR, out)                                            # AC-2
        self.assertIn("src/calc.py", offenders(out))
        self.assertNotIn(MUL, offenders(out))
        self.assertTrue(self.exists(MUL))
        self.assertEqual(self.state().pending_test_files, [MUL])                           # AC-5

    def test_a_draft_change_md_stops_before_the_tree_check_and_keeps_the_list(self):
        self.needs_info_round()
        doc = self.read(self.change_md())
        self.write(self.change_md(), doc.replace("author_model: fixture\n", "author_model: fixture\nstatus: draft\n", 1))
        self.claude(approving({MUL: TEST_REVIEW_MUL}))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR, out)                                            # AC-5: draft
        self.assertIn("draft", out)
        self.assertTrue(self.exists(MUL))
        self.assertEqual(self.state().pending_test_files, [MUL])
        self.write(self.change_md(), doc)
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)                                               # AC-5: next run tolerates
        self.assertEqual(self.state().pending_test_files, [])
        self.assertEqual(self.status().strip(), "")

    def test_a_modified_file_of_the_reviewers_own_earlier_round_is_tolerated(self):
        self.claude(claude_entry(approve_response(verdict="CHANGES_REQUESTED", findings=[blocking_finding()])))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ACTION, out)
        self.assertEqual(self.state().test_files, [MUL])                                   # committed in round 1
        self.write("src/calc.py", self.read("src/calc.py") + "\n# negatives handled\n")
        self.commit_all("fix: handle negatives")
        self.needs_info_round({MUL: TEST_REVIEW_MUL + "\n# updated by round 2\n"})
        self.assertIn(" M " + MUL, self.status())                                          # tracked, modified
        self.assertEqual(self.state().pending_test_files, [MUL])                           # AC-1
        code, out = run_cli(["preflight"])
        self.assertEqual(code, EXIT_OK, out)                                               # AC-2: modified own file
        self.claude(claude_entry(write_tests=False))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)                                               # AC-3: committed
        state = self.state()
        self.assertEqual(state.pending_test_files, [])
        self.assertEqual(len(state.test_commits), 2)
        self.assertEqual(self.committed_in(state.test_commits[-1]), [MUL])
        self.assertTrue(git(["show", "HEAD:" + MUL], self.repo).endswith("# updated by round 2\n"))
        self.assertEqual(self.status().strip(), "")


class NextRound(PendingCase):
    """AC-3: the next prompt lists the pending files; what remains is smoke-run and committed
    with that round's other files; a deleted one is simply gone."""

    def test_prompt_lists_the_pending_files_and_the_round_commits_what_remains(self):
        self.needs_info_round({MUL: TEST_REVIEW_MUL, SPACED: SPACED_TEXT})
        self.assertNotIn(SPACED, self.prompts()[-1])                                       # not yet written then
        self.write(".revali/feature__mul/response-1.md", "- integers: any Python int\n")
        self.claude(approving({MUL: TEST_REVIEW_MUL, OTHER: OTHER_TEXT}, delete=[SPACED]))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        prompt = self.prompts()[-1]
        self.assertIn("- " + MUL, prompt)                                                  # AC-3: listed
        self.assertIn("- " + SPACED, prompt)                                               # only the pending list can name it
        self.assertRegex(prompt, r"not committed|uncommitted")                             # AC-3: as uncommitted, its own
        self.assertIn("previous round", prompt)
        self.assertGreaterEqual(int(PROMPT_VERSION), 5)
        smoke = self.smoke_runs()
        self.assertEqual(len(smoke), 1, smoke)
        self.assertEqual(smoke[0]["extra_files"], [MUL, OTHER])                            # AC-3: smoke-run together
        state = self.state()
        self.assertEqual(state.pending_test_files, [])                                     # AC-3: empty afterwards
        self.assertEqual(len(state.test_commits), 1)
        self.assertEqual(self.committed_in(state.test_commits[-1]), [MUL, OTHER])          # AC-3: one commit
        self.assertEqual(sorted(state.test_files), [MUL, OTHER])
        self.assertFalse(self.exists(SPACED))                                              # AC-3: deleted one is gone
        self.assertEqual(self.status().strip(), "")
        self.assertEqual(state.stage, "ready_to_merge")

    def test_a_pending_file_the_reviewer_deleted_with_nothing_new_leaves_a_clean_tree(self):
        self.needs_info_round()
        self.claude(approving({}, delete=[MUL]))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertFalse(self.exists(MUL))                                                 # AC-3: gone
        state = self.state()
        self.assertEqual(state.pending_test_files, [])
        self.assertEqual(state.test_commits, [])                                           # nothing to commit
        self.assertEqual(state.test_files, [])
        self.assertEqual(self.smoke_runs(), [])
        self.assertEqual(self.status().strip(), "")


class Interrupted(PendingCase):
    """AC-4: the round after NEEDS_INFO stops before its commit; the pending files go with
    the other unfinished files and the list is cleared."""

    def test_a_failed_session_removes_the_pending_files_and_clears_the_list(self):
        self.needs_info_round({MUL: TEST_REVIEW_MUL, SPACED: SPACED_TEXT})
        self.claude(claude_entry(write_tests=False, is_error=True, exit=1))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertFalse(self.exists(MUL))                                                 # AC-4: removed
        self.assertFalse(self.exists(SPACED))
        self.assertIn("removed 2 unfinished test file", out)
        state = self.state()
        self.assertEqual(state.pending_test_files, [])                                     # AC-4: cleared
        self.assertFalse(state.reviewer_running)
        self.assertEqual(self.status().strip(), "")
        self.claude(approving({OTHER: OTHER_TEXT}))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)                                               # nothing left to tolerate
        self.assertEqual(self.state().test_files, [OTHER])

    def test_a_killed_session_is_cleaned_by_the_next_run_but_not_by_a_dry_run(self):
        self.needs_info_round()
        state = self.state()
        state.reviewer_running = True                  # what a kill mid-session leaves on disk
        state.set_stage(self.rdir(), "review", "killed", EXIT_ERROR)
        self.write(LEFT, "# half written by the killed session\n")
        code, out = run_cli(["run", "--dry-run"])
        self.assertEqual(code, EXIT_ERROR, out)                                            # LEFT is not tolerated
        self.assertIn(LEFT, offenders(out))
        self.assertNotIn(MUL, offenders(out))                                              # MUL still is
        self.assertTrue(self.exists(MUL) and self.exists(LEFT))                            # a dry run deletes nothing
        self.assertEqual(self.state().pending_test_files, [MUL])
        self.claude(approving({OTHER: OTHER_TEXT}))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("removed 2 unfinished test file", out)                               # AC-4: both named
        self.assertIn(MUL, out)
        self.assertIn(LEFT, out)
        self.assertFalse(self.exists(MUL))                                                 # AC-4: removed
        self.assertFalse(self.exists(LEFT))
        state = self.state()
        self.assertEqual(state.pending_test_files, [])                                     # AC-4: cleared
        self.assertFalse(state.reviewer_running)
        self.assertEqual(state.test_files, [OTHER])
        self.assertEqual(self.committed_in(state.test_commits[-1]), [OTHER])


class HookLivesInReview(PendingCase):
    """AC-6: review.py builds the interruption cleanup; pipeline.py no longer has its own copy;
    the cleanup itself behaves as before."""

    def test_review_builds_the_hook_and_pipeline_has_no_copy(self):
        review = importlib.import_module("revali.review")
        pipeline = importlib.import_module("revali.pipeline")
        self.assertTrue(hasattr(review, "interruption_cleanup"))                          # AC-6
        self.assertFalse(hasattr(pipeline, "_cleanup_after_interruption"))
        self.assertIsNone(review.interruption_cleanup(State(), self.rdir(), None))         # nothing interrupted
        flagged = State()
        flagged.reviewer_running = True
        flagged.pending_test_files = [MUL]
        hook = review.interruption_cleanup(flagged, self.rdir(), None)
        self.assertTrue(callable(hook))
        # the whole run: an interrupted previous round, a leftover, the next run sweeps and proceeds
        state = State()
        state.reviewer_running = True
        state.set_stage(self.rdir(), "stopped", "stopped by user", EXIT_ERROR)
        self.write(LEFT, "# half written\n")
        self.claude(claude_entry())
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)                                               # AC-6: unchanged
        self.assertFalse(self.exists(LEFT))
        self.assertIn(LEFT, out)
        self.assertIn("] run: removed", out)
        self.assertFalse(self.state().reviewer_running)


class ReadmeDescribesIt(unittest.TestCase):
    def test_section_states_the_needs_info_rule(self):
        with open(os.path.join(ROOT, "README.md"), "r", encoding="utf-8") as fh:
            text = fh.read()
        section = text.split("## What revali does to your repository", 1)[1].split("\n## ", 1)[0]
        self.assertIn("NEEDS_INFO round keeps its test files", section)                    # AC-7
        self.assertIn("uncommitted in `test_dir`", section)
        self.assertIn("tolerates exactly those paths", section)
        self.assertIn("next round commits or removes", section)
        self.assertIn("Do not commit them by hand", section)


if __name__ == "__main__":
    unittest.main()
