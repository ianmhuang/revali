"""defaults.toml < user config < project config, and the engine / paths checks."""
import os
import tempfile
import unittest

from tests.helpers import ROOT, rmtree_force
from revali.config import (ConfigError, load_defaults, load_user_config, merge_layers,
                           parse_project_config, paths_for)

MINIMAL = """
[project]
config_version = 1
[validate.linux]
new_test = "pytest tests"
"""


class DefaultsFileTests(unittest.TestCase):
    def test_defaults_cover_every_field(self):
        d = load_defaults()
        cfg = parse_project_config(MINIMAL, defaults=d)
        for section in ("project", "review", "validate", "merge", "paths"):
            obj = getattr(cfg, section)
            for name in obj.__dataclass_fields__:
                if name in ("platforms", "config_version"):
                    continue
                self.assertIn(name, d[section], "%s.%s has no default" % (section, name))
        plat = cfg.validate.platforms["linux"]
        for name in plat.__dataclass_fields__:
            if name != "name":
                self.assertIn(name, d["validate"]["platform"], "validate.platform.%s has no default" % name)
        self.assertEqual(list(d["engines"]), ["claude"])
        self.assertEqual(plat.command_timeout_min, 15)
        self.assertEqual(cfg.merge.checks_timeout_min, 30)
        self.assertEqual(cfg.engines["claude"].tiers[-1], "fable")

    def test_no_literal_defaults_in_code(self):
        with open(os.path.join(ROOT, "revali", "config.py"), "r", encoding="utf-8") as fh:
            text = fh.read()
        # user_home() may name ~/.revali: that is where the user layer itself lives, not a default
        for literal in ('"fable"', '"opus"', '"sonnet"', '= 2.0', '= 800', '= 20\n', '= "squash"', '"logs"'):
            self.assertNotIn(literal, text, literal)


class LayeringTests(unittest.TestCase):
    def test_user_overrides_default_project_overrides_user(self):
        user = {"review": {"model": "opus", "budget_usd": 0.5}, "merge": {"method": "rebase"},
                "validate": {"linux": {"distro": "Ubuntu-24.04"}}}
        cfg = parse_project_config(MINIMAL, user_sections=user)
        self.assertEqual(cfg.review.model, "opus")
        self.assertEqual(cfg.review.budget_usd, 0.5)
        self.assertEqual(cfg.merge.method, "rebase")
        self.assertEqual(cfg.validate.platforms["linux"].distro, "Ubuntu-24.04")
        self.assertEqual(cfg.validate.platforms["linux"].runner, "wsl")  # still the tool default
        project = MINIMAL.replace('new_test = "pytest tests"', 'new_test = "pytest tests"\ndistro = "Debian"')
        cfg = parse_project_config(project + '\n[review]\nmodel = "sonnet"\n', user_sections=user)
        self.assertEqual(cfg.review.model, "sonnet")
        self.assertEqual(cfg.review.budget_usd, 0.5)
        self.assertEqual(cfg.validate.platforms["linux"].distro, "Debian")

    def test_engine_table_layers(self):
        user = {"engines": {"claude": {"tiers": ["sonnet", "opus"]}}}
        cfg = parse_project_config(MINIMAL, user_sections=user)
        self.assertEqual(cfg.engines["claude"].tiers, ["sonnet", "opus"])
        self.assertEqual(cfg.engines["claude"].helper_prefix, "claude-haiku")  # kept from defaults

    def test_unknown_key_in_user_layer(self):
        with self.assertRaises(ConfigError) as cm:
            parse_project_config(MINIMAL, user_sections={"review": {"modle": "x"}})
        self.assertTrue(any("user config: review: unknown key 'modle'" in p for p in cm.exception.problems))

    def test_merge_layers_platform_defaults(self):
        merged = merge_layers({"validate": {"platform": {"runner": "wsl", "distro": "U"}}},
                              {"validate": {"linux": {"distro": "V"}}},
                              {"validate": {"linux": {"setup": "s"}, "other": {}}})
        self.assertEqual(merged["_platforms"]["linux"], {"runner": "wsl", "distro": "V", "setup": "s"})
        self.assertEqual(merged["_platforms"]["other"], {"runner": "wsl", "distro": "U"})
        later = merge_layers({"validate": {"platform": {"runner": "wsl", "distro": "U"}}},
                             {"validate": {"linux": {"setup": "s"}}},
                             {"validate": {"platform": {"distro": "W"}}})
        self.assertEqual(later["_platforms"]["linux"], {"runner": "wsl", "distro": "W", "setup": "s"})


