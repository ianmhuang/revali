"""Review of feature/docs-split: the README as a front page and the reference material under
docs/, checked as text against the repository files (AC-1, AC-2, AC-4, AC-5, AC-10, AC-11).
AC-3 is sampled: sentences the old README carried must appear, unwrapped and unchanged, in one
of the docs/ files and no longer in the README. Nothing here runs a pipeline."""
import glob
import os
import re
import unittest

from tests.helpers import ROOT, run_cli
from revali import EXIT_OK, VERSION

DOCS = ("workflow.md", "configuration.md", "files.md", "sandbox.md", "side-effects.md")


def read(*parts):
    with open(os.path.join(ROOT, *parts), "r", encoding="utf-8", newline="") as fh:
        return fh.read()


def unwrap(text):
    return " ".join(text.split())


def h2(text, title):
    parts = text.split("\n## %s\n" % title, 1)
    if len(parts) != 2:
        raise AssertionError("no '## %s' section" % title)
    return parts[1].split("\n## ", 1)[0]


class ReadmeIsAFrontPage(unittest.TestCase):
    """AC-1"""

    def setUp(self):
        self.text = read("README.md")

    def test_at_most_170_lines(self):
        self.assertLessEqual(len(self.text.splitlines()), 170)

    def test_the_parts_come_in_the_stated_order(self):
        markers = ["- **Developer**", "- **Reviewer**", "- **Validator**", "**Why separate sessions.**",
                   "```mermaid", "sequenceDiagram", "| Role |", "Three user actions", "Exit codes:",
                   "\nStatus:", "\n## Requirements\n", "\n## Usage\n", "\n## Documentation\n",
                   "\n## Development\n", "\n## License\n"]
        positions = []
        for marker in markers:
            self.assertIn(marker, self.text, marker)
            positions.append(self.text.index(marker))
        self.assertEqual(positions, sorted(positions), "README parts out of order: %s" % markers)

    def test_why_paragraph_has_at_most_four_sentences_and_points_at_the_full_text(self):
        m = re.search(r"\*\*Why separate sessions\.\*\*(.*?)\n\n", self.text, flags=re.S)
        self.assertIsNotNone(m, "no 'Why separate sessions' paragraph")
        body = unwrap(m.group(1))
        sentences = [s for s in re.split(r"(?<=[.!?])\s+(?=[A-Z])", body) if s.strip()]
        self.assertLessEqual(len(sentences), 4, sentences)
        self.assertIn("`docs/workflow.md`", body)

    def test_documentation_index_links_every_file_under_docs(self):
        index = h2(self.text, "Documentation")
        names = sorted(os.listdir(os.path.join(ROOT, "docs")))
        self.assertEqual(names, sorted(DOCS), "docs/ holds files the index does not know")
        for name in names:
            self.assertIn("`docs/%s`" % name, index, name)

    def test_the_moved_sections_are_no_longer_readme_headings(self):
        for heading in ("\n## Configuration\n", "\n## Files\n", "\n## Workflow\n", "\n## Sandbox\n",
                        "\n## Project setup\n", "\n## What revali does to your repository\n",
                        "\n## Several agents on one repository\n"):
            self.assertNotIn(heading, self.text, heading)

    def test_gates_and_exit_codes_stay_on_the_front_page(self):
        para = unwrap(self.text.split("Three user actions", 1)[1].split("\n\n", 1)[0])
        for code in ("`0` done / ready to merge", "`1` pipeline error", "`2` the Developer must act",
                     "`3` a human must decide", "`4` (`wait` only) still running"):
            self.assertIn(code, para, code)


