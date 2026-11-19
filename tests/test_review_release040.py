"""Acceptance tests for release/0.4.0.

AC-1:  the package version is the release's, and `revali --version` and `revali version`
       print `revali <version>`.
AC-2:  CHANGELOG.md has the section `## 0.4.0 - 2026-11-19` directly above
       `## 0.3.0 - 2026-09-07`, and the released sections still close the list in order.
AC-3:  that section opens with a sentence, not a list, and names every PR merged between
       v0.3.0 and this change (#44 to #99 without #79, an issue) and no other number.
AC-10: with `[review] tests = "archive"` the diagnosis prompt gives each of the reviewer's
       test files with the path it can be read at, under `<state_dir>/<branch>/tests/` and
       relative to the repository root; with `tests = "commit"` the list is as before.
AC-11: PROMPT_VERSION is at least 16.

The version is compared as numbers and the changelog headings are looked up, so no assert
call here holds a literal of the current version: this file is not one of the version pins
docs/releasing.md lists, and a later release does not have to edit it.
"""

import os
import re
import unittest

from revali import EXIT_ACTION, EXIT_OK, NAME, PROMPT_VERSION, VERSION
from tests.helpers import (
    ROOT,
    TEST_REVIEW_MUL,
    RepoCase,
    approve_response,
    claude_entry,
    run_cli,
)

RELEASE = (0, 4, 0)
NEW = "## 0.4.0 - 2026-11-19"
RELEASED = ["## 0.3.0 - 2026-09-07", "## 0.2.0 - 2026-09-05", "## 0.1.0 - 2026-08-30"]
# `git log --oneline v0.3.0..main`: #44 to #99, and #79 is an issue, not a PR
RELEASE_PRS = sorted(set(range(44, 100)) - {79})

FILE = "tests/test_review_mul.py"
HELPER = "tests/review_data/mul.json"  # under test_dir, off test_file_pattern
HELPER_TEXT = '{"factors": [3, 4]}\n'
ARCHIVE = ".revali/feature__mul/tests/"
LIST_HEADING = "## Test files written by the reviewer"
NEXT_HEADING = "## Output of the failed step"
TEST_ID = "tests/test_review_mul.py::MulTests::test_product"


def changelog():
    with open(os.path.join(ROOT, "CHANGELOG.md"), "r", encoding="utf-8", newline="") as fh:
        return fh.read()


def headings(text):
    return [line for line in text.split("\n") if line.startswith("## ")]


def new_section(text):
    """The body of the release's section (up to the next `## ` line), or None."""
    lines = text.split("\n")
    if NEW not in lines:
        return None
    start = lines.index(NEW) + 1
    end = next((i for i in range(start, len(lines)) if lines[i].startswith("## ")), len(lines))
    return "\n".join(lines[start:end])


def diagnosis():
    return {
        "summary": "mul adds instead of multiplying",
        "cause": "code",
        "introduced_by": "branch",
        "failures": [
            {"test": TEST_ID, "cause": "code", "introduced_by": "branch", "note": "12 != 7"}
        ],
        "recommendation": "return a * b",
    }


def review_entry(files):
    """An approving round 1 that writes exactly `files` (path -> text)."""
    tests = [
        {"path": p, "purpose": "acceptance", "covers": ["AC-1", "AC-2"], "expected": "per AC"}
        for p in files
        if p.endswith(".py")
    ]
    entry = claude_entry(approve_response(tests=tests), write_tests=False)
    entry["write_files"] = dict(files)
    return entry


class Version(unittest.TestCase):
    """AC-1, AC-11"""

    def test_the_package_version_is_the_release_or_later(self):
        self.assertRegex(VERSION, r"^\d+\.\d+\.\d+$")
        self.assertGreaterEqual(tuple(int(n) for n in VERSION.split(".")), RELEASE)

    def test_the_flag_and_the_command_print_the_package_version(self):
        for argv in (["--version"], ["version"]):
            with self.subTest(argv=argv):
                code, out = run_cli(argv)
                self.assertEqual(code, EXIT_OK, out)
                self.assertEqual(out.strip(), "%s %s" % (NAME, VERSION))

    def test_prompt_version(self):
        self.assertRegex(PROMPT_VERSION, r"^\d+$")
        self.assertGreaterEqual(int(PROMPT_VERSION), 16)