class EngineAndPathChecks(unittest.TestCase):
    def test_old_engine_value_gets_migration_message(self):
        with self.assertRaises(ConfigError) as cm:
            parse_project_config(MINIMAL + '\n[review]\nengine = "prompt"\n')
        self.assertTrue(any("now review.strategy" in p and 'engine = "claude"' in p for p in cm.exception.problems))

    def test_unknown_engine_lists_available(self):
        with self.assertRaises(ConfigError) as cm:
            parse_project_config(MINIMAL + '\n[validate]\nengine = "codex"\n')
        self.assertTrue(any("validate.engine 'codex' is unknown (available: claude)" in p
                            for p in cm.exception.problems))

    def test_retired_engine_value_only_for_review(self):
        with self.assertRaises(ConfigError) as cm:
            parse_project_config(MINIMAL + '\n[validate]\nengine = "prompt"\n')
        self.assertTrue(any("validate.engine 'prompt' is unknown" in p for p in cm.exception.problems))
        self.assertFalse(any("strategy" in p for p in cm.exception.problems))

    def test_type_error_is_reported_once(self):
        with self.assertRaises(ConfigError) as cm:
            parse_project_config(MINIMAL + '\n[merge]\nmethod = 5\n')
        self.assertEqual(cm.exception.problems, ["revali.toml: merge.method must be a string"])

    def test_platform_defaults_from_a_later_layer(self):
        user = {"validate": {"linux": {"distro": "V"}}}
        cfg = parse_project_config(MINIMAL + '\n[validate.platform]\ncommand_timeout_min = 42\n',
                                   user_sections=user)
        self.assertEqual(cfg.validate.platforms["linux"].command_timeout_min, 42)
        self.assertEqual(cfg.validate.platforms["linux"].distro, "V")

    def test_history_file_from_defaults(self):
        from revali.config import history_path
        self.assertTrue(history_path().endswith("history.jsonl"))

    def test_empty_tiers_rejected(self):
        with self.assertRaises(ConfigError) as cm:
            parse_project_config(MINIMAL + '\n[engines.claude]\ntiers = []\n')
        self.assertTrue(any("tiers must list" in p for p in cm.exception.problems))

    def test_state_dir_single_component(self):
        for bad in ("a/b", "", ".."):
            with self.assertRaises(ConfigError) as cm:
                parse_project_config(MINIMAL + '\n[paths]\nstate_dir = "%s"\n' % bad)
            self.assertTrue(any("state_dir" in p for p in cm.exception.problems), bad)
        cfg = parse_project_config(MINIMAL + '\n[paths]\nstate_dir = ".rv"\nlogs_dir = "out"\n')
        self.assertEqual((cfg.paths.state_dir, cfg.paths.logs_dir), (".rv", "out"))

    def test_prompt_override_must_exist(self):
        tmp = tempfile.mkdtemp(prefix="revali cfg ")
        self.addCleanup(rmtree_force, tmp)
        with self.assertRaises(ConfigError) as cm:
            parse_project_config(MINIMAL + '\n[review]\nprompt = "docs/p.md"\n', repo_root=tmp)
        self.assertTrue(any("review.prompt: file not found" in p for p in cm.exception.problems))
        os.makedirs(os.path.join(tmp, "docs"))
        with open(os.path.join(tmp, "docs", "p.md"), "w", encoding="utf-8") as fh:
            fh.write("x")
        cfg = parse_project_config(MINIMAL + '\n[review]\nprompt = "docs/p.md"\n', repo_root=tmp)
        self.assertEqual(cfg.review.prompt, "docs/p.md")


class UserFileTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="revali home ")
        self.addCleanup(rmtree_force, self.home)
        self._backup = os.environ.get("REVALI_HOME")
        os.environ["REVALI_HOME"] = self.home

    def tearDown(self):
        if self._backup is None:
            os.environ.pop("REVALI_HOME", None)
        else:
            os.environ["REVALI_HOME"] = self._backup

    def write(self, text):
        with open(os.path.join(self.home, "config.toml"), "w", encoding="utf-8") as fh:
            fh.write(text)

    def test_sections_and_top_keys(self):
        self.write('checklist = "mine.md"\n[review]\nmodel = "opus"\n[validate.linux]\ndistro = "D"\n')
        u = load_user_config()
        self.assertEqual(u.checklist, os.path.join(self.home, "mine.md"))
        self.assertEqual(u.sections["review"]["model"], "opus")
        self.assertEqual(u.sections["validate"]["linux"]["distro"], "D")

    def test_retired_keys(self):
        self.write('review_model = "opus"\n')
        with self.assertRaises(ConfigError) as cm:
            load_user_config()
        self.assertIn("moved to [review] model", cm.exception.problems[0])

    def test_paths_for_without_project_config(self):
        self.assertEqual(paths_for(self.home).state_dir, ".revali")


if __name__ == "__main__":
    unittest.main()
