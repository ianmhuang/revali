"""Reviewer acceptance tests for feature/internal-label: the `internal` release label.

AC-1: `internal` is one of the default `[pr] release_labels`; change.md's `release: internal`
      gives the PR that label; no kind maps to it (`kind_labels` unchanged).
AC-2: templates/release.yml and revali's own .github/release.yml have an "Internal Changes"
      category for `internal` after the catch-all one, whose `*` excludes `internal`;
      `skip-changelog` is still the only label left out of the notes.
AC-5: the built-in checklist has an item about the release label that names `internal` and
      `skip-changelog`; PROMPT_VERSION is at least 17.
AC-7 (the part that is not prose): defaults.toml and templates/revali.toml carry
      `[pr] collapse_headings`.

The label is observed through preflight, whose `ok:` line ends with `; label <name>`.
"""

import os
import re
import tomllib
import unittest

from revali.config import load_project_config
from revali.preflight import preflight
from tests.helpers import ROOT, RepoCase

CHANGE_MD = ".revali/feature__mul/change.md"
SIX = ["major", "minor", "patch", "docs", "internal", "skip-changelog"]


def read(*parts):
    with open(os.path.join(ROOT, *parts), "r", encoding="utf-8", newline="") as fh:
        return fh.read()


class _StageLog:
    def __init__(self):
        self.lines = []

    def stage(self, stage, msg):
        self.lines.append("%s: %s" % (stage, msg))

    def detail(self, msg):
        pass


class LabelTests(RepoCase):
    """AC-1."""

    def ok_line(self):
        log = _StageLog()
        preflight(self.repo, log=log)
        lines = [line for line in log.lines if line.startswith("preflight: ok: ")]
        self.assertEqual(len(lines), 1, log.lines)
        return lines[0]

    def front_matter(self, kind, release=""):
        text = self.read(CHANGE_MD)
        self.assertIn("kind: feature\n", text)
        new = "kind: %s\n" % kind + ("release: %s\n" % release if release else "")
        self.write(CHANGE_MD, text.replace("kind: feature\n", new, 1))

    def test_the_default_list_has_internal_before_skip_changelog(self):
        cfg = load_project_config(self.repo)
        self.assertEqual(cfg.pr.release_labels, SIX)
        # no kind maps to it
        self.assertEqual(cfg.pr.kind_labels, {"feature": "minor", "fix": "patch", "docs": "docs"})

    def test_release_internal_gives_the_pr_that_label(self):
        self.front_matter("feature", "internal")
        self.assertIn("; label internal)", self.ok_line())

    def test_release_internal_in_another_case_gives_the_lists_spelling(self):
        # "as any other value of the list does": names compare in any case
        self.front_matter("fix", "Internal")
        self.assertIn("; label internal)", self.ok_line())

    def test_no_kind_maps_to_internal(self):
        cfg = load_project_config(self.repo)
        self.assertIn("internal", cfg.pr.release_labels)
        self.assertNotIn("internal", cfg.pr.kind_labels.values())
        self.assertIn("; label minor)", self.ok_line())


def parse_release_yml(text):
    """(globally excluded labels, [{"title", "labels", "exclude"}]) of a GitHub release.yml
    written as revali's is: block lists, one scalar per line. Inside a category, a
    `labels:` nested deeper than the category's own keys belongs to its `exclude:`."""

    def scalar(value):
        return value.strip().strip("\"'")

    excluded, categories = [], []
    in_categories, cat, key_indent, target = False, None, 0, None
    for raw in text.splitlines():
        line = raw.split(" #", 1)[0].rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        word = line.strip()
        if word == "categories:":
            in_categories, target = True, None
        elif word.startswith("- title:"):
            cat = {"title": scalar(word.split(":", 1)[1]), "labels": [], "exclude": []}
            categories.append(cat)
            key_indent, target = indent + 2, None
        elif word == "exclude:":
            target = None
        elif word == "labels:":
            if not in_categories:
                target = excluded
            elif cat is not None:
                target = cat["labels"] if indent <= key_indent else cat["exclude"]
        elif word.startswith("- ") and target is not None:
            target.append(scalar(word[2:]))
    return excluded, categories


