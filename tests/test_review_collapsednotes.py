"""Reviewer acceptance tests for feature/internal-label: the notes `revali release` publishes.

AC-3: the Release's body is GitHub's generated notes (asked through `gh api -X POST
      repos/{owner}/{repo}/releases/generate-notes`), the `--notes` line above them, each
      section named in `[pr] collapse_headings` (default "Internal Changes") inside
      `<details><summary><heading> (<n>)</summary> ... </details>`; published with
      `gh release create <tag> --verify-tag --title <generated name> --notes-file <file>`.
      A body without such a heading is published as generated; `[]` collapses nothing.
AC-4: `--dry-run` prints those notes after the commands, asks generate-notes once and
      creates nothing; a failed generate-notes call is exit 1 with one ERROR line and the
      rerun hint, with no `gh release create`, and the rerun finishes the release.

Round 2 (what the fixes of round 1 changed): the heading is HTML-escaped inside
`<summary>`; a notes file that cannot be written stops the release with one ERROR line
before the tag is made; a `collapse_headings` entry that can never be a category title is a
configuration error that names the key.

Black box: the command line in-process on a RepoCase repository on `main` with a bare
`origin` (real git); the fake gh answers generate-notes from the scenario and records the
text of the `--notes-file` it is given.
"""

import json
import os
import tempfile
import unittest
from unittest import mock

from tests.helpers import RepoCase, git, run_cli

TAG = "v1.2.3"
REF = "refs/tags/" + TAG
PASSED = [{"name": "unit", "status": "completed", "conclusion": "success"}]
RELEASE_YML = "changelog:\n  categories:\n    - title: Fixes\n      labels: [patch]\n"
PR = "https://github.example/me/sample/pull/%d"
HEADER = "<!-- Release notes generated using configuration in .github/release.yml at v1.2.3 -->"
FOOTER = "**Full Changelog**: https://github.example/me/sample/compare/v1.2.2...v1.2.3"
MINOR = ["* Add a flag by @me in " + PR % 11]
PATCH = ["* Fix the parser by @me in " + PR % 12]
INTERNAL = [
    "* Split the suite into shards by @me in " + PR % 13,
    "* Cache the CI venv by @me in " + PR % 14,
    "* Rename a test helper by @me in " + PR % 15,
]
DOCS = ["* Explain the flag by @me in " + PR % 16]


def notes(*sections):
    """Generated notes as GitHub writes them: `### <title>` and its `* ` lines, section after
    section with no blank line between, then the changelog link."""
    lines = [HEADER, "", "## What's Changed"]
    for title, entries in sections:
        lines += ["### " + title] + entries
    return "\n".join(lines + ["", "", FOOTER])


# Internal Changes last, as the shipped release.yml orders it
GENERATED = notes(
    ("Minor Changes", MINOR), ("Patch Changes", PATCH), ("Internal Changes", INTERNAL)
)
# a project whose release.yml puts another category after it
MIDDLE = notes(("Minor Changes", MINOR), ("Internal Changes", INTERNAL), ("Docs", DOCS))
NO_INTERNAL = notes(("Minor Changes", MINOR), ("Patch Changes", PATCH))


class NotesCase(RepoCase):
    """On `main`, pushed to origin, `.github/release.yml` committed, CI green."""

    with_branch = False

    def setUp(self):
        super().setUp()
        self.write(".github/release.yml", RELEASE_YML)
        self.commit_all("release notes groups")
        git(["push", "-q", "origin", "main"], self.repo)
        self.scenario({"commit_checks": PASSED, "generated_notes": GENERATED})

    def config(self, extra):
        self.write("revali.toml", self.read("revali.toml") + "\n" + extra)
        self.commit_all("pr config")
        git(["push", "-q", "origin", "main"], self.repo)

    def head(self):
        return git(["rev-parse", "HEAD"], self.repo).strip()

    def gh_calls(self):
        return self.fake_calls("gh")

    def creates(self):
        return [c for c in self.gh_calls() if c["argv"][:2] == ["release", "create"]]

    def generates(self):
        return [
            c["argv"]
            for c in self.gh_calls()
            if c["argv"][:1] == ["api"] and any("releases/generate-notes" in a for a in c["argv"])
        ]

    def origin_tag_commit(self):
        out = git(["ls-remote", "origin", REF, REF + "^{}"], self.repo)
        refs = {}
        for line in out.splitlines():
            parts = line.split()
            if len(parts) == 2:
                refs[parts[1]] = parts[0]
        return refs.get(REF + "^{}") or refs.get(REF, "")

    def has_local_tag(self):
        return git(["tag", "-l", TAG], self.repo).strip() == TAG

    def released_body(self, argv=("release", "1.2.3")):
        """The text of the notes file of the one `gh release create` of a successful run."""
        code, out = run_cli(list(argv))
        self.assertEqual(code, 0, out)
        self.assertNotIn("Traceback", out)
        creates = self.creates()
        self.assertEqual(len(creates), 1, creates)
        self.assertIn("--notes-file", creates[0]["argv"])
        self.assertIn("body", creates[0], "the notes file did not exist when gh ran")
        return creates[0]["body"]

    def assert_collapsed(self, body, heading, entries):
        """`heading`'s section is one <details> block with its count in the summary and
        exactly `entries` inside, set off by blank lines so that GitHub renders the list
        (an HTML block runs to the next blank line; text before it is not Markdown)."""
        lines = body.split("\n")
        summary = "<details><summary>%s (%d)</summary>" % (heading, len(entries))
        self.assertEqual(lines.count(summary), 1, body)
        self.assertNotIn("### " + heading, lines)
        start = lines.index(summary)
        end = lines.index("</details>", start)
        inside = lines[start + 1 : end]
        self.assertEqual([ln for ln in inside if ln.strip()], entries, body)
        self.assertEqual(inside[0], "", body)
        self.assertEqual(inside[-1], "", body)
        self.assertEqual(lines[end + 1], "", body)
        return start, end

    def assert_in_order(self, body, expected):
        """Every line of `expected` is a line of `body`, in that order."""
        lines = body.split("\n")
        at = 0
        for want in expected:
            self.assertIn(want, lines[at:], "missing or out of order in:\n%s" % body)
            at = lines.index(want, at) + 1


