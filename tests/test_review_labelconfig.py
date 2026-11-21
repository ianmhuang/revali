"""Review tests for feature/release-labels, the configuration (AC-1) and the release notes
file (AC-7): `[pr] release_labels` and `kind_labels` with their defaults, the configuration
errors that name the offending `kind_labels` key, a list that only adds a label, a name listed
twice, `release_labels = []` as the off switch, and `templates/release.yml` grouping GitHub's
generated notes by the default labels, with revali's own `.github/release.yml` being that
file.

The configuration is observed through preflight: its `ok:` line names the label the PR gets,
and a configuration error stops it with exit 1 before anything else.
"""

import os
import unittest

from revali import EXIT_ERROR
from revali.preflight import Stop, preflight
from tests.helpers import ROOT, RepoCase

CHANGE_MD = ".revali/feature__mul/change.md"
# M3.38 added `internal`
RELEASE_LABELS = ("major", "minor", "patch", "docs", "internal", "skip-changelog")


class _StageLog:
    def __init__(self):
        self.lines = []

    def stage(self, stage, msg):
        self.lines.append("%s: %s" % (stage, msg))

    def detail(self, msg):
        pass


class ConfigCase(RepoCase):
    def config(self, extra):
        self.write("revali.toml", self.read("revali.toml") + "\n" + extra)
        self.commit_all("pr config")

    def change(self, old, new):
        text = self.read(CHANGE_MD)
        self.assertIn(old, text)
        self.write(CHANGE_MD, text.replace(old, new, 1))

    def ok_line(self):
        log = _StageLog()
        preflight(self.repo, log=log)
        lines = [line for line in log.lines if line.startswith("preflight: ok: ")]
        self.assertEqual(len(lines), 1, log.lines)
        return lines[0]

    def stop(self):
        with self.assertRaises(Stop) as cm:
            preflight(self.repo)
        self.assertEqual(cm.exception.exit_code, EXIT_ERROR)
        return cm.exception.message

    def problem_lines(self, *words):
        """The lines of the refusal that carry every one of `words`."""
        return [line for line in self.stop().splitlines() if all(w in line for w in words)]


class DefaultTests(ConfigCase):
    """AC-1: the default map is feature -> minor, fix -> patch, docs -> docs, and every
    default release label is accepted as a `release:` value."""

    def test_feature_maps_to_minor(self):
        self.assertIn("minor", self.ok_line())

    def test_fix_maps_to_patch(self):
        self.change("kind: feature\n", "kind: fix\n")
        line = self.ok_line()
        self.assertIn("patch", line)
        self.assertNotIn("minor", line)

    def test_docs_maps_to_docs(self):
        self.change("kind: feature\n", "kind: docs\n")
        # once as the kind, once as the label
        self.assertEqual(self.ok_line().count("docs"), 2)

    def test_each_default_release_label_is_a_valid_release_value(self):
        self.change("kind: feature\n", "kind: feature\nrelease: minor\n")
        for label in RELEASE_LABELS:
            with self.subTest(label=label):
                text = self.read(CHANGE_MD)
                self.write(
                    CHANGE_MD,
                    "\n".join(
                        "release: " + label if line.startswith("release:") else line
                        for line in text.split("\n")
                    ),
                )
                self.assertIn(label, self.ok_line())


class CustomTests(ConfigCase):
    """AC-1: both keys are configuration; a project's own labels replace the defaults."""

    OWN = (
        "[pr]\n"
        'release_labels = ["breaking", "feat", "bugfix"]\n'
        'kind_labels = { feature = "feat", fix = "bugfix" }\n'
    )

    def test_the_projects_map_names_the_label(self):
        self.config(self.OWN)
        self.assertIn("feat", self.ok_line().replace("feature", ""))  # the kind aside

    def test_the_projects_list_decides_what_release_may_name(self):
        self.config(self.OWN)
        self.change("kind: feature\n", "kind: feature\nrelease: breaking\n")
        self.assertIn("breaking", self.ok_line())
        self.change("release: breaking\n", "release: minor\n")  # a default, no longer listed
        message = self.stop()
        self.assertIn("change.md", message)
        self.assertIn("minor", message)

    def test_an_empty_list_turns_it_off(self):
        self.config("[pr]\nrelease_labels = []\n")
        line = self.ok_line()
        for label in RELEASE_LABELS:
            self.assertNotIn(label, line)

    def test_a_list_that_only_adds_a_label_keeps_the_default_map(self):
        # every label the default `kind_labels` maps to is still listed
        self.config(
            "[pr]\n"
            'release_labels = ["major", "minor", "patch", "docs", "skip-changelog", '
            '"security"]\n'
        )
        self.assertIn("minor", self.ok_line())
        self.change("kind: feature\n", "kind: feature\nrelease: security\n")
        line = self.ok_line()
        self.assertIn("security", line)
        self.assertNotIn("minor", line)


