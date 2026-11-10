"""The PR title follows change.md's `title` (fix/pr-title-sync, M2.4): a run that reuses an
open PR fixes a stale title, and `revali merge` checks once more before it merges."""

import os
from unittest import mock

from revali import EXIT_ERROR, EXIT_OK
from revali.procs import ProcTimeout
from tests.helpers import RepoCase, claude_entry, run_cli

TITLE = "Add mul to calc"  # the sample change.md's title
REVALI_LOG = ".revali/feature__mul/logs/revali.log"


def open_pr(title=None):
    pr = {"number": 12, "url": "https://x/pull/12", "isDraft": True}
    if title is not None:
        pr["title"] = title
    return {"prs_open": [pr]}


class TitleCase(RepoCase):
    def setUp(self):
        super().setUp()
        os.environ["REVALI_POLL_SECONDS"] = "0.01"

    def gh(self):
        return [c["argv"] for c in self.fake_calls("gh")]

    def title_edits(self):
        return [a for a in self.gh() if a[:2] == ["pr", "edit"] and "--title" in a]

    def run_ok(self, *extra):
        self.claude(claude_entry())
        code, out = run_cli(["run", "--foreground", *extra])
        self.assertEqual(code, EXIT_OK, out)
        return out


class DuringARun(TitleCase):
    def test_a_stale_title_is_replaced_once(self):  # AC-1
        self.scenario(open_pr("Old title"))
        self.run_ok()
        self.assertEqual(self.title_edits(), [["pr", "edit", "12", "--title", TITLE]])
        self.assertIn("PR title updated: 'Old title' -> '%s'" % TITLE, self.read(REVALI_LOG))

    def test_an_equal_title_is_left_alone(self):  # AC-2
        self.scenario(open_pr("  %s \n" % TITLE))
        self.run_ok()
        self.assertEqual(self.title_edits(), [])

    def test_no_title_from_gh_means_no_edit(self):  # AC-2
        self.scenario(open_pr(None))
        self.run_ok()
        self.assertEqual(self.title_edits(), [])

    def test_a_new_pr_gets_no_edit(self):  # AC-2
        self.run_ok()
        self.assertTrue(any(a[:2] == ["pr", "create"] for a in self.gh()))
        self.assertEqual(self.title_edits(), [])

    def test_a_failed_edit_is_a_warning(self):  # AC-3
        self.scenario({**open_pr("Old title"), "title_edit_exit": 1})
        self.run_ok()
        edits = self.title_edits()  # run_retry tries a failed gh call once more
        self.assertGreaterEqual(len(edits), 1)
        self.assertEqual({tuple(a) for a in edits}, {("pr", "edit", "12", "--title", TITLE)})
        self.assertIn("warning: could not update the PR title", self.read(REVALI_LOG))

    def test_a_dry_run_edits_nothing(self):  # AC-5
        self.scenario(open_pr("Old title"))
        code, out = run_cli(["run", "--foreground", "--dry-run"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertEqual([a for a in self.gh() if a[:2] == ["pr", "edit"]], [])


class AtMerge(TitleCase):
    def merged(self):
        return [a for a in self.gh() if a[:2] == ["pr", "merge"]]

    def test_a_stale_title_is_fixed_before_the_merge(self):  # AC-4
        self.run_ok()
        self.scenario({"pr_title": "Renamed on GitHub"})
        code, out = run_cli(["merge"])
        self.assertEqual(code, EXIT_OK, out)
        gh = self.gh()
        edit = gh.index(["pr", "edit", "7", "--title", TITLE])
        merge = [i for i, a in enumerate(gh) if a[:2] == ["pr", "merge"]]
        self.assertEqual(len(merge), 1)
        self.assertLess(edit, merge[0])
        self.assertIn("PR title updated: 'Renamed on GitHub' -> '%s'" % TITLE, out)

    def test_a_failed_edit_stops_the_merge(self):  # AC-4
        self.run_ok()
        self.scenario({"pr_title": "Renamed on GitHub", "title_edit_exit": 1})
        code, out = run_cli(["merge"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertEqual(self.merged(), [])
        self.assertIn("Renamed on GitHub", out)
        self.assertIn(TITLE, out)
        self.assertIn("not merged", out)

    def test_an_equal_title_is_left_alone(self):  # AC-4
        self.run_ok()
        code, out = run_cli(["merge"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertEqual(self.title_edits(), [])
        self.assertEqual(len(self.merged()), 1)

    def test_an_unreadable_title_is_a_warning(self):  # AC-4
        self.run_ok()
        self.scenario({"pr_view_exit": 1})
        code, out = run_cli(["merge"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("warning: could not read the PR title", out)
        self.assertEqual(self.title_edits(), [])
        self.assertEqual(len(self.merged()), 1)

    def test_a_missing_change_md_is_a_warning(self):  # AC-4
        self.run_ok()
        os.remove(self.change_md())
        self.scenario({"pr_title": "Renamed on GitHub"})
        code, out = run_cli(["merge"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("warning: no change.md to check the PR title against", out)
        self.assertEqual(self.title_edits(), [])


def timing_out_on(module, name, marker):
    """A stand-in for `module.name` (a procs runner) that raises ProcTimeout for the gh call
    whose argv holds `marker`, and runs every other command for real."""
    real = getattr(module, name)

    def fake(cmd, *args, **kwargs):
        if marker in cmd:
            raise ProcTimeout("timed out after 120s: gh")
        return real(cmd, *args, **kwargs)

    return mock.patch.object(module, name, side_effect=fake)


class Round1Lows(TitleCase):
    """PR #56 round 1 F1-F3: every way the title cannot be checked is a warning (or, for a
    failed edit at merge, the existing refusal), never a traceback."""

    def merged(self):
        return [a for a in self.gh() if a[:2] == ["pr", "merge"]]

    def test_a_hung_view_at_merge_is_a_warning(self):  # F1
        from revali import merge

        self.run_ok()
        with timing_out_on(merge, "run", "title"):
            code, out = run_cli(["merge"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("warning: could not read the PR title (timed out after 120s: gh)", out)
        self.assertEqual(len(self.merged()), 1)

    def test_a_hung_edit_at_merge_refuses_the_merge(self):  # F1
        from revali import pr

        self.run_ok()
        self.scenario({"pr_title": "Renamed on GitHub"})
        with timing_out_on(pr, "run_retry", "--title"):
            code, out = run_cli(["merge"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertIn("not merged", out)
        self.assertIn("timed out after 120s", out)
        self.assertNotIn("Traceback", out)
        self.assertEqual(self.merged(), [])

    def test_a_hung_edit_during_a_run_is_a_warning(self):  # F1
        from revali import pr

        self.scenario(open_pr("Old title"))
        self.claude(claude_entry())
        with timing_out_on(pr, "run_retry", "--title"):
            code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn(
            "warning: could not update the PR title: timed out after 120s", self.read(REVALI_LOG)
        )

    def test_an_unreadable_change_md_is_a_warning(self):  # F1
        self.run_ok()
        with open(self.change_md(), "wb") as fh:
            fh.write(b"---\ntitle: \xff\xfe broken\n---\n")
        code, out = run_cli(["merge"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("warning: change.md could not be read", out)
        self.assertEqual(len(self.merged()), 1)

    def test_an_empty_title_is_a_warning(self):  # F2
        self.run_ok()
        with open(self.change_md(), "r", encoding="utf-8", newline="") as fh:
            text = fh.read()
        self.assertIn("title: %s\n" % TITLE, text)
        with open(self.change_md(), "w", encoding="utf-8", newline="") as fh:
            fh.write(text.replace("title: %s\n" % TITLE, "title:\n"))
        self.scenario({"pr_title": "Renamed on GitHub"})
        code, out = run_cli(["merge"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("warning: change.md has no title to check the PR title against", out)
        self.assertEqual(self.title_edits(), [])

    def test_no_title_in_gh_answer_says_so(self):  # F3
        self.run_ok()
        self.scenario({"pr_title": None})
        code, out = run_cli(["merge"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("warning: could not read the PR title (no title in gh's answer)", out)
        self.assertNotIn('"number"', out)


class Docs(TitleCase):
    def test_side_effects(self):  # AC-6
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(here, "docs", "side-effects.md"), encoding="utf-8") as fh:
            text = " ".join(fh.read().split())
        self.assertIn("gh pr edit <n> --title", text)
        self.assertIn("revali merge", text)
