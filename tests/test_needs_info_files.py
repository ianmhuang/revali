"""AC-1..AC-7 of fix/needs-info-files: a NEEDS_INFO round's uncommitted test files are
recorded in the state, tolerated by the clean-tree check, shown to the next round, committed
or removed by it, and dropped with the other leftovers when that round is interrupted."""

import os
import unittest

from revali import EXIT_ACTION, EXIT_ERROR, EXIT_OK, PROMPT_VERSION, STATE_VERSION, pipeline
from revali.preflight import Stop, locate, preflight
from revali.review import interruption_cleanup
from revali.state import State
from tests.helpers import (
    ROOT,
    TEST_REVIEW_MUL,
    RepoCase,
    approve_response,
    claude_entry,
    git,
    run_cli,
)

PENDING = "tests/test_review_mul.py"
SECOND = "tests/test_review_zero.py"
SECOND_TEXT = TEST_REVIEW_MUL.replace("MulTests", "ZeroTests")


def asking(write_tests=True):
    data = approve_response(verdict="NEEDS_INFO", questions=["Which integers?"], tests=[])
    return claude_entry(data, write_tests=write_tests)


def approving(**files):
    """An approving entry that writes exactly `files` (path -> text) and lists them as tests."""
    tests = [
        {"path": p, "purpose": "acceptance", "covers": ["AC-1", "AC-2"], "expected": "per AC"}
        for p in files
    ]
    entry = claude_entry(approve_response(tests=tests), write_tests=False)
    entry["write_files"] = dict(files)
    return entry


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


def error_line(out):
    return next((line for line in out.splitlines() if line.startswith("ERROR:")), "")


def changed_in(sha, repo):
    return git(["show", "--name-only", "--format=", sha], repo).split()


class NeedsInfoCase(RepoCase):
    def needs_info_round(self):
        self.claude(asking())
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ACTION, out)
        return out

    def state(self):
        return State.load(self.rdir())

    def status(self):
        return git(["status", "--porcelain"], self.repo)


class Recorded(NeedsInfoCase):
    def test_pending_files_recorded_and_named(self):
        out = self.needs_info_round()
        self.assertEqual(self.state().pending_test_files, [PENDING])  # AC-1
        self.assertIn("?? " + PENDING, self.status())  # still uncommitted
        self.assertIn(PENDING, out)  # AC-1: named
        self.assertIn("do not commit", out)
        self.assertGreaterEqual(STATE_VERSION, 3)
        self.assertEqual(State().pending_test_files, [])

    def test_nothing_written_records_nothing(self):
        self.claude(asking(write_tests=False))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ACTION, out)
        self.assertEqual(self.state().pending_test_files, [])  # AC-1
        self.assertNotIn("do not commit", out)


class Tolerated(NeedsInfoCase):
    def test_dry_run_preflight_and_check_tree_pass_with_the_pending_file(self):
        self.needs_info_round()
        with self.assertRaises(Stop) as cm:
            preflight(self.repo)
        self.assertIn("not clean", cm.exception.message)  # the file is dirty
        preflight(self.repo, tolerate=[PENDING])  # AC-2: tolerated
        code, out = run_cli(["run", "--dry-run"])
        self.assertEqual(code, EXIT_OK, out)  # AC-2: dry run
        code, out = run_cli(["preflight"])
        self.assertEqual(code, EXIT_OK, out)  # AC-2: preflight
        self.assertTrue(self.exists(PENDING))
        self.assertEqual(self.state().pending_test_files, [PENDING])

    def test_other_dirty_paths_still_refused_and_the_pending_file_survives(self):
        self.needs_info_round()
        self.write("src/extra.py", "x = 1\n")
        self.claude(claude_entry())
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR, out)  # AC-2: other path
        self.assertIn("not clean", error_line(out))
        self.assertIn("src/extra.py", out)
        self.assertNotIn(PENDING, out)  # AC-2: not an offender
        self.assertTrue(self.exists(PENDING))
        self.assertEqual(self.state().pending_test_files, [PENDING])  # AC-5: kept
        os.remove(os.path.join(self.repo, "src/extra.py"))
        doc = self.read(self.change_md())
        self.write(
            self.change_md(), doc.replace("kind: feature\n", "kind: feature\nstatus: draft\n", 1)
        )
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR, out)  # AC-5: draft stops it
        self.assertTrue(self.exists(PENDING))
        self.assertEqual(self.state().pending_test_files, [PENDING])
        self.write(self.change_md(), doc)
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)  # AC-5: next run tolerates
        self.assertEqual(self.state().pending_test_files, [])

    def test_a_modified_own_earlier_file_is_tolerated(self):
        cr = claude_entry(approve_response(verdict="CHANGES_REQUESTED", findings=[finding()]))
        self.claude(cr)
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ACTION, out)
        self.assertEqual(self.state().test_files, [PENDING])  # committed in round 1
        self.write("src/calc.py", self.read("src/calc.py") + "\n# negatives handled\n")
        self.commit_all("fix")
        entry = asking(write_tests=False)
        entry["write_files"] = {PENDING: TEST_REVIEW_MUL + "\n# updated by round 2\n"}
        self.claude(entry)
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ACTION, out)
        self.assertEqual(self.state().pending_test_files, [PENDING])  # AC-1: modified file
        self.assertIn(" M " + PENDING, self.status())
        code, out = run_cli(["preflight"])
        self.assertEqual(code, EXIT_OK, out)  # AC-2: tolerated
        self.claude(claude_entry())
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)  # AC-3: committed
        self.assertEqual(self.status().strip(), "")
        self.assertEqual(self.state().pending_test_files, [])
        self.assertIn(PENDING, changed_in(self.state().test_commits[-1], self.repo))


