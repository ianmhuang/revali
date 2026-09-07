"""fix/readme-mermaid: the README's sequence diagram parses again (no `;` inside a message,
AC-1, AC-2), the Requirements and Documentation sections say what the code offers (AC-3,
AC-4), and the pages people read on GitHub link to the repository files they name (AC-5),
while the templates, the skill and the checklist stay link-free (AC-6)."""

import glob
import os
import re
import unittest

from tests.helpers import ROOT

PAGES = ["README.md", "CONVENTIONS.md", "CLAUDE.md"] + sorted(
    os.path.relpath(p, ROOT).replace(os.sep, "/")
    for p in glob.glob(os.path.join(ROOT, "docs", "*.md"))
)
# names that mean the reader's own per-project file, never revali's copy
PER_PROJECT = {"revali.toml", "CLAUDE.md", "CONVENTIONS.md"}
# references earlier reviewer tests pin verbatim, surrounding text and all
# (tests/test_review_docssplit.py: the user-config sentence, "1. `defaults.toml`")
PINNED = {
    ("docs/workflow.md", "templates/user-config.toml"),
    ("docs/configuration.md", "defaults.toml"),
}
REF = re.compile(r"`((?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+\.(?:md|toml|json|py)|LICENSE)`")
LINK = re.compile(r"\[[^\]]+\]\(([^)]+)\)")
# docs/plain-links: the link text is the plain path, `[docs/x.md](docs/x.md)`, no backticks
BACKTICKED_LINK = re.compile(r"\[`[^\]]*`\]\(")


def read(rel):
    with open(os.path.join(ROOT, rel), "r", encoding="utf-8", newline="") as fh:
        return fh.read()


def prose_lines(text):
    """(line number, line) outside fenced code blocks."""
    fence = False
    for no, line in enumerate(text.split("\n"), 1):
        if line.strip().startswith("```"):
            fence = not fence
            continue
        if not fence:
            yield no, line


def repo_file(page, ref):
    """The repository path a backticked reference names, or None when it is not one (a
    per-project file, or nothing in the repository); such a file must be written as a link."""
    if ref in PER_PROJECT and "/" not in ref:
        return None
    candidates = [ref]
    if page.startswith("docs/") and "/" not in ref:
        candidates.insert(0, "docs/" + ref)
    for c in candidates:
        if os.path.isfile(os.path.join(ROOT, c)):
            return c
    return None


class Diagram(unittest.TestCase):
    def setUp(self):
        text = read("README.md")
        block = text.split("```mermaid", 1)[1].split("```", 1)[0]
        self.lines = block.split("\n")

    def test_no_semicolon_inside_the_block(self):
        bad = [ln for ln in self.lines if ";" in ln]
        self.assertEqual(bad, [])  # AC-1: `;` ends a Mermaid statement

    def test_diagnosis_line_still_names_the_verdict(self):
        diagnosis = [ln for ln in self.lines if "diagnosis" in ln]
        self.assertEqual(len(diagnosis), 1, diagnosis)
        for word in ("code", "test", "env", "branch", "base"):
            self.assertIn(word, diagnosis[0], word)  # AC-2


class ReadmeSections(unittest.TestCase):
    def setUp(self):
        self.text = read("README.md")

    def section(self, title):
        return self.text.split("\n## %s\n" % title, 1)[1].split("\n## ", 1)[0]

    def test_requirements_name_the_local_runner(self):
        req = " ".join(self.section("Requirements").split())
        self.assertIn('`runner = "local"`', req)  # AC-3
        self.assertIn("worktree", req)
        self.assertIn("no isolation", req)
        self.assertIn("WSL", req)
        self.assertIn('`runner = "ssh"`', req)

    def test_documentation_lists_the_skill_and_the_snippet(self):
        index = self.section("Documentation")
        self.assertIn("[skill/SKILL.md](skill/SKILL.md)", index)  # AC-4
        self.assertIn("`/revali`", index)
        self.assertIn("[templates/CLAUDE-snippet.md](templates/CLAUDE-snippet.md)", index)  # AC-4
        for name in sorted(os.listdir(os.path.join(ROOT, "docs"))):
            self.assertIn("[docs/%s](docs/%s)" % (name, name), index, name)

    def test_readme_is_still_a_front_page(self):
        self.assertLessEqual(len(self.text.splitlines()), 170)


class LinksToRepositoryFiles(unittest.TestCase):
    def test_no_backticked_reference_to_a_repository_file(self):
        # a repository file is named as a plain link, `[docs/x.md](docs/x.md)`; a backticked
        # `docs/x.md` is either a missed link or, when a link, the form AC-1 forbids
        missing = []
        for page in PAGES:
            for no, line in prose_lines(read(page)):
                for m in REF.finditer(line):
                    ref = m.group(1)
                    if (page, ref) in PINNED or repo_file(page, ref) is None:
                        continue
                    missing.append("%s:%d %s" % (page, no, ref))
        self.assertEqual(missing, [])  # AC-5

    def test_every_link_target_exists_and_is_relative(self):
        broken = []
        for page in PAGES:
            here = os.path.dirname(os.path.join(ROOT, page))
            for no, line in prose_lines(read(page)):
                for m in LINK.finditer(line):
                    href = m.group(1)
                    if "://" in href or href.startswith("/"):
                        broken.append("%s:%d not relative: %s" % (page, no, href))
                    elif not os.path.isfile(os.path.join(here, href)):
                        broken.append("%s:%d missing: %s" % (page, no, href))
        self.assertEqual(broken, [])  # AC-5

    def test_link_text_is_the_plain_path(self):
        # docs/plain-links AC-1: no backticks inside the link text, as in bmc-toolkit's README
        for page in PAGES:
            for no, line in prose_lines(read(page)):
                self.assertIsNone(BACKTICKED_LINK.search(line), "%s:%d %s" % (page, no, line))

    def test_pinned_phrases_and_per_project_names_stay_plain(self):
        workflow = " ".join(read("docs/workflow.md").split())  # the sentence wraps
        self.assertIn("(see `templates/user-config.toml`)", workflow)  # AC-5 exception
        self.assertNotIn("[templates/user-config.toml]", workflow)
        configuration = read("docs/configuration.md")
        self.assertIn("1. `defaults.toml`", configuration)  # AC-5 exception
        for page in PAGES:
            self.assertNotIn("[revali.toml]", read(page), page)


class CopiedAndSessionFilesHaveNoLinks(unittest.TestCase):
    def test_templates_skill_and_checklists_are_untouched(self):
        for pattern in ("templates/*.md", "skill/SKILL.md", "checklists/*.md"):
            for path in glob.glob(os.path.join(ROOT, pattern)):
                rel = os.path.relpath(path, ROOT)
                with open(path, "r", encoding="utf-8", newline="") as fh:
                    self.assertIsNone(LINK.search(fh.read()), rel)  # AC-6


if __name__ == "__main__":
    unittest.main()
