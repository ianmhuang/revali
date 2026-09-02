"""Review tests for fix/rebase-test-ownership, AC-4, AC-5, AC-6 and AC-7: a fresh state
(`revali reset`, or a state directory that never saw the branch) on a branch that already
carries reviewer `Revali-Round` commits recovers the reviewer's files on its first round; a
file deleted since is not recovered; README states the ownership rule; a state whose rounds
survived but whose lists forgot a trailer file heals on the next run without a rewrite, and a
run where the state already holds everything logs nothing about it."""
import json
import os
import unittest

from tests.helpers import RepoCase, TEST_REVIEW_MUL, approve_response, claude_entry, git, rmtree_force, run_cli
from revali import EXIT_ACTION, EXIT_OK
from revali.state import State

HERE = os.path.dirname(os.path.abspath(__file__))
MUL = "tests/test_review_mul.py"
UPDATED = TEST_REVIEW_MUL + "\n# updated by the reviewer on a fresh state\n"
HIGH = {"id": "F1", "file": "src/calc.py", "line": 3, "severity": "high", "kind": "correctness",
        "text": "wrong for negatives", "suggestion": ""}
EARLIER = "Test files you wrote in earlier rounds"
NOT_YOURS = "are not yours"


def listed_after(prompt, marker):
    """The bullet paths of the prompt section whose first line contains `marker`."""
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


class FreshStateCase(RepoCase):
    def round_one(self):
        self.claude(claude_entry(approve_response(verdict="CHANGES_REQUESTED", findings=[HIGH])))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ACTION, out)
        state = State.load(self.rdir())
        self.assertEqual(len(state.test_commits), 1)
        self.original_sha = state.test_commits[0]
        if os.path.isfile(self.fake_log):
            os.remove(self.fake_log)

    def prompts(self):
        return [c["prompt"] for c in self.fake_calls("claude")]

    def run_fresh_round_updating_own_file(self):
        entry = claude_entry(approve_response())
        entry["write_files"] = {MUL: UPDATED}
        self.claude(entry)
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("recovered", out)                                                # AC-4: logged
        self.assertIn(self.original_sha[:10], out)
        prompts = self.prompts()
        self.assertEqual(len(prompts), 1, "the reviewer was sent back")                # AC-4: no bounce
        self.assertIn(MUL, listed_after(prompts[0], EARLIER))                          # AC-4: its own file
        self.assertNotIn(MUL, listed_after(prompts[0], NOT_YOURS))
        self.assertEqual(self.read(MUL), UPDATED)                                      # AC-4: not restored
        state = State.load(self.rdir())
        self.assertEqual(state.test_commits[0], self.original_sha)                     # AC-4: the same SHA
        self.assertEqual(len(state.test_commits), 2)
        self.assertEqual(state.test_files, [MUL])
        self.assertEqual(len(state.rounds), 1)
        self.assertEqual(git(["status", "--porcelain", "--", "tests"], self.repo).strip(), "")


