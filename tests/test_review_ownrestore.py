"""Reviewer tests for fix/stop-restores-own-tests, AC-1..AC-4, black-box through the CLI.

Round 1 is a real round (CHANGES_REQUESTED) whose test file revali commits with its own
trailer, so the file is the reviewer's own the way the pipeline establishes it. Then:
a round 2 that modifies or deletes that file and stops at the smoke run puts it back to
HEAD and the next run passes preflight (AC-1, AC-2); a killed round 2 is cleaned by the
run after it (AC-3); an edit the author made while no session was in flight stays, and
both `run` and `reset` leave it alone (AC-4). The same file may also be pending (a
NEEDS_INFO round touched it): the round after that one restores it all the same."""

import os
import unittest

from revali import EXIT_ACTION, EXIT_ERROR, EXIT_OK
from revali.state import State
from tests.helpers import TEST_REVIEW_MUL, RepoCase, approve_response, claude_entry, git, run_cli

MUL = "tests/test_review_mul.py"
OTHER = "tests/test_review_other.py"
LEFT = "tests/test_review_left.py"
EDITED = TEST_REVIEW_MUL + "\n\n# rewritten by a later session\n"
OTHER_TEXT = TEST_REVIEW_MUL.replace("MulTests", "OtherTests")
SANDBOX_DOWN = "the sandbox host did not answer"


def blocking_finding():
    return {
        "id": "F1",
        "file": "src/calc.py",
        "line": 3,
        "severity": "high",
        "kind": "correctness",
        "text": "mul ignores negative numbers",
        "suggestion": "handle them",
    }


def session(write=None, delete=(), data=None):
    """A reviewer session that writes `write` (path -> text) and deletes `delete`."""
    entry = claude_entry(data, write_tests=False)
    if write:
        entry["write_files"] = dict(write)
    if delete:
        entry["delete_files"] = list(delete)
    return entry


def covering(*paths):
    return approve_response(
        tests=[
            {"path": p, "purpose": "acceptance", "covers": ["AC-1", "AC-2"], "expected": "per AC"}
            for p in paths
        ]
    )


class OwnFileCase(RepoCase):
    """After setUp: round 1 committed MUL as the reviewer's own, the author answered with a
    fix commit, the tree is clean and no session is in flight."""

    def setUp(self):
        super().setUp()
        self.claude(
            claude_entry(
                approve_response(verdict="CHANGES_REQUESTED", findings=[blocking_finding()])
            )
        )
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ACTION, out)
        self.assertEqual(self.state().test_files, [MUL])
        self.write("src/calc.py", self.read("src/calc.py") + "\n# negatives handled\n")
        self.commit_all("fix: handle negatives")
        self.committed = git(["show", "HEAD:" + MUL], self.repo)
        self.assertEqual(self.status(), "")

    def state(self):
        return State.load(self.rdir())

    def status(self):
        return git(["status", "--porcelain", "--untracked-files=all"], self.repo).strip()

    def run_log(self):
        return self.read(".revali/feature__mul/logs/revali.log")

    def restore_lines(self, text):
        return [ln for ln in text.splitlines() if "restored" in ln and MUL in ln]

    def stop_round_two_at_the_smoke_run(self, entry):
        self.runner_scenario({"default": 0, "errors": {"smoke-r2-1": SANDBOX_DOWN}})
        self.claude(entry)
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertIn(SANDBOX_DOWN, out)  # the smoke run is what stopped it
        return out

    def next_run_passes(self):
        self.runner_scenario({"default": 0})
        self.claude(session())
        code, out = run_cli(["run", "--foreground"])
        self.assertNotIn("not clean", out)
        self.assertEqual(code, EXIT_OK, out)
        return out


