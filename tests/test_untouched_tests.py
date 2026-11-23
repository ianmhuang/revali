"""feature/review-untouched-tests (M4.6): the review prompt asks for existing tests the diff
leaves untouched that assert what it changes, and the manifest line says it lists the
manifests in the diff shown."""

import unittest

from revali import PROMPT_VERSION
from revali.preflight import preflight
from revali.review import build_prompt
from revali.state import State
from tests.helpers import RepoCase

UNTOUCHED = "Then look for existing tests the diff does not modify that exercise what it changes:"
MANIFEST_LINE = "Dependency manifests in the diff above (a file omitted from it is not listed): "


def flat(text):
    return " ".join(text.split())


class Prompt(RepoCase):
    def prompt(self, round_no=1):
        ctx = preflight(self.repo)
        ctx.log = None
        return build_prompt(ctx, State(), self.rdir(), round_no)

    def test_the_untouched_tests_instruction_names_the_test_dir(self):
        """AC-1"""
        toml = self.read("revali.toml")
        self.write(
            "revali.toml", toml.replace('test_dir = "tests"', 'test_dir = "tests/acceptance"')
        )
        self.commit_all("nested test_dir")
        p = flat(self.prompt())
        self.assertIn(UNTOUCHED, p)
        self.assertIn(
            "search `tests/acceptance/` for the functions, fields, messages, defaults, order "
            "or timing the diff alters",
            p,
        )
        self.assertIn(
            "A test that now fails, or still passes for a reason that no longer holds, is a "
            "`correctness` finding naming the test and the assertion.",
            p,
        )
        self.assertNotIn("$test_dir", p)

    def test_the_instruction_follows_the_modified_tests_step(self):
        """AC-1: it extends step 5, so the steps keep their numbers"""
        p = flat(self.prompt())
        step5 = p.index("5. For every existing test file the diff modifies")
        step6 = p.index("6. For every dependency manifest touched")
        self.assertTrue(step5 < p.index(UNTOUCHED) < step6)

    def test_a_manifest_in_the_diff_is_listed(self):
        """AC-2"""
        self.write("requirements.txt", "pytest\nrequests\n")
        self.commit_all("a dependency")
        p = self.prompt()
        self.assertIn(MANIFEST_LINE + "requirements.txt\n", p)
        self.assertNotIn("Dependency manifests this diff touches", p)

    def test_an_excluded_manifest_is_not_listed(self):
        """AC-2: the list itself is unchanged, and the line now says why it is empty"""
        toml = self.read("revali.toml")
        self.assertIn('exclude = ["*.lock"]\n', toml)
        self.write(
            "revali.toml",
            toml.replace('exclude = ["*.lock"]\n', 'exclude = ["*.lock", "requirements.txt"]\n'),
        )
        self.write("requirements.txt", "pytest\nrequests\n")
        self.commit_all("an excluded dependency")
        p = self.prompt()
        self.assertIn(MANIFEST_LINE + "(none)\n", p)
        self.assertIn("Files changed but omitted from the diff above: requirements.txt", p)


class Version(unittest.TestCase):
    def test_prompt_version_moved(self):
        """AC-3"""
        self.assertGreaterEqual(int(PROMPT_VERSION), 18)


if __name__ == "__main__":
    unittest.main()
