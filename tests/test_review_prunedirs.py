"""fix/archive-followups AC-1: in archive mode, every cleanup that deletes the placed copies
(a failed session, a second smoke failure, `revali reset`, the run after a kill) also removes
the directories under `test_dir` those copies leave empty, up to and excluding `test_dir`;
a directory that still holds another file stays. Black-box through the CLI on the fixture
repo; the directories are inspected on disk."""

import os
import unittest

from revali import EXIT_ACTION, EXIT_ERROR, EXIT_OK
from revali.state import State
from revali.testarchive import archive_dir, archived_files
from tests.helpers import TEST_REVIEW_MUL, RepoCase, approve_response, claude_entry, run_cli

FILE = "tests/test_review_mul.py"
DATA = "tests/review_data/mul.json"  # under test_dir, in a directory of the reviewer's own
DEEP = "tests/review_data/nested/deep.json"  # two levels of the reviewer's directories
FINDING = {
    "id": "F1",
    "file": "src/calc.py",
    "line": 3,
    "severity": "high",
    "kind": "correctness",
    "text": "mul ignores negative numbers",
    "suggestion": "handle them",
}


def read(path):
    with open(path, "r", encoding="utf-8", newline="") as fh:
        return fh.read()


def approving(files):
    tests = [
        {"path": p, "purpose": "acceptance", "covers": ["AC-1", "AC-2"], "expected": "per AC"}
        for p in files
        if p.endswith(".py")
    ]
    entry = claude_entry(approve_response(tests=tests), write_tests=False)
    entry["write_files"] = dict(files)
    return entry


def requesting_changes(files):
    entry = approving(files)
    entry["structured_output"].update(verdict="CHANGES_REQUESTED", findings=[FINDING])
    return entry


class PruneCase(RepoCase):
    """Round 1 (archive mode) archived FILE, DATA and DEEP and asked for changes; the author
    committed a fix. Every scenario then interrupts round 2 one way or another."""

    def setUp(self):
        super().setUp()
        os.environ["REVALI_POLL_SECONDS"] = "0.01"
        text = self.read("revali.toml").replace(
            'exclude = ["*.lock"]\n', 'exclude = ["*.lock"]\ntests = "archive"\n'
        )
        self.write("revali.toml", text)
        self.commit_all("tests mode archive")
        self.claude(requesting_changes({FILE: TEST_REVIEW_MUL, DATA: "{}\n", DEEP: "[]\n"}))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ACTION, out)
        self.assertEqual(archived_files(self.rdir()), sorted([DATA, DEEP, FILE]))
        self.assertFalse(self.isdir("tests/review_data"))  # take pruned after round 1
        self.write("src/calc.py", self.read("src/calc.py") + "\n# negatives handled\n")
        self.commit_all("fix")

    def isdir(self, rel):
        return os.path.isdir(os.path.join(self.repo, rel))

    def state(self):
        return State.load(self.rdir())

    def archived(self, rel):
        return os.path.join(archive_dir(self.rdir()), rel)

    def assert_clean_and_pruned(self):
        self.assertFalse(self.isdir("tests/review_data/nested"))
        self.assertFalse(self.isdir("tests/review_data"))
        self.assertTrue(self.isdir("tests"))  # test_dir itself stays
        self.assertTrue(self.exists("tests/test_calc.py"))  # and its tracked content
        # the archive keeps round 1
        self.assertEqual(read(self.archived(FILE)), TEST_REVIEW_MUL)
        self.assertEqual(read(self.archived(DATA)), "{}\n")
        self.assertEqual(read(self.archived(DEEP)), "[]\n")

    def leave_a_killed_round(self, extra=()):
        """The placed copies (and `extra` untracked files a failed take could not move) with
        the flag a session killed mid-round leaves behind."""
        placed = sorted([FILE, DATA, DEEP] + list(extra))
        for rel in placed:
            self.write(rel, "half written\n")
        state = self.state()
        state.reviewer_running = True
        state.placed_test_files = placed
        state.set_stage(self.rdir(), "review", "killed", EXIT_ERROR)


class FailedSession(PruneCase):
    def test_a_failed_session_prunes_the_emptied_directories(self):
        # AC-1: the copies go and so do review_data/nested and review_data
        self.claude(claude_entry(is_error=True))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertIn("removed 3 working-tree copies", out)
        self.assert_clean_and_pruned()

    def test_a_directory_with_another_file_stays(self):
        # AC-1: review_data holds the author's tracked file, so only nested/ goes
        self.write("tests/review_data/keep.json", "the author's\n")
        self.commit_all("the author's data file")
        self.claude(claude_entry(is_error=True))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertIn("removed 3 working-tree copies", out)
        self.assertFalse(self.isdir("tests/review_data/nested"))
        self.assertTrue(self.isdir("tests/review_data"))
        self.assertEqual(self.read("tests/review_data/keep.json"), "the author's\n")
        self.assertTrue(self.isdir("tests"))

    def test_a_directory_with_an_ignored_file_stays(self):
        # AC-1: a gitignored file is "another file" too; the cleanup does not delete it
        self.write(".gitignore", "tests/review_data/nested/*.log\n")
        self.commit_all("ignore logs")
        self.write("tests/review_data/nested/run.log", "kept\n")
        self.claude(claude_entry(is_error=True))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertTrue(self.isdir("tests/review_data/nested"))
        self.assertTrue(self.isdir("tests/review_data"))
        self.assertEqual(self.read("tests/review_data/nested/run.log"), "kept\n")