class StoppedRound(OwnFileCase):
    def test_a_modified_own_file_is_back_at_head_and_the_next_run_passes(self):  # AC-1
        before = len(self.restore_lines(self.run_log()))
        self.stop_round_two_at_the_smoke_run(
            session({MUL: EDITED, OTHER: OTHER_TEXT}, data=covering(MUL, OTHER))
        )
        self.assertEqual(self.read(MUL), self.committed)  # HEAD content again
        self.assertFalse(self.exists(OTHER))  # the untracked draft still goes, as before
        self.assertEqual(self.status(), "")
        named = self.restore_lines(self.run_log())
        self.assertGreater(len(named), before, self.run_log())  # the log names it
        self.assertFalse(self.state().reviewer_running)
        self.next_run_passes()
        self.assertEqual(git(["show", "HEAD:" + MUL], self.repo), self.committed)

    def test_a_deleted_own_file_is_back_at_head_and_the_next_run_passes(self):  # AC-2
        self.stop_round_two_at_the_smoke_run(
            session({OTHER: OTHER_TEXT}, delete=[MUL], data=covering(OTHER))
        )
        self.assertTrue(self.exists(MUL))
        self.assertEqual(self.read(MUL), self.committed)
        self.assertFalse(self.exists(OTHER))
        self.assertEqual(self.status(), "")
        self.assertTrue(self.restore_lines(self.run_log()), self.run_log())
        self.next_run_passes()

    def test_a_deleted_own_file_alone_comes_back(self):  # AC-2: nothing else written
        self.stop_round_two_at_the_smoke_run(session(delete=[MUL]))
        self.assertTrue(self.exists(MUL))
        self.assertEqual(self.read(MUL), self.committed)
        self.assertEqual(self.status(), "")


class InterruptedRound(OwnFileCase):
    def killed_round_two(self):
        """What a kill in the middle of round 2's session leaves: the flag, an edited own
        file, an untracked draft."""
        state = self.state()
        state.reviewer_running = True
        state.set_stage(self.rdir(), "review", "killed", EXIT_ERROR)
        self.write(MUL, EDITED)
        self.write(LEFT, "# half written by the killed session\n")

    def test_the_run_after_it_restores_the_file_and_passes_preflight(self):  # AC-3
        self.killed_round_two()
        before = len(self.restore_lines(self.run_log()))
        self.claude(session())
        code, out = run_cli(["run", "--foreground"])
        self.assertNotIn("not clean", out)
        self.assertEqual(code, EXIT_OK, out)
        self.assertEqual(self.read(MUL), self.committed)
        self.assertEqual(git(["show", "HEAD:" + MUL], self.repo), self.committed)
        self.assertFalse(self.exists(LEFT))
        self.assertEqual(self.status(), "")
        named = self.restore_lines(self.run_log())
        self.assertGreater(len(named), before, self.run_log())  # AC-3: named in the log
        self.assertFalse(self.state().reviewer_running)

    def test_a_deleted_file_is_restored_by_the_run_after_it_too(self):  # AC-3 with AC-2's case
        state = self.state()
        state.reviewer_running = True
        state.set_stage(self.rdir(), "review", "killed", EXIT_ERROR)
        os.remove(os.path.join(self.repo, MUL))
        self.assertEqual(self.status(), "D " + MUL)
        self.claude(session())
        code, out = run_cli(["run", "--foreground"])
        self.assertNotIn("not clean", out)
        self.assertEqual(code, EXIT_OK, out)
        self.assertEqual(self.read(MUL), self.committed)
        self.assertEqual(self.status(), "")


