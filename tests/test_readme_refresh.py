"""docs/readme-refresh (M3.5.3.3.4): README says what revali does today and where each part
is described, and three checks keep its command list, its tool list and the stage diagram
in docs/workflow.md from drifting from the code."""

import glob
import os
import re
import unittest

from revali.cli import build_parser
from revali.state import STAGES, TERMINAL_STAGES
from tests.helpers import ROOT

LINK = re.compile(r"\[[^\]]+\]\(([^)]+)\)")


def read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8", newline="") as fh:
        return fh.read()


def h2(text, title):
    return text.split("\n## %s\n" % title, 1)[1].split("\n## ", 1)[0]


def code_block(section):
    return section.split("```", 2)[1]


def slug(heading):
    """GitHub's anchor for a Markdown heading."""
    text = re.sub(r"[^\w\- ]", "", heading.strip().lower())
    return text.replace(" ", "-")


def headings(path):
    """(anchor, heading line) for the headings of `path` outside fenced code, with the
    anchors GitHub gives them: a repeated heading gets `-1`, `-2`, ... after the first
    (round 1 F2: `# Workflow` and `## Workflow` are `#workflow` and `#workflow-1`)."""
    seen, found, fence = {}, [], False
    for line in read(path).split("\n"):
        if line.strip().startswith("```"):
            fence = not fence
            continue
        m = None if fence else re.match(r"#{1,6} (.+)$", line)
        if m:
            base = slug(m.group(1))
            n = seen.get(base, 0)
            seen[base] = n + 1
            found.append((base if n == 0 else "%s-%d" % (base, n), line))
    return found


def anchors(path):
    return [a for a, _ in headings(path)]


def capability_rows(readme):
    block = readme.split("**What it does.**", 1)[1].split("\n\n", 2)[1]
    rows = [ln for ln in block.splitlines() if ln.startswith("|")]
    return rows[2:]  # header and separator


def broken_links(rows, here=ROOT):
    """Links in `rows` (relative to the directory `here`) whose file is missing or whose
    `#anchor` names no heading there."""
    broken = []
    for row in rows:
        for href in LINK.findall(row):
            if "://" in href:
                continue
            path, _, anchor = href.partition("#")
            full = os.path.normpath(os.path.join(here, path))
            if not os.path.isfile(full):
                broken.append(href)
            elif anchor and anchor not in anchors(os.path.relpath(full, ROOT)):
                broken.append(href)
    return broken


def cli_names():
    """Every subcommand and every option string revali's parser defines, -h aside."""
    parser = build_parser()
    names = set()
    for action in parser._actions:
        names.update(o for o in action.option_strings if o not in ("-h", "--help"))
        if isinstance(action.choices, dict):
            for name, sub in action.choices.items():
                names.add(name)
                for sub_action in sub._actions:
                    names.update(o for o in sub_action.option_strings if o not in ("-h", "--help"))
    return names


def missing_names(names, usage):
    """Names not written in `usage` as a token of their own."""
    return sorted(n for n in names if not re.search(r"(?<![\w-])%s(?![\w-])" % re.escape(n), usage))


TOOLS = os.path.join(ROOT, "tools")


def missing_tools(development, tools_dir=TOOLS):
    return sorted(
        "tools/" + os.path.basename(p)
        for p in glob.glob(os.path.join(tools_dir, "*.py"))
        if "tools/" + os.path.basename(p) not in development
    )


def diagram_states(markdown):
    """The state ids of the first mermaid stateDiagram block: transition ends and states
    declared with a description (`id : text`); `[*]` is the start, not a state."""
    block = markdown.split("```mermaid\nstateDiagram-v2\n", 1)[1].split("```", 1)[0]
    found = set()
    for line in block.splitlines():
        m = re.match(r"\s*(\S+)\s*-->\s*([^:\s]+)", line)
        if m:
            found.update(m.groups())
            continue
        m = re.match(r"\s*(\w+)\s+:\s", line)
        if m:
            found.add(m.group(1))
    found.discard("[*]")
    return found


