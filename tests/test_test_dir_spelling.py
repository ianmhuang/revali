"""fix/test-dir-spelling (M3.10): `[project] test_dir` is read once as a plain path inside the
repository, so every `under_test_dir` caller agrees for every spelling."""

import os
import unittest

from revali.config import ConfigError, parse_project_config
from revali.preflight import preflight
from revali.review import build_prompt
from revali.state import State
from revali.validate import baseline_reusable
from tests.fixtures.make_sample_repo import toml_str
from tests.helpers import ROOT, RepoCase, git

MINIMAL = """
[project]
config_version = 1
test_dir = %s
[validate.linux]
new_test = "pytest tests"
"""


def load(value):
    return parse_project_config(MINIMAL % toml_str(value))


class Spellings(unittest.TestCase):
    """AC-1"""

    def test_each_spelling_reads_as_the_plain_path(self):
        for value, plain in (
            ("tests", "tests"),
            ("./tests", "tests"),
            ("tests/", "tests"),
            ("tests\\", "tests"),
            ("./tests/", "tests"),
            ("tests//", "tests"),
            ("./tests/unit", "tests/unit"),
            ("tests\\unit", "tests/unit"),
            ("site/./tests", "site/tests"),
        ):
            self.assertEqual(load(value).project.test_dir, plain, value)


class Refused(unittest.TestCase):
    """AC-3"""

    def test_values_that_name_no_directory_inside_the_repository(self):
        for value in (
            "",
            " ",
            ".",
            "./",
            "/t",
            "\\t",
            "C:\\t",
            "C:t",
            "../t",
            "tests/../x",
            "tests\\..\\x",
            "tests ",  # PR #84 round 1 F3: a blank around a part would match no path
            " tests",
            "tests /unit",
            "tests\t",
        ):
            with self.assertRaises(ConfigError, msg=value) as cm:
                load(value)
            found = [p for p in cm.exception.problems if "project.test_dir" in p]
            self.assertTrue(found, (value, cm.exception.problems))
            self.assertIn(repr(value), found[0])  # the value as given


class Pipeline(RepoCase):
    """AC-2: the fixture's revali.toml says test_dir = "tests"; here it says "./tests"."""

    def setUp(self):
        super().setUp()
        toml = self.read("revali.toml")
        self.assertIn('test_dir = "tests"', toml)
        self.write("revali.toml", toml.replace('test_dir = "tests"', 'test_dir = "./tests"'))
        self.commit_all("dot spelling")

    def test_the_related_documents_leave_out_test_lines(self):
        git(["checkout", "-q", "main"], self.repo)
        self.write("README.md", "[usage](docs/usage.md)\n")
        self.write("docs/usage.md", "Call `build_prompt` here.\n")
        self.commit_all("docs on the base")
        git(["push", "-q", "origin", "main"], self.repo)
        git(["checkout", "-q", "feature/mul"], self.repo)
        git(["merge", "-q", "--no-edit", "main"], self.repo)
        self.write("tests/test_x.py", "build_prompt(ctx)\n")
        self.commit_all("test only")
        ctx = preflight(self.repo)
        ctx.log = None
        self.assertNotIn("mentions `build_prompt`", build_prompt(ctx, State(), self.rdir(), 1))
        self.assertEqual(ctx.cfg.project.test_dir, "tests")

    def test_validation_reuses_the_baseline_after_a_reviewer_commit(self):
        st = State()
        st.baseline_sha = git(["rev-parse", "HEAD"], self.repo).strip()
        self.write("tests/test_review_x.py", "# reviewer file\n")
        git(["add", "--", "tests/test_review_x.py"], self.repo)
        git(["commit", "-q", "-m", "test: x\n\nRevali-Round: 1\n"], self.repo)
        ctx = preflight(self.repo)
        ctx.log = None
        self.assertEqual(baseline_reusable(ctx, st), st.baseline_sha)


class Documented(unittest.TestCase):
    """AC-4"""

    def test_defaults_and_template_say_what_test_dir_accepts(self):
        for name in ("defaults.toml", os.path.join("templates", "revali.toml")):
            with open(os.path.join(ROOT, name), encoding="utf-8") as fh:
                text = fh.read()
            start = text.index("\ntest_dir =")
            note = " ".join(text[start : text.index("\ntest_file_pattern", start)].split())
            self.assertIn("inside the repository", note, name)
            self.assertIn("./tests", note, name)
