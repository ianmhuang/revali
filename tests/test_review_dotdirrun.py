"""Reviewer acceptance tests for fix/test-dir-spelling (AC-1, AC-2): a project whose
revali.toml spells `[project] test_dir` as `./tests` goes through a whole run as one that
spells it `tests`. End to end through the CLI on the fixture repository (fake gh, claude and
runner, real git), in commit mode and in archive mode, plus the prompt for other spellings.

The spelling and the documents are committed on main (pushed), so neither is in the
branch's diff: a `./tests` in the reviewer's prompt can only come from the configuration,
and a "mentions" line only from the related documents.
"""

import os
import unittest

from revali import EXIT_OK
from revali.preflight import preflight
from revali.review import build_prompt
from revali.state import State
from tests.fixtures.make_sample_repo import toml_str
from tests.helpers import TEST_REVIEW_MUL, RepoCase, claude_entry, git, run_cli

FILE = "tests/test_review_mul.py"
PLAIN = 'test_dir = "tests"'
EXCLUDE = 'exclude = ["*.lock"]\n'
MUL = "\n\ndef mul(a, b):\n    return a * b\n\n\nkept_value = 1\n"


def mentioned(prompt):
    """Every line of the prompt that lists a mention (the related documents)."""
    return "\n".join(line for line in prompt.splitlines() if "mentions `" in line)


class DotSpellingCase(RepoCase):
    """main carries `test_dir = "./tests"` and a guide that names one word a source file
    adds (`kept_value`) and one only a file under tests/ adds (`probe_token`); feature/mul
    restarts from it with both files changed."""

    review_line = ""

    def setUp(self):
        super().setUp()
        os.environ["REVALI_POLL_SECONDS"] = "0.01"
        git(["checkout", "-q", "main"], self.repo)
        toml = self.read("revali.toml")
        self.assertIn(PLAIN, toml)
        self.assertIn(EXCLUDE, toml)
        toml = toml.replace(PLAIN, 'test_dir = "./tests"', 1)
        self.write("revali.toml", toml.replace(EXCLUDE, EXCLUDE + self.review_line, 1))
        self.write("README.md", "# sample\n\n[guide](docs/guide.md)\n")
        self.write("docs/guide.md", "# guide\n\nSet `kept_value`.\nTests use `probe_token`.\n")
        self.commit_all("the spelling and the documents on the base")
        git(["push", "-q", "origin", "main"], self.repo)
        git(["checkout", "-q", "-B", "feature/mul", "main"], self.repo)
        self.write("src/calc.py", self.read("src/calc.py") + MUL)
        self.write("tests/test_probe.py", "probe_token = 1\n")
        self.commit_all("Add mul")

    def head(self):
        return git(["rev-parse", "HEAD"], self.repo).strip()

    def steps(self, label):
        """The step names of every fake sandbox session run under `label`, in order."""
        return [c["steps"] for c in self.fake_calls("runner") if c["label"] == label]


class CommitMode(DotSpellingCase):
    def test_a_run_ends_ready_as_with_the_plain_spelling(self):
        self.claude(claude_entry())
        reviewed = self.head()  # the commit the baseline runs on
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        # the reviewer's file lies under test_dir: committed, not reverted as outside it
        self.assertIn(FILE, git(["ls-files", "--", "tests"], self.repo).splitlines())
        self.assertEqual(self.read(FILE), TEST_REVIEW_MUL)
        self.assertEqual(git(["rev-parse", "HEAD~1"], self.repo).strip(), reviewed)
        state = State.load(self.rdir())
        self.assertEqual(state.test_files, [FILE])
        self.assertEqual(state.validations[0]["result"], "PASS")
        # AC-2: the reviewer's commit touches only tests/, so validation reuses the baseline
        self.assertEqual(state.baseline_sha, reviewed)
        self.assertEqual(self.steps("baseline"), [["test"]])
        self.assertEqual(self.steps("validate-r1"), [["new_test"]])
        sessions = self.fake_calls("claude")
        self.assertEqual(len(sessions), 1, len(sessions))
        prompt = sessions[0]["prompt"]
        # AC-1: the prompt names the directory in its plain form
        self.assertIn("`tests/`", prompt)
        self.assertNotIn("`./tests", prompt)
        # AC-2: the related documents list the source file's name, not the test file's
        listed = mentioned(prompt)
        self.assertIn("`kept_value`", listed)
        self.assertNotIn("`probe_token`", listed)


class ArchiveMode(DotSpellingCase):
    review_line = 'tests = "archive"\n'

    def test_the_reviewers_file_is_archived_and_validated(self):
        self.claude(claude_entry())
        reviewed = self.head()
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertEqual(self.head(), reviewed)  # archive mode commits nothing
        self.assertEqual(git(["status", "--porcelain"], self.repo).strip(), "")
        self.assertFalse(self.exists(FILE))
        archived = os.path.join(self.rdir(), "tests", "tests", "test_review_mul.py")
        self.assertTrue(os.path.isfile(archived), archived)
        self.assertEqual(State.load(self.rdir()).test_files, [FILE])
        validate = [c for c in self.fake_calls("runner") if c["label"] == "validate-r1"]
        self.assertEqual(len(validate), 1, validate)
        self.assertEqual(validate[0]["extra_files"], [FILE])
        self.assertEqual(validate[0]["steps"], ["new_test"])  # AC-2: the baseline is reused


class PromptSpellings(RepoCase):
    """AC-1: whatever the spelling, the prompt tells the reviewer the plain directory."""

    def test_the_prompt_names_the_plain_directory(self):
        written = PLAIN
        for value, plain in (
            ("tests/", "tests"),
            ("tests\\", "tests"),
            ("./tests/", "tests"),
            ("./tests/unit", "tests/unit"),
            ("tests\\unit", "tests/unit"),
        ):
            with self.subTest(value=value):
                toml = self.read("revali.toml")
                self.assertIn(written, toml)
                spelled = "test_dir = " + toml_str(value)
                self.write("revali.toml", toml.replace(written, spelled, 1))
                written = spelled
                self.commit_all("test_dir spelled %s" % spelled)
                ctx = preflight(self.repo)
                self.assertEqual(ctx.cfg.project.test_dir, plain)
                prompt = build_prompt(ctx, State(), self.rdir(), 1)
                self.assertIn("`%s/`" % plain, prompt)
                # the spelling as written, where the prompt puts the directory
                self.assertNotIn("`%s/`" % value, prompt)


if __name__ == "__main__":
    unittest.main()