class NextRound(NeedsInfoCase):
    def test_prompt_lists_the_pending_file_and_the_round_commits_it(self):
        self.needs_info_round()
        first_prompt = self.fake_calls("claude")[-1]["prompt"]
        self.assertNotIn("not committed yet", first_prompt)
        self.write(".revali/feature__mul/response-1.md", "- integers: any Python int\n")
        self.claude(approving(**{PENDING: TEST_REVIEW_MUL, SECOND: SECOND_TEXT}))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        prompt = self.fake_calls("claude")[-1]["prompt"]
        self.assertIn("not committed yet", prompt)  # AC-3: listed
        self.assertIn("- " + PENDING, prompt)
        self.assertGreaterEqual(int(PROMPT_VERSION), 5)
        state = self.state()
        self.assertEqual(state.pending_test_files, [])  # AC-3: cleared
        self.assertEqual(sorted(changed_in(state.test_commits[-1], self.repo)), [PENDING, SECOND])
        self.assertEqual(sorted(state.test_files), [PENDING, SECOND])
        self.assertEqual(self.status().strip(), "")

    def test_a_file_the_reviewer_deleted_is_simply_gone(self):
        self.needs_info_round()
        entry = approving(**{SECOND: SECOND_TEXT})
        entry["delete_files"] = [PENDING]
        self.claude(entry)
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertFalse(self.exists(PENDING))  # AC-3: gone
        state = self.state()
        self.assertEqual(changed_in(state.test_commits[-1], self.repo), [SECOND])
        self.assertEqual(state.pending_test_files, [])
        self.assertEqual(state.test_files, [SECOND])


class Interrupted(NeedsInfoCase):
    def test_a_failed_next_session_removes_the_pending_file_and_clears_the_list(self):
        self.needs_info_round()
        self.claude(claude_entry(is_error=True))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertFalse(self.exists(PENDING))  # AC-4: removed
        self.assertEqual(self.state().pending_test_files, [])  # AC-4: cleared
        self.assertEqual(self.status().strip(), "")

    def test_a_killed_next_session_is_cleaned_by_the_run_after_it(self):
        self.needs_info_round()
        state = self.state()
        state.reviewer_running = True  # what a session killed mid-round leaves behind
        state.set_stage(self.rdir(), "review", "killed", EXIT_ERROR)
        self.claude(approving(**{SECOND: SECOND_TEXT}))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("removed 1 unfinished test file", out)  # AC-4: named
        self.assertIn(PENDING, out)
        self.assertFalse(self.exists(PENDING))  # AC-4: removed
        state = self.state()
        self.assertEqual(state.pending_test_files, [])
        self.assertEqual(state.test_files, [SECOND])

    def test_a_modified_own_tracked_pending_file_goes_back_to_head_when_the_next_round_fails(self):
        # Round 1 commits the reviewer's file, round 2 (NEEDS_INFO) modifies it, the session of
        # round 3 fails: the modification is the reviewer's, so it goes back to HEAD and the
        # following run does not refuse a file the author was told to leave alone (round 1, F1).
        cr = claude_entry(approve_response(verdict="CHANGES_REQUESTED", findings=[finding()]))
        self.claude(cr)
        self.assertEqual(run_cli(["run", "--foreground"])[0], EXIT_ACTION)
        committed = self.read(PENDING)
        self.write("src/calc.py", self.read("src/calc.py") + "\n# negatives handled\n")
        self.commit_all("fix")
        entry = asking(write_tests=False)
        entry["write_files"] = {PENDING: TEST_REVIEW_MUL + "\n# updated by round 2\n"}
        self.claude(entry)
        self.assertEqual(run_cli(["run", "--foreground"])[0], EXIT_ACTION)
        self.assertEqual(self.state().pending_test_files, [PENDING])
        self.claude(claude_entry(is_error=True))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertEqual(self.read(PENDING), committed)  # back to HEAD
        self.assertEqual(self.status().strip(), "")
        self.assertEqual(self.state().pending_test_files, [])
        self.claude(claude_entry())
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)  # not refused


class HookLivesInReview(NeedsInfoCase):
    def test_hook_built_by_review_and_not_by_pipeline(self):
        self.assertIsNone(interruption_cleanup(State(), self.rdir(), None))  # AC-6
        flagged = State()
        flagged.reviewer_running = True
        flagged.pending_test_files = [PENDING]
        hook = interruption_cleanup(flagged, self.rdir(), None)
        self.assertTrue(callable(hook))
        self.write(PENDING, "# half written\n")
        hook(locate(self.repo))
        self.assertFalse(self.exists(PENDING))  # AC-6: same cleanup
        self.assertFalse(flagged.reviewer_running)
        self.assertEqual(flagged.pending_test_files, [])
        self.assertFalse(hasattr(pipeline, "_cleanup_after_interruption"))


class ReadmeDescribesIt(unittest.TestCase):
    def test_section_states_the_needs_info_rule(self):
        with open(os.path.join(ROOT, "docs", "side-effects.md"), "r", encoding="utf-8") as fh:
            text = fh.read()
        section = text.split("# What revali does to your repository", 1)[1].split("\n## ", 1)[0]
        self.assertIn("NEEDS_INFO round keeps its test files", section)  # AC-7
        self.assertIn("tolerates exactly those paths", section)
        self.assertIn("next round commits or removes", section)
        self.assertIn("Do not commit them by hand", section)


if __name__ == "__main__":
    unittest.main()