class PublishedNotes(NotesCase):
    """AC-3."""

    def test_the_internal_section_is_collapsed_with_its_count(self):
        body = self.released_body()
        start, end = self.assert_collapsed(body, "Internal Changes", INTERNAL)
        # everything else is as GitHub generated it, in its order, outside the block
        before = [HEADER, "## What's Changed", "### Minor Changes"] + MINOR
        before += ["### Patch Changes"] + PATCH
        self.assert_in_order(body, before + ["</details>", FOOTER])
        lines = body.split("\n")
        for line in before:
            self.assertLess(lines.index(line), start, body)
        self.assertGreater(lines.index(FOOTER), end, body)
        self.assertEqual(body.count("<details>"), 1, body)
        self.assertEqual(body.count("</details>"), 1, body)
        # each entry is listed once: collapsed, not copied
        for entry in MINOR + PATCH + INTERNAL:
            self.assertEqual(lines.count(entry), 1, body)

    def test_the_notes_line_goes_above_the_generated_notes(self):
        body = self.released_body(("release", "1.2.3", "--notes", "Faster cold start."))
        lines = body.split("\n")
        self.assertEqual(lines[0], "Faster cold start.")
        self.assertLess(lines.index("Faster cold start."), lines.index("## What's Changed"))
        self.assertEqual(body.count("Faster cold start."), 1, body)
        self.assert_collapsed(body, "Internal Changes", INTERNAL)

    def test_without_notes_the_body_starts_with_the_generated_notes(self):
        body = self.released_body()
        self.assertTrue(body.startswith(HEADER + "\n"), body)

    def test_the_create_command(self):
        # the title is the name GitHub generated, not one revali makes up
        answer = json.dumps({"name": "Release 1.2.3", "body": GENERATED})
        self.scenario({"generate_notes_raw": answer})
        self.released_body(("release", "1.2.3", "--notes", "Faster cold start."))
        argv = self.creates()[0]["argv"]
        self.assertEqual(argv[:4], ["release", "create", TAG, "--verify-tag"])
        self.assertEqual(argv[argv.index("--title") + 1], "Release 1.2.3")
        self.assertEqual(len(argv), 8, argv)
        self.assertNotIn("--generate-notes", argv)
        self.assertNotIn("--notes", argv)
        self.assertNotIn("Faster cold start.", argv)
        # the notes file is a temporary one outside the repository, gone afterwards
        notes_file = argv[argv.index("--notes-file") + 1]
        self.assertFalse(os.path.exists(notes_file), notes_file)
        inside = os.path.realpath(self.repo) + os.sep
        self.assertFalse(os.path.realpath(notes_file).startswith(inside), notes_file)

    def test_the_notes_are_asked_once_before_the_release_is_created(self):
        self.released_body()
        calls = self.generates()
        self.assertEqual(len(calls), 1, calls)
        argv = calls[0]
        self.assertIn("repos/{owner}/{repo}/releases/generate-notes", argv)
        self.assertEqual(argv[argv.index("-X") + 1], "POST")
        self.assertIn("tag_name=" + TAG, argv)
        order = [c["argv"] for c in self.gh_calls()]
        self.assertLess(order.index(argv), order.index(self.creates()[0]["argv"]))
        # the tag is on HEAD on origin as before
        self.assertEqual(self.origin_tag_commit(), self.head())

    def test_a_section_in_the_middle_leaves_the_next_heading_outside(self):
        self.scenario({"generated_notes": MIDDLE})
        body = self.released_body()
        start, end = self.assert_collapsed(body, "Internal Changes", INTERNAL)
        lines = body.split("\n")
        self.assertLess(lines.index("### Minor Changes"), start)
        self.assertGreater(lines.index("### Docs"), end)
        self.assert_in_order(body, ["</details>", "### Docs"] + DOCS + [FOOTER])

    def test_a_body_without_the_heading_is_published_as_generated(self):
        self.scenario({"generated_notes": NO_INTERNAL})
        self.assertEqual(self.released_body(), NO_INTERNAL)

    def test_a_body_without_the_heading_keeps_the_notes_line_above(self):
        self.scenario({"generated_notes": NO_INTERNAL})
        body = self.released_body(("release", "1.2.3", "--notes", "Two fixes."))
        self.assertTrue(body.startswith("Two fixes.\n"), body)
        self.assertTrue(body.endswith(NO_INTERNAL), body)
        self.assertNotIn("<details>", body)

    def test_a_heading_only_mentioned_in_an_entry_is_not_a_section(self):
        entry = "* Document the Internal Changes section by @me in " + PR % 17
        generated = notes(("Minor Changes", MINOR), ("Docs", [entry]))
        self.scenario({"generated_notes": generated})
        self.assertEqual(self.released_body(), generated)

    def test_collapse_headings_empty_collapses_nothing(self):
        self.config("[pr]\ncollapse_headings = []\n")
        self.assertEqual(self.released_body(), GENERATED)

    def test_the_headings_come_from_the_configuration(self):
        self.config('[pr]\ncollapse_headings = ["Patch Changes", "Minor Changes"]\n')
        body = self.released_body()
        self.assert_collapsed(body, "Patch Changes", PATCH)
        self.assert_collapsed(body, "Minor Changes", MINOR)
        lines = body.split("\n")
        self.assertIn("### Internal Changes", lines)
        self.assertEqual(body.count("<details>"), 2, body)
        self.assert_in_order(body, ["### Internal Changes"] + INTERNAL + [FOOTER])

    def test_the_heading_is_escaped_inside_the_summary(self):
        # a category title is text; inside <summary> it is HTML
        title = "CI & <tests>"
        self.config("[pr]\ncollapse_headings = ['%s']\n" % title)
        generated = notes(("Minor Changes", MINOR), (title, INTERNAL))
        self.scenario({"generated_notes": generated})
        body = self.released_body()
        self.assert_collapsed(body, "CI &amp; &lt;tests&gt;", INTERNAL)
        lines = body.split("\n")
        self.assertNotIn("### " + title, lines)
        self.assertNotIn("<details><summary>%s (%d)</summary>" % (title, len(INTERNAL)), lines)
        self.assertIn("### Minor Changes", lines)


