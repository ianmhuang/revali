"""release/0.5.0: the 0.5.0 changelog section (AC-2 to AC-5), the audit's medium finding on
changelog tests that take a heading by position (AC-6) and the audit's lows (AC-7, AC-8).
The version itself is pinned in the tests docs/releasing.md names. Documents are read with
their whitespace folded, so a rewrap does not matter but dropping a corrected statement
does. Headings are looked up, never taken by position, so a newer section does not break
these tests (AC-6)."""

import hashlib
import os
import re
import unittest

from tests.helpers import ROOT

NEW = "## 0.5.0 - 2026-11-21"
PREVIOUS = "## 0.4.0 - 2026-11-19"
# every PR merged between v0.4.0 and this change (`git log --oneline v0.4.0..main`)
RELEASE_PRS = list(range(101, 113))
PREAMBLE = (
    "# Changelog\n\nOne section per release, newest first, dated by its tag. A section is "
    "written when the\nrelease is made ([docs/releasing.md](docs/releasing.md)); changes "
    "merged since the newest\nsection are not listed until then.\n\n"
)
# sha256 of CHANGELOG.md from the 0.4.0 heading to the end, as released
RELEASED_SHA256 = "66b21d0f7edd30344f8cc84bc811bd0c7dfb6b78d967e24ceae0e50bb6eb08e4"