class SecondSmokeFailure(PruneCase):
    def test_the_second_smoke_failure_prunes_the_emptied_directories(self):
        # AC-1: exit 2 from new_test twice ends the round; the placed copies and their
        # directories go
        self.runner_scenario(
            {
                "default": 0,
                "results": {"smoke-r2-1": {"new_test": 2}, "smoke-r2-2": {"new_test": 2}},
            }
        )
        bad = approving({FILE: TEST_REVIEW_MUL + "\nsyntax error\n", DATA: "{}\n", DEEP: "[]\n"})
        self.claude(bad, bad)
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertIn("removed 3 working-tree copies", out)
        self.assert_clean_and_pruned()


class Reset(PruneCase):
    def test_reset_prunes_the_emptied_directories(self):
        # AC-1
        self.leave_a_killed_round()
        code, out = run_cli(["reset"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIsNone(self.state())
        self.assert_clean_and_pruned()

    def test_reset_keeps_a_directory_the_author_uses(self):
        # AC-1
        self.write("tests/review_data/keep.json", "the author's\n")
        self.commit_all("the author's data file")
        self.leave_a_killed_round()
        code, out = run_cli(["reset"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertFalse(self.isdir("tests/review_data/nested"))
        self.assertTrue(self.isdir("tests/review_data"))
        self.assertEqual(self.read("tests/review_data/keep.json"), "the author's\n")


class NextRunAfterAKill(PruneCase):
    def test_the_run_after_a_kill_prunes_what_the_cleanup_emptied(self):
        # AC-1: a file a failed take could not move sits in a directory of its own that the
        # next round never places back, so only the cleanup's pruning can remove it
        stray = "tests/z_data/extra.json"
        self.leave_a_killed_round(extra=[stray])
        self.claude(approving({FILE: TEST_REVIEW_MUL, DATA: "{}\n", DEEP: "[]\n"}))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("removed 4 working-tree copies", out)
        self.assertFalse(self.exists(stray))
        self.assertFalse(self.isdir("tests/z_data"))
        self.assertFalse(self.isdir("tests/review_data"))  # placed back, then taken
        self.assertTrue(self.isdir("tests"))
        self.assertEqual(archived_files(self.rdir()), sorted([DATA, DEEP, FILE]))

    def test_the_run_after_a_kill_prunes_even_when_its_own_round_fails(self):
        # AC-1: cleanup before preflight, then a failed session's cleanup; nothing is left
        self.leave_a_killed_round(extra=["tests/z_data/extra.json"])
        self.claude(claude_entry(is_error=True))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertFalse(self.isdir("tests/z_data"))
        self.assert_clean_and_pruned()


class OtherTestDir(RepoCase):
    def test_test_dir_itself_stays_when_it_holds_nothing_else(self):
        # AC-1: with test_dir = "acceptance" (no tracked file in it) the reviewer's
        # subdirectory goes but the configured directory stays
        os.environ["REVALI_POLL_SECONDS"] = "0.01"
        text = self.read("revali.toml").replace(
            'exclude = ["*.lock"]\n', 'exclude = ["*.lock"]\ntests = "archive"\n'
        )
        text = text.replace('test_dir = "tests"', 'test_dir = "acceptance"')
        self.write("revali.toml", text)
        self.commit_all("tests in acceptance/, archive mode")
        test = "acceptance/test_review_mul.py"
        data = "acceptance/data/mul.json"
        self.claude(requesting_changes({test: TEST_REVIEW_MUL, data: "{}\n"}))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ACTION, out)
        self.assertTrue(os.path.isdir(os.path.join(self.repo, "acceptance")))
        self.write("src/calc.py", self.read("src/calc.py") + "\n# negatives handled\n")
        self.commit_all("fix")
        self.claude(claude_entry(is_error=True))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertIn("removed 2 working-tree copies", out)
        self.assertFalse(os.path.isdir(os.path.join(self.repo, "acceptance", "data")))
        self.assertTrue(os.path.isdir(os.path.join(self.repo, "acceptance")))
        self.assertEqual(archived_files(self.rdir()), sorted([data, test]))


if __name__ == "__main__":
    unittest.main()
