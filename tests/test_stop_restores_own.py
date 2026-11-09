"""AC-1..AC-7 of fix/stop-restores-own-tests: a round that stops before its commit (or was
interrupted) restores the reviewer's own test files from earlier rounds that the session
modified or deleted; an author's change outside a session is left for preflight to refuse;
the lint gate sees the reviewer's untracked files when the lint line walks `git ls-files`,
and leaves them untracked afterwards."""

import os
import unittest
from unittest import mock

from revali import EXIT_ERROR, EXIT_OK
from revali.preflight import Stop, preflight
from revali.procs import ProcTimeout
from revali.review import lint_check
from revali.state import State
from tests.fixtures.make_sample_repo import PY
from tests.helpers import TEST_REVIEW_MUL, RepoCase, claude_entry, git, run_cli

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FILE = "tests/test_review_mul.py"
EDITED = TEST_REVIEW_MUL + "\n    def test_one(self):\n        self.assertEqual(mul(1, 5), 5)\n"
SANDBOX_ERROR = "wslpath failed for x: Wsl/Service/WSAETIMEDOUT"

# Lints only what git tracks, the way tinybmc's tools/ci/lint.py does: an untracked file
# is invisible to it.
LS_FILES_LINT = """import subprocess
import sys

listed = subprocess.run(
    ["git", "ls-files", "--", "tests"], capture_output=True, text=True, check=True
).stdout.split("\\n")
bad = []
for path in listed:
    if path.startswith("tests/test_review_") and path.endswith(".py"):
        with open(path, encoding="utf-8") as fh:
            if "# BAD" in fh.read():
                bad.append(path)
for path in bad:
    print("lint error in " + path)
sys.exit(1 if bad else 0)
"""
BAD_TEST = TEST_REVIEW_MUL.replace("import unittest", "import unittest  # BAD")


def porcelain(repo):
    return git(["status", "--porcelain", "--untracked-files=all"], repo)


class OwnFileCase(RepoCase):
    """FILE is the reviewer's own: a round-1 test commit with the Revali-Round trailer."""

    def setUp(self):
        super().setUp()
        self.write(FILE, TEST_REVIEW_MUL)
        git(["add", "-A"], self.repo)
        git(["commit", "-q", "-m", "test: review tests (round 1)\n\nRevali-Round: 1"], self.repo)

    def log_text(self):
        return self.read(".revali/feature__mul/logs/revali.log")

    def previous_state(self, reviewer_running, pending=()):
        state = State()
        state.test_files = [FILE]
        state.pending_test_files = list(pending)
        state.reviewer_running = reviewer_running
        stage = "stopped" if reviewer_running else "error"
        state.set_stage(self.rdir(), stage, "previous run at %s" % stage, EXIT_ERROR)

    def assert_clean_and_restored(self):
        self.assertEqual(self.read(FILE), TEST_REVIEW_MUL)
        self.assertEqual(porcelain(self.repo).strip(), "")


class StoppedRound(OwnFileCase):
    def setUp(self):
        super().setUp()
        self.runner_scenario({"default": 0, "errors": {"smoke-r1-1": SANDBOX_ERROR}})

    def test_a_modified_own_file_is_restored_and_the_next_run_passes_preflight(self):
        edit = claude_entry(write_tests=False)
        edit["write_files"] = {FILE: EDITED}
        self.claude(edit)
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR, out)  # AC-1
        self.assertIn("sandbox failed", out)
        self.assert_clean_and_restored()
        restored = [ln for ln in self.log_text().splitlines() if "of the reviewer's own" in ln]
        self.assertEqual(len(restored), 1, self.log_text())
        self.assertIn(FILE, restored[0])
        self.assertIn("] review: restored 1 test file(s)", restored[0])
        self.runner_scenario({"default": 0})
        self.claude(claude_entry(write_tests=False))
        code, out = run_cli(["run", "--foreground"])
        self.assertNotIn("not clean", out)  # AC-1: the next run gets past preflight
        self.assertEqual(code, EXIT_OK, out)

    def test_a_deleted_own_file_comes_back(self):
        drop = claude_entry(write_tests=False)
        drop["delete_files"] = [FILE]
        self.claude(drop)
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR, out)  # AC-2
        self.assertTrue(self.exists(FILE))
        self.assert_clean_and_restored()
        self.assertIn(FILE, [ln for ln in out.splitlines() if "of the reviewer's own" in ln][0])


class InterruptedRound(OwnFileCase):
    def test_the_next_run_restores_what_the_interrupted_session_changed(self):
        self.previous_state(reviewer_running=True)
        self.write(FILE, EDITED)
        self.claude(claude_entry(write_tests=False))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)  # AC-3
        self.assertNotIn("not clean", out)
        line = [ln for ln in self.log_text().splitlines() if "of the reviewer's own" in ln]
        self.assertEqual(len(line), 1, self.log_text())
        self.assertIn("] run: restored", line[0])
        self.assertIn("the interrupted run", line[0])
        self.assertIn(FILE, line[0])
        self.assertEqual(git(["show", "HEAD~0:" + FILE], self.repo), TEST_REVIEW_MUL)

    def test_reset_after_an_interrupted_round_restores_it_too(self):
        self.previous_state(reviewer_running=True)
        self.write(FILE, EDITED)
        code, out = run_cli(["reset"])
        self.assertEqual(code, EXIT_OK, out)  # AC-3 (docs: reset after an interrupted round)
        self.assert_clean_and_restored()
        self.assertIn(FILE, out)