class DocsHoldTheFormerSections(unittest.TestCase):
    """AC-2"""

    def test_workflow_doc(self):
        text = read("docs", "workflow.md")
        self.assertTrue(text.startswith("# Workflow\n"), text[:40])
        for title in ("Why separate sessions", "Workflow", "Several agents on one repository", "Project setup"):
            self.assertIn("\n## %s\n" % title, text, title)
        why = unwrap(h2(text, "Why separate sessions"))
        # the two clauses the README's short version dropped are what make it the full paragraph
        self.assertIn("the Developer never reviews the change it wrote", why)
        self.assertIn("derives its tests from the criteria", why)
        self.assertIn("it does not remove blind spots the models share", why)
        whole = unwrap(text)
        self.assertIn("`repo: <working tree root>  branch: <branch>`", text)       # the identity line
        self.assertIn("`run` takes `.revali/tree.lock`", whole)                     # the tree lock
        self.assertIn("After exit code 2 the author fixes or answers", whole)      # exit 2
        self.assertIn("A run that stops without a result", whole)                  # a dead run
        self.assertIn("status: draft", h2(text, "Workflow"))
        self.assertIn("`templates/revali.toml`", h2(text, "Project setup"))

    def test_sandbox_doc(self):
        text = read("docs", "sandbox.md")
        self.assertTrue(text.startswith("# Sandbox\n"), text[:40])
        for phrase in ('`[validate.linux] runner = "wsl"`', '`runner = "ssh"`', '`runner = "local"`',
                       "`connect_timeout_s`", "`transfer_timeout_min`", "BatchMode=yes"):
            self.assertIn(phrase, text, phrase)

    def test_side_effects_doc(self):
        text = read("docs", "side-effects.md")
        self.assertTrue(text.startswith("# What revali does to your repository\n"), text[:60])
        whole = unwrap(text)   # phrases may cross a line wrap
        for phrase in ("Read this before the first run.", "appends the state directory (`.revali/`)",
                       "`gh pr merge --<method> --delete-branch`", "never merges on its own"):
            self.assertIn(phrase, whole, phrase)

    def test_files_doc(self):
        text = read("docs", "files.md")
        self.assertTrue(text.startswith("# Files\n"), text[:40])
        self.assertIn("| Document | Written by | Read by | Default location | Config key |", text)
        self.assertTrue(any(l.startswith("| `tree.lock`") for l in text.splitlines()), "no tree.lock row")
        self.assertIn("Branch `feature/x` maps to directory `feature__x`.", unwrap(text))
        self.assertIn("`~/.revali/` itself moves with the `REVALI_HOME` environment variable.", unwrap(text))

    def test_configuration_doc(self):
        text = read("docs", "configuration.md")
        self.assertTrue(text.startswith("# Configuration\n"), text[:40])
        for phrase in ("Three layers, the most specific wins", "1. `defaults.toml`", "2. `~/.revali/config.toml`",
                       "3. `revali.toml`", 'Models: `model = "auto"`', "`REVALI_DISABLE=1`"):
            self.assertIn(phrase, text, phrase)


class MovedTextIsVerbatim(unittest.TestCase):
    """AC-3, sampled: one or two sentences from each former README section, as the old README
    had them, must be in docs/ unchanged (unwrapped) and gone from the README."""

    SENTENCES = (
        ("workflow.md", "The acceptance criteria come before the code."),
        ("workflow.md", "Approval means deleting the `status: draft` line; preflight refuses a draft "
                        "(`change.md: status is 'draft'; review it and remove the status line`), so nothing "
                        "runs on unapproved criteria."),
        ("workflow.md", "A working tree runs one pipeline at a time: `run` takes `.revali/tree.lock` next to "
                        "the branch lock, and a second `run` in the same checkout, on any branch, is refused "
                        "with the running branch and pid."),
        ("workflow.md", "Two clones with the same branch checked out are not a supported layout: they would "
                        "share the PR and the sandbox directory."),
        ("workflow.md", "Fresh context removes the author's bias toward its own change; it does not remove "
                        "blind spots the models share, which is why `revali stats` tracks the first-try "
                        "approval rate."),
        ("workflow.md", "User-level options live in `~/.revali/config.toml` (see `templates/user-config.toml`); "
                        "`REVALI_HOME` overrides the directory."),
        ("configuration.md", "Unknown keys are errors in every layer."),
        ("configuration.md", "`fallback_model = \"auto\"` is the tiers below the chosen one, strongest first."),
        ("files.md", "Branch `feature/x` maps to directory `feature__x`. `~/.revali/` itself moves with the "
                     "`REVALI_HOME` environment variable."),
        ("sandbox.md", "Every call runs with `BatchMode=yes`: nothing prompts, so key-based login must already "
                       "work and the host key must be known (run `ssh <host>` once by hand)."),
        ("sandbox.md", "`runner = \"local\"` uses a git worktree on the host with no isolation."),
        ("side-effects.md", "It never modifies files outside `test_dir` and the state directory, never commits a "
                            "change to a test file the reviewer did not write, never merges on its own, and "
                            "never runs on a repo you do not own."),
        ("side-effects.md", "This is the only deletion inside `test_dir` revali performs, so keep your own files "
                            "off `test_file_pattern`"),
    )

    def test_sentences_moved_unchanged(self):
        readme = unwrap(read("README.md"))
        for name, sentence in self.SENTENCES:
            with self.subTest(doc=name, sentence=sentence[:50]):
                self.assertIn(sentence, unwrap(read("docs", name)))
                self.assertNotIn(sentence, readme, "still in the README")