class AuthorsEditStays(OwnFileCase):
    """AC-4: `reviewer_running` is false here (round 1 ended with a verdict)."""

    def setUp(self):
        super().setUp()
        self.assertFalse(self.state().reviewer_running)
        self.calls = len(self.fake_calls("claude"))

    def assert_run_refuses_and_keeps_the_edit(self):
        self.claude(session())
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertIn("working tree is not clean", out)
        self.assertIn(MUL, out.split("not clean", 1)[1])
        self.assertEqual(self.read(MUL), EDITED)  # content unchanged
        self.assertEqual(len(self.fake_calls("claude")), self.calls)  # no session started

    def test_run_stops_at_preflight_and_keeps_the_edit(self):
        self.write(MUL, EDITED)
        self.assert_run_refuses_and_keeps_the_edit()
        self.assert_run_refuses_and_keeps_the_edit()  # and again: nothing flipped a flag

    def test_reset_keeps_the_edit(self):
        self.write(MUL, EDITED)
        code, out = run_cli(["reset"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertEqual(self.read(MUL), EDITED)
        self.assertIn("M " + MUL, self.status())
        self.calls = len(self.fake_calls("claude"))
        self.assert_run_refuses_and_keeps_the_edit()

    def test_reset_with_pending_files_drops_those_and_keeps_the_edit(self):
        pending = approve_response(verdict="NEEDS_INFO", questions=["Which integers?"], tests=[])
        self.claude(session({OTHER: OTHER_TEXT}, data=pending))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ACTION, out)
        self.assertEqual(self.state().pending_test_files, [OTHER])
        self.assertFalse(self.state().reviewer_running)
        self.write(MUL, EDITED)
        code, out = run_cli(["reset"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertFalse(self.exists(OTHER))  # the pending draft goes, as before
        self.assertEqual(self.read(MUL), EDITED)  # the author's edit does not


class PendingOwnFile(OwnFileCase):
    """Round 2 asks (NEEDS_INFO) after touching MUL, so MUL is the reviewer's own file and
    pending at once. The round after it must still leave MUL with its HEAD content when it
    stops (AC-2) or was killed (AC-3), deleted file included."""

    def asking_round(self, write=None, delete=()):
        data = approve_response(verdict="NEEDS_INFO", questions=["Which integers?"], tests=[])
        self.claude(session(write, delete, data))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ACTION, out)
        self.assertEqual(self.state().pending_test_files, [MUL])
        self.assertFalse(self.state().reviewer_running)

    def stop_the_round_after_it_at_the_smoke_run(self, entry):
        # the asking round has no smoke run, so whichever number the next round carries
        self.runner_scenario(
            {"default": 0, "errors": {"smoke-r2-1": SANDBOX_DOWN, "smoke-r3-1": SANDBOX_DOWN}}
        )
        self.claude(entry)
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertIn(SANDBOX_DOWN, out)

    def assert_back_at_head(self):
        self.assertTrue(self.exists(MUL))
        self.assertEqual(self.read(MUL), self.committed)
        self.assertEqual(self.status(), "")
        self.assertEqual(self.state().pending_test_files, [])

    def test_modified_while_asking_then_deleted_by_the_round_that_stops(self):  # AC-2
        self.asking_round({MUL: EDITED})
        self.stop_the_round_after_it_at_the_smoke_run(
            session({OTHER: OTHER_TEXT}, delete=[MUL], data=covering(OTHER))
        )
        self.assert_back_at_head()
        self.assertFalse(self.exists(OTHER))
        self.next_run_passes()

    def test_deleted_while_asking_and_the_round_after_it_stops(self):  # AC-2
        self.asking_round(delete=[MUL])
        self.assertFalse(self.exists(MUL))
        self.stop_the_round_after_it_at_the_smoke_run(
            session({OTHER: OTHER_TEXT}, data=covering(OTHER))
        )
        self.assert_back_at_head()
        self.next_run_passes()

    def test_deleted_while_asking_and_the_round_after_it_was_killed(self):  # AC-3
        self.asking_round(delete=[MUL])
        state = self.state()
        state.reviewer_running = True
        state.set_stage(self.rdir(), "review", "killed", EXIT_ERROR)
        self.write(LEFT, "# half written by the killed session\n")
        self.claude(session())
        code, out = run_cli(["run", "--foreground"])
        self.assertNotIn("not clean", out)
        self.assertEqual(code, EXIT_OK, out)
        self.assertFalse(self.exists(LEFT))
        self.assert_back_at_head()
        self.assertEqual(git(["show", "HEAD:" + MUL], self.repo), self.committed)


if __name__ == "__main__":
    unittest.main()
