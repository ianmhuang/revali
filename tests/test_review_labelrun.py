"""Review tests for feature/release-labels, the run side (AC-2 to AC-6): the PR a run opens or
reuses carries the one release label change.md asks for (`release:`, else `[pr] kind_labels`
for its kind), the other labels of `[pr] release_labels` are taken off, a label the repository
lacks is created first, every label failure is a warning, and a dry run only names the label.

The fixture's change.md is kind `feature`, so the default label is `minor`. A new PR is #7
(the gh stub's `pr create`); an open PR handed in through `prs_open` is #12.
"""

import os
import re
import unittest

from revali import EXIT_ERROR, EXIT_OK
from revali.preflight import Stop, preflight
from tests.helpers import RepoCase, claude_entry, run_cli

REVALI_LOG = ".revali/feature__mul/logs/revali.log"
CHANGE_MD = ".revali/feature__mul/change.md"
LABEL_WARNING = re.compile(r"^\[[0-9:]+\] \S+: warning: .*label", re.IGNORECASE)


class _StageLog:
    def __init__(self):
        self.lines = []

    def stage(self, stage, msg):
        self.lines.append("%s: %s" % (stage, msg))

    def detail(self, msg):
        pass


def open_pr(*labels):
    return {
        "prs_open": [
            {
                "number": 12,
                "url": "https://github.example/me/sample/pull/12",
                "isDraft": True,
                "title": "Add mul to calc",
                "labels": [{"name": name} for name in labels],
            }
        ]
    }


class LabelRunCase(RepoCase):
    def setUp(self):
        super().setUp()
        os.environ["REVALI_POLL_SECONDS"] = "0.01"

    def gh(self):
        return [c["argv"] for c in self.fake_calls("gh")]

    def label_changes(self, number):
        """(added, removed): the label names every `gh pr edit <number>` of the run gave to
        --add-label and --remove-label, comma lists split."""
        added, removed = [], []
        for argv in self.gh():
            if argv[:3] != ["pr", "edit", str(number)]:
                continue
            for flag, bucket in (("--add-label", added), ("--remove-label", removed)):
                for at, arg in enumerate(argv[:-1]):
                    if arg == flag:
                        bucket.extend(argv[at + 1].split(","))
        return added, removed

    def label_edits(self):
        return [
            a
            for a in self.gh()
            if a[:2] == ["pr", "edit"] and ("--add-label" in a or "--remove-label" in a)
        ]

    def label_creates(self):
        return [a for a in self.gh() if a[:2] == ["label", "create"]]

    def config(self, extra):
        self.write("revali.toml", self.read("revali.toml") + "\n" + extra)
        self.commit_all("pr config")

    def front(self, line):
        text = self.read(CHANGE_MD)
        self.assertIn("kind: feature\n", text)
        self.write(CHANGE_MD, text.replace("kind: feature\n", "kind: feature\n" + line + "\n", 1))

    def run_ok(self, *extra):
        self.claude(claude_entry())
        code, out = run_cli(["run", "--foreground", *extra])
        self.assertEqual(code, EXIT_OK, out)
        return out

    def ok_lines(self):
        log = _StageLog()
        preflight(self.repo, log=log)
        return [line for line in log.lines if line.startswith("preflight: ok: ")]

    def label_warnings(self):
        return [line for line in self.read(REVALI_LOG).splitlines() if LABEL_WARNING.match(line)]


class ReleaseFieldTests(LabelRunCase):
    """AC-2: `release: <label>` overrides the kind's label; a value outside `release_labels`
    stops preflight with exit 1; the `ok:` line names the label."""

    def test_ok_line_names_the_kinds_label(self):
        lines = self.ok_lines()
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("minor", lines[0])

    def test_ok_line_names_the_release_field_instead(self):
        self.front("release: major")
        lines = self.ok_lines()
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("major", lines[0])
        self.assertNotIn("minor", lines[0])

    def test_a_release_outside_the_list_stops_preflight_with_exit_1(self):
        self.front("release: minr")
        with self.assertRaises(Stop) as cm:
            preflight(self.repo)
        self.assertEqual(cm.exception.exit_code, EXIT_ERROR)
        self.assertEqual(EXIT_ERROR, 1)
        self.assertIn("change.md", cm.exception.message)
        self.assertIn("minr", cm.exception.message)

    def test_a_release_outside_the_list_opens_no_pr(self):
        self.front("release: minr")
        self.claude(claude_entry())
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertEqual([a for a in self.gh() if a[:2] == ["pr", "create"]], [])
        self.assertEqual(self.label_edits(), [])
        self.assertEqual(self.label_creates(), [])


class NewPrTests(LabelRunCase):
    """AC-3: the draft PR carries exactly the wanted label, or none."""

    def test_the_kinds_label(self):
        self.run_ok()
        self.assertEqual(self.label_changes(7), (["minor"], []))

    def test_the_release_field_wins_over_the_kind(self):
        self.front("release: major")
        self.run_ok()
        self.assertEqual(self.label_changes(7), (["major"], []))

    def test_the_configured_map_decides_the_kinds_label(self):
        self.config('[pr]\nkind_labels = { feature = "patch" }\n')
        self.run_ok()
        self.assertEqual(self.label_changes(7), (["patch"], []))

    def test_a_kind_without_an_entry_gets_no_label(self):
        self.config('[pr]\nkind_labels = { fix = "patch" }\n')
        self.run_ok()
        self.assertEqual(self.label_edits(), [])
        self.assertEqual(self.label_creates(), [])

    def test_turned_off_gets_no_label(self):
        self.config("[pr]\nrelease_labels = []\n")
        self.run_ok()
        self.assertEqual(self.label_edits(), [])
        self.assertEqual(self.label_creates(), [])


