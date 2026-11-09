"""A detached `revali run` refuses a dirty working tree before it starts anything
(fix/run-checks-tree-before-detach). Starting the child is replaced by a mock in the cases
that should start, so no pipeline runs; the refusals must not reach it at all.
"""

import os
from unittest import mock

from revali import EXIT_ERROR, EXIT_OK, gitops
from revali.procs import ProcTimeout
from revali.state import State, lock_path
from tests.helpers import RepoCase, git, run_cli

HEADER = "working tree is not clean; commit or stash first:"
DEAD_PID = 999999999


class DetachedCase(RepoCase):
    def dirty(self, rel="src/calc.py"):
        path = os.path.join(self.repo, rel)
        with open(path, "a", encoding="utf-8", newline="") as fh:
            fh.write("\n# not committed\n")

    def run_detached(self):
        with mock.patch("revali.pipeline.spawn_detached", return_value=DEAD_PID) as spawn:
            code, out = run_cli(["run"])
        return code, out, spawn

    def tree_lock(self):
        return os.path.join(self.repo, ".revali", "tree.lock")

    def run_log(self):
        return os.path.join(self.rdir(), "logs", "run.log")


class Refused(DetachedCase):
    """AC-1"""

    def test_modified_file(self):
        self.dirty()
        code, out, spawn = self.run_detached()
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertIn("ERROR: " + HEADER, out)
        self.assertIn("src/calc.py", out)
        self.assertNotIn("started revali run", out)
        spawn.assert_not_called()
        self.assertFalse(os.path.isfile(lock_path(self.rdir())))
        self.assertFalse(os.path.isfile(self.tree_lock()))
        self.assertFalse(os.path.isfile(self.run_log()))
        # recorded as the child records a preflight stop (round 1, F1)
        state = State.load(self.rdir())
        self.assertEqual((state.stage, state.last_exit), ("error", EXIT_ERROR))
        self.assertTrue(state.message.startswith(HEADER), state.message)

    def test_untracked_file(self):
        self.write("notes.txt", "scratch\n")
        code, out, spawn = self.run_detached()
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertIn("notes.txt", out)
        spawn.assert_not_called()

    def test_wait_reports_the_refusal_not_the_previous_result(self):
        # round 1, F1: a branch last left ready to merge must not read as ready after a
        # refused start; `run` then `wait` ends with exit 1 and the refusal, as on main
        State(stage="ready_to_merge", message="validation 1 passed", last_exit=0, rounds=[{}]).save(
            self.rdir()
        )
        self.dirty()
        code, out, spawn = self.run_detached()
        self.assertEqual(code, EXIT_ERROR, out)
        spawn.assert_not_called()
        code, waited = run_cli(["wait", "--timeout", "5s"])
        self.assertEqual(code, EXIT_ERROR, waited)
        self.assertIn("error: " + HEADER, waited)
        self.assertNotIn("validation 1 passed", waited)
        code, status = run_cli(["status"])
        self.assertIn("stage: error", status)
        self.assertIn("src/calc.py", status)
        self.assertEqual(len(State.load(self.rdir()).rounds), 1)  # the rest of the state stays