class CrossReferencesFollowTheMove(unittest.TestCase):
    """AC-4"""

    MOVED = ("Configuration", "Files", "Workflow", "Sandbox", "Project setup",
             "What revali does to your repository", "Several agents on one repository")

    def test_conventions_name_the_docs(self):
        text = read("CONVENTIONS.md")
        self.assertIn("`docs/side-effects.md`", text)
        self.assertIn("`docs/`", text)
        self.assertNotIn('"What revali does to your repository"', text)

    def test_template_conventions_name_the_docs(self):
        self.assertIn("`docs/`", read("templates", "CONVENTIONS.md"))

    def test_defaults_comment_points_at_the_configuration_page(self):
        text = read("defaults.toml")
        self.assertIn("docs/configuration.md", text)
        self.assertNotIn('README "Configuration"', text)

    def test_no_shipped_file_points_at_a_readme_section_that_moved(self):
        files = ["CLAUDE.md", "CONVENTIONS.md", "defaults.toml", os.path.join("skill", "SKILL.md")]
        for pattern in ("templates/*", "checklists/*.md", "prompts/*.md", "docs/*.md"):
            files.extend(os.path.relpath(p, ROOT) for p in glob.glob(os.path.join(ROOT, pattern)))
        titles = "|".join(re.escape(t) for t in self.MOVED)
        stale = re.compile(r"README[^\n]*[\"'“](%s)[\"'”]" % titles)
        for rel in files:
            if not os.path.isfile(os.path.join(ROOT, rel)):
                continue
            for line in read(rel).splitlines():
                self.assertIsNone(stale.search(line), "%s: %s" % (rel, line))


class VersionIs020(unittest.TestCase):
    """AC-5"""

    def test_constant(self):
        self.assertEqual(VERSION, "0.2.0")

    def test_version_command(self):
        code, out = run_cli(["version"])
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(out.strip(), "revali 0.2.0")

    def test_status_line_names_the_new_version_only(self):
        text = read("README.md")
        status = [l for l in text.splitlines() if l.startswith("Status:")]
        self.assertEqual(len(status), 1, status)
        para = unwrap(text[text.index(status[0]):].split("\n\n", 1)[0])
        self.assertIn("0.2.0", para)
        self.assertNotIn("0.1.0", text)


class WorktreeModeCondition(unittest.TestCase):
    """AC-10"""

    def test_several_agents_says_when_merge_uses_worktree_mode(self):
        body = unwrap(h2(read("docs", "workflow.md"), "Several agents on one repository"))
        self.assertIn("only when the base branch is checked out in another worktree", body)
        self.assertIn("merges like the primary tree, with `--delete-branch`", body)


class SshVerificationRecord(unittest.TestCase):
    """AC-11"""

    def test_sandbox_doc_records_date_setup_repository_kind_and_outcome(self):
        text = read("docs", "sandbox.md")
        self.assertIn("\n## Verification record\n", text)
        record = unwrap(h2(text, "Verification record"))
        self.assertRegex(record, r"\b20\d\d-\d\d-\d\d\b")                    # date
        self.assertIn("sshd", record)                                         # the sshd setup
        self.assertIn("key-only login", record)
        self.assertRegex(record, r"private repository|private GitHub repository")   # repository kind
        self.assertIn("`runner = \"ssh\"`", record)
        for outcome in ("APPROVE", "PASS", "`revali merge`"):                 # the run outcome
            self.assertIn(outcome, record, outcome)

    def test_readme_status_points_at_the_record(self):
        text = read("README.md")
        status = next(l for l in text.splitlines() if l.startswith("Status:"))
        para = unwrap(text[text.index(status):].split("\n\n", 1)[0])
        self.assertIn("`docs/sandbox.md`", para)


if __name__ == "__main__":
    unittest.main()
