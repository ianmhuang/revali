"""Acceptance tests for fix/state-file-tolerance, the reset half (AC-3, AC-4, AC-5): `revali
reset` on a state.json that is a JSON object with a field of the wrong type cleans up the
reviewer's uncommitted test files from the fields it can read and names the ones it skipped;
it leaves the files and says so when a field that cleanup reads is unusable or the file is
not a JSON object; every other command still refuses such a file.
Black box through the CLI on the fixture repository."""

import json
import os

from revali import EXIT_ACTION, EXIT_ERROR, EXIT_OK
from revali.state import State
from tests.helpers import TEST_REVIEW_MUL, RepoCase, approve_response, claude_entry, git, run_cli
from tests.test_review_archivemode import (
    DATA,
    FILE,
    SECOND,
    SECOND_TEXT,
    ArchiveModeCase,
    read,
    requesting_changes,
)

MINE = "tests/test_review_mine.py"  # the author's own untracked draft, on test_file_pattern
HIGH = {
    "id": "F1",
    "file": "src/calc.py",
    "line": 3,
    "severity": "high",
    "kind": "correctness",
    "text": "wrong for negatives",
    "suggestion": "",
}


class HandEdits:
    """What a hand edit does to `.revali/feature__mul/state.json`, for a RepoCase."""

    def state_file(self):
        return State.path(self.rdir())

    def raw(self):
        with open(self.state_file(), "rb") as fh:
            return fh.read()

    def put_raw(self, data):
        os.makedirs(self.rdir(), exist_ok=True)
        with open(self.state_file(), "wb") as fh:
            fh.write(data)

    def edit(self, **fields):
        data = json.loads(self.raw().decode("utf-8"))
        data.update(fields)
        self.put_raw(json.dumps(data, indent=2).encode("utf-8") + b"\n")

    def tests_status(self):
        return git(["status", "--porcelain", "--", "tests"], self.repo).strip()

    def needs_info_round(self):
        data = approve_response(verdict="NEEDS_INFO", questions=["Which integers?"], tests=[])
        self.claude(claude_entry(data))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ACTION, out)
        self.assertEqual(json.loads(self.raw().decode("utf-8"))["pending_test_files"], [FILE])
        self.assertTrue(self.exists(FILE))