class Tolerated(DetachedCase):
    """AC-2, AC-3, AC-4: what preflight accepts does not stop the start."""

    def test_clean_tree_starts(self):
        code, out, spawn = self.run_detached()
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("started revali run (pid %d)" % DEAD_PID, out)
        spawn.assert_called_once()

    def test_pending_needs_info_file(self):
        self.write("tests/test_review_pending.py", "# a NEEDS_INFO round's draft\n")
        State(stage="needs_action", pending_test_files=["tests/test_review_pending.py"]).save(
            self.rdir()
        )
        code, out, spawn = self.run_detached()
        self.assertEqual(code, EXIT_OK, out)
        spawn.assert_called_once()

    def test_pending_does_not_cover_other_files(self):
        self.write("tests/test_review_pending.py", "# a NEEDS_INFO round's draft\n")
        self.dirty()
        State(stage="needs_action", pending_test_files=["tests/test_review_pending.py"]).save(
            self.rdir()
        )
        code, out, spawn = self.run_detached()
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertIn("src/calc.py", out)
        self.assertNotIn("test_review_pending.py", out)
        spawn.assert_not_called()

    def test_interrupted_round_is_left_to_the_child(self):
        self.write("tests/test_review_half.py", "# an interrupted session's draft\n")
        self.dirty()
        State(stage="review", reviewer_running=True).save(self.rdir())
        code, out, spawn = self.run_detached()
        self.assertEqual(code, EXIT_OK, out)
        spawn.assert_called_once()

    def test_state_directory_does_not_count(self):
        # round 1, F3: the fixture ignores .revali/, which would hide the file from git
        # status whatever the filter does; without the ignore line git reports it and only
        # the state-directory filter keeps it out
        self.write(".gitignore", ".venv/\n__pycache__/\n")
        self.commit_all("state directory not ignored")
        self.write(".revali/feature__mul/notes.md", "my notes\n")
        status = git(["status", "--porcelain", "--untracked-files=all"], self.repo)
        self.assertIn(".revali/", status)
        code, out, spawn = self.run_detached()
        self.assertEqual(code, EXIT_OK, out)
        spawn.assert_called_once()


class LeftToTheChild(DetachedCase):
    """round 1, F2: when the check itself cannot answer, the child starts and reports."""

    def test_state_file_that_is_not_an_object(self):
        self.dirty()
        os.makedirs(self.rdir(), exist_ok=True)
        with open(State.path(self.rdir()), "w", encoding="utf-8", newline="") as fh:
            fh.write("[1, 2]\n")
        code, out, spawn = self.run_detached()
        self.assertEqual(code, EXIT_OK, out)
        self.assertNotIn("Traceback", out)
        spawn.assert_called_once()

    def test_git_status_failing_or_timing_out(self):
        self.dirty()
        for exc in (gitops.GitError("status failed"), ProcTimeout("timed out after 120s: git")):
            with mock.patch("revali.gitops.dirty_paths", side_effect=exc):
                code, out, spawn = self.run_detached()
            self.assertEqual(code, EXIT_OK, (exc, out))
            spawn.assert_called_once()
            for name in (lock_path(self.rdir()), self.tree_lock()):
                if os.path.isfile(name):
                    os.remove(name)  # the mocked child never runs to release them


class SameAsPreflight(DetachedCase):
    """AC-5, AC-6: the foreground and dry runs keep preflight's refusal, with the same text."""

    def block(self, out):
        lines = out.splitlines()
        start = next(i for i, ln in enumerate(lines) if HEADER in ln)
        rest = [ln for ln in lines[start + 1 :] if ln.startswith("  ")]
        return [lines[start].split(HEADER, 1)[1]] + rest

    def test_foreground_and_detached_say_the_same(self):
        self.dirty()
        self.write("notes.txt", "scratch\n")
        code, detached, _ = self.run_detached()
        self.assertEqual(code, EXIT_ERROR, detached)
        code, foreground = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR, foreground)
        self.assertIn(HEADER, foreground)
        self.assertEqual(self.block(detached), self.block(foreground))

    def test_dry_run_still_refuses_in_preflight(self):
        self.dirty()
        with mock.patch("revali.pipeline.spawn_detached") as spawn:
            code, out = run_cli(["run", "--dry-run"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertIn(HEADER, out)
        spawn.assert_not_called()


class StatusShowsWholeMessage(DetachedCase):
    """AC-7"""

    def test_every_line_of_a_multi_line_stop(self):
        self.dirty()
        self.write("notes.txt", "scratch\n")
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR, out)
        code, status = run_cli(["status"])
        self.assertIn("message: " + HEADER, status)
        self.assertIn("src/calc.py", status)
        self.assertIn("notes.txt", status)


class Docs(RepoCase):
    """AC-8"""

    def test_workflow_md(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "docs", "workflow.md"), "r", encoding="utf-8") as fh:
            text = " ".join(fh.read().split())
        self.assertIn("before it detaches", text)
