"""Reviewer acceptance tests for feature/archive-outside-merge, AC-7: what `revali stats`
makes of the rows `revali archive` writes.

A pipeline whose last row is `merged` counts in the "merged" column whether `revali merge` or
`revali archive` wrote that row; one whose last row is `closed` counts neither as merged nor
as passed; one line gives how many pipelines were merged outside `revali merge` and how many
were closed without merging.

The numbers are read from the table by column name and from the one line that names both
counts. On the base branch that line does not exist and there is no `archive` subcommand."""

import re

from revali import EXIT_OK
from revali.config import history_path
from revali.state import State, append_history
from revali.stats import summarise
from tests.helpers import RepoCase, git, run_cli

REPO = "me/sample"


def row(branch, pr, stage, **extra):
    return dict(repo=REPO, branch=branch, pr=pr, stage=stage, fixes=0, rounds=1, **extra)


def table_row(text):
    """The repository's line of the stats table as {column name: cell}."""
    lines = text.splitlines()
    header = [line for line in lines if line.startswith("| repo |")]
    cells = [line for line in lines if line.startswith("| %s |" % REPO)]
    assert len(header) == 1 and len(cells) == 1, text
    names = [c.strip() for c in header[0].strip("|").split("|")]
    return dict(zip(names, [c.strip() for c in cells[0].strip("|").split("|")], strict=True))


def outside_counts(text):
    """(merged outside `revali merge`, closed without merging) from the one line with both."""
    lines = [
        line
        for line in text.splitlines()
        if "merged outside" in line and "closed without merging" in line
    ]
    assert len(lines) == 1, text
    outside = re.search(r"merged outside\D*(\d+)", lines[0])
    closed = re.search(r"closed without merging\D*(\d+)", lines[0])
    assert outside and closed, lines[0]
    return int(outside.group(1)), int(closed.group(1))


class Summary(RepoCase):
    def test_merged_either_way_counts_as_merged_and_closed_as_neither(self):
        rows = [
            row("a", 1, "ready_to_merge"),
            row("a", 1, "merged"),  # through `revali merge`
            row("b", 2, "ready_to_merge"),
            row("b", 2, "merged", merged_outside=True, stage_before="ready_to_merge"),
            row("c", 3, "ready_to_merge"),
            row("c", 3, "closed", stage_before="ready_to_merge"),
            row("d", 4, "ready_to_merge"),  # still waiting for its merge
        ]
        text = summarise(rows)
        cells = table_row(text)
        self.assertEqual(cells["runs"], "4")
        self.assertEqual(cells["merged"], "2")
        # passed: a, b and d; the closed one is not among them
        self.assertEqual(cells["first-try pass"], "3/3")
        self.assertEqual(outside_counts(text), (1, 1))

    def test_a_closed_pipeline_alone_has_passed_nothing(self):
        text = summarise(
            [
                row("c", 3, "ready_to_merge"),
                row("c", 3, "closed", stage_before="ready_to_merge"),
            ]
        )
        cells = table_row(text)
        self.assertEqual(cells["merged"], "0")
        self.assertEqual(cells["first-try pass"], "-")
        self.assertEqual(outside_counts(text), (0, 1))

    def test_the_line_is_there_with_nothing_to_count(self):
        text = summarise([row("a", 1, "ready_to_merge"), row("a", 1, "merged")])
        self.assertEqual(table_row(text)["merged"], "1")
        self.assertEqual(outside_counts(text), (0, 0))

    def test_pipelines_are_counted_not_rows(self):
        """A branch closed under one PR and merged under a later one is two pipelines; a
        `revali merge` whose record `revali archive` finished (AC-3: `stage_before` is
        `merged`, no `merged_outside`) is one pipeline, merged, and not merged outside."""
        rows = [
            row("e", 5, "closed", stage_before="needs_action"),
            row("e", 6, "merged", merged_outside=True, stage_before="ready_to_merge"),
            row("f", 7, "ready_to_merge"),
            row("f", 7, "merged"),  # written by `revali merge`
            row("f", 7, "merged", stage_before="merged"),  # written by `revali archive`
        ]
        text = summarise(rows)
        cells = table_row(text)
        self.assertEqual(cells["runs"], "3")
        self.assertEqual(cells["merged"], "2")
        self.assertEqual(outside_counts(text), (1, 1))


class AfterArchive(RepoCase):
    """`revali archive` then `revali stats`, on the rows the commands write themselves."""

    def ready(self, stage="ready_to_merge"):
        """A branch whose last run ended READY TO MERGE: its state and its history row."""
        git(["push", "-q", "-u", "origin", "feature/mul"], self.repo)
        State(
            repo=REPO,
            branch="feature/mul",
            base="main",
            stage=stage,
            last_exit=EXIT_OK,
            pr_number=7,
        ).save(self.rdir())
        append_history(history_path(None), row("feature/mul", 7, "ready_to_merge"))
        git(["checkout", "-q", "main"], self.repo)

    def archive(self, pr_state):
        pr = {"number": 7, "url": "https://github.example/me/sample/pull/7", "state": pr_state}
        self.scenario({"pr_create": pr})
        code, out = run_cli(["archive", "--branch", "feature/mul"])
        self.assertEqual(code, EXIT_OK, out)

    def stats(self):
        code, out = run_cli(["stats"])
        self.assertEqual(code, 0, out)
        return out

    def test_ready_then_merged_elsewhere(self):
        self.ready()
        before = self.stats()
        self.assertEqual(table_row(before)["merged"], "0")
        self.archive("MERGED")
        text = self.stats()
        cells = table_row(text)
        self.assertEqual(cells["runs"], "1")  # the archive row joined the run's pipeline
        self.assertEqual(cells["merged"], "1")
        self.assertEqual(cells["first-try pass"], "1/1")
        self.assertEqual(outside_counts(text), (1, 0))

    def test_a_revali_merge_finished_by_archive_is_not_merged_outside(self):
        """AC-3 with AC-7: `revali merge` merged the PR and recorded it, and stopped before
        its archive; the `archive` that finishes the record adds nothing to "merged outside"."""
        self.ready(stage="merged")
        append_history(history_path(None), row("feature/mul", 7, "merged"))
        self.archive("MERGED")
        text = self.stats()
        cells = table_row(text)
        self.assertEqual(cells["runs"], "1")
        self.assertEqual(cells["merged"], "1")
        self.assertEqual(outside_counts(text), (0, 0))

    def test_ready_then_closed(self):
        self.ready()
        self.assertEqual(table_row(self.stats())["first-try pass"], "1/1")
        self.archive("CLOSED")
        text = self.stats()
        cells = table_row(text)
        self.assertEqual(cells["runs"], "1")
        self.assertEqual(cells["merged"], "0")
        self.assertEqual(cells["first-try pass"], "-")  # no longer counted as ready to merge
        self.assertEqual(outside_counts(text), (0, 1))
