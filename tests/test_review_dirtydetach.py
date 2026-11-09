"""Reviewer acceptance tests for fix/run-checks-tree-before-detach.

A detached `revali run` on a tree preflight would refuse prints preflight's refusal at once
and starts nothing (AC-1); what preflight tolerates still starts: a NEEDS_INFO round's pending
files (AC-2), an interrupted round's leftovers (AC-3), the state directory (AC-4). The
foreground and dry runs refuse as before (AC-5), every entry point prints the same block for
the same tree (AC-6), and `status` shows every line of the recorded refusal (AC-7).

Black-box through the CLI on the fixture repository (real git). The one patch is the spawn
seam, so a start is observed without a pipeline running. On the base branch `run` detaches
whatever the tree looks like, so every test that expects a refusal from the detached `run`
fails there; the AC-5 and AC-7 tests pin behaviour the base branch already has, as those ACs
ask.
"""

import os
from unittest import mock

from revali import EXIT_ERROR, EXIT_OK
from revali.state import State, lock_path
from tests.helpers import RepoCase, git, run_cli

HEADER = "working tree is not clean; commit or stash first:"
CHILD_PID = 999999999  # no such process on any host


class DetachCase(RepoCase):
    def detached(self):
        """`revali run` with the spawn seam replaced: (exit code, stdout, the mock)."""
        with mock.patch("revali.pipeline.spawn_detached", return_value=CHILD_PID) as spawn:
            code, out = run_cli(["run"])
        return code, out, spawn

    def block(self, out):
        """The indented entries that follow the ERROR line of a clean-tree refusal."""
        lines = out.splitlines()
        self.assertIn("ERROR: " + HEADER, lines, out)
        entries = []
        for line in lines[lines.index("ERROR: " + HEADER) + 1 :]:
            if not line.startswith("  "):
                break
            entries.append(line)
        return entries

    def paths(self, out):
        return sorted(entry.strip().split(" ", 1)[1] for entry in self.block(out))

    def edit_tracked(self, rel="src/calc.py"):
        with open(os.path.join(self.repo, rel), "a", encoding="utf-8", newline="") as fh:
            fh.write("\n# not committed\n")

    def tree_lock(self, state_dir=".revali"):
        return os.path.join(self.repo, state_dir, "tree.lock")

    def run_log(self):
        return os.path.join(self.rdir(), "logs", "run.log")

    def assert_nothing_started(self, out, spawn):
        spawn.assert_not_called()
        self.assertNotIn("started revali run", out)
        self.assertFalse(os.path.isfile(lock_path(self.rdir())), "a run lock was left")
        self.assertFalse(os.path.isfile(self.tree_lock()), "a tree lock was left")

    def assert_started(self, code, out, spawn):
        self.assertEqual(code, EXIT_OK, out)
        self.assertNotIn(HEADER, out)
        self.assertIn("started revali run (pid %d)" % CHILD_PID, out)
        spawn.assert_called_once()
        self.assertIn("--foreground", spawn.call_args[0][0])


class RefusedBeforeDetach(DetachCase):
    """AC-1"""

    def test_a_failed_add_leaves_modified_and_untracked_files(self):
        self.edit_tracked()
        self.write("src/extra.py", "VALUE = 1\n")
        code, out, spawn = self.detached()
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertEqual(self.paths(out), ["src/calc.py", "src/extra.py"])
        self.assert_nothing_started(out, spawn)
        self.assertIsNone(State.load(self.rdir()))
        self.assertFalse(os.path.isfile(self.run_log()))

    def test_a_staged_addition_and_a_deleted_file_count(self):
        self.write("src/staged.py", "VALUE = 2\n")
        git(["add", "src/staged.py"], self.repo)
        os.remove(os.path.join(self.repo, "README.md"))
        code, out, spawn = self.detached()
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertEqual(self.paths(out), ["README.md", "src/staged.py"])
        self.assert_nothing_started(out, spawn)

    def test_the_previous_result_and_log_stay_as_they_were(self):
        State(stage="needs_action", message="changes requested in round 1", last_exit=2).save(
            self.rdir()
        )
        os.makedirs(os.path.dirname(self.run_log()), exist_ok=True)
        with open(self.run_log(), "wb") as fh:
            fh.write(b"the earlier run's output\n")
        with open(State.path(self.rdir()), "rb") as fh:
            state_before = fh.read()
        self.edit_tracked()
        code, out, spawn = self.detached()
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertEqual(self.paths(out), ["src/calc.py"])
        self.assert_nothing_started(out, spawn)
        with open(State.path(self.rdir()), "rb") as fh:
            self.assertEqual(fh.read(), state_before)
        state = State.load(self.rdir())
        self.assertEqual((state.stage, state.last_exit), ("needs_action", 2))
        with open(self.run_log(), "rb") as fh:
            self.assertEqual(fh.read(), b"the earlier run's output\n")


