"""Review tests for fix/rebase-test-ownership, AC-1, AC-2, AC-3 and AC-5: after the author
rewrites the branch history, the reviewer's own test files are read back from the commits
between the base and HEAD that carry a `Revali-Round` trailer, so the next round may update
them; a rewrite that drops the trailer leaves them protected as before."""
import os
import unittest

from tests.helpers import RepoCase, TEST_REVIEW_MUL, approve_response, claude_entry, git, run_cli
from revali import EXIT_ACTION, EXIT_ERROR, EXIT_OK
from revali.state import State

MUL = "tests/test_review_mul.py"
MUL2 = "tests/test_review_mul2.py"
OLD = "tests/test_review_old.py"
UPDATED = TEST_REVIEW_MUL + "\n# updated by the reviewer after the rewrite\n"
HOLLOW = "import unittest\n\n\nclass Hollow(unittest.TestCase):\n    def test_nothing(self):\n        pass\n"
OLD_PR_TESTS = TEST_REVIEW_MUL.replace("MulTests", "OldTests")
HIGH = {"id": "F1", "file": "src/calc.py", "line": 3, "severity": "high", "kind": "correctness",
        "text": "wrong for negatives", "suggestion": ""}
EARLIER = "Test files you wrote in earlier rounds"
NOT_YOURS = "are not yours"


def listed_after(prompt, marker):
    """The bullet paths of the prompt section whose first line contains `marker`: the lines
    starting with '- ' after that line, up to the first blank line. Empty when absent."""
    lines = prompt.splitlines()
    start = next((i for i, line in enumerate(lines) if marker in line), None)
    if start is None:
        return []
    out = []
    for line in lines[start + 1:]:
        if line.startswith("- "):
            out.append(line[2:].strip())
        elif not line.strip():
            break
    return out


class RewriteCase(RepoCase):
    """Round 1 asks for changes and commits the reviewer's `tests/test_review_mul.py` with the
    trailer; each test then rewrites the branch in its own way."""

    def round_one(self):
        self.claude(claude_entry(approve_response(verdict="CHANGES_REQUESTED", findings=[HIGH])))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ACTION, out)
        state = State.load(self.rdir())
        self.assertEqual(state.test_files, [MUL])
        self.assertEqual(len(state.test_commits), 1)
        self.original_sha = state.test_commits[0]
        self.original_text = self.read(MUL)
        self.forget_calls()

    def forget_calls(self):
        if os.path.isfile(self.fake_log):
            os.remove(self.fake_log)

    def prompts(self):
        return [c["prompt"] for c in self.fake_calls("claude")]

    def advance_main(self, files):
        """main gains a commit with `files` and origin/main follows, so a rebase moves the base."""
        git(["checkout", "-q", "main"], self.repo)
        for rel, text in files.items():
            self.write(rel, text)
        self.commit_all("main moves on")
        git(["push", "-q", "origin", "main"], self.repo)
        git(["checkout", "-q", "feature/mul"], self.repo)

    def trailer_shas(self):
        """Commits between origin/main and HEAD whose message has a Revali-Round line, oldest
        first; read from the raw message so the test does not rely on git's trailer parser."""
        shas = git(["log", "--reverse", "--format=%H", "origin/main..HEAD"], self.repo).split()
        out = []
        for sha in shas:
            body = git(["show", "-s", "--format=%B", sha], self.repo)
            if any(line.startswith("Revali-Round:") for line in body.splitlines()):
                out.append(sha)
        return out


