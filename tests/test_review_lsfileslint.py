"""Reviewer tests for fix/stop-restores-own-tests, AC-5 and AC-6, black-box through the CLI.

`[project] lint` is a script that asks `git ls-files` what to check, so a file git does not
list is invisible to it. A lint error in the reviewer's new (untracked) file must still
bounce the reviewer once and end the run on the second attempt (AC-5); whatever lint
returned, the reviewer's new files are untracked afterwards and a stopped round deletes
them as before (AC-6), also when the run was killed while the lint line was running, and
also a draft the killed session had staged (in the index, in no commit)."""

import unittest

from revali import EXIT_ACTION, EXIT_ERROR, EXIT_OK
from revali.state import State
from tests.fixtures.make_sample_repo import PY
from tests.helpers import TEST_REVIEW_MUL, RepoCase, approve_response, claude_entry, git, run_cli

MUL = "tests/test_review_mul.py"
SPACED = "tests/test_review_my topic.py"
THIRD = "tests/test_review_third.py"
STAGED = "tests/test_review_staged.py"
CHANGED = "tests/test_review_changed.py"
MARK = "# lint-red"
RED = TEST_REVIEW_MUL.replace("import unittest", "import unittest  " + MARK)
SPACED_TEXT = TEST_REVIEW_MUL.replace("MulTests", "TopicTests")
THIRD_TEXT = TEST_REVIEW_MUL.replace("MulTests", "ThirdTests")

# Checks only what `git ls-files` lists, one "E900 <path>" line per offending file.
TRACKED_ONLY_LINT = """import subprocess
import sys

listed = subprocess.run(
    ["git", "ls-files", "-z"], capture_output=True, text=True, encoding="utf-8", check=True
).stdout.split("\\0")
red = []
for path in listed:
    name = path.rsplit("/", 1)[-1]
    if path.startswith("tests/") and name.startswith("test_review_") and name.endswith(".py"):
        with open(path, encoding="utf-8") as fh:
            if "# lint-red" in fh.read():
                red.append(path)
for path in red:
    print("E900 " + path)
sys.exit(1 if red else 0)
"""


def writes(files, data=None):
    entry = claude_entry(data, write_tests=False)
    entry["write_files"] = dict(files)
    return entry


def asking(files):
    data = approve_response(verdict="NEEDS_INFO", questions=["Which rounding?"], tests=[])
    return writes(files, data)


def third_test():
    return {
        "path": THIRD,
        "purpose": "acceptance",
        "covers": ["AC-1", "AC-2"],
        "expected": "per AC",
    }


class TrackedOnlyLint(RepoCase):
    def setUp(self):
        super().setUp()
        self.write("tracked_lint.py", TRACKED_ONLY_LINT)
        self.write(
            "revali.toml",
            self.read("revali.toml").replace('lint = ""', 'lint = "%s tracked_lint.py"' % PY),
        )
        self.assertIn("tracked_lint.py", self.read("revali.toml"))
        self.commit_all("a lint line that checks only tracked files")
        self.head = git(["rev-parse", "HEAD"], self.repo).strip()

    def entries(self):
        """{path: XY} from `git status --porcelain -z`, untracked files listed one by one."""
        out = git(["status", "--porcelain", "-z", "--untracked-files=all"], self.repo)
        return {field[3:]: field[:2] for field in out.split("\0") if len(field) > 3}

    def staged(self):
        return git(["diff", "--cached", "--name-only"], self.repo).strip()


class RedNewFile(TrackedOnlyLint):
    def test_a_red_untracked_file_bounces_the_reviewer_once(self):  # AC-5
        self.claude(writes({MUL: RED}), writes({MUL: TEST_REVIEW_MUL}))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        calls = self.fake_calls("claude")
        self.assertEqual(len(calls), 2, out)  # sent back exactly once
        self.assertNotIn("E900 " + MUL, calls[0]["prompt"])
        self.assertIn("E900 " + MUL, calls[1]["prompt"])  # with the lint output
        self.assertNotIn(MARK, git(["show", "HEAD:" + MUL], self.repo))  # the clean file went in
        self.assertEqual(self.entries(), {})

    def test_the_same_error_on_the_second_attempt_ends_the_run(self):  # AC-5, AC-6
        self.claude(writes({MUL: RED}), writes({MUL: RED}))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertIn("E900 " + MUL, out)  # AC-5: the error text in the message
        self.assertEqual(len(self.fake_calls("claude")), 2)
        self.assertEqual(git(["rev-parse", "HEAD"], self.repo).strip(), self.head)  # no commit
        self.assertFalse(self.exists(MUL))  # AC-6: the stopped round deleted it, as before
        self.assertEqual(self.entries(), {})
        self.assertEqual(self.staged(), "")

    def test_a_red_file_with_a_space_in_its_name_is_seen_and_deleted_too(self):  # AC-5, AC-6
        red_spaced = SPACED_TEXT.replace("import unittest", "import unittest  " + MARK)
        files = {MUL: TEST_REVIEW_MUL, SPACED: red_spaced}
        self.claude(writes(files), writes(files))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertIn("E900 " + SPACED, out)
        self.assertNotIn("E900 " + MUL, out)
        self.assertFalse(self.exists(MUL))
        self.assertFalse(self.exists(SPACED))
        self.assertEqual(self.entries(), {})
        self.assertEqual(self.staged(), "")


