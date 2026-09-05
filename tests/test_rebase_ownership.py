"""Acceptance tests for fix/rebase-test-ownership: after a history rewrite (or on a fresh
state) the reviewer's own test files are recognised again from the commits on the branch
that carry the `Revali-Round` trailer, so the reviewer may update them; a rewrite that
drops the trailer leaves them protected."""

import json
import os
import unittest

from revali import EXIT_OK
from revali.state import State
from tests.helpers import TEST_REVIEW_MUL, RepoCase, approve_response, claude_entry, git, run_cli

HERE = os.path.dirname(os.path.abspath(__file__))
UPDATED = TEST_REVIEW_MUL.replace(
    "mul(9, 0), 0", "mul(9, 0), 0)\n        self.assertEqual(mul(0, 9), 0"
)
HOLLOW = (
    "import unittest\n\n\n"
    "class Hollow(unittest.TestCase):\n"
    "    def test_nothing(self):\n"
    "        pass\n"
)
HIGH = {
    "id": "F1",
    "file": "src/calc.py",
    "line": 3,
    "severity": "high",
    "kind": "correctness",
    "text": "wrong for negatives",
    "suggestion": "",
}
EARLIER = "Test files you wrote in earlier rounds"
NOT_YOURS = "are not yours"


def trailer_commits(repo):
    out = git(
        [
            "log",
            "--reverse",
            "--format=%H %(trailers:key=Revali-Round,valueonly)",
            "origin/main..HEAD",
        ],
        repo,
    )
    return [line.split()[0] for line in out.splitlines() if len(line.split()) == 2]


def section(prompt, marker):
    """The lines of the prompt paragraph that starts with `marker`; empty when absent."""
    if marker not in prompt:
        return ""
    return prompt.split(marker, 1)[1].split("\n\n", 1)[0]


def prompts(case):
    return [c["prompt"] for c in case.fake_calls("claude")]


class RewriteCase(RepoCase):
    """Round 1 asks for changes and commits `tests/test_review_mul.py`; subclasses rewrite."""

    def first_round(self):
        self.claude(claude_entry(approve_response(verdict="CHANGES_REQUESTED", findings=[HIGH])))
        run_cli(["run", "--foreground"])
        self.assertEqual(len(State.load(self.rdir()).test_commits), 1)
        self.first_commit = State.load(self.rdir()).test_commits[0]
        if os.path.isfile(self.fake_log):
            os.remove(self.fake_log)

    def move_main(self, name="NOTES.md"):
        git(["checkout", "-q", "main"], self.repo)
        self.write(name, "moved\n")
        self.commit_all("main moves on")
        git(["push", "-q", "origin", "main"], self.repo)
        git(["checkout", "-q", "feature/mul"], self.repo)

    def fix_and_commit(self, note="fix"):
        self.write("src/calc.py", self.read("src/calc.py") + "\n# %s\n" % note)
        self.commit_all(note)


class RebaseKeepsOwnership(RewriteCase):
    def test_rebased_test_commit_stays_the_reviewers(self):
        self.first_round()
        self.move_main()
        git(["rebase", "-q", "main"], self.repo)
        self.fix_and_commit()
        rebased = trailer_commits(self.repo)
        self.assertEqual(len(rebased), 1)
        self.assertNotEqual(rebased[0], self.first_commit)
        second = claude_entry(approve_response())
        second["write_files"]["tests/test_review_mul.py"] = UPDATED  # its own file, updated
        self.claude(second)
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("starts over", out)
        self.assertIn("recovered", out)  # AC-2: logged
        self.assertIn(rebased[0][:10], out)
        self.assertIn("tests/test_review_mul.py", out.split("recovered", 1)[1].split("\n", 1)[0])
        ps = prompts(self)
        self.assertEqual(len(ps), 1, "no bounce")  # AC-1
        self.assertIn("tests/test_review_mul.py", section(ps[0], EARLIER))
        self.assertNotIn("tests/test_review_mul.py", section(ps[0], NOT_YOURS))
        self.assertEqual(self.read("tests/test_review_mul.py"), UPDATED)
        state = State.load(self.rdir())
        self.assertEqual(state.test_files, ["tests/test_review_mul.py"])
        self.assertEqual(state.test_commits[0], rebased[0])  # AC-2: recovered
        self.assertEqual(len(state.test_commits), 2)  # plus round 1's
        self.assertEqual(git(["status", "--porcelain", "--", "tests"], self.repo).strip(), "")

    def test_second_rewrite_is_detected_again(self):
        self.first_round()
        self.move_main()
        git(["rebase", "-q", "main"], self.repo)
        self.fix_and_commit()
        self.claude(claude_entry(approve_response()))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        self.move_main("MORE.md")
        git(["rebase", "-q", "main"], self.repo)
        self.claude(claude_entry(approve_response()))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("starts over", out)  # AC-2
        state = State.load(self.rdir())
        self.assertEqual(state.test_files, ["tests/test_review_mul.py"])
        self.assertEqual(state.test_commits, trailer_commits(self.repo))
        for sha in state.test_commits:
            self.assertEqual(git(["merge-base", "--is-ancestor", sha, "HEAD"], self.repo), "")