class WhatItDoes(unittest.TestCase):
    """AC-1"""

    def test_the_table_follows_the_roles_and_comes_before_the_why(self):
        text = read("README.md")
        at = text.index("**What it does.**")
        self.assertGreater(at, text.index("- **Validator**"))
        self.assertLess(at, text.index("**Why separate sessions.**"))

    def test_every_row_links_a_page_and_every_link_resolves(self):
        rows = capability_rows(read("README.md"))
        self.assertGreaterEqual(len(rows), 10)
        for row in rows:
            self.assertTrue(LINK.search(row), row)
        self.assertEqual(broken_links(rows), [])

    def test_a_wrong_anchor_or_file_is_reported(self):
        rows = [
            "| x | [docs/workflow.md](docs/workflow.md#no-such-heading) |",
            "| y | [docs/nope.md](docs/nope.md) |",
            "| z | [docs/workflow.md](docs/workflow.md#running) |",
        ]
        self.assertEqual(broken_links(rows), ["docs/workflow.md#no-such-heading", "docs/nope.md"])

    def test_a_repeated_heading_is_numbered_as_github_does(self):  # round 1 F2
        found = dict(headings("docs/workflow.md"))
        self.assertEqual(found["workflow"], "# Workflow")
        self.assertEqual(found["workflow-1"], "## Workflow")
        # the acceptance-criteria row lands on the section, not on the page title
        self.assertIn("(docs/workflow.md#workflow-1)", capability_rows(read("README.md"))[0])

    def test_every_heading_link_on_the_linked_pages_resolves(self):  # round 1 follow-up
        # test_doc_links accepts `#heading` on these pages; the heading is checked here
        pages = ["README.md", "CONVENTIONS.md", "CLAUDE.md"] + sorted(
            os.path.relpath(p, ROOT).replace(os.sep, "/")
            for p in glob.glob(os.path.join(ROOT, "docs", "*.md"))
        )
        broken = []
        for page in pages:
            lines = [ln for ln in read(page).split("\n") if "#" in ln and "](" in ln]
            here = os.path.dirname(os.path.join(ROOT, page))
            broken += ["%s: %s" % (page, b) for b in broken_links(lines, here) if "#" in b]
        self.assertEqual(broken, [])

    def test_the_rows_name_the_newer_capabilities(self):
        table = " ".join(capability_rows(read("README.md")))
        for phrase in (
            "release label",
            "`windows`",
            "earlier reviewer tests",
            "GitHub issue",
            "[merge] method",
            "benchmark",
            "audit",
        ):
            self.assertIn(phrase, table, phrase)


class DiagramAndRoleTable(unittest.TestCase):
    """AC-2"""

    def setUp(self):
        text = read("README.md")
        self.diagram = text.split("```mermaid", 1)[1].split("```", 1)[0]
        self.reviewer = next(ln for ln in text.splitlines() if ln.startswith("| Reviewer |"))

    def line(self, needle):
        found = [ln for ln in self.diagram.splitlines() if needle in ln]
        self.assertEqual(len(found), 1, needle)
        return found[0]

    def test_the_reviewer_input(self):
        msg = self.line("R->>B:")
        self.assertIn("related docs", msg)
        self.assertIn("earlier reviewer tests", msg)
        self.assertIn("documents related to the diff", self.reviewer)
        self.assertIn("earlier reviewer tests the change modifies", self.reviewer)

    def test_format_before_lint(self):
        self.assertIn("format and lint", self.line("AC coverage"))
        self.assertIn("`format` line", self.reviewer)

    def test_the_pr_step_and_the_merge_step(self):
        self.assertIn("title and release label", self.line("draft PR"))
        merge = self.line("wait for CI")
        self.assertIn("release label", merge)
        # "squash merge" stays: test_review_followupdocs finds the merge line by it
        self.assertIn("squash merge (or another [merge] method)", merge)


class RunnerTable(unittest.TestCase):
    """AC-3"""

    def test_one_row_per_runner_with_the_recorded_values(self):
        req = h2(read("README.md"), "Requirements")
        rows = {}
        for line in req.splitlines():
            m = re.match(r'\| `runner = "(\w+)"`', line)
            if m:
                rows[m.group(1)] = [c.strip() for c in line.strip("|").split("|")]
        self.assertEqual(sorted(rows), ["local", "ssh", "wsl"])
        for cells in rows.values():
            self.assertEqual(len(cells), 5, cells)
        self.assertEqual(rows["wsl"][3], "`linux`")
        self.assertIn("no isolation", rows["local"][2])
        self.assertIn("`windows` not yet", rows["ssh"][4])
        record = read("docs", "sandbox.md").split("## Verification record", 1)[1]
        for date in ("2026-08-30", "2026-09-04"):
            self.assertIn(date, record)
            self.assertIn(date, rows["wsl"][4] + rows["ssh"][4])
        self.assertIn("has not been run end to end", record)  # the windows-over-ssh cell