class ErrorTests(ConfigCase):
    """AC-1: a `kind_labels` key that is no change.md kind, or a value outside
    `release_labels`, is a configuration error that names the key."""

    def broken(self, extra):
        # not committed: the configuration is read, and refused, before the tree is looked at
        self.write("revali.toml", self.read("revali.toml") + "\n" + extra)

    def test_a_key_that_is_not_a_kind(self):
        self.broken('[pr]\nkind_labels = { feature = "minor", chore = "patch" }\n')
        self.assertTrue(self.problem_lines("kind_labels", "chore"), self.stop())

    def test_a_value_outside_release_labels(self):
        self.broken('[pr]\nkind_labels = { feature = "minor", fix = "hotpatch" }\n')
        lines = self.problem_lines("kind_labels", "fix")
        self.assertTrue(lines, self.stop())
        self.assertFalse(self.problem_lines("kind_labels.feature"), self.stop())

    def test_a_value_dropped_from_a_shortened_list(self):
        self.broken('[pr]\nrelease_labels = ["minor", "patch"]\n')  # docs = "docs" is left over
        self.assertTrue(self.problem_lines("kind_labels", "docs"), self.stop())

    def test_an_unknown_pr_key(self):
        self.broken('[pr]\nrelease_label = ["minor"]\n')
        self.assertTrue(self.problem_lines("release_label"), self.stop())

    def test_a_label_listed_twice(self):
        # docs/configuration.md: a name listed twice is an error
        self.broken('[pr]\nrelease_labels = ["minor", "patch", "docs", "patch"]\n')
        self.assertTrue(self.problem_lines("release_labels", "patch"), self.stop())
        self.assertFalse(self.problem_lines("kind_labels"), self.stop())

    def test_a_label_listed_twice_in_another_case(self):
        # GitHub compares label names without regard to case, so `Minor` is `minor` again
        self.broken('[pr]\nrelease_labels = ["minor", "patch", "Minor", "docs"]\n')
        self.assertTrue(self.problem_lines("release_labels", "Minor"), self.stop())
        self.assertFalse(self.problem_lines("kind_labels"), self.stop())


def _parse_release_yml(text):
    """(excluded labels, [(category title, [labels])]) of a GitHub release.yml as plain as
    the one revali ships: block lists, one scalar per line. A category's own `exclude:`
    (M3.38: Other Changes leaves `internal` to the category after it) is skipped: its
    labels are neither the global exclusions nor the category's labels."""
    excluded, categories = [], []
    section, target = "", None
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if line == "exclude:" and categories:
            section, target = "category-exclude", None
        elif line in ("exclude:", "categories:"):
            section, target = line[:-1], None
        elif line.startswith("- title:"):
            section = "categories"
            categories.append((line.split(":", 1)[1].strip().strip("\"'"), []))
            target = categories[-1][1]
        elif line == "labels:":
            if section == "exclude":
                target = excluded
            elif section == "category-exclude":
                target = []
        elif line.startswith("- ") and target is not None:
            target.append(line[2:].strip().strip("\"'"))
    return excluded, categories


class ReleaseYmlTests(unittest.TestCase):
    """AC-7"""

    def read(self, *parts):
        with open(os.path.join(ROOT, *parts), "rb") as fh:
            return fh.read()

    def test_the_template_groups_by_the_labels_in_order(self):
        excluded, categories = _parse_release_yml(
            self.read("templates", "release.yml").decode("utf-8")
        )
        self.assertEqual(excluded, ["skip-changelog"])
        self.assertEqual(
            [labels for _, labels in categories],
            [["major"], ["minor"], ["patch"], ["docs"], ["*"], ["internal"]],  # M3.38
        )
        titles = [title.lower() for title, _ in categories]
        for title, word in zip(titles, ("major", "minor", "patch", "docs"), strict=False):
            self.assertIn(word, title)
        self.assertEqual(len(set(titles)), 6, titles)

    def test_revalis_own_file_is_the_template(self):
        self.assertEqual(self.read(".github", "release.yml"), self.read("templates", "release.yml"))


if __name__ == "__main__":
    unittest.main()
