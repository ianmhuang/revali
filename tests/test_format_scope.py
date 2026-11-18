"""fix/lint-wording-format-docs: the prompt's formatting sentence does not mention lint when
`[project] lint` is empty (AC-1, AC-2), and `[project] format` runs over every file under
`test_dir` that differs from HEAD, which in archive mode includes the earlier rounds' files
placed back (AC-3 documents it, AC-4 pins it)."""

import json
import os
import unittest

from revali import EXIT_ACTION, PROMPT_VERSION
from revali.preflight import preflight
from revali.review import build_prompt
from revali.state import State
from tests.fixtures.make_sample_repo import PY
from tests.helpers import TEST_REVIEW_MUL, RepoCase, run_cli
from tests.test_reviewer_format import FORMATTER, LINTCHECK
from tests.test_tests_archive import (
    FILE,
    SECOND,
    SECOND_TEXT,
    ArchiveCase,
    approving,
    requesting_changes,
)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SENTENCE = "formats your test files"


def with_format(case, lint):
    case.fmt_log = os.path.join(case.tmp, "fmt.log")
    os.environ["REVALI_TEST_FMT_LOG"] = case.fmt_log
    case.write("fmt.py", FORMATTER)
    case.write("lintcheck.py", LINTCHECK)
    toml = case.read("revali.toml")
    if lint:
        toml = toml.replace('lint = ""', 'lint = "%s lintcheck.py"' % PY)
    toml = toml.replace("[project]\n", '[project]\nformat = "%s fmt.py {files}"\n' % PY, 1)
    case.write("revali.toml", toml)
    case.commit_all("format line%s" % (" and lint" if lint else ""))


def fmt_calls(case):
    if not os.path.isfile(case.fmt_log):
        return []
    with open(case.fmt_log, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def prompt_of(case):
    return build_prompt(preflight(case.repo), State(), case.rdir(), 1)


class Wording(RepoCase):
    def test_without_lint_the_sentence_names_no_lint(self):  # AC-1
        with_format(self, lint=False)
        prompt = prompt_of(self)
        line = [ln for ln in prompt.splitlines() if SENTENCE in ln]
        self.assertEqual(len(line), 1, prompt)
        self.assertNotIn("lint", line[0].lower())
        self.assertIn("fmt.py", line[0])
        self.assertNotIn("Before lint", prompt)

    def test_with_lint_the_sentence_is_unchanged(self):  # AC-1
        with_format(self, lint=True)
        prompt = prompt_of(self)
        self.assertIn("Before lint the script %s with `" % SENTENCE, prompt)
        self.assertRegex(prompt, r"lint command `[^`\n]*lintcheck\.py` must pass")

    def test_without_format_there_is_no_sentence(self):  # AC-1
        self.assertNotIn(SENTENCE, prompt_of(self))

    def test_prompt_version(self):  # AC-2
        self.assertGreaterEqual(int(PROMPT_VERSION), 13)


class TwoRounds(ArchiveCase):
    """Round 1 writes FILE and asks for changes; round 2 writes only SECOND."""

    def setUp(self):
        super().setUp()
        with_format(self, lint=False)

    def two_rounds(self):
        self.claude(requesting_changes(**{FILE: TEST_REVIEW_MUL}))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ACTION, out)
        self.assertEqual(fmt_calls(self), [[FILE]])
        self.write("src/calc.py", self.read("src/calc.py") + "\n# negatives handled\n")
        self.commit_all("fix")
        self.run_ok(approving(**{SECOND: SECOND_TEXT}))  # round 2 does not touch FILE
        return fmt_calls(self)[-1]


class ArchiveRounds(TwoRounds):
    def test_round_two_formats_the_placed_back_file_too(self):  # AC-4
        self.assertEqual(self.two_rounds(), sorted([FILE, SECOND]))


class CommitRounds(TwoRounds):
    """The contrast: in commit mode round 1's file is committed, so round 2 formats only
    what it wrote."""

    def setUp(self):
        super().setUp()
        self.switch_mode("commit")

    def test_round_two_formats_only_its_own_file(self):
        self.assertEqual(self.two_rounds(), [SECOND])


class Docs(unittest.TestCase):
    """AC-3"""

    def text(self, rel):
        with open(os.path.join(ROOT, "docs", rel), encoding="utf-8") as fh:
            return " ".join(fh.read().split())

    def test_configuration_md(self):
        text = self.text("configuration.md")
        para = text.split("`[project] format` (default empty)", 1)[1]
        para = para.split("`[paths] archive_dir`", 1)[0]
        self.assertNotIn("that attempt's new test files", para)
        for word in ("`test_dir`", "HEAD", 'tests = "archive"', "placed back"):
            self.assertIn(word, para)

    def test_template_comment(self):  # round 1 F1
        with open(os.path.join(ROOT, "templates", "revali.toml"), encoding="utf-8") as fh:
            line = [ln for ln in fh if ln.lstrip("# ").startswith("format =")]
        self.assertEqual(len(line), 1, line)
        self.assertNotIn("new test files", line[0])
        self.assertIn("differ from HEAD", line[0])

    def test_side_effects_md(self):
        text = self.text("side-effects.md")
        bullet = text.split("runs `[project] format` on the host", 1)[1].split(" - ", 1)[0]
        self.assertNotIn("the test files the reviewer wrote in each attempt", bullet)
        for word in ("`test_dir`", "HEAD", 'tests = "archive"', "placed back"):
            self.assertIn(word, bullet)


if __name__ == "__main__":
    unittest.main()
