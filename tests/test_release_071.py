"""release/0.7.1: the 0.7.1 changelog section (AC-2 to AC-4, AC-7), the audit's lows
(AC-5) and the docs/releasing.md wording #123 left (AC-6). The version itself is pinned in
the tests docs/releasing.md names. Documents are read with their whitespace folded, so a
rewrap does not matter but dropping a corrected statement does. Headings are looked up,
never taken by position, so a newer section does not break these tests."""

import hashlib
import os
import re
import unittest

from revali import review
from tests.helpers import ROOT

NEW = "## 0.7.1 - 2026-11-24"
PREVIOUS = "## 0.7.0 - 2026-11-23"
# every PR merged between v0.7.0 and this change (`git log --oneline v0.7.0..main`)
RELEASE_PRS = [122, 123]
# sha256 of CHANGELOG.md from the 0.7.0 heading to the end, as v0.7.0 has it. A deliberate
# edit of a released section updates this hash.
RELEASED_SHA256 = "c2813146e41e87aba4a555117e2776c4ce86903e4c78ae660a558a02aecbd136"
# sha256 of the preamble, CHANGELOG.md up to its first section whichever release that is,
# as v0.7.0 has it
PREAMBLE_SHA256 = "5bd589852800d0c0b8380e264e8c2ff86e7ec7eacd1d239100447f28ab786b0d"


