"""release/0.7.0: the 0.7.0 changelog section (AC-2 to AC-4, AC-7), the audit's lows
(AC-5) and the two must-includes #119 left (AC-6). The version itself is pinned in the
tests docs/releasing.md names. Documents are read with their whitespace folded, so a rewrap
does not matter but dropping a corrected statement does. Headings are looked up, never
taken by position, so a newer section does not break these tests."""

import hashlib
import os
import re
import unittest

from revali import EXIT_ERROR
from tests.helpers import ROOT, git
from tests.helpers import run_cli as run
from tests.test_release_branch import TwoBranches

NEW = "## 0.7.0 - 2026-11-23"
PREVIOUS = "## 0.6.0 - 2026-11-23"
# every PR merged between v0.6.0 and this change (`git log --oneline v0.6.0..main`)
RELEASE_PRS = [119, 120]
# sha256 of CHANGELOG.md from the 0.6.0 heading to the end, as released. A deliberate edit
# of a released section updates this hash.
RELEASED_SHA256 = "0310b88b8c9ebe2505e13ce31911d3fb3843a75627b09e62fef93f1dbfd26da8"
# sha256 of the preamble, CHANGELOG.md up to its first section whichever release that is,
# as v0.6.0 has it (review round 1 F1: not up to the 0.7.0 heading, which a later section
# would move)
PREAMBLE_SHA256 = "5bd589852800d0c0b8380e264e8c2ff86e7ec7eacd1d239100447f28ab786b0d"


def raw(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8", newline="") as fh:
        return fh.read()


def flat(*parts):
    return " ".join(raw(*parts).split())


def section_070():
    text = raw("CHANGELOG.md")
    return text.split("\n%s\n" % NEW, 1)[1].split("\n## ", 1)[0]


def part(name):
    """The lines of the 0.7.0 section from the line that starts with `name` to the next
    blank line, folded."""
    body = section_070()
    start = body.index("\n" + name)
    return " ".join(body[start:].split("\n\n", 1)[0].split())


class Changelog(unittest.TestCase):
    """AC-2 to AC-4, AC-7"""

    def test_the_new_section_is_directly_above_0_6_0(self):
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
        named = {int(n) for n in re.findall(r"#(\d+)\b", section_070())}
        self.assertEqual(sorted(set(RELEASE_PRS) - named), [], "PRs left out")
        self.assertEqual(sorted(named - set(RELEASE_PRS)), [], "PRs not of this release")

    def test_opens_with_one_lead_sentence(self):
        lead = " ".join(section_070().strip().split("\n\n", 1)[0].split())
        self.assertEqual(len(re.findall(r"\.(\s|$)", lead)), 1, lead)
        for word in ("`revali release`", "releases from another", "existing tests"):
            self.assertIn(word, lead)

    def test_upgrading(self):
        text = part("Upgrading from 0.6.0")
        for needle in (
            "PROMPT_VERSION goes from 17 to 18 (#120)",
            "existing tests the diff does not modify",
            "a file left out by `[review] exclude` is not listed",
            "closes the 0.6.0 known issue",
            '`[project] release_branch` (#119), default `""`',
            "`revali release --dry-run` prints a first line",
            "`defaults.toml` apart from that key",
            "exit codes",
            "`state.json`",
        ):
            with self.subTest(needle=needle):
                self.assertIn(needle, text)

    def test_known_issue_is_the_modified_tests_line(self):
        text = part("Known issues")
        self.assertIn('"Existing test files this diff modifies"', text)
        self.assertIn("`[review] exclude`", text)
        # the 0.6.0 known issue is closed, not carried forward
        self.assertNotIn("this diff touches", text)

    def test_the_prompt_line_the_known_issue_quotes_is_still_there(self):
        self.assertIn("Existing test files this diff modifies", raw("prompts", "review.md"))


class AuditLows(unittest.TestCase):
    """AC-5"""

    def test_readme_baseline_not_on_dry_run(self):
        text = flat("README.md")
        self.assertIn(
            "until the first review round is recorded, and not for `kind: docs` or "
            "`run --dry-run`.",
            text,
        )

    def test_releasing_notes_may_be_left_out(self):
        text = flat("docs", "releasing.md")
        self.assertIn(
            "`--notes` may be left out: the tag message is then `<repository name> <version>` "
            "and the Release carries the generated notes alone.",
            text,
        )

    def test_releasing_gh_line_carries_the_tag(self):
        text = flat("docs", "releasing.md")
        self.assertIn(
            "`gh release create v<version> --verify-tag --title <title> --notes-file <file>`",
            text,
        )
        self.assertNotIn("`gh release create --verify-tag", text)

    def test_template_archive_dir_as_defaults(self):
        text = flat("templates", "revali.toml")
        self.assertIn("relative = under ~/.revali/, # # ~ and absolute paths as written", text)
        self.assertIn("~ and absolute paths as written", flat("defaults.toml"))


class SingleBranchNote(unittest.TestCase):
    """AC-6, the documentation half"""

    def test_releasing_from_another_branch_names_set_branches(self):
        text = raw("docs", "releasing.md")
        body = text.split("\n## Releasing from another branch\n", 1)[1].split("\n## ", 1)[0]
        body = " ".join(body.split())
        self.assertIn("cloned with a single branch", body)
        self.assertIn("`git remote set-branches --add origin develop` once", body)


class TreeReadError(TwoBranches):
    """AC-6, the code half: git cannot read the merged side's tree, and the Failed message
    carries what git said instead of an empty reason."""

    def test_the_message_carries_gits_error(self):
        # a commit straight on main makes HEAD's tree differ from develop's, so removing
        # develop's root tree object leaves everything before the tree check readable
        self.write("main-only.txt", "not on develop\n")
        self.commit_all("straight on main")
        git(["push", "-q", "origin", "main"], self.repo)
        self.merge_release()
        merged = self.parents()[1]
        tree = git(["rev-parse", "%s^{tree}" % merged], self.repo).strip()
        loose = os.path.join(self.repo, ".git", "objects", tree[:2], tree[2:])
        self.assertTrue(os.path.exists(loose), "the tree is not a loose object")
        os.chmod(loose, 0o644)
        os.remove(loose)
        code, out = run(["release", "7.8.9"])
        self.assertEqual(code, EXIT_ERROR, out)
        line = [ln for ln in out.splitlines() if "^{tree} failed:" in ln]
        self.assertEqual(len(line), 1, out)
        reason = line[0].split("^{tree} failed:", 1)[1].strip()
        self.assertTrue(reason, out)
        self.assertNotIn("Refused", out)


if __name__ == "__main__":
    unittest.main()