class RebaseOntoMovedBase(RewriteCase):
    def test_rebased_file_is_still_the_reviewers(self):
        self.round_one()
        # the moved base also brings a reviewer file from an earlier PR, which stays off limits
        self.advance_main({OLD: OLD_PR_TESTS, "NOTES.md": "moved\n"})
        git(["rebase", "-q", "main"], self.repo)
        rebased = self.trailer_shas()
        self.assertEqual(len(rebased), 1)
        self.assertNotEqual(rebased[0], self.original_sha)
        entry = claude_entry(approve_response())
        entry["write_files"] = {MUL: UPDATED}          # modifies its own earlier file only
        self.claude(entry)
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        prompts = self.prompts()
        self.assertEqual(len(prompts), 1, "the reviewer was sent back")                # AC-1: no bounce
        self.assertIn(MUL, listed_after(prompts[0], EARLIER))                          # AC-1: listed as its own
        self.assertNotIn(MUL, listed_after(prompts[0], NOT_YOURS))                     # AC-1: not off limits
        self.assertIn(OLD, listed_after(prompts[0], NOT_YOURS))                        # the earlier PR's file is
        self.assertEqual(self.read(MUL), UPDATED)                                      # AC-1: not restored
        state = State.load(self.rdir())
        self.assertEqual(len(state.test_commits), 2)
        self.assertEqual(state.test_commits[0], rebased[0])                            # AC-2: the new SHA
        self.assertNotIn(self.original_sha, state.test_commits)                        # AC-2: the old one is gone
        committed = git(["show", "--name-only", "--format=", state.test_commits[1]], self.repo).split()
        self.assertEqual(committed, [MUL])                                             # AC-1: committed this round
        self.assertEqual(state.test_files, [MUL])
        self.assertEqual(git(["status", "--porcelain", "--", "tests"], self.repo).strip(), "")
        recovered = [line for line in out.splitlines() if "recovered" in line]
        self.assertEqual(len(recovered), 1, out)                                       # AC-2: the log names them
        self.assertIn(rebased[0][:10], recovered[0])
        self.assertIn(MUL, recovered[0])
        self.assertEqual(self.read(OLD), OLD_PR_TESTS)

    def test_second_rewrite_is_detected_and_recovered_again(self):
        self.round_one()
        self.advance_main({"NOTES.md": "moved\n"})
        git(["rebase", "-q", "main"], self.repo)
        entry = claude_entry(approve_response())
        entry["write_files"] = {MUL: UPDATED}
        self.claude(entry)
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        after_first = State.load(self.rdir()).test_commits
        self.assertEqual(len(after_first), 2)                       # the recovered one plus this round's
        self.forget_calls()
        self.advance_main({"MORE.md": "moved again\n"})
        git(["rebase", "-q", "main"], self.repo)
        on_branch = self.trailer_shas()
        self.assertEqual(len(on_branch), 2)
        self.claude(claude_entry(approve_response(), write_tests=False))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("starts over", out)                                              # AC-2: detected again
        self.assertIn("recovered", out)
        state = State.load(self.rdir())
        self.assertEqual(state.test_commits, on_branch)                                # AC-2: replaced, nothing stale
        for sha in after_first:
            self.assertNotIn(sha, state.test_commits)
        self.assertEqual(state.test_files, [MUL])
        self.assertIn(MUL, listed_after(self.prompts()[0], EARLIER))
        self.assertEqual(self.read(MUL), UPDATED)


class ResetAndCherryPick(RewriteCase):
    def test_reviewer_commit_replayed_onto_a_new_author_commit_keeps_ownership(self):
        # A rewrite that is not a rebase onto the base: the author redoes their own commit and
        # replays the reviewer's on top; the trailer travels with it under a new SHA.
        self.round_one()
        git(["reset", "-q", "--hard", "origin/main"], self.repo)
        self.write("src/calc.py", self.read("src/calc.py") + "\n\ndef mul(a, b):\n    return a * b\n")
        self.commit_all("Add mul, rewritten")
        git(["cherry-pick", self.original_sha], self.repo)
        replayed = self.trailer_shas()
        self.assertEqual(len(replayed), 1)
        self.assertNotEqual(replayed[0], self.original_sha)
        entry = claude_entry(approve_response())
        entry["write_files"] = {MUL: UPDATED}
        self.claude(entry)
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("starts over", out)
        self.assertEqual(len(self.prompts()), 1)                                       # AC-1: no bounce
        self.assertIn(MUL, listed_after(self.prompts()[0], EARLIER))
        self.assertEqual(self.read(MUL), UPDATED)
        state = State.load(self.rdir())
        self.assertEqual(state.test_commits[0], replayed[0])                           # AC-2
        self.assertEqual(state.test_files, [MUL])


