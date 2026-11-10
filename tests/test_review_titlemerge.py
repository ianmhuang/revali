"""Review tests for fix/pr-title-sync, the merge side (AC-4): `revali merge` reads the PR title
with `gh pr view <n> --json title` before `gh pr merge`; a title that differs from change.md's
is edited first and the output says so; a failed edit stops with exit 1 and nothing is merged;
a title that cannot be read, or a missing change.md, is a warning and the merge goes on.

The scenario is the one in the Request: the branch is READY TO MERGE, then change.md's `title`
is changed (the file is not tracked, so HEAD does not move) and the PR still has the old one.
"""

import os
import unittest

from revali import EXIT_ERROR, EXIT_OK
from tests.helpers import RepoCase, claude_entry, git, run_cli

OLD = "Add mul to calc"  # the fixture change.md's title: what the PR was created with
NEW = "Multiply two numbers in calc"
PR = "7"  # the gh stub's `pr create` answer
VIEW_TITLE = ["pr", "view", PR, "--json", "title"]


class MergeCase(RepoCase):
    def ready(self):
        """A full run: draft PR #7 opened with the old title, reviewed, validated."""
        self.claude(claude_entry())
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)

    def retitle(self, new):
        with open(self.change_md(), "r", encoding="utf-8", newline="") as fh:
            text = fh.read()
        self.assertIn("title: %s\n" % OLD, text)
        with open(self.change_md(), "w", encoding="utf-8", newline="") as fh:
            fh.write(text.replace("title: %s\n" % OLD, "title: %s\n" % new, 1))

    def gh(self):
        return [c["argv"] for c in self.fake_calls("gh")]

    def title_edits(self):
        return [a for a in self.gh() if a[:2] == ["pr", "edit"] and "--title" in a]

    def merges(self):
        return [a for a in self.gh() if a[:2] == ["pr", "merge"]]

    def warnings(self, out, word):
        return [
            line for line in out.splitlines() if "warning" in line.lower() and word in line.lower()
        ]


class StaleTitleAtMergeTests(MergeCase):
    def test_title_is_read_and_fixed_before_the_merge(self):
        self.ready()
        self.retitle(NEW)
        self.scenario({"pr_title": OLD})
        code, out = run_cli(["merge"])
        self.assertEqual(code, EXIT_OK, out)
        gh = self.gh()
        self.assertEqual(self.title_edits(), [["pr", "edit", PR, "--title", NEW]])
        merge_at = [i for i, a in enumerate(gh) if a[:2] == ["pr", "merge"]]
        self.assertEqual(len(merge_at), 1, gh)
        self.assertIn(VIEW_TITLE, gh)
        self.assertLess(gh.index(VIEW_TITLE), merge_at[0])
        self.assertLess(gh.index(["pr", "edit", PR, "--title", NEW]), merge_at[0])
        # "and says so": one line of the output names the title replaced and its replacement
        self.assertTrue([line for line in out.splitlines() if OLD in line and NEW in line], out)

    def test_a_title_renamed_on_github_is_put_back(self):
        # Out of scope says change.md wins over a title changed by hand on GitHub
        self.ready()
        self.scenario({"pr_title": "Renamed on GitHub"})
        code, out = run_cli(["merge"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertEqual(self.title_edits(), [["pr", "edit", PR, "--title", OLD]])
        self.assertEqual(len(self.merges()), 1)

    def test_a_failed_edit_stops_with_exit_1_and_merges_nothing(self):
        self.ready()
        self.retitle(NEW)
        self.scenario({"pr_title": OLD, "title_edit_exit": 1})
        code, out = run_cli(["merge"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertEqual(EXIT_ERROR, 1)
        self.assertEqual(self.merges(), [])
        self.assertGreaterEqual(len(self.title_edits()), 1)  # it did try
        # nothing of the merge's local follow-up happened either
        self.assertEqual(
            git(["rev-parse", "--abbrev-ref", "HEAD"], self.repo).strip(), "feature/mul"
        )
        self.assertTrue(os.path.isfile(self.change_md()))


class NothingToFixAtMergeTests(MergeCase):
    def test_equal_titles_are_read_but_not_edited(self):
        self.ready()
        self.scenario({"pr_title": "  %s \n" % OLD})  # compared stripped, as during a run
        code, out = run_cli(["merge"])
        self.assertEqual(code, EXIT_OK, out)
        gh = self.gh()
        self.assertIn(VIEW_TITLE, gh)
        self.assertLess(
            gh.index(VIEW_TITLE), [i for i, a in enumerate(gh) if a[:2] == ["pr", "merge"]][0]
        )
        self.assertEqual(self.title_edits(), [])
        self.assertEqual(len(self.merges()), 1)

    def test_a_failed_view_is_a_warning_and_the_merge_goes_on(self):
        self.ready()
        self.retitle(NEW)
        self.scenario({"pr_title": OLD, "pr_view_exit": 1})
        code, out = run_cli(["merge"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn(VIEW_TITLE, self.gh())  # it did ask
        self.assertTrue(self.warnings(out, "title"), out)
        self.assertEqual(self.title_edits(), [])
        self.assertEqual(len(self.merges()), 1)

    def test_an_answer_without_a_title_is_a_warning_and_the_merge_goes_on(self):
        self.ready()
        self.retitle(NEW)
        self.scenario({"pr_title": None})  # gh answers, but `title` is null
        code, out = run_cli(["merge"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertTrue(self.warnings(out, "title"), out)
        self.assertEqual(self.title_edits(), [])
        self.assertEqual(len(self.merges()), 1)

    def test_a_missing_change_md_is_a_warning_and_the_merge_goes_on(self):
        self.ready()
        os.remove(self.change_md())
        self.scenario({"pr_title": "Renamed on GitHub"})
        code, out = run_cli(["merge"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertTrue(self.warnings(out, "change.md"), out)
        self.assertEqual(self.title_edits(), [])
        self.assertEqual(len(self.merges()), 1)

    def test_an_undecodable_change_md_is_a_warning_and_the_merge_goes_on(self):
        # round 1 F1: a change.md that is there but cannot be read is the same position as a
        # missing one: nothing to compare the PR title with
        self.ready()
        with open(self.change_md(), "wb") as fh:
            fh.write(b"---\ntitle: \xff\xfe broken\n---\n")
        self.scenario({"pr_title": "Renamed on GitHub"})
        code, out = run_cli(["merge"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertTrue(self.warnings(out, "change.md"), out)
        self.assertEqual(self.title_edits(), [])
        self.assertEqual(len(self.merges()), 1)

    def test_a_change_md_without_a_title_is_a_warning_and_the_merge_goes_on(self):
        # round 1 F2: an emptied title is reported, like a missing change.md
        self.ready()
        self.retitle("")
        self.scenario({"pr_title": "Renamed on GitHub"})
        code, out = run_cli(["merge"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertTrue(self.warnings(out, "title"), out)
        self.assertEqual(self.title_edits(), [])
        self.assertEqual(len(self.merges()), 1)


if __name__ == "__main__":
    unittest.main()