class ReusedPrTests(LabelRunCase):
    """AC-4: a run that reuses an open PR adds the wanted label, removes the other release
    labels and leaves the rest alone."""

    def test_other_release_labels_go_and_foreign_ones_stay(self):
        self.scenario(open_pr("patch", "bug", "docs"))
        self.run_ok()
        added, removed = self.label_changes(12)
        self.assertEqual(added, ["minor"])
        self.assertEqual(sorted(removed), ["docs", "patch"])  # `bug` is not revali's

    def test_a_label_set_by_hand_is_put_back(self):
        self.scenario(open_pr("major"))  # someone relabelled the PR on GitHub
        self.run_ok()
        self.assertEqual(self.label_changes(12), (["minor"], ["major"]))

    def test_the_release_field_replaces_the_kinds_label(self):
        self.front("release: skip-changelog")
        self.scenario(open_pr("minor", "bug"))
        self.run_ok()
        self.assertEqual(self.label_changes(12), (["skip-changelog"], ["minor"]))

    def test_a_pr_that_already_matches_is_not_edited(self):
        self.scenario(open_pr("bug", "minor"))
        self.run_ok()
        self.assertEqual(self.label_edits(), [])
        self.assertEqual(self.label_creates(), [])

    def test_no_wanted_label_takes_the_release_labels_off(self):
        self.config('[pr]\nkind_labels = { fix = "patch" }\n')
        self.scenario(open_pr("minor", "bug"))
        self.run_ok()
        self.assertEqual(self.label_changes(12), ([], ["minor"]))

    def test_turned_off_leaves_every_label_alone(self):
        self.config("[pr]\nrelease_labels = []\n")
        self.scenario(open_pr("patch", "bug"))
        self.run_ok()
        self.assertEqual(self.label_edits(), [])


class MissingLabelTests(LabelRunCase):
    """AC-5: a wanted label the repository lacks is created once, then applied; a failure is
    one warning line and the run ends as it would have."""

    def test_a_missing_label_is_created_once_before_it_is_applied(self):
        self.run_ok()  # the stub's repository has only `bug`
        gh = self.gh()
        creates = self.label_creates()
        self.assertEqual(len(creates), 1, creates)
        self.assertEqual(creates[0][2], "minor")
        edits = self.label_edits()
        self.assertEqual(len(edits), 1, edits)
        self.assertLess(gh.index(creates[0]), gh.index(edits[0]))
        self.assertEqual(self.label_warnings(), [])

    def test_a_label_the_repository_has_is_not_created(self):
        self.scenario({"labels": ["bug", "minor"]})
        self.run_ok()
        self.assertEqual(self.label_creates(), [])
        self.assertEqual(self.label_changes(7), (["minor"], []))

    def test_a_failed_create_is_one_warning_and_the_run_is_ready(self):
        self.scenario({"label_create_exit": 1})
        out = self.run_ok()
        self.assertIn("READY TO MERGE", out)
        warnings = self.label_warnings()
        self.assertEqual(len(warnings), 1, warnings)
        self.assertIn("minor", warnings[0])

    def test_a_failed_add_is_one_warning_and_the_run_is_ready(self):
        self.scenario({"labels": ["bug", "minor"], "label_edit_exit": 1})
        out = self.run_ok()
        self.assertIn("READY TO MERGE", out)
        self.assertEqual(len(self.label_warnings()), 1, self.label_warnings())

    def test_a_failed_remove_is_one_warning_and_the_run_is_ready(self):
        self.config('[pr]\nkind_labels = { fix = "patch" }\n')
        self.scenario(dict(open_pr("minor"), label_edit_exit=1))
        out = self.run_ok()
        self.assertIn("READY TO MERGE", out)
        self.assertEqual(len(self.label_warnings()), 1, self.label_warnings())


class DryRunTests(LabelRunCase):
    """AC-6: `run --dry-run` names the label and changes nothing on GitHub."""

    def dry_run(self):
        code, out = run_cli(["run", "--foreground", "--dry-run"])
        self.assertEqual(code, EXIT_OK, out)
        return [line for line in self.read(REVALI_LOG).splitlines() if "dry run" in line]

    def assert_github_untouched(self):
        changing = [
            a for a in self.gh() if a[:2] in (["pr", "create"], ["pr", "edit"], ["label", "create"])
        ]
        self.assertEqual(changing, [])

    def test_it_names_the_kinds_label(self):
        lines = self.dry_run()
        self.assertTrue([line for line in lines if "minor" in line], lines)
        self.assert_github_untouched()

    def test_it_names_the_release_field(self):
        self.front("release: major")
        lines = self.dry_run()
        self.assertTrue([line for line in lines if "major" in line], lines)
        self.assertFalse([line for line in lines if "minor" in line], lines)
        self.assert_github_untouched()


if __name__ == "__main__":
    unittest.main()
