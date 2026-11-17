"""fix/docs-section-test-note (M3.11): the related-documents section says in its lead
sentence that names on the changed lines under `test_dir` do not count."""

import unittest

from revali import PROMPT_VERSION
from revali.preflight import preflight
from revali.review import build_prompt
from revali.state import State
from tests.helpers import RepoCase

SECTION = "## Documents related to this change\n\n"


class LeadSentence(RepoCase):
    def lead(self):
        ctx = preflight(self.repo)
        ctx.log = None
        p = build_prompt(ctx, State(), self.rdir(), 1)
        start = p.index(SECTION) + len(SECTION)
        return p[start : p.index("\n", start)], p

    def test_it_names_the_projects_test_dir(self):
        """AC-1"""
        toml = self.read("revali.toml")
        self.write(
            "revali.toml", toml.replace('test_dir = "tests"', 'test_dir = "tests/acceptance"')
        )
        self.commit_all("nested test_dir")
        lead, _ = self.lead()
        self.assertIn("outside `tests/acceptance/`", lead)
        self.assertIn("a link to a file there, or its path, still counts", lead)

    def test_the_rest_is_as_before(self):
        """AC-2"""
        self.write("README.md", "# sample\n\n[usage](docs/usage.md)\n")
        self.write("docs/usage.md", "Set `max_retries` to retry.\n")
        self.write("src/calc.py", self.read("src/calc.py") + "\nmax_retries = 3\n")
        self.commit_all("docs")
        lead, p = self.lead()
        self.assertTrue(
            lead.startswith(
                "Documents reachable by relative links from README.md, CONVENTIONS.md, "
                "CLAUDE.md that link to a file this diff changes, or name in a code span "
                "something it adds or removes"
            ),
            lead,
        )
        self.assertTrue(
            lead.endswith(
                "They are not in the diff unless marked; read each one and compare it with "
                "the new behaviour."
            ),
            lead,
        )
        self.assertIn("- `docs/usage.md` (changed in this diff): mentions `max_retries`", p)
        self.assertIn("Map of the documentation (depth = links from the nearest root):", p)


class Version(unittest.TestCase):
    def test_prompt_version_moved(self):
        """AC-3"""
        self.assertGreaterEqual(int(PROMPT_VERSION), 12)