class ResetReadsWhatItCan(HandEdits, RepoCase):
    """AC-3, AC-4"""

    def test_the_pending_file_goes_when_only_other_fields_are_bad(self):
        self.needs_info_round()
        self.write(MINE, "# the author's own draft\n")
        self.edit(fixes="1", cost_usd="a lot", models_used="fable")
        code, out = run_cli(["reset"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertNotIn("Traceback", out)
        self.assertFalse(self.exists(FILE))
        self.assertIn(FILE, out)  # named, as for a good file
        self.assertTrue(self.exists(MINE))  # as for a good file: only the pending list goes
        for name in ("fixes", "cost_usd", "models_used"):  # every skipped field, not the first
            self.assertIn(name, out)
        self.assertFalse(os.path.exists(self.state_file()))
        # nothing is left that the next run would refuse
        os.remove(os.path.join(self.repo, MINE))
        self.assertEqual(self.tests_status(), "")
        self.claude(claude_entry())
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)

    def test_an_interrupted_round_is_swept(self):
        self.needs_info_round()
        self.write(SECOND, SECOND_TEXT)  # the killed session's own draft, in no list
        self.edit(reviewer_running=True, pending_test_files=[], pr_number=True)
        code, out = run_cli(["reset"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertFalse(self.exists(FILE))
        self.assertFalse(self.exists(SECOND))
        self.assertEqual(self.tests_status(), "")
        self.assertIn("pr_number", out)
        self.assertFalse(os.path.exists(self.state_file()))

    def test_an_interrupted_round_gives_the_reviewers_committed_file_back(self):
        self.claude(claude_entry(approve_response(verdict="CHANGES_REQUESTED", findings=[HIGH])))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ACTION, out)
        self.assertEqual(self.tests_status(), "")  # round 1 committed its test file
        committed = self.read(FILE)
        self.write(FILE, committed + "\n# half updated by a killed session\n")
        self.edit(reviewer_running=True, models_used="fable")
        code, out = run_cli(["reset"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertEqual(self.read(FILE), committed)
        self.assertEqual(self.tests_status(), "")
        self.assertIn("models_used", out)
        self.assertFalse(os.path.exists(self.state_file()))

    def test_the_cleanup_runs_only_when_the_fields_it_reads_are_usable(self):
        """AC-4 and AC-3 on the same leftover: every file the cleanup cannot go by leaves it
        and says so; the first one whose only bad field is elsewhere removes it."""
        self.needs_info_round()
        listed = '"pending_test_files": ["%s"]' % FILE
        unusable = {
            "the pending list holds a null": '{"pending_test_files": ["%s", null]}' % FILE,
            "the pending list is a string": '{"pending_test_files": "%s"}' % FILE,
            "the interrupted flag is a string": '{%s, "reviewer_running": "yes"}' % listed,
            "not JSON": "{%s," % listed,
            "not an object": '["%s"]' % FILE,
        }
        for what, text in unusable.items():
            with self.subTest(state=what):
                self.put_raw(text.encode("utf-8"))
                code, out = run_cli(["reset"])
                self.assertEqual(code, EXIT_OK, out)
                self.assertNotIn("Traceback", out)
                self.assertIn("not cleaned up", out)
                self.assertTrue(self.exists(FILE))
                self.assertFalse(os.path.exists(self.state_file()))
        self.put_raw(('{%s, "fixes": "1"}' % listed).encode("utf-8"))
        code, out = run_cli(["reset"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertNotIn("not cleaned up", out)
        self.assertFalse(self.exists(FILE))
        self.assertFalse(os.path.exists(self.state_file()))

    def test_a_state_that_lists_nothing_is_not_reported_as_left_behind(self):
        """AC-3 with nothing to clean up: the readable fields name no pending file and no
        interrupted round, so nothing is deleted and nothing is said to be left behind."""
        self.needs_info_round()
        self.edit(fixes="1", pending_test_files=[], reviewer_running=False)
        code, out = run_cli(["reset"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertNotIn("Traceback", out)
        self.assertIn("fixes", out)
        self.assertNotIn("not cleaned up", out)
        self.assertTrue(self.exists(FILE))  # no longer listed: as for a good file, it stays
        self.assertFalse(os.path.exists(self.state_file()))

    def test_a_refused_cleanup_still_names_every_skipped_field(self):
        """AC-3 / AC-4: the skipped fields are named whether or not the cleanup can run."""
        self.needs_info_round()
        self.edit(pending_test_files=[FILE, None], cost_usd="a lot", models_used="fable")
        code, out = run_cli(["reset"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertNotIn("Traceback", out)
        self.assertIn("not cleaned up", out)
        for name in ("pending_test_files", "cost_usd", "models_used"):
            self.assertIn(name, out)
        self.assertTrue(self.exists(FILE))
        self.assertFalse(os.path.exists(self.state_file()))


class ArchiveModeCopies(HandEdits, ArchiveModeCase):
    """AC-3, archive mode"""

    def test_the_placed_copies_go_and_the_archive_stays(self):
        self.claude(requesting_changes({FILE: TEST_REVIEW_MUL, DATA: "{}\n"}))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ACTION, out)
        self.fix_and_commit("negatives handled")
        # what a session killed mid-round leaves: the placed copies (one half edited), a new
        # draft, the flag and the list of the copies the round recorded before it started
        self.write(FILE, TEST_REVIEW_MUL + "\n# half updated\n")
        self.write(DATA, "{}\n")
        self.write(SECOND, SECOND_TEXT)
        self.edit(
            reviewer_running=True,
            placed_test_files=[DATA, FILE],
            stage="review",
            last_exit=-1,
            fixes="1",
        )
        code, out = run_cli(["reset"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertFalse(self.exists(FILE))
        self.assertFalse(self.exists(DATA))  # off the pattern: only the placed list names it
        self.assertFalse(self.exists(SECOND))
        self.assertEqual(self.status(), "")
        self.assertEqual(read(self.archived(FILE)), TEST_REVIEW_MUL)
        self.assertEqual(read(self.archived(DATA)), "{}\n")
        self.assertIn("fixes", out)
        self.assertFalse(os.path.exists(self.state_file()))


class TheOthersStillRefuse(HandEdits, RepoCase):
    """AC-5"""

    def test_every_other_command_refuses_the_file_reset_cleans_up_from(self):
        self.needs_info_round()
        self.edit(fixes="1")
        text = self.raw()
        commands = (
            ["run", "--foreground"],
            ["wait", "--timeout", "1s"],
            ["status"],
            ["merge"],
            ["stop"],
        )
        for argv in commands:
            with self.subTest(command=argv[0]):
                code, out = run_cli(argv)
                self.assertEqual(code, EXIT_ERROR, out)
                self.assertNotIn("Traceback", out)
                errors = [line for line in out.splitlines() if line.startswith("ERROR: ")]
                self.assertEqual(len(errors), 1, out)
                self.assertIn("state.json", errors[0])
                self.assertIn("fixes", errors[0])
                self.assertEqual(self.raw(), text)  # reported, not repaired
                self.assertTrue(self.exists(FILE))
        code, out = run_cli(["reset"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertFalse(self.exists(FILE))
        self.assertFalse(os.path.exists(self.state_file()))