class HeadingsConfig(NotesCase):
    """docs/configuration.md: a heading is the title of a release.yml category, written
    without its `###` and without surrounding spaces; anything else is a configuration
    error. Such a value could never match a section, so it is refused, not ignored."""

    BAD = (
        '[" Internal Changes"]',
        '["Internal Changes "]',
        '["### Internal Changes"]',
        '[""]',
        '["Internal\\nChanges"]',
        "[7]",
        '"Internal Changes"',
    )

    def test_a_value_that_cannot_be_a_title_is_a_configuration_error(self):
        base = self.read("revali.toml")
        for number, value in enumerate(self.BAD):
            with self.subTest(value=value):
                self.write("revali.toml", base + "\n[pr]\ncollapse_headings = %s\n" % value)
                self.commit_all("pr config %d" % number)
                git(["push", "-q", "origin", "main"], self.repo)
                code, out = run_cli(["release", "1.2.3"])
                self.assertNotEqual(code, 0, out)
                self.assertNotIn("Traceback", out)
                errors = [ln for ln in out.splitlines() if ln.startswith("ERROR")]
                self.assertTrue(errors, out)
                self.assertTrue(any("collapse_headings" in ln for ln in errors), out)
                self.assertEqual(self.generates(), [])
                self.assertEqual(self.creates(), [])
                self.assertFalse(self.has_local_tag())
                self.assertEqual(self.origin_tag_commit(), "")

    def test_a_plain_title_is_accepted(self):
        # the other side: a title with inner spaces and punctuation is a valid entry
        self.config('[pr]\ncollapse_headings = ["Patch Changes", "Tests, CI: tooling"]\n')
        body = self.released_body()
        self.assert_collapsed(body, "Patch Changes", PATCH)


