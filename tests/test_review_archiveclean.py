"""Acceptance tests for archive mode's failure paths and its rules: a round that stops early
leaves no copies behind and does not touch the archive (AC-5), the state's list is rebuilt
from the archive and a tracked path is the author's (AC-6), and the mode is fixed for the
life of a branch (AC-7)."""

import os
import unittest

from revali import EXIT_ACTION, EXIT_ERROR, EXIT_OK
from revali.state import State
from tests.helpers import TEST_REVIEW_MUL, claude_entry, git, run_cli
from tests.test_review_archivemode import (
    DATA,
    FILE,
    SECOND,
    SECOND_TEXT,
    ArchiveModeCase,
    approving,
    read,
    requesting_changes,
)


class InterruptedRoundCase(ArchiveModeCase):
    """Round 1 archived two files and asked for changes; the author committed a fix."""

    def setUp(self):
        super().setUp()
        self.claude(requesting_changes({FILE: TEST_REVIEW_MUL, DATA: "{}\n"}))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ACTION, out)
        self.fix_and_commit("negatives handled")

    def assert_archive_is_round_one(self):
        self.assertEqual(read(self.archived(FILE)), TEST_REVIEW_MUL)
        self.assertEqual(read(self.archived(DATA)), "{}\n")

    def leave_a_killed_round(self):
        """What a session killed mid-round leaves: the placed copies (one half edited), a new
        draft, and the flag the round set before spawning the reviewer."""
        self.write(FILE, TEST_REVIEW_MUL + "\n# half updated\n")
        self.write(DATA, "{}\n")
        self.write(SECOND, SECOND_TEXT)
        state = self.state()
        state.reviewer_running = True
        state.placed_test_files = [DATA, FILE]  # what place_back records before it copies
        state.set_stage(self.rdir(), "review", "killed", EXIT_ERROR)


