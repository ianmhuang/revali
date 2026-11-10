"""Review tests for fix/pr-title-sync, the run side: a run that reuses an open PR gives it
change.md's title when the two differ (AC-1), leaves it alone when they are equal, when gh
reported no title, or when the PR is new (AC-2), carries on with one warning when the edit
fails (AC-3), and edits nothing in a dry run (AC-5).

The scenario is the one in the Request: change.md's `title` is changed after the draft PR was
opened, so the open PR still carries the old one.
"""

import unittest

from revali import EXIT_OK
from tests.helpers import RepoCase, claude_entry, run_cli

OLD = "Add mul to calc"  # the fixture change.md's title, and what the open PR still says
NEW = "Multiply two numbers in calc"
LOG = ".revali/feature__mul/logs/revali.log"
PR = 12


def open_pr(**fields):
    pr = {"number": PR, "url": "https://github.example/me/sample/pull/%d" % PR, "isDraft": True}
    pr.update(fields)
    return {"prs_open": [pr]}


class RunCase(RepoCase):
    def retitle(self, new):
        """change.md gets a new title, as the author would edit it after the PR was opened."""
        with open(self.change_md(), "r", encoding="utf-8", newline="") as fh:
            text = fh.read()
        self.assertIn("title: %s\n" % OLD, text)
        with open(self.change_md(), "w", encoding="utf-8", newline="") as fh:
            fh.write(text.replace("title: %s\n" % OLD, "title: %s\n" % new, 1))

    def gh(self):
        return [c["argv"] for c in self.fake_calls("gh")]

    def title_edits(self):
        return [a for a in self.gh() if a[:2] == ["pr", "edit"] and "--title" in a]

    def creates(self):
        return [a for a in self.gh() if a[:2] == ["pr", "create"]]

    def run_to_ready(self):
        self.claude(claude_entry())
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        return out

    def log_lines(self):
        return self.read(LOG).splitlines()


class StaleTitleTests(RunCase):
    """AC-1"""

    def test_reused_pr_gets_the_new_title_with_one_edit(self):
        self.retitle(NEW)
        self.scenario(open_pr(title=OLD))
        self.run_to_ready()
        self.assertEqual(self.creates(), [])  # the open PR was reused
        self.assertEqual(self.title_edits(), [["pr", "edit", str(PR), "--title", NEW]])

    def test_the_log_names_the_old_and_the_new_title(self):
        self.retitle(NEW)
        self.scenario(open_pr(title=OLD))
        self.run_to_ready()
        both = [line for line in self.log_lines() if OLD in line and NEW in line]
        self.assertEqual(len(both), 1, self.read(LOG))

    def test_whitespace_around_the_pr_title_does_not_hide_a_difference(self):
        self.retitle(NEW)
        self.scenario(open_pr(title="  %s \n" % OLD))
        self.run_to_ready()
        self.assertEqual(self.title_edits(), [["pr", "edit", str(PR), "--title", NEW]])


class NothingToEditTests(RunCase):
    """AC-2"""

    def test_equal_titles(self):
        self.scenario(open_pr(title=OLD))
        self.run_to_ready()
        self.assertEqual(self.creates(), [])
        self.assertEqual(self.title_edits(), [])

    def test_equal_but_for_surrounding_whitespace(self):
        self.scenario(open_pr(title="\t %s  \n" % OLD))
        self.run_to_ready()
        self.assertEqual(self.title_edits(), [])

    def test_gh_reported_no_title_key(self):
        self.retitle(NEW)
        self.scenario(open_pr())
        self.run_to_ready()
        self.assertEqual(self.creates(), [])
        self.assertEqual(self.title_edits(), [])

    def test_gh_reported_a_null_title(self):
        self.retitle(NEW)
        self.scenario(open_pr(title=None))
        self.run_to_ready()
        self.assertEqual(self.title_edits(), [])

    def test_a_new_pr_is_created_with_the_title_and_not_edited(self):
        self.retitle(NEW)
        self.run_to_ready()
        creates = self.creates()
        self.assertEqual(len(creates), 1)
        self.assertEqual(creates[0][creates[0].index("--title") + 1], NEW)
        self.assertEqual(self.title_edits(), [])


class FailedEditTests(RunCase):
    """AC-3"""

    def test_one_warning_with_ghs_message_and_the_run_goes_on(self):
        self.retitle(NEW)
        self.scenario({**open_pr(title=OLD), "title_edit_exit": 1})
        self.run_to_ready()  # exit 0: the review and the validation still happened
        edits = self.title_edits()
        self.assertGreaterEqual(len(edits), 1)  # the edit was attempted
        self.assertEqual({tuple(a) for a in edits}, {("pr", "edit", str(PR), "--title", NEW)})
        warnings = [
            line
            for line in self.log_lines()
            if "warning" in line.lower() and "title" in line.lower()
        ]
        self.assertEqual(len(warnings), 1, self.read(LOG))
        self.assertIn("gh_stub: pr edit --title failed", warnings[0])


class DryRunTests(RunCase):
    """AC-5"""

    def test_a_dry_run_edits_nothing(self):
        self.retitle(NEW)
        self.scenario(open_pr(title=OLD))
        code, out = run_cli(["run", "--foreground", "--dry-run"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertEqual([a for a in self.gh() if a[:2] == ["pr", "edit"]], [])


if __name__ == "__main__":
    unittest.main()