class UntrackedAfterTheGate(TrackedOnlyLint):
    """A NEEDS_INFO round is linted and keeps its files uncommitted, so the tree after it
    shows what the lint gate left behind."""

    def test_green_leaves_the_new_files_untracked(self):  # AC-6
        self.claude(asking({MUL: TEST_REVIEW_MUL, SPACED: SPACED_TEXT}))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ACTION, out)
        self.assertEqual(self.entries(), {MUL: "??", SPACED: "??"})
        self.assertEqual(self.staged(), "")
        self.assertEqual(git(["rev-parse", "HEAD"], self.repo).strip(), self.head)

    def test_red_then_green_leaves_the_new_files_untracked(self):  # AC-5, AC-6
        self.claude(
            asking({MUL: RED, SPACED: SPACED_TEXT}),
            asking({MUL: TEST_REVIEW_MUL, SPACED: SPACED_TEXT}),
        )
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ACTION, out)
        calls = self.fake_calls("claude")
        self.assertEqual(len(calls), 2, out)  # AC-5: the red attempt was bounced
        self.assertIn("E900 " + MUL, calls[1]["prompt"])
        self.assertNotIn(MARK, self.read(MUL))
        self.assertEqual(self.entries(), {MUL: "??", SPACED: "??"})  # AC-6
        self.assertEqual(self.staged(), "")


class KilledDuringTheGate(TrackedOnlyLint):
    """AC-6, "a stopped round still deletes them as before": a run killed while the lint line
    is running cannot unmark its files, so they stay intent-to-add with the round flagged as
    in flight. The run after it, or `reset`, must dispose of them like any other draft."""

    def killed_at_the_gate(self, *drafts):
        """A NEEDS_INFO round leaves MUL pending; the round after it is killed while its lint
        line runs over `drafts`."""
        self.claude(asking({MUL: TEST_REVIEW_MUL}))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ACTION, out)
        state = State.load(self.rdir())
        self.assertEqual(state.pending_test_files, [MUL])
        state.reviewer_running = True
        state.set_stage(self.rdir(), "review", "killed", EXIT_ERROR)
        for path in drafts:
            if not self.exists(path):
                self.write(path, "# half written by the killed session\n")
            git(["add", "-N", "--", path], self.repo)
        entries = self.entries()
        for path in drafts:
            self.assertEqual(entries.get(path), " A", entries)  # in the index, in no commit

    def test_the_next_run_deletes_the_marked_drafts_and_goes_on(self):
        self.killed_at_the_gate(MUL, SPACED)
        self.claude(writes({THIRD: THIRD_TEXT}, approve_response(tests=[third_test()])))
        code, out = run_cli(["run", "--foreground"])
        self.assertNotIn("not clean", out)
        self.assertEqual(code, EXIT_OK, out)
        self.assertFalse(self.exists(MUL))
        self.assertFalse(self.exists(SPACED))
        listed = git(["ls-files", "-z"], self.repo).split("\0")
        self.assertNotIn(MUL, listed)
        self.assertNotIn(SPACED, listed)
        self.assertIn(THIRD, listed)  # the new round's own file was committed
        self.assertEqual(self.entries(), {})
        state = State.load(self.rdir())
        self.assertEqual(state.pending_test_files, [])
        self.assertFalse(state.reviewer_running)

    def test_a_marked_draft_that_was_not_pending_goes_too(self):
        self.killed_at_the_gate(SPACED)  # MUL stays untracked and pending
        self.claude(writes({THIRD: THIRD_TEXT}, approve_response(tests=[third_test()])))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertFalse(self.exists(MUL))
        self.assertFalse(self.exists(SPACED))
        self.assertEqual(self.entries(), {})

    def test_reset_deletes_the_marked_drafts(self):
        self.killed_at_the_gate(MUL, SPACED)
        code, out = run_cli(["reset"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertFalse(self.exists(MUL))
        self.assertFalse(self.exists(SPACED))
        self.assertEqual(self.entries(), {})
        self.assertEqual(self.staged(), "")

    def staged_drafts(self):
        """Two more drafts of the killed session, in the index with content and in no commit:
        one staged as it is (`A `), one staged and then changed (`AM`)."""
        self.killed_at_the_gate(SPACED)
        for path in (STAGED, CHANGED):
            self.write(path, "# half written by the killed session\n")
            git(["add", "--", path], self.repo)
        self.write(CHANGED, "# half written by the killed session, then more\n")
        entries = self.entries()
        self.assertEqual(entries.get(STAGED), "A ", entries)
        self.assertEqual(entries.get(CHANGED), "AM", entries)

    def test_the_next_run_deletes_staged_drafts_too(self):
        self.staged_drafts()
        self.claude(writes({THIRD: THIRD_TEXT}, approve_response(tests=[third_test()])))
        code, out = run_cli(["run", "--foreground"])
        self.assertNotIn("not clean", out)
        self.assertEqual(code, EXIT_OK, out)
        listed = git(["ls-files", "-z"], self.repo).split("\0")
        for path in (MUL, SPACED, STAGED, CHANGED):
            self.assertFalse(self.exists(path), path)
            self.assertNotIn(path, listed)
        self.assertIn(THIRD, listed)
        self.assertEqual(self.entries(), {})
        self.assertEqual(State.load(self.rdir()).pending_test_files, [])

    def test_reset_deletes_staged_drafts_too(self):
        self.staged_drafts()
        code, out = run_cli(["reset"])
        self.assertEqual(code, EXIT_OK, out)
        for path in (MUL, SPACED, STAGED, CHANGED):
            self.assertFalse(self.exists(path), path)
        self.assertEqual(self.entries(), {})
        self.assertEqual(self.staged(), "")


if __name__ == "__main__":
    unittest.main()