class ChangelogSection(unittest.TestCase):
    """AC-2, AC-3"""

    def setUp(self):
        self.text = changelog()
        self.body = new_section(self.text)

    def test_the_section_sits_directly_above_the_previous_release(self):
        found = headings(self.text)
        self.assertIn(NEW, found)
        self.assertEqual(found.count(NEW), 1)
        self.assertEqual(found[found.index(NEW) + 1 :], RELEASED)

    def test_the_package_version_has_a_section(self):
        # AC-1 and AC-2 together: the version the package reports is a released section
        prefix = "## %s - " % VERSION
        self.assertEqual(len([h for h in headings(self.text) if h.startswith(prefix)]), 1)

    def test_every_pr_of_the_release_is_named_and_no_other_number(self):
        self.assertIsNotNone(self.body, "no section %s" % NEW)
        named = {int(n) for n in re.findall(r"#(\d+)\b", self.body)}
        self.assertEqual(sorted(set(RELEASE_PRS) - named), [], "PRs of the release not named")
        self.assertEqual(sorted(named - set(RELEASE_PRS)), [], "numbers outside the release")

    def test_it_opens_with_a_sentence_not_a_list(self):
        self.assertIsNotNone(self.body, "no section %s" % NEW)
        paragraphs = [p.strip() for p in self.body.split("\n\n") if p.strip()]
        self.assertGreater(len(paragraphs), 1, paragraphs)
        lead = paragraphs[0]
        self.assertFalse(lead.startswith(("-", "*", "#")), lead)
        self.assertNotIn("\n- ", lead)
        self.assertFalse(lead.endswith(":"), lead)
        self.assertIn(".", lead)


class DiagnosisPrompt(RepoCase):
    """AC-10: what the diagnosis session is told about the reviewer's test files, read from
    the prompt the session received."""

    def setUp(self):
        super().setUp()
        os.environ["REVALI_POLL_SECONDS"] = "0.01"

    def archive_mode(self):
        before = self.read("revali.toml")
        line = 'exclude = ["*.lock"]\n'
        after = before.replace(line, line + 'tests = "archive"\n')
        self.assertNotEqual(after, before, "the fixture's [review] table has no exclude line")
        self.write("revali.toml", after)
        self.commit_all("tests mode archive")

    def failed_validation(self, files):
        """A run whose round 1 approves and writes `files` and whose validation fails at
        `new_test`: the one diagnosis call, as the fake claude recorded it."""
        self.runner_scenario({"default": 0, "results": {"validate-r1": {"new_test": 1}}})
        self.claude(review_entry(files), claude_entry(diagnosis(), write_tests=False))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ACTION, out)
        calls = [c for c in self.fake_calls("claude") if LIST_HEADING in c["prompt"]]
        self.assertEqual(len(calls), 1, "expected one diagnosis session\n" + out)
        return calls[0]

    def listed(self, prompt):
        self.assertIn(NEXT_HEADING, prompt)
        return prompt.split(LIST_HEADING, 1)[1].split(NEXT_HEADING, 1)[0].strip()

    def test_archive_mode_gives_each_file_with_the_path_it_can_be_read_at(self):
        self.archive_mode()
        files = {FILE: TEST_REVIEW_MUL, HELPER: HELPER_TEXT}
        call = self.failed_validation(files)
        section = self.listed(call["prompt"])
        lines = section.split("\n")
        for rel, text in files.items():
            with self.subTest(rel=rel):
                # the premise: the file is in the archive only
                self.assertFalse(self.exists(rel), "%s is in the working tree" % rel)
                where = ARCHIVE + rel
                mine = [line for line in lines if where in line]
                self.assertEqual(len(mine), 1, section)
                # the line still names the file by its repository path
                self.assertIn(rel, mine[0].replace(where, ""))
                # relative to the repository root: nothing is written in front of the path
                tokens = re.findall(r"[^\s`'\"()]*" + re.escape(where), mine[0])
                self.assertEqual(tokens, [where], mine[0])
                # and it can be read there, from the directory the session runs in
                path = os.path.join(call["cwd"], *where.split("/"))
                self.assertTrue(os.path.isfile(path), path)
                with open(path, "r", encoding="utf-8", newline="") as fh:
                    self.assertEqual(fh.read(), text)
        self.assertEqual(
            os.path.normcase(os.path.realpath(call["cwd"])),
            os.path.normcase(os.path.realpath(self.repo)),
        )

    def test_commit_mode_lists_the_files_as_before(self):
        call = self.failed_validation({FILE: TEST_REVIEW_MUL})
        section = self.listed(call["prompt"])
        self.assertEqual(section, "- " + FILE)
        self.assertTrue(self.exists(FILE))


if __name__ == "__main__":
    unittest.main()
