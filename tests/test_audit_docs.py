"""fix/audit-docs: the corrections the 0.4.0 documentation audit asked for (AC-1 to AC-7).
Each test checks the corrected statement in its document and that the false one is gone."""

import os
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def text(rel):
    with open(os.path.join(ROOT, rel), "r", encoding="utf-8", newline="") as fh:
        return " ".join(fh.read().split())


class DiagnosisInput(unittest.TestCase):
    """AC-1"""

    def test_readme_and_workflow(self):
        for rel in ("README.md", "docs/workflow.md"):
            with self.subTest(rel=rel):
                doc = text(rel)
                self.assertNotIn("sees only the failure output", doc)
                for phrase in (
                    "the diagnosis session gets change.md, tests.md",
                    "Reviewer's test files",
                    "read-only",
                    "neither the Developer's nor the Reviewer's reasoning",
                ):
                    self.assertIn(phrase, doc)
        self.assertIn(
            "the rerun on the base tip and the other platforms' outcomes", text("docs/workflow.md")
        )
        self.assertIn(
            "the failing step's output, the base rerun and the other platforms' outcomes",
            text("README.md"),
        )


class SideEffects(unittest.TestCase):
    """AC-2"""

    def setUp(self):
        self.doc = text("docs/side-effects.md")

    def test_the_missing_side_effects_are_listed(self):
        for phrase in (
            "`gh pr ready <n>` once a validation passes",
            "`gh pr edit <n> --body-file <file>`",
            "`git worktree add --detach`",
            "`git worktree remove --force` and `git worktree prune`",
            "`git push --force-with-lease` instead of a plain push",
            "in the default commit mode when the reviewer's test commits are no longer in HEAD",
            "`git branch -D <branch>`, its result in the `local:` line",
        ):
            self.assertIn(phrase, self.doc)

    def test_the_guard_outside_test_dir(self):
        self.assertIn("any change outside `test_dir` and the state directory", self.doc)
        self.assertIn("is restored with `git checkout HEAD -- <path>`", self.doc)
        self.assertIn("made while the reviewer session runs is lost", self.doc)

    def test_the_false_sentence_is_gone(self):
        self.assertNotIn("never modifies files outside", self.doc)
        self.assertIn("Outside `test_dir` and the state directory it changes only what", self.doc)


class Skill(unittest.TestCase):
    """AC-3"""

    def setUp(self):
        self.doc = text("skill/SKILL.md")

    def test_ac_cannot_be_waived(self):
        self.assertNotIn("let the user waive them", self.doc)
        self.assertIn("Acceptance criteria cannot be waived", self.doc)
        self.assertIn("`kind: docs` included", self.doc)

    def test_when_the_baseline_runs(self):
        self.assertNotIn("preflight runs the suite again as a safety net", self.doc)
        self.assertIn("On a branch's first run (no review round yet, not `kind: docs`)", self.doc)
        self.assertIn("can leave the branch pushed with its draft PR open", self.doc)


class Configuration(unittest.TestCase):
    """AC-4"""

    def setUp(self):
        self.doc = text("docs/configuration.md")

    def test_user_level_keys(self):
        self.assertNotIn("every key may appear in any", self.doc)
        self.assertIn(
            "`checklist` and `history_path` at the top level and `[paths] history_file` "
            "belong in `~/.revali/config.toml`",
            self.doc,
        )

    def test_issue_rule(self):
        self.assertNotIn("One validation opens one issue for all such failures", self.doc)
        self.assertIn("each diagnosis opens at most one issue", self.doc)
        self.assertIn("an open issue in the repository with exactly the same title", self.doc)

    def test_revali_disable(self):
        self.assertNotIn("switches revali off entirely", self.doc)
        self.assertIn("`run --dry-run` and `preflight` with exit 1", self.doc)
        self.assertIn(
            "`wait`, `status`, `stop`, `clean`, `merge` and `archive` do not read it", self.doc
        )


class HistoryRows(unittest.TestCase):
    """AC-5"""

    def test_which_commands_write_a_row(self):
        doc = text("docs/files.md")
        self.assertNotIn("one row per `run` that started, `merge`, `stop` or `archive`", doc)
        for phrase in (
            "but not a finished `run --dry-run`",
            "a stop before `gh pr merge` (a dirty tree, HEAD moved, failed or pending CI) included",
            "not one that found nothing to stop",
            "even when the move of the state directory then fails",
        ):
            self.assertIn(phrase, doc)


class IssueTemplate(unittest.TestCase):
    """AC-6"""

    def test_merge_comment_conditions(self):
        doc = text("templates/issue.md")
        self.assertNotIn("When a PR revali handles merges", doc)
        self.assertIn(
            "When `revali merge` merges the PR of a branch whose validation opened or reused "
            "this issue",
            doc,
        )
        self.assertIn("A fix merged any other way gets no comment.", doc)


class Conventions(unittest.TestCase):
    """AC-7"""

    def setUp(self):
        self.doc = text("CONVENTIONS.md")

    def test_preflight_and_lint(self):
        self.assertNotIn("`revali preflight` runs both", self.doc)
        self.assertIn(
            "`revali preflight` and `run --dry-run` print the line and do not run it", self.doc
        )

    def test_defaults_rule(self):
        self.assertNotIn("No default value in code", self.doc)
        self.assertIn("models, budgets, the stage and platform timeouts, paths", self.doc)
        self.assertIn("the fixed file names", self.doc)
        self.assertIn("stay in code", self.doc)


if __name__ == "__main__":
    unittest.main()
