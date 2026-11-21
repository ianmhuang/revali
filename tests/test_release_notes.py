"""M3.38: the `internal` release label, the release notes `revali release` builds from
GitHub's generated notes with the sections of `[pr] collapse_headings` collapsed, and the
Reviewer's check of a PR's release label.

Real git with a bare `origin`; the fake gh answers `repo view`, the check runs,
`releases/generate-notes` and `release view` / `release create` from the scenario.
"""

import os
import unittest

from revali import EXIT_ERROR, EXIT_OK
from revali.config import parse_project_config, release_label
from revali.release import collapse_sections
from tests.helpers import ROOT, RepoCase, git, run_cli

GREEN = [{"name": "test", "status": "completed", "conclusion": "success"}]
RELEASE_YML = "changelog:\n  categories:\n    - title: Fixes\n      labels: [patch]\n"
URL = "https://github.example/me/sample/pull/%d"
GENERATED = (
    "<!-- Release notes generated using configuration in .github/release.yml at v7.8.9 -->\n"
    "\n"
    "## What's Changed\n"
    "### Minor Changes\n"
    "* Add a command by @me in %s\n"
    "### Patch Changes\n"
    "* Fix a crash by @me in %s\n"
    "### Internal Changes\n"
    "* Split CI into shards by @me in %s\n"
    "* Speed the suite up by @me in %s\n"
    "\n"
    "\n"
    "**Full Changelog**: https://github.example/me/sample/compare/v7.8.8...v7.8.9"
) % (URL % 1, URL % 2, URL % 3, URL % 4)
COLLAPSED = (
    "<!-- Release notes generated using configuration in .github/release.yml at v7.8.9 -->\n"
    "\n"
    "## What's Changed\n"
    "### Minor Changes\n"
    "* Add a command by @me in %s\n"
    "### Patch Changes\n"
    "* Fix a crash by @me in %s\n"
    "\n"
    "<details><summary>Internal Changes (2)</summary>\n"
    "\n"
    "* Split CI into shards by @me in %s\n"
    "* Speed the suite up by @me in %s\n"
    "\n"
    "</details>\n"
    "\n"
    "\n"
    "\n"
    "**Full Changelog**: https://github.example/me/sample/compare/v7.8.8...v7.8.9"
) % (URL % 1, URL % 2, URL % 3, URL % 4)


def read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8", newline="") as fh:
        return fh.read()


def flat(*parts):
    return " ".join(read(*parts).split())


class Case(RepoCase):
    with_branch = False  # on main, pushed to origin

    def setUp(self):
        super().setUp()
        self.write(".github/release.yml", RELEASE_YML)
        self.commit_all("release notes config")
        git(["push", "-q", "origin", "main"], self.repo)
        self.scenario({"commit_checks": GREEN, "generated_notes": GENERATED})

    def head(self):
        return git(["rev-parse", "HEAD"], self.repo).strip()

    def calls(self, prefix):
        return [c for c in self.fake_calls("gh") if c["argv"][: len(prefix)] == prefix]

    def creates(self):
        return self.calls(["release", "create"])

    def generate_calls(self):
        return [
            c["argv"]
            for c in self.calls(["api"])
            if any("releases/generate-notes" in a for a in c["argv"])
        ]

    def config(self, extra):
        self.write("revali.toml", self.read("revali.toml") + "\n" + extra)
        self.commit_all("pr config")
        git(["push", "-q", "origin", "main"], self.repo)


class InternalLabel(RepoCase):
    """AC-1"""

    def test_the_default_list(self):
        cfg = parse_project_config(self.read("revali.toml"), repo_root=self.repo)
        self.assertEqual(
            cfg.pr.release_labels,
            ["major", "minor", "patch", "docs", "internal", "skip-changelog"],
        )
        self.assertEqual(cfg.pr.kind_labels, {"feature": "minor", "fix": "patch", "docs": "docs"})
        self.assertEqual(cfg.pr.collapse_headings, ["Internal Changes"])

    def test_release_internal_gives_the_label(self):
        cfg = parse_project_config(self.read("revali.toml"), repo_root=self.repo)

        class Doc:
            kind = "feature"
            release = "internal"

        self.assertEqual(release_label(cfg, Doc()), "internal")

    def test_collapse_headings_must_be_names(self):
        for value in ('[""]', '["a\\nb"]', "[1]"):
            with self.subTest(value=value):
                with self.assertRaises(Exception) as ctx:
                    parse_project_config(
                        self.read("revali.toml") + "\n[pr]\ncollapse_headings = %s\n" % value,
                        repo_root=self.repo,
                    )
                self.assertIn("collapse_headings", str(ctx.exception))