class UsageNamesTheCli(unittest.TestCase):
    """AC-4"""

    def test_every_subcommand_and_option_is_in_the_usage_block(self):
        names = cli_names()
        for expected in ("run", "--dry-run", "--foreground", "--branch", "--verbose", "--version"):
            self.assertIn(expected, names)
        usage = code_block(h2(read("README.md"), "Usage"))
        self.assertEqual(missing_names(names, usage), [])

    def test_every_subcommand_line_says_what_it_does(self):
        usage = code_block(h2(read("README.md"), "Usage"))
        lines = [ln for ln in usage.splitlines() if ln.strip()]
        commands = {n for n in cli_names() if not n.startswith("-")}
        for name in commands:
            mine = [ln for ln in lines if re.search(r"revali\.py %s(?![\w-])" % name, ln)]
            self.assertTrue(mine, name)
            self.assertTrue(any("#" in ln for ln in mine), name)

    def test_a_missing_name_is_reported(self):
        usage = "revali.py run --dry-run   # x\nrevali.py rerun\n"
        self.assertEqual(
            missing_names({"run", "--dry-run", "--base", "re"}, usage), ["--base", "re"]
        )


class DevelopmentNamesTheTools(unittest.TestCase):
    """AC-5"""

    def test_every_tool_is_named(self):
        dev = code_block(h2(read("README.md"), "Development"))
        self.assertEqual(missing_tools(dev), [])

    def test_a_missing_tool_is_reported(self):
        self.assertIn("tools/doc_audit.py", missing_tools("python tools/review_bench.py\n"))

    def test_the_documentation_list(self):
        index = h2(read("README.md"), "Documentation")
        self.assertIn("[bench/README.md](bench/README.md)", index)
        flat = " ".join(index.split())
        for phrase in ("release labels", "platforms", "the stages of a run", "release notes"):
            self.assertIn(phrase, flat, phrase)


class StageDiagram(unittest.TestCase):
    """AC-6"""

    def test_the_diagram_names_exactly_the_stages_the_code_defines(self):
        self.assertEqual(diagram_states(read("docs", "workflow.md")), set(STAGES))

    def test_each_end_state_names_its_exit_code(self):
        text = read("docs", "workflow.md")
        block = text.split("```mermaid\nstateDiagram-v2\n", 1)[1].split("```", 1)[0]
        codes = {
            "ready_to_merge": 0,
            "needs_action": 2,
            "needs_human": 3,
            "error": 1,
            "stopped": 1,
            "merged": 0,
            "closed": 0,  # round 1 F1
        }
        # every end state the code defines has its code here, so a new one is not missed
        self.assertEqual(set(codes), set(TERMINAL_STAGES))
        for stage, code in codes.items():
            # the line that leads into the end state, or declares it, carries its code
            line = [
                ln
                for ln in block.splitlines()
                if re.search(r"-->\s*%s\b" % stage, ln) or re.match(r"\s*%s\s+:" % stage, ln)
            ]
            self.assertTrue(line, stage)
            self.assertTrue(all("exit %d" % code in ln for ln in line), stage)

    def test_a_stage_missing_or_extra_shows(self):
        md = "```mermaid\nstateDiagram-v2\n    [*] --> preflight\n    preflight --> zap: x\n```\n"
        self.assertEqual(diagram_states(md), {"preflight", "zap"})


class OtherDocs(unittest.TestCase):
    """AC-7"""

    def test_workflow_states_the_ac_line_format_and_the_tracked_state_refusal(self):
        flat = " ".join(read("docs", "workflow.md").split())
        self.assertIn("`- AC-n: text`", flat)
        self.assertIn("(`- AC-2 (PR #30 F1): text`)", flat)
        self.assertIn("indented continuation lines", flat)
        self.assertIn("stops preflight with exit 1", flat)
        self.assertIn("while git tracks any file under the state directory", flat)

    def test_configuration_has_the_label_table(self):
        text = read("docs", "configuration.md")
        for row in (
            "| `kind: feature` | `minor` | Minor Changes |",
            "| `kind: fix` | `patch` | Patch Changes |",
            "| `kind: docs` | `docs` | Docs |",
        ):
            self.assertIn(row, text)

    def test_releasing_says_the_file_comes_before_the_tag(self):
        flat = " ".join(read("docs", "releasing.md").split())
        self.assertIn("GitHub reads `.github/release.yml` from the tagged commit", flat)
        self.assertIn("ungrouped", flat)


if __name__ == "__main__":
    unittest.main()