class AuthorsChangeIsLeftAlone(OwnFileCase):
    def test_run_refuses_the_tree_and_keeps_the_change(self):
        self.previous_state(reviewer_running=False)
        self.write(FILE, EDITED)
        self.claude(claude_entry(write_tests=False))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR, out)  # AC-4
        self.assertIn("not clean", out)
        self.assertEqual(self.read(FILE), EDITED)
        self.assertNotIn("of the reviewer's own", out)
        self.assertEqual(self.fake_calls("claude"), [])

    def test_a_first_run_refuses_the_tree_and_keeps_the_change(self):
        self.write(FILE, EDITED)
        self.claude(claude_entry(write_tests=False))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR, out)  # AC-4: no state at all
        self.assertIn("not clean", out)
        self.assertEqual(self.read(FILE), EDITED)

    def test_reset_with_only_pending_files_keeps_the_change(self):
        pend = "tests/test_review_pend.py"
        self.previous_state(reviewer_running=False, pending=[pend])
        self.write(pend, "# pending from a NEEDS_INFO round\n")
        self.write(FILE, EDITED)
        code, out = run_cli(["reset"])
        self.assertEqual(code, EXIT_OK, out)  # AC-4
        self.assertFalse(self.exists(pend))  # the pending file goes as before
        self.assertEqual(self.read(FILE), EDITED)
        self.assertNotIn("of the reviewer's own", out)

    def test_reset_with_nothing_pending_keeps_the_change(self):
        self.previous_state(reviewer_running=False)
        self.write(FILE, EDITED)
        code, out = run_cli(["reset"])
        self.assertEqual(code, EXIT_OK, out)  # AC-4
        self.assertEqual(self.read(FILE), EDITED)


class LsFilesLintCase(RepoCase):
    def setUp(self):
        super().setUp()
        self.write("lintcheck.py", LS_FILES_LINT)
        self.write(
            "revali.toml",
            self.read("revali.toml").replace('lint = ""', 'lint = "%s lintcheck.py"' % PY),
        )
        self.commit_all("lint line that walks git ls-files")


class LintSeesUntrackedFiles(LsFilesLintCase):
    def entry(self, text):
        e = claude_entry(write_tests=False)
        e["write_files"] = {FILE: text}
        return e

    def test_a_bad_new_file_bounces_once(self):
        self.claude(self.entry(BAD_TEST), self.entry(TEST_REVIEW_MUL))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)  # AC-5
        cl = self.fake_calls("claude")
        self.assertEqual(len(cl), 2)
        self.assertIn("lint error in " + FILE, cl[1]["prompt"])
        self.assertNotIn("# BAD", git(["show", "HEAD:" + FILE], self.repo))

    def test_the_same_error_twice_ends_the_run(self):
        head = git(["rev-parse", "HEAD"], self.repo).strip()
        self.claude(self.entry(BAD_TEST), self.entry(BAD_TEST))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR, out)  # AC-5
        self.assertIn("lint error in " + FILE, out)
        self.assertEqual(git(["rev-parse", "HEAD"], self.repo).strip(), head)
        self.assertFalse(self.exists(FILE))  # AC-6: the stopped round still deleted it
        self.assertEqual(porcelain(self.repo).strip(), "")


class FilesStayUntracked(LsFilesLintCase):
    SPACED = "tests/test_review_my topic.py"

    def untracked(self):
        out = porcelain(self.repo)
        return {line[3:].strip('"') for line in out.splitlines() if line.startswith("?? ")}

    def check(self, text):
        ctx = preflight(self.repo)
        self.write(FILE, text)
        self.write(self.SPACED, TEST_REVIEW_MUL.replace("MulTests", "TopicTests"))
        return ctx

    def test_red_result(self):
        ctx = self.check(BAD_TEST)
        problem = lint_check(ctx, [FILE, self.SPACED], None)
        self.assertIn("lint error in " + FILE, problem)  # AC-5 at the unit level
        self.assertEqual(self.untracked(), {FILE, self.SPACED})  # AC-6

    def test_green_result(self):
        ctx = self.check(TEST_REVIEW_MUL)
        self.assertIsNone(lint_check(ctx, [FILE, self.SPACED], None))
        self.assertEqual(self.untracked(), {FILE, self.SPACED})  # AC-6

    def test_a_timeout(self):
        ctx = self.check(TEST_REVIEW_MUL)
        with mock.patch("revali.review.run_shell", side_effect=ProcTimeout("timed out")):
            with self.assertRaises(Stop):
                lint_check(ctx, [FILE, self.SPACED], None)
        self.assertEqual(self.untracked(), {FILE, self.SPACED})  # AC-6

    def test_a_modified_tracked_file_is_not_touched_in_the_index(self):
        self.write(FILE, TEST_REVIEW_MUL)
        self.commit_all("an earlier round's file")
        ctx = preflight(self.repo)
        self.write(FILE, EDITED)
        self.assertIsNone(lint_check(ctx, [FILE], None))
        self.assertEqual(porcelain(self.repo).strip(), "M " + FILE)  # unstaged, as before


class Docs(unittest.TestCase):
    def test_side_effects_lists_both(self):
        with open(os.path.join(ROOT, "docs", "side-effects.md"), encoding="utf-8") as fh:
            text = " ".join(fh.read().split())
        self.assertIn("intent-to-add (`git add -N`)", text)  # AC-7
        self.assertIn("`git reset -q -- <files>`", text)
        self.assertIn(
            "a tracked file of the reviewer's own from an earlier round that the session "
            "modified or deleted is restored from HEAD",
            text,
        )


if __name__ == "__main__":
    unittest.main()
