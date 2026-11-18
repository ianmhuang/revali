"""Reviewer acceptance tests for fix/lint-wording-format-docs.

AC-1: the prompt's formatting sentence names lint only when `[project] lint` is set.
AC-2: PROMPT_VERSION is 13 or later.
AC-4: in archive mode, round 2's format call gets round 1's placed-back file and round 2's
new file.
"""

import json
import os
import unittest

from revali import EXIT_ACTION, EXIT_OK, PROMPT_VERSION
from revali.preflight import preflight
from revali.review import build_prompt
from revali.state import State
from tests.fixtures.make_sample_repo import PY
from tests.helpers import TEST_REVIEW_MUL, RepoCase, approve_response, claude_entry, run_cli

PHRASE = "formats your test files"
FIRST = "tests/test_review_mul.py"
LATER = "tests/test_review_later.py"
LATER_TEXT = TEST_REVIEW_MUL.replace("MulTests", "LaterTests")

# Records the paths it is given, one JSON list per call, in the file REVALI_TEST_ARGS_LOG
# names (outside the repository); rewrites nothing.
RECORDER = """import json
import os
import sys

with open(os.environ["REVALI_TEST_ARGS_LOG"], "a", encoding="utf-8") as fh:
    fh.write(json.dumps([a.replace("\\\\", "/") for a in sys.argv[1:]]) + "\\n")
"""
PASSING_LINT = "import sys\n\nsys.exit(0)\n"


def configure(case, fmt, lint, archive=False):
    """Commit a revali.toml with the format and / or lint line set, and the scripts they run."""
    case.args_log = os.path.join(case.tmp, "format-args.log")
    os.environ["REVALI_TEST_ARGS_LOG"] = case.args_log
    case.write("record_args.py", RECORDER)
    case.write("passing_check.py", PASSING_LINT)
    toml = case.read("revali.toml")
    if lint:
        assert 'lint = ""' in toml, toml
        toml = toml.replace('lint = ""', 'lint = "%s passing_check.py"' % PY, 1)
    if fmt:
        assert "[project]\n" in toml, toml
        toml = toml.replace(
            "[project]\n", '[project]\nformat = "%s record_args.py {files}"\n' % PY, 1
        )
    if archive:
        review_key = 'exclude = ["*.lock"]\n'
        assert review_key in toml, toml
        toml = toml.replace(review_key, review_key + 'tests = "archive"\n', 1)
    case.write("revali.toml", toml)
    case.commit_all("reviewer acceptance test configuration")


def prompt_of(case):
    return build_prompt(preflight(case.repo), State(), case.rdir(), 1)


def sentence_lines(prompt):
    return [line for line in prompt.splitlines() if PHRASE in line]


def format_calls(case):
    if not os.path.isfile(case.args_log):
        return []
    with open(case.args_log, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def answer(verdict, **files):
    """A reviewer answer with `verdict` that writes exactly `files` (path -> text)."""
    tests = [
        {"path": p, "purpose": "acceptance", "covers": ["AC-1", "AC-2"], "expected": "per AC"}
        for p in files
    ]
    data = approve_response(tests=tests)
    if verdict == "CHANGES_REQUESTED":
        data.update(
            verdict="CHANGES_REQUESTED",
            findings=[
                {
                    "id": "F1",
                    "file": "src/calc.py",
                    "line": 3,
                    "severity": "high",
                    "kind": "correctness",
                    "text": "mul ignores negative numbers",
                    "suggestion": "handle them",
                }
            ],
        )
    entry = claude_entry(data, write_tests=False)
    entry["write_files"] = dict(files)
    return entry


class FormatSentence(RepoCase):
    """AC-1"""

    def test_format_without_lint_does_not_mention_lint(self):
        configure(self, fmt=True, lint=False)
        prompt = prompt_of(self)
        lines = sentence_lines(prompt)
        self.assertEqual(len(lines), 1, prompt)
        sentence = lines[0]
        self.assertNotIn("lint", sentence.lower())
        self.assertNotIn("Before lint", prompt)
        self.assertNotIn("lint command", prompt)  # there is no lint step to announce
        # it still says when the formatting happens (after the answer) and with what
        self.assertIn("after", sentence.lower())
        self.assertIn("answer", sentence.lower())
        self.assertIn("record_args.py {files}", sentence)

    def test_format_with_lint_keeps_the_sentence(self):
        configure(self, fmt=True, lint=True)
        prompt = prompt_of(self)
        lines = sentence_lines(prompt)
        self.assertEqual(len(lines), 1, prompt)
        self.assertEqual(
            lines[0],
            "Before lint the script formats your test files with `%s record_args.py {files}`, "
            "so you need not format them yourself." % PY,
        )
        self.assertIn("passing_check.py` must pass on your files", prompt)

    def test_no_format_no_sentence_with_or_without_lint(self):
        self.assertEqual(sentence_lines(prompt_of(self)), [])
        configure(self, fmt=False, lint=True)
        prompt = prompt_of(self)
        self.assertEqual(sentence_lines(prompt), [])
        self.assertIn("passing_check.py` must pass on your files", prompt)


class PromptVersion(unittest.TestCase):
    """AC-2"""

    def test_the_version_was_bumped(self):
        self.assertGreaterEqual(int(PROMPT_VERSION), 13)


class ArchiveFormatScope(RepoCase):
    """AC-4"""

    def setUp(self):
        super().setUp()
        os.environ["REVALI_POLL_SECONDS"] = "0.01"
        configure(self, fmt=True, lint=True, archive=True)

    def test_round_two_formats_the_placed_back_file_and_the_new_one(self):
        self.claude(answer("CHANGES_REQUESTED", **{FIRST: TEST_REVIEW_MUL}))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ACTION, out)
        self.assertEqual(format_calls(self), [[FIRST]])  # round 1: only what it wrote

        self.write("src/calc.py", self.read("src/calc.py") + "\n# negatives handled\n")
        self.commit_all("fix")
        self.claude(answer("APPROVE", **{LATER: LATER_TEXT}))  # round 2 leaves FIRST alone
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)

        calls = format_calls(self)
        self.assertGreaterEqual(len(calls), 2, calls)  # round 2 ran the format line as well
        self.assertEqual(sorted(calls[-1]), sorted([FIRST, LATER]))


if __name__ == "__main__":
    unittest.main()
