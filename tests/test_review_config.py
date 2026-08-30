"""AC-5 / AC-6 / AC-7: no literal paths or model names left in revali/, the PR #4
follow-ups in config.py, and the README additions."""
import json
import os
import unittest

from tests.helpers import ROOT, RepoCase, claude_entry, run_cli
from revali import EXIT_OK
from revali.config import (ConfigError, UserConfig, history_path, load_defaults, parse_project_config,
                           user_home)

MINIMAL = """
[project]
config_version = 1
[validate.linux]
new_test = "pytest tests"
"""


def package_sources():
    pkg = os.path.join(ROOT, "revali")
    out = {}
    for name in sorted(os.listdir(pkg)):
        if name.endswith(".py"):
            with open(os.path.join(pkg, name), "r", encoding="utf-8") as fh:
                out[name] = fh.read()
    return out


class NoLiteralsInCode(unittest.TestCase):
    def test_paths_and_file_names_come_from_the_config(self):
        for name, text in package_sources().items():
            if name != "config.py":   # user_home() is the location of the user layer itself
                self.assertNotIn('".revali"', text, name)
                self.assertNotIn("'.revali'", text, name)
            self.assertNotIn(".revali/sandbox", text, name)
            self.assertNotIn('"history.jsonl"', text, name)
            self.assertNotIn("'history.jsonl'", text, name)
            self.assertNotIn('"logs"', text, name)
            self.assertNotIn("'logs'", text, name)
            for const in ("REVIEW_DIR =", "PROMPT_PATH =", "SCHEMA_PATH =", "BUILTIN_CHECKLIST =",
                          "SANDBOX_ROOT =", 'os.path.join(TOOL_ROOT, "prompts"', 'os.path.join(TOOL_ROOT, "schemas"'):
                self.assertNotIn(const, text, "%s still has %s" % (name, const))

    def test_no_model_names_in_the_stages(self):
        # claude.py keeps its family list until the engine interface change (out of scope)
        for name, text in package_sources().items():
            if name == "claude.py":
                continue
            for literal in ('"fable"', '"opus"', '"sonnet"', "'fable'", "'opus'", "'sonnet'"):
                self.assertNotIn(literal, text, "%s: %s" % (name, literal))


class ConfigFollowUps(unittest.TestCase):
    def test_retired_engine_message_is_for_review_only(self):
        with self.assertRaises(ConfigError) as cm:
            parse_project_config(MINIMAL + '\n[validate]\nengine = "prompt"\n')
        self.assertTrue(any("validate.engine 'prompt' is unknown" in p for p in cm.exception.problems),
                        cm.exception.problems)
        self.assertFalse(any("strategy" in p for p in cm.exception.problems), cm.exception.problems)
        with self.assertRaises(ConfigError) as cm:
            parse_project_config(MINIMAL + '\n[review]\nengine = "hybrid"\n')
        self.assertTrue(any("review.engine 'hybrid' is now review.strategy" in p for p in cm.exception.problems),
                        cm.exception.problems)

    def test_wrong_type_is_reported_once(self):
        with self.assertRaises(ConfigError) as cm:
            parse_project_config(MINIMAL + '\n[merge]\nmethod = 5\n[review]\nmax_fixes = "two"\n')
        self.assertCountEqual(cm.exception.problems,
                              ["revali.toml: merge.method must be a string",
                               "revali.toml: review.max_fixes must be an integer"])
        with self.assertRaises(ConfigError) as cm:
            parse_project_config(MINIMAL.replace('new_test = "pytest tests"',
                                                 'new_test = "pytest tests"\ncommand_timeout_min = "x"'))
        self.assertEqual(cm.exception.problems, ["revali.toml: validate.linux.command_timeout_min must be an integer"])

    def test_later_layer_platform_defaults_reach_an_earlier_platform(self):
        user = {"validate": {"linux": {"distro": "V"}}}
        cfg = parse_project_config(MINIMAL + '\n[validate.platform]\ncommand_timeout_min = 42\n',
                                   user_sections=user)
        self.assertEqual(cfg.validate.platforms["linux"].command_timeout_min, 42)
        self.assertEqual(cfg.validate.platforms["linux"].distro, "V")
        # and the user layer's [validate.platform] reaches a platform the project names
        user = {"validate": {"platform": {"distro": "FromUser"}}}
        cfg = parse_project_config(MINIMAL, user_sections=user)
        self.assertEqual(cfg.validate.platforms["linux"].distro, "FromUser")

    def test_history_file_name_is_a_default(self):
        name = load_defaults()["paths"]["history_file"]
        self.assertTrue(name.endswith(".jsonl"), name)
        self.assertEqual(os.path.basename(history_path()), name)
        with self.assertRaises(ConfigError) as cm:
            parse_project_config(MINIMAL + '\n[paths]\nhistory_file = "a/b.jsonl"\n')
        self.assertTrue(any("paths.history_file" in p for p in cm.exception.problems), cm.exception.problems)

    def test_history_file_is_honoured_from_the_user_layer_only(self):
        # AC-6 (round-1 F2): the key is wired for the user file and refused in the project file
        user = UserConfig(sections={"paths": {"history_file": "runs.jsonl"}})
        self.assertEqual(os.path.basename(history_path(user)), "runs.jsonl")
        self.assertEqual(os.path.dirname(history_path(user)), user_home())
        # an explicit history_path still wins over the file name
        both = UserConfig(history_path=os.path.join("elsewhere", "h.jsonl"),
                          sections={"paths": {"history_file": "runs.jsonl"}})
        self.assertEqual(history_path(both), os.path.join("elsewhere", "h.jsonl"))
        # the user layer's value validates like the other [paths] keys
        parse_project_config(MINIMAL, user_sections={"paths": {"history_file": "runs.jsonl"}})
        with self.assertRaises(ConfigError) as cm:
            parse_project_config(MINIMAL, user_sections={"paths": {"history_file": "a/b.jsonl"}})
        self.assertTrue(any("paths.history_file must be a single file name" in p for p in cm.exception.problems),
                        cm.exception.problems)
        # the project file may not set it, even to a valid name
        with self.assertRaises(ConfigError) as cm:
            parse_project_config(MINIMAL + '\n[paths]\nhistory_file = "runs.jsonl"\n')
        self.assertTrue(any("paths.history_file is a user-level key" in p for p in cm.exception.problems),
                        cm.exception.problems)


