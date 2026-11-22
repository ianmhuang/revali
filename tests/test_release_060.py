"""release/0.6.0: the 0.6.0 changelog section (AC-2 to AC-5), the audit's medium finding on
README's issue comments (AC-6) and the audit's lows (AC-7, AC-8). The version itself is
pinned in the tests docs/releasing.md names. Documents are read with their whitespace
folded, so a rewrap does not matter but dropping a corrected statement does. Headings are
looked up, never taken by position, so a newer section does not break these tests."""

import hashlib
import os
import re
import unittest

from tests.helpers import ROOT

NEW = "## 0.6.0 - 2026-11-22"
PREVIOUS = "## 0.5.0 - 2026-11-21"
# every PR merged between v0.5.0 and this change (`git log --oneline v0.5.0..main`)
RELEASE_PRS = [114, 115, 116, 117]
# sha256 of CHANGELOG.md from the 0.5.0 heading to the end, as released. A deliberate edit
# of a released section updates this hash.
RELEASED_SHA256 = "f8a176da6bb05e7842ab0478e0141741a09aed04dd230554047b099cb431b4fd"


def raw(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8", newline="") as fh:
        return fh.read()


def flat(*parts):
    return " ".join(raw(*parts).split())


def section_060():
    text = raw("CHANGELOG.md")
    return text.split("\n%s\n" % NEW, 1)[1].split("\n## ", 1)[0]


def part(name):
    """The lines of the 0.6.0 section from the line that starts with `name` to the next
    blank line, folded."""
    body = section_060()
    start = body.index("\n" + name)
    return " ".join(body[start:].split("\n\n", 1)[0].split())


class Changelog(unittest.TestCase):
    """AC-2 to AC-5"""

    def test_the_new_section_is_directly_above_0_5_0(self):
        headings = [line for line in raw("CHANGELOG.md").split("\n") if line.startswith("## ")]
        self.assertEqual(headings.count(NEW), 1)
        self.assertEqual(headings[headings.index(NEW) + 1], PREVIOUS)

    def test_the_released_sections_are_unchanged(self):
        text = raw("CHANGELOG.md")
        released = text[text.index(PREVIOUS) :]
        self.assertEqual(hashlib.sha256(released.encode("utf-8")).hexdigest(), RELEASED_SHA256)

    def test_every_pr_of_the_release_and_no_other(self):
        named = {int(n) for n in re.findall(r"#(\d+)\b", section_060())}
        self.assertEqual(sorted(set(RELEASE_PRS) - named), [], "PRs left out")
        self.assertEqual(sorted(named - set(RELEASE_PRS)), [], "PRs not of this release")

    def test_opens_with_one_lead_sentence(self):
        lead = " ".join(section_060().strip().split("\n\n", 1)[0].split())
        self.assertEqual(len(re.findall(r"\.(\s|$)", lead)), 1, lead)
        for word in ("hint", "cost", "collapsed"):
            self.assertIn(word, lead)

    def test_upgrading(self):
        text = part("Upgrading from 0.5.0")
        for needle in (
            "PROMPT_VERSION goes from 16 to 17",
            "`internal`",
            "`[pr] collapse_headings`",
            "templates/release.yml",
            "`hint:` line",
            "defaults.toml",
            "state.json",
        ):
            with self.subTest(needle=needle):
                self.assertIn(needle, text)

    def test_the_last_fix_cycle_note(self):
        self.assertIn("stops for a human to decide (#117)", " ".join(section_060().split()))

    def test_known_issues(self):
        text = part("Known issues")
        for needle in ("prompts/review.md", "Dependency manifests", "`[review] exclude`"):
            with self.subTest(needle=needle):
                self.assertIn(needle, text)


class ReadmeIssueComments(unittest.TestCase):
    """AC-6: the audit's medium finding"""

    def test_capability_table(self):
        self.assertIn(
            "comments on the issues its validations opened or reused that a branch commit "
            "says it fixes",
            flat("README.md"),
        )
        self.assertNotIn("comments on the issues the branch fixes", flat("README.md"))

    def test_sequence_diagram(self):
        self.assertIn(
            "R->>G: comment on the issues revali opened or reused for the branch that a branch "
            "commit says it fixes\n",
            raw("README.md"),
        )


class ReadmeLows(unittest.TestCase):
    """AC-7"""

    def test_needs_info_neither_smoke_runs_nor_commits(self):
        text = raw("README.md")
        self.assertIn("smoke run of the tests (feature and fix, not on NEEDS_INFO)", text)
        self.assertIn("NEEDS_INFO leaves the tests uncommitted for the next round", text)
        self.assertIn("a NEEDS_INFO round neither smoke-runs nor commits them", flat("README.md"))

    def test_release_line_names_the_commit_without_checks(self):
        line = next(ln for ln in raw("README.md").splitlines() if "revali.py release " in ln)
        self.assertIn("no check run failed or running (none: released with a note)", line)
        self.assertNotIn("green CI", line)

    def test_diagnosis_number_is_the_validations(self):
        text = flat("README.md")
        self.assertIn(
            "n is the validation's number, `Validation <n>` in tests.md, not the review " "round's",
            text,
        )
        self.assertIn("`<n>` there being the validation's number, not the round's", text)


class OtherDocuments(unittest.TestCase):
    """AC-8"""

    def test_conventions_names_the_docgraph_read(self):
        self.assertIn(
            "of the project's Markdown documents for the documentation graph "
            "([revali/docgraph.py](revali/docgraph.py)), which run in text mode",
            flat("CONVENTIONS.md"),
        )

    def test_workflow_stage_message_and_last_cycle(self):
        self.assertIn(
            "carries a short form of it, followed by `; last fix cycle: the next run stops for "
            "a human` when that round used the last fix cycle",
            flat("docs", "workflow.md"),
        )

    def test_workflow_unavailable_lines_end_with_the_raw_path(self):
        text = flat("docs", "workflow.md")
        self.assertIn("for this run only; raw output saved to <path>`", text)
        self.assertIn("again later, or ...; raw output saved to <path>`", text)

    def test_configuration_dry_run_keeps_the_archive(self):
        self.assertIn(
            "is removed from the archive (`run --dry-run` only says so and deletes nothing)",
            flat("docs", "configuration.md"),
        )

    def test_configuration_ok_line(self):
        text = flat("docs", "configuration.md")
        self.assertIn("`ok: <title> (<kind>, <n> AC: <ids>; label <name>)`", text)
        self.assertNotIn("`ok:` line ends with `; label <name>`", text)

    def test_side_effects_tag_push(self):
        text = flat("docs", "side-effects.md")
        self.assertIn(
            "A tag already on HEAD here is not made again, and is pushed when origin lacks "
            "it; one already on origin on HEAD is neither made nor pushed",
            text,
        )
        self.assertNotIn("here or on origin, is neither made nor pushed", text)

    def test_side_effects_owner_check(self):
        self.assertIn(
            "The owner check is preflight's, so it is `run` and `preflight` that refuse such "
            "a repo; `init`, `archive` and `release` do not check the owner",
            flat("docs", "side-effects.md"),
        )

    def test_releasing_only_order(self):
        text = flat("docs", "releasing.md")
        self.assertIn(
            "audited once each in the order `--dry-run` lists them, not the order given", text
        )
        self.assertNotIn("in the list's order", text)

    def test_releasing_fallback(self):
        text = flat("docs", "releasing.md")
        self.assertIn("a fallback model named there is used as written", text)
        self.assertNotIn("The auditor runs on its model only", text)

    def test_skill_lists_the_github_actions(self):
        text = flat("skill", "SKILL.md")
        for needle in (
            "draft PR and its comments",
            "marking the PR ready",
            "an issue for a failure the base already had",
            "docs/side-effects.md",
        ):
            with self.subTest(needle=needle):
                self.assertIn(needle, text)


if __name__ == "__main__":
    unittest.main()
