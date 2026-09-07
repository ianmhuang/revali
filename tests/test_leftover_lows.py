"""fix/leftover-lows: a test file the author deleted and re-created under the reviewer's file
name is the author's (the newest add in base..HEAD decides), the ownership recovery names
trailer commits none of whose files survive, `commit_paths` runs without `--root`,
`_close_stopped` restores the whole state, and the state-write retry meets a real reader on
Windows."""

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from dataclasses import asdict
from unittest import mock

from revali import EXIT_OK, gitops
from revali.state import State, write_json_atomic
from tests import test_rebase_ownership as ro
from tests.helpers import TEST_REVIEW_MUL, RepoCase, approve_response, claude_entry, git, run_cli

RECREATED = "the reviewer's test file(s) were deleted and re-created"
MUL2 = TEST_REVIEW_MUL.replace("MulTests", "MulTests2")
AUTHORS_OWN = (
    "import unittest\n\n"
    "from src.calc import mul\n\n\n"
    "class AuthorsMul(unittest.TestCase):\n"
    "    def test_one(self):\n"
    "        self.assertEqual(mul(1, 1), 1)\n"
)


def _mul2_entry():
    entry = claude_entry(
        approve_response(
            tests=[
                {
                    "path": "tests/test_review_mul2.py",
                    "purpose": "p",
                    "covers": ["AC-1", "AC-2"],
                    "expected": "e",
                }
            ]
        ),
        write_tests=False,
    )
    entry["write_files"] = {"tests/test_review_mul2.py": MUL2}
    return entry


class RecreatedFileIsTheAuthors(ro.RewriteCase):
    def delete_and_recreate(self):
        git(["rm", "-q", "tests/test_review_mul.py"], self.repo)
        git(["commit", "-q", "-m", "drop the reviewer's test"], self.repo)
        self.write("tests/test_review_mul.py", AUTHORS_OWN)
        self.commit_all("my own mul test")
        return git(["rev-parse", "HEAD"], self.repo).strip()

    def test_recreated_file_is_protected_and_dropped(self):  # AC-1
        self.first_round()
        recreated_by = self.delete_and_recreate()
        first = claude_entry(approve_response())
        first["write_files"]["tests/test_review_mul.py"] = ro.UPDATED  # not its file any more
        self.claude(first, _mul2_entry())
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertNotIn("starts over", out)  # no rewrite: the round-1 commit is still in HEAD
        self.assertIn(RECREATED, out)
        line = out.split(RECREATED, 1)[1].split("\n", 1)[0]
        self.assertIn("tests/test_review_mul.py", line)
        self.assertIn(recreated_by[:10], line)
        self.assertEqual(self.read("tests/test_review_mul.py"), AUTHORS_OWN)  # restored
        ps = ro.prompts(self)
        self.assertEqual(len(ps), 2)  # bounced once
        self.assertIn("tests/test_review_mul.py", ro.section(ps[0], ro.NOT_YOURS))
        self.assertNotIn(ro.EARLIER, ps[0])
        self.assertIn("tests/test_review_mul.py", ps[1].split("Corrections required", 1)[1])
        state = State.load(self.rdir())
        self.assertEqual(state.test_files, ["tests/test_review_mul2.py"])
        self.assertEqual(len(state.test_commits), 2)  # round 1's commit is still counted

    def test_the_drop_is_logged_once(self):  # AC-1: quiet afterwards
        self.first_round()
        self.delete_and_recreate()
        self.claude(_mul2_entry())
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn(RECREATED, out)
        self.assertEqual(State.load(self.rdir()).test_files, ["tests/test_review_mul2.py"])
        self.fix_and_commit()
        self.claude(claude_entry(approve_response(), write_tests=False))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertNotIn(RECREATED, out)
        self.assertNotIn("recovered", out)
        self.assertEqual(State.load(self.rdir()).test_files, ["tests/test_review_mul2.py"])

    def test_after_a_rewrite_the_recreated_file_is_not_recovered(self):  # AC-1 + rewrite
        self.first_round()
        self.delete_and_recreate()
        self.move_main()
        git(["rebase", "-q", "main"], self.repo)
        self.claude(_mul2_entry())
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("starts over", out)
        self.assertNotIn("recovered", out)
        self.assertNotIn(RECREATED, out)  # a cleared state has nothing to drop
        self.assertIn("none of", out)  # the round-1 commit has no file left that is its own
        self.assertIn("tests/test_review_mul.py", ro.section(ro.prompts(self)[0], ro.NOT_YOURS))
        self.assertEqual(State.load(self.rdir()).test_files, ["tests/test_review_mul2.py"])


