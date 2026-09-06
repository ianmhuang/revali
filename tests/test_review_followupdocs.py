"""The documentation side of fix/issue-followups: `docs/files.md` names both issue files by
the number each carries (AC-2); the README diagram, role table, "What a run does" paragraph
and Status paragraph reflect PR #28 to #33 while the README stays a front page (AC-6)."""

import os
import re
import unittest

from revali import VERSION
from tests.helpers import ROOT


def repo_file(*parts):
    with open(os.path.join(ROOT, *parts), "r", encoding="utf-8", newline="") as fh:
        return fh.read()


def table_rows(text):
    rows = []
    for line in text.splitlines():
        if line.startswith("|") and not re.match(r"^\|[-| ]+\|$", line):
            rows.append([c.strip() for c in line.strip().strip("|").split("|")])
    return rows


class FilesMd(unittest.TestCase):
    def setUp(self):
        self.text = repo_file("docs", "files.md")
        self.rows = table_rows(self.text)

    def row_with(self, needle):
        hits = [r for r in self.rows if needle in r[0]]
        self.assertEqual(len(hits), 1, "one row whose first cell mentions %r" % needle)
        return hits[0]

    def test_issue_body_copy_is_numbered_by_the_validation(self):
        row = self.row_with("issue body")
        joined = " | ".join(row)
        self.assertIn("logs/issue-<validation>.md", joined)  # AC-2
        self.assertNotIn("logs/issue-<n>.md", joined)
        self.assertRegex(joined.lower(), r"validation")
        self.assertIn("gh issue create", joined)

    def test_merge_comment_copy_has_its_own_row(self):
        row = self.row_with("issue comment")
        joined = " | ".join(row)
        self.assertIn("logs/issue-<issue number>-merged.md", joined)  # AC-2
        self.assertIn("revali merge", row[1])  # its writer
        self.assertIn("gh issue comment", row[2])  # its reader
        self.assertIn("<issue number>", joined)

    def test_no_row_uses_the_ambiguous_form(self):
        self.assertNotIn("issue-<n>", self.text)


class WriteRetryKey(unittest.TestCase):
    """Round 1 F1: `[paths] write_retry_s` now bounds a read waiting for a writer as well as a
    write waiting for a reader, and every place that describes the key says so."""

    def test_files_md_state_row_names_both_directions(self):
        rows = table_rows(repo_file("docs", "files.md"))
        row = next(r for r in rows if r[0].startswith("`state.json`"))
        note = row[-1]
        self.assertIn("write_retry_s", note)
        self.assertRegex(note, r"write waits for a reader")
        self.assertRegex(note, r"read[^|]*waits for a writer")
        self.assertIn("`wait`", note)  # the readers that poll

    def test_defaults_toml_comment_names_both_directions(self):
        line = self.key_line(repo_file("defaults.toml"))
        self.assertIn("write", line)
        self.assertRegex(line, r"read that a writer blocks")

    def test_user_config_template_comment_names_both_directions(self):
        line = self.key_line(repo_file("templates", "user-config.toml"))
        self.assertIn("write waits for a reader", line)
        self.assertRegex(line, r"read for a writer")

    def key_line(self, text):
        hits = [ln for ln in text.splitlines() if re.match(r"^#?\s*write_retry_s\s*=", ln)]
        self.assertEqual(len(hits), 1, hits)
        return hits[0]