class Collapse(unittest.TestCase):
    """AC-3: the body transformation on its own"""

    def test_the_internal_section_is_collapsed_with_its_count(self):
        self.assertEqual(collapse_sections(GENERATED, ["Internal Changes"]), COLLAPSED)

    def test_a_body_without_the_heading_is_unchanged(self):
        body = GENERATED.replace("### Internal Changes", "### Other Changes")
        self.assertEqual(collapse_sections(body, ["Internal Changes"]), body)

    def test_no_headings_collapses_nothing(self):
        self.assertEqual(collapse_sections(GENERATED, []), GENERATED)

    def test_a_section_in_the_middle_keeps_the_next_heading_apart(self):
        body = GENERATED.replace("### Internal Changes", "### Docs").replace(
            "### Patch Changes", "### Internal Changes"
        )
        out = collapse_sections(body, ["Internal Changes"])
        self.assertIn(
            "<details><summary>Internal Changes (1)</summary>\n\n* Fix a crash by @me in %s\n\n"
            "</details>\n\n### Docs\n" % (URL % 2),
            out,
        )

    def test_a_heading_with_no_entries_is_left_alone(self):
        body = "## What's Changed\n### Internal Changes\n\n**Full Changelog**: x"
        self.assertEqual(collapse_sections(body, ["Internal Changes"]), body)