def raw(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8", newline="") as fh:
        return fh.read()


def flat(*parts):
    return " ".join(raw(*parts).split())


def section_071():
    text = raw("CHANGELOG.md")
    return text.split("\n%s\n" % NEW, 1)[1].split("\n## ", 1)[0]


def part(name):
    """The lines of the 0.7.1 section from the line that starts with `name` to the next
    blank line, folded."""
    body = section_071()
    start = body.index("\n" + name)
    return " ".join(body[start:].split("\n\n", 1)[0].split())


class Changelog(unittest.TestCase):
    """AC-2 to AC-4, AC-7"""

    def test_the_new_section_is_directly_above_0_7_0(self):
        headings = [line for line in raw("CHANGELOG.md").split("\n") if line.startswith("## ")]
        self.assertEqual(headings.count(NEW), 1)
        self.assertEqual(headings[headings.index(NEW) + 1], PREVIOUS)

    def test_the_preamble_and_released_sections_are_unchanged(self):
        text = raw("CHANGELOG.md")
        released = text[text.index(PREVIOUS) :]
        self.assertEqual(hashlib.sha256(released.encode("utf-8")).hexdigest(), RELEASED_SHA256)
        preamble = text[: text.index("\n## ") + 1]
        self.assertEqual(hashlib.sha256(preamble.encode("utf-8")).hexdigest(), PREAMBLE_SHA256)

    def test_every_pr_of_the_release_and_no_other(self):
        named = {int(n) for n in re.findall(r"#(\d+)\b", section_071())}
        self.assertEqual(sorted(set(RELEASE_PRS) - named), [], "PRs left out")
        self.assertEqual(sorted(named - set(RELEASE_PRS)), [], "PRs not of this release")

    def test_opens_with_one_lead_sentence(self):
        lead = " ".join(section_071().strip().split("\n\n", 1)[0].split())
        self.assertEqual(len(re.findall(r"\.(\s|$)", lead)), 1, lead)
        for word in ("`[review] exclude`", "marked as omitted", "new path"):
            self.assertIn(word, lead)

    def test_upgrading(self):
        text = part("Upgrading from 0.7.0")
        for needle in (
            "PROMPT_VERSION goes from 18 to 20",
            "#122 (19)",
            '"Existing test files this diff modifies"',
            "`[review] exclude`",
            "closes the 0.7.0 known issue",
            "#123 (20)",
            "names the new path",
            "`git diff` both names",
            "`revali.toml` keys",
            "`defaults.toml` values",
            "exit codes",
            "`state.json`",
        ):
            with self.subTest(needle=needle):
                self.assertIn(needle, text)

    def test_the_mark_upgrading_quotes_is_the_code_s(self):
        quoted = '"%s"' % review.OMITTED_TEST_MARK
        self.assertIn(quoted, part("Upgrading from 0.7.0"))

    def test_the_prompt_line_upgrading_quotes_is_still_there(self):
        self.assertIn("Existing test files this diff modifies", raw("prompts", "review.md"))

    def test_known_issue_is_the_diagnosis_heading(self):
        text = part("Known issues")
        for needle in (
            "prompts/diagnose.md",
            '"(last $log_lines lines)"',
            "`local` runner",
            "standard output followed by standard error",
        ):
            with self.subTest(needle=needle):
                self.assertIn(needle, text)
        # the 0.7.0 known issue is closed, not carried forward
        self.assertNotIn("Existing test files", text)

    def test_known_issue_names_the_inexact_released_sentences(self):
        # review round 1 F2: the audit's two lows on released sections get a pointer here,
        # as the sections themselves are not rewritten
        text = part("Known issues")
        for needle in (
            '0.7.0\'s "`revali release --dry-run` prints a first line"',
            "docs/releasing.md has it right",
            "0.6.0's hint sentence",
            '"instead of another round"',
            "docs/workflow.md has it right",
        ):
            with self.subTest(needle=needle):
                self.assertIn(needle, text)
        changelog = raw("CHANGELOG.md")
        self.assertIn("`revali release --dry-run` prints a first line", changelog)
        self.assertIn("instead of another round (#116)", " ".join(changelog.split()))

    def test_the_heading_the_known_issue_quotes_is_still_there(self):
        self.assertIn(
            "## Output of the failed step (last $log_lines lines)", raw("prompts", "diagnose.md")
        )


class AuditLows(unittest.TestCase):
    """AC-5"""

    def test_readme_tools_need_a_budget(self):
        lines = raw("README.md").split("\n")
        for tool in (
            "python tools/review_bench.py --dry-run",
            "python tools/doc_audit.py --dry-run",
        ):
            with self.subTest(tool=tool):
                line = [ln for ln in lines if ln.startswith(tool)]
                self.assertEqual(len(line), 1, line)
                self.assertIn("without --dry-run it spends and needs --budget-usd", line[0])

    def test_sandbox_branch_characters(self):
        self.assertIn(
            "`<branch>` is the run's branch with `/` written as `__` and any other character "
            "outside `[A-Za-z0-9._-]` as `_`",
            flat("docs", "sandbox.md"),
        )

    def test_releasing_refusal_names_five_paths(self):
        self.assertIn(
            "the message names the paths that differ (the first five, then a count of the rest)",
            flat("docs", "releasing.md"),
        )

    def test_bench_third_case(self):
        text = flat("bench", "README.md")
        self.assertIn(
            "a $15 budget is only sure to reach a third case with a lower "
            "`--session-budget-usd`",
            text,
        )
        self.assertNotIn("a $15 budget runs three cases only", text)

    def test_bench_description_cut(self):
        self.assertIn(
            "cut to 200 characters ending in `...` when it is longer", flat("bench", "README.md")
        )

    def test_connect_timeout_comment_names_wsl(self):
        for parts in (("templates", "revali.toml"), ("defaults.toml",)):
            with self.subTest(file=parts):
                line = [ln for ln in raw(*parts).split("\n") if "connect_timeout_s = 15" in ln]
                self.assertEqual(len(line), 1, line)
                self.assertNotIn("ssh only", line[0])
                # the comment goes on over the next line, which starts with its own `#`
                comment = " ".join(raw(*parts).replace("#", " ").split())
                # review round 1 F1: an upper bound of each call, not a wait
                self.assertIn("wsl: each preflight probe call is bounded at this + 60 s", comment)


class PromptVersionReason(unittest.TestCase):
    """AC-6"""

    def test_releasing_gives_the_conventions_reason(self):
        text = flat("docs", "releasing.md")
        self.assertIn(
            "They are not the Reviewer's prompt or schema, so changing them does not change "
            "`PROMPT_VERSION`.",
            text,
        )
        self.assertNotIn("Neither is under `prompts/`", text)

    def test_conventions_states_the_rule_it_follows(self):
        self.assertIn(
            "A change to the reviewer's prompt as revali renders it, its schema or a checklist",
            flat("CONVENTIONS.md"),
        )


if __name__ == "__main__":
    unittest.main()
