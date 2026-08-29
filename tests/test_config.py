import os
import unittest

from tests.helpers import ROOT
from revali.config import ConfigError, parse_project_config


def template_text():
    with open(os.path.join(ROOT, "templates", "revali.toml"), "r", encoding="utf-8") as fh:
        return fh.read()


MINIMAL = """
[project]
config_version = 1
[validate.linux]
new_test = "pytest tests"
"""


class ConfigTests(unittest.TestCase):
    def test_template_parses(self):
        cfg = parse_project_config(template_text())
        self.assertEqual(cfg.project.base_branch, "main")
        self.assertEqual(cfg.validate.platforms["linux"].runner, "wsl")
        self.assertEqual(cfg.review.max_fixes, 2)
        self.assertFalse(cfg.merge.auto_merge)

    def test_minimal_uses_defaults(self):
        cfg = parse_project_config(MINIMAL)
        self.assertEqual(cfg.project.test_dir, "tests")
        self.assertEqual(cfg.review.model, "fable")
        self.assertEqual(cfg.validate.platforms["linux"].new_test, "pytest tests")

    def test_unknown_key_is_error(self):
        with self.assertRaises(ConfigError) as cm:
            parse_project_config(MINIMAL + "\n[review]\nmodle = 'x'\n")
        self.assertTrue(any("unknown key 'modle'" in p for p in cm.exception.problems))

    def test_unknown_section_is_error(self):
        with self.assertRaises(ConfigError) as cm:
            parse_project_config(MINIMAL + "\n[deploy]\nx = 1\n")
        self.assertTrue(any("unknown section [deploy]" in p for p in cm.exception.problems))

    def test_windows_platform_rejected_in_v1(self):
        text = MINIMAL.replace("[validate.linux]", '[project]\nplatforms = ["linux", "windows"]\n[validate.linux]')
        text = text.replace("[project]\nconfig_version = 1\n[project]", "[project]\nconfig_version = 1")
        with self.assertRaises(ConfigError) as cm:
            parse_project_config(text)
        self.assertTrue(any("windows" in p and "not supported" in p for p in cm.exception.problems))

    def test_new_test_required(self):
        with self.assertRaises(ConfigError) as cm:
            parse_project_config("[project]\nconfig_version = 1\n[validate.linux]\ntest = 'pytest'\n")
        self.assertTrue(any("new_test is required" in p for p in cm.exception.problems))

    def test_missing_platform_table(self):
        with self.assertRaises(ConfigError) as cm:
            parse_project_config("[project]\nconfig_version = 1\n")
        self.assertTrue(any("missing [validate.linux]" in p for p in cm.exception.problems))

    def test_auto_merge_forced_off_with_warning(self):
        cfg = parse_project_config(MINIMAL + "\n[merge]\nauto_merge = true\n")
        self.assertFalse(cfg.merge.auto_merge)
        self.assertTrue(any("auto_merge" in w for w in cfg.warnings))

    def test_wrong_types(self):
        with self.assertRaises(ConfigError) as cm:
            parse_project_config(MINIMAL + "\n[review]\nmax_fixes = 'two'\nexclude = 'x'\n")
        problems = " ".join(cm.exception.problems)
        self.assertIn("max_fixes must be an integer", problems)
        self.assertIn("exclude must be a list", problems)

    def test_bad_toml(self):
        with self.assertRaises(ConfigError) as cm:
            parse_project_config("[project\n")
        self.assertIn("not valid TOML", cm.exception.problems[0])

    def test_config_version_mismatch(self):
        with self.assertRaises(ConfigError) as cm:
            parse_project_config(MINIMAL.replace("config_version = 1", "config_version = 9"))
        self.assertTrue(any("config_version" in p for p in cm.exception.problems))

    def test_engine_and_change_source_v1_only(self):
        with self.assertRaises(ConfigError) as cm:
            parse_project_config(MINIMAL + "\n[review]\nengine = 'hybrid'\n")
        self.assertTrue(any("engine" in p for p in cm.exception.problems))
        with self.assertRaises(ConfigError) as cm:
            parse_project_config(MINIMAL.replace("config_version = 1", "config_version = 1\nchange_source = 'openspec'"))
        self.assertTrue(any("change_source" in p for p in cm.exception.problems))

    def test_all_problems_reported_together(self):
        with self.assertRaises(ConfigError) as cm:
            parse_project_config("[project]\nconfig_version = 1\nfoo = 1\n[review]\nbar = 2\n")
        self.assertGreaterEqual(len(cm.exception.problems), 3)


if __name__ == "__main__":
    unittest.main()