class Publishes(Case):
    """AC-3"""

    def test_the_published_notes(self):
        code, out = run_cli(["release", "7.8.9", "--notes", "Why this version exists."])
        self.assertEqual(code, EXIT_OK, out)
        creates = self.creates()
        self.assertEqual(len(creates), 1, creates)
        argv = creates[0]["argv"]
        self.assertEqual(argv[:4], ["release", "create", "v7.8.9", "--verify-tag"])
        self.assertEqual(argv[argv.index("--title") + 1], "v7.8.9")
        self.assertIn("--notes-file", argv)
        self.assertNotIn("--generate-notes", argv)
        self.assertNotIn("--notes", argv)
        self.assertEqual(creates[0]["body"], "Why this version exists.\n\n" + COLLAPSED)
        # the notes file is gone afterwards
        self.assertFalse(os.path.exists(argv[argv.index("--notes-file") + 1]))

    def test_the_generate_notes_request(self):
        run_cli(["release", "7.8.9"])
        calls = self.generate_calls()
        self.assertEqual(len(calls), 1, calls)
        argv = calls[0]
        self.assertEqual(argv[:3], ["api", "-X", "POST"])
        self.assertIn("repos/{owner}/{repo}/releases/generate-notes", argv)
        self.assertIn("tag_name=v7.8.9", argv)
        self.assertIn("target_commitish=%s" % self.head(), argv)

    def test_no_notes_option_publishes_the_body_alone(self):
        code, out = run_cli(["release", "7.8.9"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertEqual(self.creates()[0]["body"], COLLAPSED)

    def test_collapse_headings_empty_publishes_as_generated(self):
        self.config("[pr]\ncollapse_headings = []\n")
        code, out = run_cli(["release", "7.8.9"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertEqual(self.creates()[0]["body"], GENERATED)

    def test_another_heading_from_the_config(self):
        self.config('[pr]\ncollapse_headings = ["Patch Changes"]\n')
        code, out = run_cli(["release", "7.8.9"])
        self.assertEqual(code, EXIT_OK, out)
        body = self.creates()[0]["body"]
        self.assertIn("<details><summary>Patch Changes (1)</summary>", body)
        self.assertIn("### Internal Changes\n", body)

    def test_an_existing_release_needs_no_notes(self):
        self.scenario({"releases": {"v7.8.9": "https://github.example/me/sample/r/1"}})
        git(["tag", "-a", "v7.8.9", "-m", "x"], self.repo)
        git(["push", "-q", "origin", "refs/tags/v7.8.9"], self.repo)
        code, out = run_cli(["release", "7.8.9"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertEqual(self.generate_calls(), [])
        self.assertEqual(self.creates(), [])


class DryRunAndFailures(Case):
    """AC-4"""

    def test_the_dry_run_prints_the_notes(self):
        code, out = run_cli(["release", "7.8.9", "--notes", "Why.", "--dry-run"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn(
            "dry run: would run gh release create v7.8.9 --verify-tag --title v7.8.9 "
            '--notes-file "<the notes below>"',
            out,
        )
        notes = out.split("dry run: the release notes:\n", 1)[1]
        self.assertTrue(notes.startswith("Why.\n\n" + COLLAPSED), notes)
        self.assertEqual(len(self.generate_calls()), 1)
        self.assertEqual(self.creates(), [])
        self.assertEqual(git(["tag", "-l", "v7.8.9"], self.repo).strip(), "")

    def test_a_failed_generate_notes_call(self):
        self.scenario({"generate_notes_exit": 1})
        code, out = run_cli(["release", "7.8.9"])
        self.assertEqual(code, EXIT_ERROR, out)
        errors = [ln for ln in out.splitlines() if ln.startswith("ERROR")]
        self.assertEqual(len(errors), 1, out)
        self.assertIn("generate-notes", errors[0])
        self.assertIn("run the same command again to finish", errors[0])
        self.assertNotIn("Traceback", out)
        self.assertEqual(self.creates(), [])
        self.assertEqual(git(["tag", "-l", "v7.8.9"], self.repo).strip(), "")
        # and the rerun finishes the release
        self.scenario({"generate_notes_exit": 0})
        code, out = run_cli(["release", "7.8.9"])
        self.assertEqual(code, EXIT_OK, out)

    def test_an_answer_that_is_not_the_notes(self):
        self.scenario({"generate_notes_raw": "not json"})
        code, out = run_cli(["release", "7.8.9"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertEqual(len([ln for ln in out.splitlines() if ln.startswith("ERROR")]), 1, out)
        self.assertEqual(self.creates(), [])


class Files(unittest.TestCase):
    """AC-2, AC-5 to AC-7"""

    def test_release_yml_internal_after_other(self):
        for parts in (("templates", "release.yml"), (".github", "release.yml")):
            text = read(*parts)
            with self.subTest(file="/".join(parts)):
                other = text.index("- title: Other Changes")
                internal = text.index("- title: Internal Changes")
                self.assertLess(other, internal)
                other_block = text[other:internal]
                self.assertIn('- "*"', other_block)
                self.assertIn("exclude:", other_block)
                self.assertIn("- internal", other_block[other_block.index("exclude:") :])
                self.assertIn("- internal", text[internal:])
                top_exclude = text[text.index("exclude:") : text.index("categories:")]
                self.assertIn("skip-changelog", top_exclude)
                self.assertNotIn("internal", top_exclude)

    def test_the_checklist_item_and_prompt_version(self):
        from revali import PROMPT_VERSION

        self.assertGreaterEqual(int(PROMPT_VERSION), 17)
        text = flat("checklists", "default.md")
        self.assertIn("release label", text)
        self.assertIn("`internal` or `skip-changelog`", text)

    def test_the_rule_in_change_md(self):
        text = flat("templates", "change.md")
        self.assertIn('"release: internal"', text)
        self.assertIn('"release: skip-changelog"', text)

    def test_the_documents(self):
        releasing = flat("docs", "releasing.md")
        self.assertIn("Internal Changes", releasing)
        self.assertIn("collapse_headings", releasing)
        self.assertIn("still listed", releasing)
        config = flat("docs", "configuration.md")
        self.assertIn("| `release: internal` | `internal` |", config)
        self.assertIn("collapse_headings", config)
        self.assertIn("releases/generate-notes", flat("docs", "side-effects.md"))
        self.assertIn("collapse_headings", read("defaults.toml"))
        self.assertIn("collapse_headings", read("templates", "revali.toml"))


if __name__ == "__main__":
    unittest.main()