class HistoryFileEndToEnd(RepoCase):
    def test_run_records_history_under_the_configured_name(self):
        with open(os.path.join(self.home, "config.toml"), "w", encoding="utf-8") as fh:
            fh.write('[paths]\nhistory_file = "runs.jsonl"\n')
        self.claude(claude_entry())
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertTrue(os.path.isfile(os.path.join(self.home, "runs.jsonl")))
        self.assertFalse(os.path.exists(os.path.join(self.home, "history.jsonl")))
        with open(os.path.join(self.home, "runs.jsonl"), "r", encoding="utf-8") as fh:
            record = json.loads(fh.readline())
        self.assertEqual(record["branch"], "feature/mul")
        self.assertEqual(record["stage"], "ready_to_merge")
        code, out = run_cli(["stats"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("history: " + os.path.join(self.home, "runs.jsonl"), out)
        self.assertIn("history rows: 1", out)

    def test_documented_keys_exist(self):
        d = load_defaults()
        self.assertIn("sandbox_dir", d["validate"]["platform"])
        for key in ("prompt", "schema", "checklist_builtin"):
            self.assertIn(key, d["review"])
        for key in ("prompt", "schema"):
            self.assertIn(key, d["validate"])
        for key in ("state_dir", "logs_dir", "history_file"):
            self.assertIn(key, d["paths"])


class ReadmeAdditions(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(ROOT, "README.md"), "r", encoding="utf-8") as fh:
            self.readme = fh.read()

    def test_files_section(self):
        self.assertIn("\n## Files\n", self.readme)
        files = self.readme.split("\n## Files\n", 1)[1].split("\n## ", 1)[0]
        self.assertIn("| Document | Written by | Read by | Default location | Config key |", files)
        for token in ("`change.md`", "`tests.md`", "`diagnose-n.json`", "[paths] state_dir", "[paths] logs_dir",
                      "sandbox_dir", "history_file", "checklist_builtin", "[review] prompt", "[validate] prompt",
                      "test_guide", "test_file_pattern"):
            self.assertIn(token, files, token)

    def test_model_paragraph_in_configuration(self):
        conf = self.readme.split("\n## Configuration\n", 1)[1].split("\n## ", 1)[0]
        self.assertIn('`model = "auto"`', conf)
        self.assertIn("one tier above", conf)
        self.assertIn("one tier below", conf)
        self.assertIn('`fallback_model = "auto"`', conf)
        self.assertIn("author_model", conf)


if __name__ == "__main__":
    unittest.main()
