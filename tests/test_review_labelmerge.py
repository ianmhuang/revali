"""Review tests for feature/release-labels, the merge side (AC-4, with AC-5's creation):
`revali merge` makes the PR's release labels match change.md once more before `gh pr merge`,
as a run does, and a failure there is a warning, never a refusal.

Each test first runs the pipeline to READY TO MERGE (draft PR #7, labelled `minor` for the
fixture's kind `feature`), then sets what GitHub reports for the PR (`pr_labels`) and the
repository (`labels`) and merges.
"""

import os
import unittest

from revali import EXIT_OK
from tests.helpers import RepoCase, claude_entry, run_cli

CHANGE_MD = ".revali/feature__mul/change.md"
PR = "7"  # the gh stub's `pr create` answer


class LabelMergeCase(RepoCase):
    def setUp(self):
        super().setUp()
        os.environ["REVALI_POLL_SECONDS"] = "0.01"

    def gh(self):
        return [c["argv"] for c in self.fake_calls("gh")]

    def ready(self):
        """A full run to READY TO MERGE; remembers how many gh calls it made."""
        self.claude(claude_entry())
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        self.before = len(self.gh())

    def merge(self):
        """`revali merge`, which must succeed; returns (its output, its gh calls)."""
        code, out = run_cli(["merge"])
        self.assertEqual(code, EXIT_OK, out)
        return out, self.gh()[self.before :]

    def config(self, extra):
        self.write("revali.toml", self.read("revali.toml") + "\n" + extra)
        self.commit_all("pr config")

    @staticmethod
    def label_edits(calls):
        return [
            a
            for a in calls
            if a[:3] == ["pr", "edit", PR] and ("--add-label" in a or "--remove-label" in a)
        ]

    @staticmethod
    def changes(calls):
        """(added, removed) label names over the `gh pr edit` calls, comma lists split."""
        added, removed = [], []
        for argv in LabelMergeCase.label_edits(calls):
            for flag, bucket in (("--add-label", added), ("--remove-label", removed)):
                for at, arg in enumerate(argv[:-1]):
                    if arg == flag:
                        bucket.extend(argv[at + 1].split(","))
        return added, removed

    @staticmethod
    def merge_at(calls):
        return [at for at, a in enumerate(calls) if a[:2] == ["pr", "merge"]]

    @staticmethod
    def label_warnings(out):
        return [
            line
            for line in out.splitlines()
            if "warning" in line.lower() and "label" in line.lower()
        ]


class LabelsAtMergeTests(LabelMergeCase):
    def test_a_label_changed_by_hand_is_put_back_before_the_merge(self):
        self.ready()
        self.scenario({"pr_labels": ["patch", "bug"], "labels": ["bug", "minor", "patch"]})
        out, calls = self.merge()
        self.assertEqual(self.changes(calls), (["minor"], ["patch"]))  # `bug` stays
        merged = self.merge_at(calls)
        self.assertEqual(len(merged), 1, calls)
        self.assertLess(calls.index(self.label_edits(calls)[-1]), merged[0])
        self.assertEqual(self.label_warnings(out), [])

    def test_change_md_edited_after_the_run_decides_the_label(self):
        # change.md is not tracked, so HEAD has not moved and the branch is still ready
        self.ready()
        text = self.read(CHANGE_MD)
        self.write(CHANGE_MD, text.replace("kind: feature\n", "kind: feature\nrelease: major\n", 1))
        self.scenario({"pr_labels": ["minor"], "labels": ["bug", "minor"]})
        out, calls = self.merge()
        self.assertEqual(self.changes(calls), (["major"], ["minor"]))
        # AC-5: the repository has no `major` yet, so it is created once, before the edit
        creates = [a for a in calls if a[:2] == ["label", "create"]]
        self.assertEqual(len(creates), 1, creates)
        self.assertEqual(creates[0][2], "major")
        self.assertLess(calls.index(creates[0]), calls.index(self.label_edits(calls)[0]))
        self.assertEqual(len(self.merge_at(calls)), 1)

    def test_a_pr_that_matches_is_not_edited(self):
        self.ready()
        self.scenario({"pr_labels": ["minor", "bug"], "labels": ["bug", "minor"]})
        out, calls = self.merge()
        # it looked: the PR's labels were asked for, and nothing had to change
        self.assertTrue(
            [a for a in calls if a[:3] == ["pr", "view", PR] and "labels" in " ".join(a)], calls
        )
        self.assertEqual(self.label_edits(calls), [])
        self.assertEqual([a for a in calls if a[:2] == ["label", "create"]], [])
        self.assertEqual(len(self.merge_at(calls)), 1)

    def test_turned_off_the_labels_are_left_alone(self):
        self.config("[pr]\nrelease_labels = []\n")
        self.ready()
        self.scenario({"pr_labels": ["patch", "major"]})
        out, calls = self.merge()
        self.assertEqual(self.label_edits(calls), [])
        self.assertEqual(len(self.merge_at(calls)), 1)


class LabelFailuresAtMergeTests(LabelMergeCase):
    """A label that cannot be read, created or changed warns; the PR is merged all the same."""

    def test_a_failed_edit_warns_and_merges(self):
        self.ready()
        self.scenario({"pr_labels": ["patch"], "labels": ["minor", "patch"], "label_edit_exit": 1})
        out, calls = self.merge()
        self.assertTrue(self.label_edits(calls), calls)  # it did try
        self.assertTrue(self.label_warnings(out), out)
        self.assertEqual(len(self.merge_at(calls)), 1)

    def test_a_failed_create_warns_and_merges(self):
        self.ready()
        self.scenario({"pr_labels": [], "labels": ["bug"], "label_create_exit": 1})
        out, calls = self.merge()
        self.assertTrue([a for a in calls if a[:2] == ["label", "create"]], calls)
        self.assertTrue(self.label_warnings(out), out)
        self.assertEqual(len(self.merge_at(calls)), 1)

    def test_labels_that_cannot_be_read_warn_and_merge(self):
        self.ready()
        self.scenario({"pr_view_exit": 1})
        out, calls = self.merge()
        self.assertTrue(self.label_warnings(out), out)
        self.assertEqual(self.label_edits(calls), [])
        self.assertEqual(len(self.merge_at(calls)), 1)


if __name__ == "__main__":
    unittest.main()