class AfterReset(FreshStateCase):
    def test_reset_then_run_recovers_on_round_one(self):
        self.round_one()
        code, out = run_cli(["reset"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIsNone(State.load(self.rdir()))
        self.write("src/calc.py", self.read("src/calc.py") + "\n# fix\n")
        self.commit_all("fix negatives")
        self.run_fresh_round_updating_own_file()

    def test_reset_does_not_recover_a_deleted_file(self):
        self.round_one()
        git(["rm", "-q", "--", MUL], self.repo)
        git(["commit", "-q", "-m", "drop the reviewer's test"], self.repo)
        code, out = run_cli(["reset"])
        self.assertEqual(code, EXIT_OK, out)
        self.claude(claude_entry(approve_response()))          # writes the file anew
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertNotIn("recovered", out)                                             # AC-5
        prompt = self.prompts()[0]
        self.assertNotIn(EARLIER, prompt)                                              # AC-5: not the reviewer's
        self.assertNotIn(MUL, listed_after(prompt, NOT_YOURS))
        state = State.load(self.rdir())
        self.assertEqual(state.test_files, [MUL])                                      # written anew this round
        self.assertEqual(self.read(MUL), TEST_REVIEW_MUL)


class StateDirectoryThatNeverSawTheBranch(FreshStateCase):
    def test_first_round_of_an_unknown_state_recovers(self):
        self.round_one()
        rdir = self.rdir()
        for name in os.listdir(rdir):                          # keep change.md only
            if name == "change.md":
                continue
            path = os.path.join(rdir, name)
            if os.path.isdir(path):
                rmtree_force(path)
            else:
                os.remove(path)
        self.assertEqual(os.listdir(rdir), ["change.md"])
        self.run_fresh_round_updating_own_file()


class BranchWithoutReviewerCommits(FreshStateCase):
    def test_first_run_recovers_nothing_and_stamps_the_trailer(self):
        self.claude(claude_entry(approve_response()))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertNotIn("recovered", out)
        self.assertNotIn("Revali-Round", out)
        self.assertNotIn(EARLIER, self.prompts()[0])
        state = State.load(self.rdir())
        self.assertEqual(len(state.test_commits), 1)
        body = git(["show", "-s", "--format=%B", state.test_commits[0]], self.repo)
        self.assertIn("Revali-Round: 1", body)                 # what a later recovery reads


class StateThatForgotTheFiles(FreshStateCase):
    """AC-7: the recovery runs on every run, not only after a rewrite or on a fresh state."""

    def edit_state(self, **fields):
        path = State.path(self.rdir())
        with open(path, "r", encoding="utf-8", newline="") as fh:
            data = json.load(fh)
        data.update(fields)
        with open(path, "w", encoding="utf-8", newline="") as fh:
            json.dump(data, fh)

    def fix(self):
        self.write("src/calc.py", self.read("src/calc.py") + "\n# fix\n")
        self.commit_all("fix negatives")

    def test_lost_test_files_are_listed_again_without_a_rewrite(self):
        self.round_one()
        self.edit_state(test_files=[])                         # written before the rule existed
        self.fix()
        entry = claude_entry(approve_response())
        entry["write_files"] = {MUL: UPDATED}
        self.claude(entry)
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertNotIn("starts over", out)                                           # AC-7: no rewrite
        recovered = [line for line in out.splitlines() if "recovered" in line]
        self.assertEqual(len(recovered), 1, out)                                       # AC-7: listed again
        self.assertIn(MUL, recovered[0])
        self.assertIn(self.original_sha[:10], recovered[0])
        prompts = self.prompts()
        self.assertEqual(len(prompts), 1, "the reviewer was sent back")
        self.assertIn(MUL, listed_after(prompts[0], EARLIER))
        self.assertNotIn(MUL, listed_after(prompts[0], NOT_YOURS))
        self.assertEqual(self.read(MUL), UPDATED)                                      # not restored
        state = State.load(self.rdir())
        self.assertEqual(len(state.rounds), 2)                                         # rounds intact
        self.assertEqual(state.test_files, [MUL])
        self.assertEqual(state.test_commits[0], self.original_sha)
        self.assertEqual(len(state.test_commits), 2)

    def test_lost_test_commits_are_recovered_without_a_rewrite(self):
        """Only the commit list forgot the SHA; the state still names the file. The commit
        comes back in front and the log stays quiet about files that were never missing."""
        self.round_one()
        self.edit_state(test_commits=[])
        self.fix()
        self.claude(claude_entry(approve_response(), write_tests=False))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertNotIn("starts over", out)
        state = State.load(self.rdir())
        self.assertEqual(state.test_commits, [self.original_sha])                      # AC-7: healed
        self.assertEqual(state.test_files, [MUL])
        self.assertIn(MUL, listed_after(self.prompts()[0], EARLIER))

    def test_complete_state_logs_nothing_on_an_ordinary_round(self):
        self.round_one()
        before = State.load(self.rdir())
        self.fix()
        self.claude(claude_entry(approve_response(), write_tests=False))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertNotIn("recovered", out)                                             # AC-7: quiet
        self.assertNotIn("Revali-Round", out)
        self.assertNotIn("trailer", out)
        self.assertNotIn("starts over", out)
        state = State.load(self.rdir())
        self.assertEqual(state.test_commits, before.test_commits)                      # nothing reordered
        self.assertEqual(state.test_files, before.test_files)
        self.assertEqual(len(state.rounds), 2)
        self.assertIn(MUL, listed_after(self.prompts()[0], EARLIER))


class ReadmeStatesTheOwnershipRule(unittest.TestCase):
    def test_section_names_the_trailer_and_what_dropping_it_means(self):
        with open(os.path.join(os.path.dirname(HERE), "README.md"), "r", encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("## What revali does to your repository", text)
        part = text.split("## What revali does to your repository", 1)[1].split("\n## ", 1)[0]
        self.assertIn("Revali-Round", part)                                            # AC-6: the rule
        self.assertIn("rebase", part)
        self.assertIn("squash", part.lower())                                          # AC-6: dropping it
        self.assertIn("must not", part)


if __name__ == "__main__":
    unittest.main()