class ReadmeDiagram(unittest.TestCase):
    def setUp(self):
        self.text = repo_file("README.md")
        start = self.text.index("```mermaid")
        end = self.text.index("```", start + 10)
        self.diagram = self.text[start:end]
        self.lines = [ln.strip() for ln in self.diagram.splitlines()]

    def index(self, needle):
        for i, line in enumerate(self.lines):
            if needle in line:
                return i
        self.fail("%r not in the diagram" % needle)

    def test_lint_and_smoke_run_before_the_tests_are_committed(self):
        answer = self.index("acceptance tests")
        commit = self.index("commit tests")
        gate = [i for i, ln in enumerate(self.lines) if "lint" in ln and "smoke" in ln]
        self.assertEqual(len(gate), 1, self.lines)  # AC-6
        self.assertGreater(gate[0], answer)
        self.assertLess(gate[0], commit)

    def test_fail_path_shows_the_base_rerun_and_the_issue(self):
        fail = self.index("else FAIL")
        exit2 = next(i for i, ln in enumerate(self.lines) if i > fail and "exit 2" in ln)
        fail_block = self.lines[fail:exit2]
        rerun = [i for i, ln in enumerate(fail_block) if "base tip" in ln]
        diagnosis = [i for i, ln in enumerate(fail_block) if "diagnosis" in ln]
        issue = [i for i, ln in enumerate(fail_block) if "issue" in ln and "->>G" in ln]
        self.assertEqual(len(rerun), 1, fail_block)  # AC-6: the rerun on the base tip
        self.assertEqual(len(diagnosis), 1, fail_block)
        self.assertEqual(len(issue), 1, fail_block)  # AC-6: the issue, on the FAIL path
        self.assertLess(rerun[0], diagnosis[0])  # the rerun feeds the diagnosis
        self.assertLess(diagnosis[0], issue[0])  # the diagnosis decides on the issue
        verdict = fail_block[diagnosis[0]]
        self.assertIn("branch", verdict)  # introduced by branch or base
        self.assertIn("base", verdict)

    def test_issue_comment_follows_the_merge(self):
        merge = self.index("squash merge")
        after = self.lines[merge + 1 :]
        self.assertTrue(any("issue" in ln and "comment" in ln for ln in after), after)  # AC-6

    def test_validation_arrow_names_the_baseline_exception(self):
        i = self.index("sandbox:")
        self.assertIn("baseline", self.lines[i])
        self.assertIn("existing suite", self.lines[i])


class ReadmeRoleTable(unittest.TestCase):
    def setUp(self):
        self.text = repo_file("README.md")
        rows = table_rows(self.text)
        header = next(r for r in rows if r[0] == "Role")
        self.writes = header.index("Writes")
        self.row = {r[0]: r for r in rows if r[0] in ("Reviewer", "Validator")}

    def test_reviewer_tests_gated_by_lint_and_smoke_run(self):
        writes = self.row["Reviewer"][self.writes]
        self.assertIn("lint", writes)  # AC-6
        self.assertIn("smoke", writes)
        self.assertIn("test_dir", writes)

    def test_validator_row_names_the_issue(self):
        writes = self.row["Validator"][self.writes]
        self.assertIn("issue", writes)  # AC-6
        self.assertIn("introduced_by", writes)
        self.assertIn("diagnose-n.json", writes)


class ReadmeParagraphs(unittest.TestCase):
    def setUp(self):
        self.text = repo_file("README.md")

    def paragraph(self, marker):
        idx = self.text.index(marker)
        return " ".join(self.text[idx:].split("\n\n", 1)[0].split())

    def test_what_a_run_does_names_the_new_steps(self):
        p = self.paragraph("What a run does")
        self.assertIn("lint", p)
        self.assertIn("base tip", p)  # AC-6: the rerun
        self.assertRegex(p, r"branch or the base")  # the diagnosis's verdict
        self.assertIn("issue", p)
        self.assertRegex(p, r"reviewer.s test commits")  # kept for the earlier tests
        self.assertIn("existing suite", p)
        self.assertIn("baseline", p)

    def test_status_names_pr_28_to_33_and_what_they_brought(self):
        m = re.search(r"^Status:.*?(?=\n\n)", self.text, flags=re.M | re.S)
        self.assertIsNotNone(m)
        p = " ".join(m.group(0).split())
        self.assertRegex(p, r"PR #28 to #33")  # AC-6
        self.assertIn("PR #21 to #24", p)
        for word in ("timing", "baseline", "base tip", "lint", "issue"):
            self.assertIn(word, p, word)
        self.assertIn("package version %s" % VERSION, p)
        self.assertIn("Verified end to end on a private GitHub repository", p)

    def test_readme_stays_a_front_page(self):
        lines = self.text.splitlines()
        self.assertLessEqual(len(lines), 170, len(lines))  # AC-6
        for sentence in (
            "until the first review round is recorded",
            "not a FAIL verdict",
            "**Why separate sessions.**",
            "revali reviews its own changes on this",
        ):
            self.assertIn(sentence, self.text, sentence)


if __name__ == "__main__":
    unittest.main()