class SquashWithoutTrailer(RewriteCase):
    def squash(self):
        """The author folds the reviewer's commit into their own: no trailer survives."""
        self.round_one()
        git(["reset", "-q", "--soft", "origin/main"], self.repo)
        self.commit_all("Add mul with tests")
        self.assertEqual(self.trailer_shas(), [])

    def test_modified_file_is_restored_and_the_session_sent_back_once(self):
        self.squash()
        first = claude_entry(approve_response())
        first["write_files"] = {MUL: HOLLOW}
        second = claude_entry(approve_response(tests=[{"path": MUL2, "purpose": "p", "covers": ["AC-1", "AC-2"],
                                                       "expected": "e"}]), write_tests=False)
        second["write_files"] = {MUL2: TEST_REVIEW_MUL.replace("MulTests", "MulTests2")}
        self.claude(first, second)
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("starts over", out)
        self.assertIn("Revali-Round", out)                                             # AC-3: the log says so
        self.assertNotIn("recovered", out)                                             # AC-3: nothing recovered
        self.assertEqual(self.read(MUL), self.original_text)                           # AC-3: restored
        prompts = self.prompts()
        self.assertEqual(len(prompts), 2)                                              # AC-3: sent back once
        self.assertIn(MUL, listed_after(prompts[0], NOT_YOURS))                        # AC-3: protected
        self.assertNotIn(EARLIER, prompts[0])
        self.assertIn(MUL, prompts[1].split("Corrections required", 1)[1])
        state = State.load(self.rdir())
        self.assertEqual(state.test_files, [MUL2])
        self.assertEqual(len(state.test_commits), 1)
        self.assertEqual(git(["show", "--name-only", "--format=", state.test_commits[0]], self.repo).split(),
                         [MUL2])

    def test_second_offence_still_ends_with_exit_1_and_no_commit(self):
        self.squash()
        head = git(["rev-parse", "HEAD"], self.repo).strip()
        entry = claude_entry(approve_response())
        entry["write_files"] = {MUL: HOLLOW}
        self.claude(entry, entry)
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR, out)                                        # AC-3: as before
        self.assertEqual(len(self.prompts()), 2)
        self.assertEqual(self.read(MUL), self.original_text)
        self.assertEqual(git(["rev-parse", "HEAD"], self.repo).strip(), head)
        self.assertEqual(git(["status", "--porcelain", "--", "tests"], self.repo).strip(), "")
        state = State.load(self.rdir())
        self.assertEqual(state.test_files, [])
        self.assertEqual(state.test_commits, [])


class DeletedFileIsNotRecovered(RewriteCase):
    def test_file_removed_later_on_the_branch_is_not_the_reviewers(self):
        self.round_one()
        git(["rm", "-q", "--", MUL], self.repo)
        git(["commit", "-q", "-m", "drop the reviewer's test"], self.repo)
        self.advance_main({"NOTES.md": "moved\n"})
        git(["rebase", "-q", "main"], self.repo)
        self.assertEqual(len(self.trailer_shas()), 1)          # the trailer commit is still there
        self.claude(claude_entry(approve_response()))          # writes the file anew (untracked now)
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("starts over", out)
        self.assertNotIn("recovered", out)                                             # AC-5
        prompt = self.prompts()[0]
        self.assertNotIn(EARLIER, prompt)                                              # AC-5: not listed as its own
        self.assertNotIn(MUL, listed_after(prompt, NOT_YOURS))                         # HEAD has no such file
        state = State.load(self.rdir())
        self.assertEqual(state.test_files, [MUL])                                      # written and committed anew
        self.assertEqual(state.test_commits[-1], git(["rev-parse", "HEAD"], self.repo).strip())
        self.assertEqual(self.read(MUL), TEST_REVIEW_MUL)


if __name__ == "__main__":
    unittest.main()