class SquashDropsOwnership(RewriteCase):
    def test_without_trailer_the_file_is_protected(self):
        self.first_round()
        original = self.read("tests/test_review_mul.py")
        git(["reset", "-q", "--soft", "origin/main"], self.repo)
        self.commit_all("Add mul with tests")  # the author folded the reviewer's commit in
        self.assertEqual(trailer_commits(self.repo), [])
        first = claude_entry(approve_response())
        first["write_files"]["tests/test_review_mul.py"] = HOLLOW
        second = claude_entry(
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
        second["write_files"] = {
            "tests/test_review_mul2.py": TEST_REVIEW_MUL.replace("MulTests", "MulTests2")
        }
        self.claude(first, second)
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("starts over", out)
        self.assertIn("Revali-Round", out)  # AC-3: says so
        self.assertNotIn("recovered", out)
        self.assertEqual(self.read("tests/test_review_mul.py"), original)  # restored
        ps = prompts(self)
        self.assertEqual(len(ps), 2)  # bounced once
        self.assertIn("tests/test_review_mul.py", section(ps[0], NOT_YOURS))
        self.assertNotIn(EARLIER, ps[0])
        self.assertIn("tests/test_review_mul.py", ps[1].split("Corrections required", 1)[1])
        state = State.load(self.rdir())
        self.assertEqual(state.test_files, ["tests/test_review_mul2.py"])


class FreshStateRecovers(RewriteCase):
    def test_reset_then_run_knows_the_reviewers_files(self):
        self.first_round()
        code, out = run_cli(["reset"])
        self.assertEqual(code, EXIT_OK, out)
        self.fix_and_commit()
        entry = claude_entry(approve_response())
        entry["write_files"]["tests/test_review_mul.py"] = UPDATED
        self.claude(entry)
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("recovered", out)  # AC-4
        ps = prompts(self)
        self.assertEqual(len(ps), 1)
        self.assertIn("tests/test_review_mul.py", section(ps[0], EARLIER))
        self.assertEqual(self.read("tests/test_review_mul.py"), UPDATED)
        state = State.load(self.rdir())
        self.assertEqual(state.test_files, ["tests/test_review_mul.py"])
        self.assertEqual(state.test_commits[0], self.first_commit)
        self.assertEqual(len(state.test_commits), 2)

    def test_state_that_forgot_the_files_heals_on_the_next_run(self):
        """A state whose rounds are intact but whose lists lost the file (written before this
        rule existed) is repaired on the next run, without a rewrite."""
        self.first_round()
        path = State.path(self.rdir())
        with open(path, "r", encoding="utf-8", newline="") as fh:
            data = json.load(fh)
        data["test_files"] = []
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(data, fh)
        self.fix_and_commit()
        entry = claude_entry(approve_response())
        entry["write_files"]["tests/test_review_mul.py"] = UPDATED
        self.claude(entry)
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertNotIn("starts over", out)
        self.assertIn("recovered", out)  # AC-7
        self.assertEqual(len(prompts(self)), 1)
        self.assertIn("tests/test_review_mul.py", section(prompts(self)[0], EARLIER))
        self.assertEqual(self.read("tests/test_review_mul.py"), UPDATED)
        state = State.load(self.rdir())
        self.assertEqual(state.test_files, ["tests/test_review_mul.py"])
        self.assertEqual(len(state.rounds), 2)

    def test_unchanged_state_logs_nothing(self):
        """Round 2 of an ordinary branch: the recovery finds what the state already holds."""
        self.first_round()
        self.fix_and_commit()
        self.claude(claude_entry(approve_response()))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertNotIn("recovered", out)  # AC-7
        self.assertNotIn("Revali-Round", out)
        self.assertEqual(State.load(self.rdir()).test_files, ["tests/test_review_mul.py"])

    def test_new_branch_without_reviewer_commits_is_quiet(self):
        self.claude(claude_entry(approve_response()))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertNotIn("recovered", out)
        self.assertNotIn("Revali-Round", out)


class DeletedFileNotRecovered(RewriteCase):
    def test_only_files_head_tracks_count(self):
        self.first_round()
        git(["rm", "-q", "tests/test_review_mul.py"], self.repo)
        git(["commit", "-q", "-m", "drop the reviewer's test"], self.repo)
        self.move_main()
        git(["rebase", "-q", "main"], self.repo)
        self.claude(claude_entry(approve_response()))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("starts over", out)
        self.assertIn("none of", out)  # AC-5
        ps = prompts(self)
        self.assertNotIn(EARLIER, ps[0])
        state = State.load(self.rdir())
        self.assertEqual(state.test_files, ["tests/test_review_mul.py"])  # written anew
        self.assertEqual(len(state.test_commits), 2)  # the old one still counts


class ReadmeStatesTheRule(unittest.TestCase):
    def test_readme_names_the_trailer(self):
        with open(
            os.path.join(os.path.dirname(HERE), "docs", "side-effects.md"), "r", encoding="utf-8"
        ) as fh:
            text = fh.read()
        part = text.split("# What revali does to your repository", 1)[1].split("\n## ", 1)[0]
        self.assertIn("Revali-Round", part)  # AC-6
        self.assertIn("rebase", part)


if __name__ == "__main__":
    unittest.main()