class WhatPreflightToleratesStillStarts(DetachCase):
    """AC-2, AC-3, AC-4. Each case also shows the refusal next to it, so it fails on the
    base branch, where nothing is refused."""

    def test_pending_needs_info_files_and_nothing_else(self):  # AC-2
        pending = "tests/test_review_draft.py"
        self.write(pending, "# the reviewer's draft from a NEEDS_INFO round\n")
        State(stage="needs_action", last_exit=2, pending_test_files=[pending]).save(self.rdir())
        code, out, spawn = self.detached()
        self.assert_started(code, out, spawn)

        self.edit_tracked()
        code, out, spawn = self.detached()
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertEqual(self.paths(out), ["src/calc.py"])  # the pending file is not listed
        spawn.assert_not_called()
        # the same view of the same tree as the preflight command (AC-6)
        code, preflight_out = run_cli(["preflight"])
        self.assertEqual(code, EXIT_ERROR, preflight_out)
        self.assertEqual(self.block(out), self.block(preflight_out))

    def test_an_interrupted_round_is_left_to_the_child(self):  # AC-3
        self.write("tests/test_review_half.py", "# an interrupted session's draft\n")
        self.edit_tracked()
        edited = self.read("src/calc.py")
        State(stage="needs_action", last_exit=2).save(self.rdir())
        code, out, spawn = self.detached()  # no interrupted round: the same tree is refused
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertEqual(self.paths(out), ["src/calc.py", "tests/test_review_half.py"])
        spawn.assert_not_called()

        State(stage="review", reviewer_running=True).save(self.rdir())
        code, out, spawn = self.detached()
        self.assert_started(code, out, spawn)
        # the cleanup is the child's: the parent touched nothing
        self.assertTrue(self.exists("tests/test_review_half.py"))
        self.assertEqual(self.read("src/calc.py"), edited)
        self.assertTrue(State.load(self.rdir()).reviewer_running)

    def test_files_under_the_state_directory_do_not_count(self):  # AC-4
        # the fixture ignores .revali/; without that rule git reports what lies under it
        self.write(".gitignore", ".venv/\n__pycache__/\n")
        git(["add", ".gitignore"], self.repo)
        git(["commit", "-q", "-m", "stop ignoring the state directory"], self.repo)
        State(stage="needs_action", last_exit=2).save(self.rdir())
        self.write(".revali/feature__mul/notes.md", "my notes\n")
        status = git(["status", "--porcelain", "--untracked-files=all"], self.repo)
        self.assertIn(".revali/feature__mul/notes.md", status)
        code, out, spawn = self.detached()
        self.assert_started(code, out, spawn)

        self.write("notes.txt", "scratch\n")
        code, out, spawn = self.detached()
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertEqual(self.paths(out), ["notes.txt"])  # nothing under .revali/ is listed
        spawn.assert_not_called()

    def test_a_configured_state_directory_does_not_count(self):  # AC-4
        self.write("revali.toml", self.read("revali.toml") + '\n[paths]\nstate_dir = ".rv"\n')
        git(["add", "revali.toml"], self.repo)
        git(["commit", "-q", "-m", "state directory .rv"], self.repo)
        self.write(".rv/feature__mul/notes.md", "my notes\n")
        status = git(["status", "--porcelain", "--untracked-files=all"], self.repo)
        self.assertIn(".rv/feature__mul/notes.md", status)
        code, out, spawn = self.detached()
        self.assert_started(code, out, spawn)

        self.write("notes.txt", "scratch\n")
        code, out, spawn = self.detached()
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertEqual(self.paths(out), ["notes.txt"])
        spawn.assert_not_called()


class SameWordsAsPreflight(DetachCase):
    """AC-5, AC-6"""

    def mixed_tree(self):
        self.edit_tracked()
        self.write("notes.txt", "scratch\n")
        self.write("src/staged.py", "VALUE = 2\n")
        git(["add", "src/staged.py"], self.repo)
        os.remove(os.path.join(self.repo, "README.md"))
        return ["README.md", "notes.txt", "src/calc.py", "src/staged.py"]

    def test_foreground_and_dry_run_refuse_in_preflight_as_before(self):  # AC-5
        expected = self.mixed_tree()
        for argv in (["run", "--dry-run"], ["run", "--foreground"]):
            with self.subTest(argv=argv):
                with mock.patch("revali.pipeline.spawn_detached") as spawn:
                    code, out = run_cli(argv)
                self.assertEqual(code, EXIT_ERROR, out)
                self.assertEqual(self.paths(out), expected)
                spawn.assert_not_called()
                # the run itself stopped: its refusal is the recorded result, locks released
                state = State.load(self.rdir())
                self.assertEqual((state.stage, state.last_exit), ("error", EXIT_ERROR))
                self.assertEqual(state.message, "\n".join([HEADER] + self.block(out)))
                self.assertFalse(os.path.isfile(lock_path(self.rdir())))
                self.assertFalse(os.path.isfile(self.tree_lock()))

    def test_every_entry_point_prints_the_same_block(self):  # AC-6
        expected = self.mixed_tree()
        code, detached, spawn = self.detached()
        self.assertEqual(code, EXIT_ERROR, detached)
        spawn.assert_not_called()
        self.assertEqual(self.paths(detached), expected)
        for argv in (["preflight"], ["run", "--dry-run"], ["run", "--foreground"]):
            with self.subTest(argv=argv):
                code, out = run_cli(argv)
                self.assertEqual(code, EXIT_ERROR, out)
                self.assertEqual(self.block(detached), self.block(out))

    def test_a_long_list_is_cut_the_same_way(self):  # AC-6
        for i in range(25):
            self.write("scratch/f%02d.txt" % i, "x\n")
        code, detached, spawn = self.detached()
        self.assertEqual(code, EXIT_ERROR, detached)
        spawn.assert_not_called()
        code, foreground = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR, foreground)
        self.assertTrue(self.block(foreground))
        self.assertEqual(self.block(detached), self.block(foreground))


class StatusShowsTheWholeMessage(DetachCase):
    """AC-7"""

    def test_every_line_of_a_multi_line_stop(self):
        self.edit_tracked()
        self.write("notes.txt", "scratch\n")
        self.write("src/extra.py", "VALUE = 1\n")
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR, out)
        entries = self.block(out)
        self.assertEqual(len(entries), 3, out)
        code, status = run_cli(["status"])
        self.assertEqual(code, EXIT_OK, status)
        lines = status.splitlines()
        self.assertIn("message: " + HEADER, lines, status)
        start = lines.index("message: " + HEADER)
        self.assertEqual(lines[start + 1 : start + 1 + len(entries)], entries)