class EditsAndReviewerRecreation(ro.RewriteCase):
    def test_an_author_edit_keeps_the_reviewers_file(self):  # AC-2
        self.first_round()
        self.write("tests/test_review_mul.py", self.read("tests/test_review_mul.py") + "\n# ok\n")
        self.commit_all("touch the reviewer's test")
        entry = claude_entry(approve_response())
        entry["write_files"]["tests/test_review_mul.py"] = ro.UPDATED
        self.claude(entry)
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertNotIn(RECREATED, out)
        ps = ro.prompts(self)
        self.assertEqual(len(ps), 1)  # no bounce
        self.assertIn("tests/test_review_mul.py", ro.section(ps[0], ro.EARLIER))
        self.assertEqual(self.read("tests/test_review_mul.py"), ro.UPDATED)
        self.assertEqual(State.load(self.rdir()).test_files, ["tests/test_review_mul.py"])

    def test_the_reviewer_may_recreate_a_file_the_author_deleted(self):  # AC-2
        self.first_round()
        git(["rm", "-q", "tests/test_review_mul.py"], self.repo)
        git(["commit", "-q", "-m", "drop the reviewer's test"], self.repo)
        self.claude(claude_entry(approve_response()))  # writes tests/test_review_mul.py anew
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertNotIn(RECREATED, out)
        self.assertEqual(len(ro.prompts(self)), 1)
        self.assertEqual(self.read("tests/test_review_mul.py"), TEST_REVIEW_MUL)
        self.assertEqual(len(ro.trailer_commits(self.repo)), 2)
        state = State.load(self.rdir())
        self.assertEqual(state.test_files, ["tests/test_review_mul.py"])
        # the file is the reviewer's again: its newest add carries the trailer
        added_by = gitops.last_add_commit(
            "origin/main", "HEAD", "tests/test_review_mul.py", self.repo
        )
        self.assertEqual(added_by, ro.trailer_commits(self.repo)[-1])


class EmptiedCommitIsNamed(ro.RewriteCase):
    def test_recovery_names_the_commit_whose_files_are_gone(self):  # AC-4
        self.first_round()
        self.fix_and_commit()
        self.claude(_mul2_entry())  # round 2: a second trailer commit with a second file
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        first, second = ro.trailer_commits(self.repo)
        git(["rm", "-q", "tests/test_review_mul2.py"], self.repo)
        git(["commit", "-q", "-m", "drop the second test"], self.repo)
        self.move_main()
        git(["rebase", "-q", "main"], self.repo)
        rebased = ro.trailer_commits(self.repo)
        self.assertEqual(len(rebased), 2)
        self.claude(claude_entry(approve_response(), write_tests=False))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("starts over", out)
        self.assertIn("recovered", out)
        line = out.split("recovered", 1)[1].split("\n", 1)[0]
        self.assertIn("tests/test_review_mul.py", line)
        self.assertIn("none of the files of 1 of them is still the reviewer's", line)
        self.assertIn(rebased[1][:10], line.split("none of the files", 1)[1])
        state = State.load(self.rdir())
        self.assertEqual(state.test_files, ["tests/test_review_mul.py"])
        self.assertEqual(state.test_commits[:2], rebased)

    def test_a_state_that_kept_its_files_but_lost_its_commits(self):  # AC-4: no recovered line
        """Nothing to recover (the files are in the state), yet the emptied commit is new to
        the state: it gets a line of its own."""
        self.first_round()
        self.fix_and_commit()
        self.claude(_mul2_entry())
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        first, second = ro.trailer_commits(self.repo)
        git(["rm", "-q", "tests/test_review_mul2.py"], self.repo)
        git(["commit", "-q", "-m", "drop the second test"], self.repo)
        path = State.path(self.rdir())
        with open(path, "r", encoding="utf-8", newline="") as fh:
            data = json.load(fh)
        data["test_commits"] = []
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(data, fh)
        self.claude(claude_entry(approve_response(), write_tests=False))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertNotIn("recovered", out)
        self.assertNotIn("starts over", out)
        lines = [ln for ln in out.splitlines() if "none of their test files" in ln]
        self.assertEqual(len(lines), 1, out)
        self.assertIn(second[:10], lines[0])
        self.assertNotIn(first[:10], lines[0])
        state = State.load(self.rdir())
        self.assertEqual(state.test_commits[:2], [first, second])
        self.assertEqual(
            state.test_files, ["tests/test_review_mul.py", "tests/test_review_mul2.py"]
        )

    def test_nothing_new_logs_nothing(self):  # AC-4: once
        self.first_round()
        self.fix_and_commit()
        self.claude(_mul2_entry())
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        git(["rm", "-q", "tests/test_review_mul2.py"], self.repo)
        git(["commit", "-q", "-m", "drop the second test"], self.repo)
        self.claude(claude_entry(approve_response(), write_tests=False))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertNotIn("recovered", out)
        self.assertNotIn("none of", out)