class StoppedRoundTests(InterruptedRoundCase):
    def test_unusable_session_removes_the_copies(self):
        # AC-5
        entry = claude_entry(is_error=True)
        entry["write_files"] = {SECOND: SECOND_TEXT}
        self.claude(entry)
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertEqual(self.status(), "")
        self.assertFalse(self.exists(FILE))
        self.assertFalse(self.exists(DATA))
        self.assertFalse(self.exists(SECOND))
        self.assert_archive_is_round_one()
        self.assertFalse(os.path.exists(self.archived(SECOND)))
        self.assertEqual(sorted(self.state().test_files), [DATA, FILE])
        self.assertFalse(self.state().reviewer_running)

    def test_second_smoke_failure_removes_the_copies(self):
        # AC-5: exit 2 from new_test means "could not run"; the second one ends the round
        self.runner_scenario(
            {
                "default": 0,
                "results": {"smoke-r2-1": {"new_test": 2}, "smoke-r2-2": {"new_test": 2}},
            }
        )
        broken = approving({FILE: TEST_REVIEW_MUL + "\nsyntax error here\n", DATA: "{}\n"})
        self.claude(broken, broken)
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertIn("cannot run", out)
        self.assertEqual(self.status(), "")
        self.assertFalse(self.exists(FILE))
        self.assert_archive_is_round_one()
        self.assertEqual(sorted(self.state().test_files), [DATA, FILE])

    def test_next_run_cleans_up_after_a_kill(self):
        # AC-5, AC-6
        self.leave_a_killed_round()
        self.claude(approving({FILE: TEST_REVIEW_MUL + "\n# round 2\n", DATA: "{}\n"}))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertEqual(self.status(), "")
        self.assertFalse(self.exists(SECOND))
        self.assertEqual(read(self.archived(FILE)), TEST_REVIEW_MUL + "\n# round 2\n")
        self.assertEqual(read(self.archived(DATA)), "{}\n")
        self.assertFalse(os.path.exists(self.archived(SECOND)))
        self.assertEqual(sorted(self.state().test_files), [DATA, FILE])

    def test_an_occupied_archived_path_stops_the_run_and_keeps_the_occupant(self):
        # AC-5: a gitignored file (the only occupant preflight lets through) at an archived
        # path ends the run before any copy is placed; the cleanup deletes nothing, least of
        # all the occupant, and the archive keeps round 1
        self.write(".gitignore", self.read(".gitignore") + "tests/review_data/\n")
        self.commit_all("ignore the data directory")
        self.write(DATA, "the author's data\n")
        self.claude(approving({FILE: TEST_REVIEW_MUL}))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertIn(DATA, out)
        self.assertIn("already exists", out)
        self.assertEqual(self.read(DATA), "the author's data\n")
        self.assertFalse(self.exists(FILE))
        self.assertNotIn("removed", out)
        self.assert_archive_is_round_one()
        state = self.state()
        self.assertEqual(state.placed_test_files, [])
        self.assertFalse(state.reviewer_running)
        self.assertEqual(sorted(state.test_files), [DATA, FILE])
        # the same run again: still refused, the occupant still there
        self.claude(approving({FILE: TEST_REVIEW_MUL}))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertEqual(self.read(DATA), "the author's data\n")

    def test_a_killed_round_records_what_it_placed(self):
        # AC-5: the state lists the placed copies before the reviewer starts, so a kill at any
        # later point leaves exactly that list for the next run; a normal round clears it
        entry = claude_entry(is_error=True)
        self.claude(entry)
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertEqual(self.state().placed_test_files, [])
        self.assertIn("removed 2 working-tree copies", out)
        self.claude(approving({FILE: TEST_REVIEW_MUL, DATA: "{}\n"}))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("placed 2 archived test file(s) back", out)
        self.assertEqual(self.state().placed_test_files, [])
        self.assertEqual(self.status(), "")

    def test_reset_removes_the_copies_and_keeps_the_archive(self):
        # AC-5, AC-6
        self.leave_a_killed_round()
        code, out = run_cli(["reset"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertEqual(self.status(), "")
        self.assertFalse(self.exists(FILE))
        self.assertFalse(self.exists(DATA))
        self.assertFalse(self.exists(SECOND))
        self.assert_archive_is_round_one()
        self.assertIsNone(self.state())
        # the next run rebuilds the list from the archive and hands both files to the reviewer
        self.claude(approving({FILE: TEST_REVIEW_MUL, DATA: "{}\n"}))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        prompt = self.fake_calls("claude")[-1]["prompt"]
        self.assertIn("- " + FILE, prompt)
        self.assertNotIn("not yours", prompt)
        self.assertEqual(sorted(self.state().test_files), [DATA, FILE])
        self.assertEqual(self.status(), "")
        self.assertEqual(self.runner_calls("validate-r1")[-1]["extra_files"], [DATA, FILE])


class OwnershipTests(ArchiveModeCase):
    def test_a_tracked_path_is_the_authors(self):
        # AC-6
        self.claude(requesting_changes({FILE: TEST_REVIEW_MUL}))
        self.assertEqual(run_cli(["run", "--foreground"])[0], EXIT_ACTION)
        authors = "# the author's own file under the reviewer's name\n"
        self.write(FILE, authors)
        self.fix_and_commit("author takes the name")
        self.claude(approving({SECOND: SECOND_TEXT}))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn(FILE, out)
        self.assertFalse(os.path.exists(self.archived(FILE)))
        self.assertEqual(read(self.archived(SECOND)), SECOND_TEXT)
        self.assertEqual(self.state().test_files, [SECOND])
        self.assertEqual(self.read(FILE), authors)
        prompt = self.fake_calls("claude")[-1]["prompt"]
        self.assertIn("not yours", prompt)
        self.assertEqual(self.runner_calls("validate-r2")[-1]["extra_files"], [SECOND])

    def test_a_dry_run_reports_but_keeps_the_taken_over_path(self):
        # AC-6: `run --dry-run` deletes nothing (docs/side-effects.md), the archive included;
        # the next real run removes the author's path from the archive and the state
        self.claude(requesting_changes({FILE: TEST_REVIEW_MUL, DATA: "{}\n"}))
        self.assertEqual(run_cli(["run", "--foreground"])[0], EXIT_ACTION)
        self.write(DATA, "the author's data\n")
        self.fix_and_commit("author takes the data path")
        code, out = run_cli(["run", "--foreground", "--dry-run"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("dry run", out)
        self.assertIn(DATA, out)
        self.assertEqual(read(self.archived(DATA)), "{}\n")
        self.assertEqual(read(self.archived(FILE)), TEST_REVIEW_MUL)
        self.assertEqual(self.read(DATA), "the author's data\n")
        self.claude(approving({FILE: TEST_REVIEW_MUL}))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertFalse(os.path.exists(self.archived(DATA)))
        self.assertEqual(read(self.archived(FILE)), TEST_REVIEW_MUL)
        self.assertEqual(self.state().test_files, [FILE])
        self.assertEqual(self.read(DATA), "the author's data\n")

    def test_the_reviewer_may_not_touch_the_taken_over_file(self):
        # AC-6
        self.claude(requesting_changes({FILE: TEST_REVIEW_MUL}))
        self.assertEqual(run_cli(["run", "--foreground"])[0], EXIT_ACTION)
        self.write(FILE, "# the author's\n")
        self.fix_and_commit("author takes the name")
        bad = approving({FILE: TEST_REVIEW_MUL})
        self.claude(bad, bad)
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertEqual(self.read(FILE), "# the author's\n")
        self.assertEqual(self.status(), "")
        self.assertFalse(os.path.exists(self.archived(FILE)))


class ModeRuleTests(ArchiveModeCase):
    def remote_tip(self):
        return git(["rev-parse", "feature/mul"], self.info["remote"]).strip()

    def test_switch_to_commit_is_refused_before_the_push(self):
        # AC-7
        self.claude(requesting_changes({FILE: TEST_REVIEW_MUL}))
        self.assertEqual(run_cli(["run", "--foreground"])[0], EXIT_ACTION)
        self.set_tests_key("commit")
        tip = self.remote_tip()
        self.assertNotEqual(tip, self.head())
        self.claude(approving({FILE: TEST_REVIEW_MUL}))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ACTION, out)
        self.assertIn("commit", out)
        self.assertIn("archive", out)
        self.assertEqual(self.remote_tip(), tip)
        self.assertEqual(self.status(), "")
        self.assertEqual(self.trailers(), [])
        self.assertEqual(read(self.archived(FILE)), TEST_REVIEW_MUL)
        self.assertEqual(len(self.state().rounds), 1)
        # setting it back lets the review go on
        self.set_tests_key("archive")
        self.claude(approving({FILE: TEST_REVIEW_MUL}))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertEqual(self.trailers(), [])

    def test_switch_to_archive_is_refused(self):
        # AC-7
        self.set_tests_key("commit")
        self.claude(requesting_changes({FILE: TEST_REVIEW_MUL}))
        self.assertEqual(run_cli(["run", "--foreground"])[0], EXIT_ACTION)
        self.assertEqual(self.state().tests_mode, "commit")
        self.set_tests_key("archive")
        tip = self.remote_tip()
        self.claude(approving({FILE: TEST_REVIEW_MUL}))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ACTION, out)
        self.assertIn("commit", out)
        self.assertIn("archive", out)
        self.assertEqual(self.remote_tip(), tip)
        self.assertEqual(len(self.trailers()), 1)
        self.assertFalse(os.path.isdir(os.path.join(self.rdir(), "tests")))

    def test_an_older_state_counts_as_commit(self):
        # AC-7
        self.set_tests_key("commit")
        self.claude(requesting_changes({FILE: TEST_REVIEW_MUL}))
        self.assertEqual(run_cli(["run", "--foreground"])[0], EXIT_ACTION)
        state = self.state()
        state.tests_mode = ""  # written before the key existed
        state.save(self.rdir())
        self.set_tests_key("archive")
        self.claude(approving({FILE: TEST_REVIEW_MUL}))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ACTION, out)
        self.assertIn('"commit"', out)
        self.assertEqual(len(self.trailers()), 1)

    def test_a_restart_after_a_rewrite_clears_the_mode(self):
        # AC-7
        self.set_tests_key("commit")
        self.claude(requesting_changes({FILE: TEST_REVIEW_MUL}))
        self.assertEqual(run_cli(["run", "--foreground"])[0], EXIT_ACTION)
        self.assertEqual(len(self.trailers()), 1)
        git(["reset", "-q", "--hard", "HEAD~1"], self.repo)  # drops the reviewer's commit
        self.fix_and_commit("negatives handled")
        self.set_tests_key("archive")
        self.claude(approving({FILE: TEST_REVIEW_MUL}))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("starts over", out)
        state = self.state()
        self.assertEqual(state.tests_mode, "archive")
        self.assertEqual(len(state.rounds), 1)
        self.assertEqual(self.trailers(), [])
        self.assertEqual(read(self.archived(FILE)), TEST_REVIEW_MUL)

    def test_state_field_default(self):
        # AC-7: an older state file has no tests_mode
        self.assertEqual(State().tests_mode, "")
        self.assertIsNone(State.load(self.rdir()))


if __name__ == "__main__":
    unittest.main()