def raw(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8", newline="") as fh:
        return fh.read()


def flat(*parts):
    return " ".join(raw(*parts).split())


def section_050():
    text = raw("CHANGELOG.md")
    return text.split("\n%s\n" % NEW, 1)[1].split("\n## ", 1)[0]


def part(name):
    """The lines of the 0.5.0 section from the line that starts with `name` to the next
    blank line, folded."""
    body = section_050()
    start = body.index("\n" + name)
    return " ".join(body[start:].split("\n\n", 1)[0].split())


class Changelog(unittest.TestCase):
    """AC-2 to AC-5"""

    def test_the_new_section_is_directly_above_0_4_0(self):
        headings = [line for line in raw("CHANGELOG.md").split("\n") if line.startswith("## ")]
        self.assertEqual(headings.count(NEW), 1)
        self.assertEqual(headings[headings.index(NEW) + 1], PREVIOUS)

    def test_the_preamble_and_the_released_sections_are_unchanged(self):
        text = raw("CHANGELOG.md")
        self.assertTrue(text.startswith(PREAMBLE + NEW + "\n"), text[: len(PREAMBLE) + 40])
        released = text[text.index(PREVIOUS) :]
        self.assertEqual(hashlib.sha256(released.encode("utf-8")).hexdigest(), RELEASED_SHA256)

    def test_every_pr_of_the_release_and_no_other(self):
        named = {int(n) for n in re.findall(r"#(\d+)\b", section_050())}
        self.assertEqual(sorted(set(RELEASE_PRS) - named), [], "PRs left out")
        self.assertEqual(sorted(named - set(RELEASE_PRS)), [], "PRs not of this release")

    def test_opens_with_one_lead_sentence(self):
        lead = " ".join(section_050().strip().split("\n\n", 1)[0].split())
        self.assertEqual(len(re.findall(r"\.(\s|$)", lead)), 1, lead)
        for word in ("`revali init`", "`revali release`"):
            self.assertIn(word, lead)

    def test_upgrading(self):
        text = part("Upgrading from 0.4.0")
        for needle in (
            "`revali init` (#103)",
            "`revali release` (#112)",
            "not UTF-8",
            "BOM",
            "not valid TOML",
            "PROMPT_VERSION",
            "defaults.toml",
            "templates/",
            "state.json",
        ):
            with self.subTest(needle=needle):
                self.assertIn(needle, text)

    def test_known_issues(self):
        text = part("Known issues")
        for needle in ("0.2.0", "`version`", "`stats`", "`init`", "`release`"):
            with self.subTest(needle=needle):
                self.assertIn(needle, text)

    def test_the_windows_shards_are_six_since_104(self):
        self.assertIn("six shards (#104)", " ".join(section_050().split()))


class ChangelogTestsLookHeadingsUp(unittest.TestCase):
    """AC-6: the audit's medium finding"""

    def test_releasing_md_states_the_rule(self):
        self.assertIn(
            "A test that checks a changelog section looks its heading up instead of taking it "
            "by position, so the next release's section does not break it; the pin detector "
            "does not see a heading.",
            flat("docs", "releasing.md"),
        )

    def test_no_changelog_test_takes_the_first_headings(self):
        tests = os.path.join(ROOT, "tests")
        for name in sorted(os.listdir(tests)):
            if not (name.startswith("test_") and name.endswith(".py")):
                continue
            text = raw("tests", name)
            if "CHANGELOG.md" not in text:
                continue
            with self.subTest(test=name):
                self.assertIsNone(re.search(r"headings\[:\d+\]", text))


class InitDocuments(unittest.TestCase):
    """AC-7"""

    def test_readme_init_line(self):
        line = next(
            ln for ln in raw("README.md").splitlines() if "revali.py init " in ln and "#" in ln
        )
        for needle in ("missing", "CLAUDE.md block", ".gitignore"):
            with self.subTest(needle=needle):
                self.assertIn(needle, line)

    def test_files_md_claude_block(self):
        self.assertIn(
            "appended whole, heading included, to an existing `CLAUDE.md` that does not have "
            "the block's heading, in that file's line ending",
            flat("docs", "files.md"),
        )

    def test_conventions_names_the_crlf_write(self):
        self.assertIn(
            "the block `revali init` appends to an existing CLAUDE.md, which takes that "
            "file's line ending (CRLF when the file has it)",
            flat("CONVENTIONS.md"),
        )

    def test_releasing_md_release_yml_from_init(self):
        self.assertIn(
            "`revali init` writes `.github/release.yml` from "
            "[templates/release.yml](../templates/release.yml) when the project has none; a "
            "project set up without `init` copies it there by hand.",
            flat("docs", "releasing.md"),
        )


class OtherDocuments(unittest.TestCase):
    """AC-8"""

    def test_workflow_stats(self):
        self.assertIn(
            "A branch counts once per PR, by its last recorded stop: `ready_to_merge` or "
            "`merged`.",
            flat("docs", "workflow.md"),
        )

    def test_sandbox_wsl_hint(self):
        text = flat("docs", "sandbox.md")
        self.assertIn(
            'the message adds "the WSL service did not answer; `wsl --shutdown` restarts '
            "WSL, which stops every running distro, then run revali again\" (other projects' "
            "distros included); revali never runs it.",
            text,
        )

    def test_configuration_platform_named(self):
        self.assertIn(
            "stops at the first that cannot start, naming it (`validate.<name>: ...`) when "
            "more than one platform is listed.",
            flat("docs", "configuration.md"),
        )

    def test_configuration_setup_or_build(self):
        self.assertIn(
            "A `setup` or `build` step that fails on any platform stops validation at once "
            "with exit 1 (an environment problem, not a verdict): no diagnosis, and the "
            "platforms after it are not run.",
            flat("docs", "configuration.md"),
        )

    def test_configuration_reviewer_model_reasons(self):
        self.assertIn(
            "(above, the same tier as, below; or `<model> is not on the ladder`, `author "
            "model not given`, or `author <model> is not on the ladder`)",
            flat("docs", "configuration.md"),
        )

    def test_files_md_sandbox_logs(self):
        text = flat("docs", "files.md")
        self.assertIn(
            "`<label>.results` (the step results the wsl or ssh sandbox script writes and "
            "revali reads back), `<label>-clone.log` (the script's clone and checkout)",
            text,
        )


if __name__ == "__main__":
    unittest.main()