class CommitPathsWithoutRoot(RepoCase):
    def test_paths_of_a_branch_commit(self):  # AC-3
        self.write("tests/test_review_mul.py", TEST_REVIEW_MUL)
        self.write("src/calc.py", self.read("src/calc.py") + "\n# x\n")
        self.commit_all("two files")
        sha = git(["rev-parse", "HEAD"], self.repo).strip()
        with mock.patch("revali.gitops.git_ok", wraps=gitops.git_ok) as spy:
            paths = gitops.commit_paths(sha, self.repo)
        self.assertEqual(paths, ["src/calc.py", "tests/test_review_mul.py"])
        self.assertNotIn("--root", spy.call_args[0][0])
        self.assertEqual(
            gitops.commit_paths(sha, self.repo, diff_filter="A"), ["tests/test_review_mul.py"]
        )


class CloseStoppedRestoresEverything(unittest.TestCase):
    def setUp(self):
        from tests.helpers import rmtree_force

        self.rdir = tempfile.mkdtemp(prefix="revali close ")
        self.addCleanup(rmtree_force, self.rdir)

    def _state(self):
        return State(
            branch="b",
            base="main",
            stage="review",
            message="reviewer round 1",
            last_exit=-1,
            started_at="2026-01-01T00:00:00+0000",
            updated_at="2026-01-01T00:05:00+0000",
            rounds=[{"round": 1}],
            test_files=["tests/test_review_mul.py"],
            fixes=2,
        )

    def test_every_field_is_back_after_a_failed_write(self):  # AC-5
        from revali.pipeline import _close_stopped

        state = self._state()
        before = asdict(state)
        with mock.patch("revali.state.write_json_atomic", side_effect=PermissionError("busy")):
            with mock.patch("sys.stdout"):
                ok = _close_stopped(state, self.rdir, "stopped by user at stage 'review'")
        self.assertFalse(ok)
        self.assertEqual(asdict(state), before)

    def test_a_successful_stop_still_records_the_outcome(self):  # AC-5: unchanged
        from revali.pipeline import _close_stopped

        state = self._state()
        with mock.patch("revali.pipeline._record_history"):
            ok = _close_stopped(state, self.rdir, "stopped by user at stage 'review'")
        self.assertTrue(ok)
        saved = State.load(self.rdir)
        self.assertEqual((saved.stage, saved.last_exit), ("stopped", 1))
        self.assertEqual(saved.rounds, [{"round": 1}])
        self.assertNotEqual(saved.updated_at, "2026-01-01T00:05:00+0000")


HOLDER = (
    "import sys, time\n"
    "f = open(sys.argv[1], 'r')\n"
    "print('open', flush=True)\n"
    "time.sleep(float(sys.argv[2]))\n"
    "f.close()\n"
)


@unittest.skipUnless(sys.platform == "win32", "a plain open() blocks the rename on Windows only")
class RealReaderOnWindows(unittest.TestCase):
    def test_write_waits_for_a_child_that_holds_the_file(self):  # AC-6
        from tests.helpers import rmtree_force

        tmp = tempfile.mkdtemp(prefix="revali hold ")
        self.addCleanup(rmtree_force, tmp)
        path = os.path.join(tmp, "state.json")
        write_json_atomic(path, {"stage": "review"})
        child = subprocess.Popen(
            [sys.executable, "-c", HOLDER, path, "0.6"],
            stdout=subprocess.PIPE,
            text=True,
        )
        self.addCleanup(child.stdout.close)
        self.addCleanup(child.wait)  # cleanups run last-added first: wait, then close the pipe
        self.assertEqual(child.stdout.readline().strip(), "open")
        started = time.monotonic()
        with mock.patch("revali.state.os.replace", wraps=os.replace) as spy:
            write_json_atomic(path, {"stage": "validate"})  # the default window, 2 s
        elapsed = time.monotonic() - started
        self.assertGreater(spy.call_count, 1, "the child never blocked the rename")
        self.assertLess(elapsed, 2.0)
        self.assertEqual(State.load(tmp).stage, "validate")
        self.assertEqual([f for f in os.listdir(tmp) if f.startswith(".tmp-")], [])
