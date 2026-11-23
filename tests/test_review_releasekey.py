"""Reviewer acceptance tests for the configuration side of `[project] release_branch`: the
default in defaults.toml, the key in the loaded configuration, and the commented-out line
in templates/revali.toml.
"""

import os
import re
import tomllib
import unittest

from revali.config import ConfigError, load_defaults, parse_project_config
from tests.helpers import ROOT

PROJECT = """[project]
config_version = 1
base_branch = "develop"
%s
[validate.linux]
runner = "local"
new_test = "python -m unittest discover -s tests"
"""


def template_text():
    path = os.path.join(ROOT, "templates", "revali.toml")
    with open(path, "r", encoding="utf-8", newline="") as fh:
        return fh.read()


class TheDefault(unittest.TestCase):
    """AC-1, AC-5: defaults.toml carries the key, empty."""

    def test_defaults_toml_has_the_key_empty(self):
        project = load_defaults()["project"]
        self.assertIn("release_branch", project)
        self.assertEqual(project["release_branch"], "")

    def test_a_project_that_leaves_it_out_gets_the_empty_default(self):
        cfg = parse_project_config(PROJECT % "", user_sections={})
        self.assertEqual(cfg.project.release_branch, "")
        self.assertEqual(cfg.project.base_branch, "develop")


class TheKey(unittest.TestCase):
    """AC-1: a `[project]` key of revali.toml, a string."""

    def test_a_project_sets_it(self):
        cfg = parse_project_config(PROJECT % 'release_branch = "main"\n', user_sections={})
        self.assertEqual(cfg.project.release_branch, "main")
        self.assertEqual(cfg.project.base_branch, "develop")

    def test_a_value_that_is_not_a_string_is_a_configuration_error(self):
        for value in ("true", '["main"]', "7"):
            with self.subTest(value=value):
                with self.assertRaises(ConfigError) as caught:
                    parse_project_config(PROJECT % ("release_branch = %s\n" % value))
                problems = [p for p in caught.exception.problems if "release_branch" in p]
                self.assertEqual(len(problems), 1, caught.exception.problems)
                self.assertNotIn("unknown key", problems[0])


class TheTemplate(unittest.TestCase):
    """AC-5: templates/revali.toml carries the key commented out."""

    def test_the_key_is_there_as_a_comment_only(self):
        text = template_text().replace("\r\n", "\n")
        project = tomllib.loads(text)["project"]
        self.assertNotIn("release_branch", project)  # commented out: the default applies
        commented = re.findall(r"(?m)^#[ \t]*(release_branch[ \t]*=.*)$", text)
        self.assertEqual(len(commented), 1, commented)
        # the line is a valid setting once the `#` is taken away
        self.assertIsInstance(tomllib.loads(commented[0])["release_branch"], str)

    def test_the_template_with_the_key_uncommented_still_parses(self):
        text = template_text().replace("\r\n", "\n")
        uncommented = re.sub(r"(?m)^#[ \t]*(release_branch[ \t]*=)", r"\1", text, count=1)
        self.assertNotEqual(uncommented, text)
        project = tomllib.loads(uncommented)["project"]
        self.assertIsInstance(project["release_branch"], str)


if __name__ == "__main__":
    unittest.main()