class DryRunAndFailure(NotesCase):
    """AC-4."""

    def assert_nothing_made(self):
        self.assertFalse(self.has_local_tag())
        self.assertEqual(self.origin_tag_commit(), "")
        self.assertEqual(self.creates(), [])

    def test_the_dry_run_prints_the_notes_after_the_commands(self):
        code, out = run_cli(["release", "1.2.3", "--notes", "Faster cold start.", "--dry-run"])
        self.assertEqual(code, 0, out)
        lines = out.splitlines()
        commands = [i for i, ln in enumerate(lines) if ln.startswith("dry run: would run ")]
        self.assertEqual(len(commands), 3, out)  # tag, push, release create
        create = lines[commands[-1]]
        self.assertIn("gh release create %s --verify-tag" % TAG, create)
        self.assertIn("--notes-file", create)
        self.assertNotIn("--generate-notes", create)
        # the notes it would publish: the lead line, the collapsed section, the rest
        after = lines[commands[-1] + 1 :]
        self.assertIn("Faster cold start.", after)
        summary = "<details><summary>Internal Changes (%d)</summary>" % len(INTERNAL)
        self.assertIn(summary, after)
        self.assertLess(after.index("Faster cold start."), after.index("## What's Changed"))
        for line in ["### Minor Changes"] + MINOR + PATCH + INTERNAL + ["</details>", FOOTER]:
            self.assertIn(line, after)
        self.assertNotIn("### Internal Changes", after)
        # one generate-notes request, which stores nothing; nothing made
        self.assertEqual(len(self.generates()), 1, self.generates())
        self.assert_nothing_made()

    def test_a_failed_generate_notes_call_stops_before_the_release(self):
        self.scenario({"generate_notes_exit": 1})
        code, out = run_cli(["release", "1.2.3", "--notes", "Faster cold start."])
        self.assertEqual(code, 1, out)
        self.assertNotIn("Traceback", out)
        errors = [ln for ln in out.splitlines() if ln.startswith("ERROR")]
        self.assertEqual(len(errors), 1, out)
        self.assertIn("generate-notes", errors[0])
        self.assertIn("run the same command again", errors[0])
        self.assertEqual(self.creates(), [])
        self.assertNotIn("RELEASED", out)
        # the rerun, once GitHub answers, finishes the release with the collapsed notes
        self.scenario({"generate_notes_exit": 0})
        body = self.released_body(("release", "1.2.3", "--notes", "Faster cold start."))
        self.assertEqual(body.split("\n")[0], "Faster cold start.")
        self.assert_collapsed(body, "Internal Changes", INTERNAL)
        self.assertEqual(self.origin_tag_commit(), self.head())

    def test_an_answer_without_the_notes_stops_before_the_release(self):
        self.scenario({"generate_notes_raw": json.dumps({"message": "Not Found"})})
        code, out = run_cli(["release", "1.2.3"])
        self.assertEqual(code, 1, out)
        self.assertNotIn("Traceback", out)
        errors = [ln for ln in out.splitlines() if ln.startswith("ERROR")]
        self.assertEqual(len(errors), 1, out)
        self.assertEqual(self.creates(), [])

    def test_a_failed_dry_run_request_is_an_error_too(self):
        self.scenario({"generate_notes_raw": "<html>bad gateway</html>"})
        code, out = run_cli(["release", "1.2.3", "--dry-run"])
        self.assertEqual(code, 1, out)
        self.assertEqual(len([ln for ln in out.splitlines() if ln.startswith("ERROR")]), 1, out)
        self.assert_nothing_made()

    def test_a_notes_file_that_cannot_be_written_stops_before_the_tag(self):
        # docs/side-effects.md: the notes go in a temporary file written before the tag is
        # made. With no usable temporary directory the release ends in one ERROR line with
        # the rerun hint and has made nothing; the rerun, with one, finishes it.
        missing = os.path.join(self.tmp, "no", "such", "directory")
        with mock.patch.object(tempfile, "tempdir", missing):
            code, out = run_cli(["release", "1.2.3", "--notes", "Faster cold start."])
        self.assertEqual(code, 1, out)
        self.assertNotIn("Traceback", out)
        errors = [ln for ln in out.splitlines() if ln.startswith("ERROR")]
        self.assertEqual(len(errors), 1, out)
        self.assertIn("run the same command again", errors[0])
        self.assertNotIn("RELEASED", out)
        self.assert_nothing_made()
        body = self.released_body(("release", "1.2.3", "--notes", "Faster cold start."))
        self.assertEqual(body.split("\n")[0], "Faster cold start.")
        self.assertEqual(self.origin_tag_commit(), self.head())


if __name__ == "__main__":
    unittest.main()