class ReleaseYmlTests(unittest.TestCase):
    """AC-2."""

    FILES = (("templates", "release.yml"), (".github", "release.yml"))

    def test_internal_changes_follows_the_catch_all_that_excludes_it(self):
        for parts in self.FILES:
            with self.subTest(file="/".join(parts)):
                excluded, categories = parse_release_yml(read(*parts))
                titles = [c["title"] for c in categories]
                self.assertIn("Internal Changes", titles)
                internal = categories[titles.index("Internal Changes")]
                self.assertEqual(internal["labels"], ["internal"])
                self.assertEqual(internal["exclude"], [])
                catch_all = [i for i, c in enumerate(categories) if "*" in c["labels"]]
                self.assertEqual(len(catch_all), 1, categories)
                other = categories[catch_all[0]]
                self.assertEqual(other["title"], "Other Changes")
                # GitHub puts a PR in the first category that takes it: `*` would take an
                # `internal` PR unless the category excludes that label
                self.assertIn("internal", other["exclude"])
                self.assertLess(catch_all[0], titles.index("Internal Changes"))
                # no category before the catch-all takes `internal` either
                for cat in categories[: catch_all[0]]:
                    self.assertNotIn("internal", cat["labels"], cat)

    def test_skip_changelog_is_still_the_only_label_left_out(self):
        for parts in self.FILES:
            with self.subTest(file="/".join(parts)):
                excluded, categories = parse_release_yml(read(*parts))
                self.assertEqual(excluded, ["skip-changelog"])
                self.assertIn("Internal Changes", [c["title"] for c in categories])

    def test_the_other_categories_are_as_before(self):
        excluded, categories = parse_release_yml(read("templates", "release.yml"))
        self.assertEqual(
            [(c["title"], c["labels"]) for c in categories],
            [
                ("Major Changes", ["major"]),
                ("Minor Changes", ["minor"]),
                ("Patch Changes", ["patch"]),
                ("Docs", ["docs"]),
                ("Other Changes", ["*"]),
                ("Internal Changes", ["internal"]),
            ],
        )

    def test_revalis_own_file_is_the_template(self):
        self.assertEqual(read(".github", "release.yml"), read("templates", "release.yml"))
        self.assertIn("Internal Changes", read(".github", "release.yml"))


class ChecklistTests(unittest.TestCase):
    """AC-5."""

    def test_an_item_checks_the_release_label(self):
        text = read("checklists", "default.md").replace("\r\n", "\n")
        items = re.split(r"(?m)^(?=\d+\. )", text)[1:]
        about = [
            item
            for item in items
            if "`internal`" in item and "`skip-changelog`" in item and "`release:`" in item
        ]
        self.assertEqual(len(about), 1, items)
        self.assertIn("finding", about[0])

    def test_prompt_version_was_raised(self):
        from revali import PROMPT_VERSION

        self.assertGreaterEqual(int(PROMPT_VERSION), 17)


class ConfigFilesTests(unittest.TestCase):
    """AC-1, AC-7: the keys in the files a user reads and copies."""

    def test_defaults_toml(self):
        pr = tomllib.loads(read("defaults.toml"))["pr"]
        self.assertEqual(pr["release_labels"], SIX)
        self.assertEqual(pr["collapse_headings"], ["Internal Changes"])
        self.assertNotIn("internal", pr["kind_labels"].values())

    def test_the_template_has_the_defaults(self):
        pr = tomllib.loads(read("templates", "revali.toml"))["pr"]
        self.assertEqual(pr["release_labels"], SIX)
        self.assertEqual(pr["collapse_headings"], ["Internal Changes"])

    def test_the_collapsed_heading_is_a_category_of_release_yml(self):
        # the default heading must be one GitHub writes for the shipped release.yml,
        # or nothing is ever collapsed
        pr = tomllib.loads(read("defaults.toml"))["pr"]
        _, categories = parse_release_yml(read("templates", "release.yml"))
        for heading in pr["collapse_headings"]:
            self.assertIn(heading, [c["title"] for c in categories])


if __name__ == "__main__":
    unittest.main()
