"""Acceptance tests for the three-layer configuration (defaults.toml < user < project).

Black-box against revali.config's public loaders. Every test fails on the base
branch, where parse_project_config has no user layer, no [paths], no engines,
and load_user_config has no `sections`.
"""
import os
import tempfile
import tomllib
import unittest

from tests.helpers import ROOT, rmtree_force
from revali.config import (ConfigError, EngineCfg, MergeCfg, PathsCfg, PlatformCfg, ProjectCfg,
                           ReviewCfg, ValidateCfg, load_defaults, load_project_config,
                           load_user_config, parse_project_config)

MINIMAL = """
[project]
config_version = 1
[validate.linux]
new_test = "pytest tests"
"""


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def _problems(fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except ConfigError as exc:
        return exc.problems
    raise AssertionError("ConfigError expected")


class IsolatedHome(unittest.TestCase):
    """A private REVALI_HOME and a temp repo root per test."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="revali review cfg ")
        self.addCleanup(rmtree_force, self.tmp)
        self.home = os.path.join(self.tmp, "home")
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(self.home)
        os.makedirs(self.repo)
        self._backup = os.environ.get("REVALI_HOME")
        os.environ["REVALI_HOME"] = self.home
        self.addCleanup(self._restore)

    def _restore(self):
        if self._backup is None:
            os.environ.pop("REVALI_HOME", None)
        else:
            os.environ["REVALI_HOME"] = self._backup

    def user_file(self, text):
        _write(os.path.join(self.home, "config.toml"), text)

    def project_file(self, text):
        _write(os.path.join(self.repo, "revali.toml"), text)


class AC1DefaultsFile(unittest.TestCase):
    def test_every_dataclass_field_has_a_default(self):
        d = load_defaults()
        for section, dc in (("project", ProjectCfg), ("review", ReviewCfg), ("validate", ValidateCfg),
                            ("merge", MergeCfg), ("paths", PathsCfg)):
            for name in dc.__dataclass_fields__:
                if name in ("platforms", "config_version"):
                    continue
                self.assertIn(name, d[section], "%s.%s missing from defaults.toml" % (section, name))
        for name in PlatformCfg.__dataclass_fields__:
            if name != "name":
                self.assertIn(name, d["validate"]["platform"], "validate.platform.%s" % name)
        self.assertTrue(d["engines"], "defaults.toml must ship at least one [engines.<name>]")
        for ename, table in d["engines"].items():
            for name in EngineCfg.__dataclass_fields__:
                if name != "name":
                    self.assertIn(name, table, "engines.%s.%s" % (ename, name))

    def test_values_come_from_the_file_not_from_code(self):
        # Change a model, a budget, a timeout and a file name in a copy of the
        # defaults; the parsed config must follow the copy.
        with open(os.path.join(ROOT, "defaults.toml"), "r", encoding="utf-8") as fh:
            text = fh.read()
        altered = tomllib.loads(text)
        altered["review"]["model"] = "zeta"
        altered["review"]["budget_usd"] = 9.5
        altered["review"]["timeout_min"] = 77
        altered["review"]["checklist"] = "RULES.md"
        altered["validate"]["platform"]["command_timeout_min"] = 41
        altered["merge"]["method"] = "rebase"
        altered["paths"]["state_dir"] = ".rv"
        cfg = parse_project_config(MINIMAL, defaults=altered)
        self.assertEqual(cfg.review.model, "zeta")
        self.assertEqual(cfg.review.budget_usd, 9.5)
        self.assertEqual(cfg.review.timeout_min, 77)
        self.assertEqual(cfg.review.checklist, "RULES.md")
        self.assertEqual(cfg.validate.platforms["linux"].command_timeout_min, 41)
        self.assertEqual(cfg.merge.method, "rebase")
        self.assertEqual(cfg.paths.state_dir, ".rv")

    def test_config_module_has_no_literal_defaults(self):
        with open(os.path.join(ROOT, "revali", "config.py"), "r", encoding="utf-8") as fh:
            source = fh.read()
        for literal in ('"fable"', '"opus"', '"sonnet"', '"Ubuntu"', '"CONVENTIONS.md"',
                        "= 2.0", "= 1.0", "= 800", "= 15", "= 30"):
            self.assertNotIn(literal, source, "literal default %s in config.py" % literal)

    def test_defaults_file_itself_is_checked(self):
        tmp = tempfile.mkdtemp(prefix="revali review defaults ")
        self.addCleanup(rmtree_force, tmp)
        bad = os.path.join(tmp, "defaults.toml")
        _write(bad, '[review]\nmodle = "x"\n[nonsense]\na = 1\n')
        problems = _problems(load_defaults, bad)
        joined = "\n".join(problems)
        self.assertIn("defaults.toml", joined)
        self.assertIn("modle", joined)
        self.assertIn("nonsense", joined)


class AC2Precedence(IsolatedHome):
    def test_user_beats_defaults_project_beats_user(self):
        user = {"review": {"model": "opus", "max_fixes": 5}, "validate": {"budget_usd": 3.5},
                "merge": {"method": "rebase"}, "paths": {"logs_dir": "out"},
                "project": {"test_dir": "spec"}}
        cfg = parse_project_config(MINIMAL, user_sections=user)
        self.assertEqual(cfg.review.model, "opus")            # user over default
        self.assertEqual(cfg.review.max_fixes, 5)
        self.assertEqual(cfg.validate.budget_usd, 3.5)
        self.assertEqual(cfg.merge.method, "rebase")
        self.assertEqual(cfg.paths.logs_dir, "out")
        self.assertEqual(cfg.project.test_dir, "spec")
        self.assertEqual(cfg.review.effort, "high")            # untouched default survives

        project = MINIMAL + '\n[review]\nmodel = "sonnet"\n[merge]\nmethod = "merge"\n[paths]\nlogs_dir = "l"\n'
        cfg = parse_project_config(project, user_sections=user)
        self.assertEqual(cfg.review.model, "sonnet")           # project over user
        self.assertEqual(cfg.review.max_fixes, 5)              # user value the project left alone
        self.assertEqual(cfg.merge.method, "merge")
        self.assertEqual(cfg.paths.logs_dir, "l")

    def test_precedence_through_the_real_files(self):
        self.user_file('[review]\nmodel = "opus"\nbudget_usd = 0.25\n[validate.linux]\ndistro = "Ubuntu-24.04"\n')
        self.project_file(MINIMAL + '\n[review]\nmodel = "sonnet"\n')
        cfg = load_project_config(self.repo)
        self.assertEqual(cfg.review.model, "sonnet")
        self.assertEqual(cfg.review.budget_usd, 0.25)
        self.assertEqual(cfg.validate.platforms["linux"].distro, "Ubuntu-24.04")
        self.assertEqual(cfg.validate.platforms["linux"].new_test, "pytest tests")
        # The project did not set the model: the user layer must win over the default.
        self.project_file(MINIMAL)
        self.assertEqual(load_project_config(self.repo).review.model, "opus")

    def test_platform_table_starts_from_platform_defaults(self):
        d = load_defaults()["validate"]["platform"]
        user = {"validate": {"linux": {"distro": "Debian"}}}
        cfg = parse_project_config(MINIMAL + '\n[validate.extra]\nnew_test = "x"\n', user_sections=user)
        linux = cfg.validate.platforms["linux"]
        self.assertEqual(linux.distro, "Debian")
        self.assertEqual(linux.runner, d["runner"])
        self.assertEqual(linux.command_timeout_min, d["command_timeout_min"])
        self.assertEqual(linux.sandbox_dir, d["sandbox_dir"])
        extra = cfg.validate.platforms["extra"]
        self.assertEqual(extra.distro, d["distro"])
        self.assertEqual(extra.runner, d["runner"])
        self.assertEqual(extra.new_test, "x")

    def test_engine_table_layers_per_engine(self):
        user = {"engines": {"claude": {"helper_prefix": "claude-tiny"}}}
        cfg = parse_project_config(MINIMAL + '\n[engines.claude]\ntiers = ["a", "b"]\n', user_sections=user)
        self.assertEqual(cfg.engines["claude"].tiers, ["a", "b"])            # project
        self.assertEqual(cfg.engines["claude"].helper_prefix, "claude-tiny")  # user
        cfg = parse_project_config(MINIMAL, user_sections=user)
        self.assertEqual(cfg.engines["claude"].tiers, load_defaults()["engines"]["claude"]["tiers"])


class AC3LayerErrors(IsolatedHome):
    def test_user_layer_problems_are_labelled_and_collected(self):
        self.user_file('review_model = "opus"\nbogus = 1\n[review]\nmodle = "x"\n[deploy]\nx = 1\n')
        problems = _problems(load_user_config)
        joined = "\n".join(problems)
        self.assertIn("review_model", joined)
        self.assertIn("bogus", joined)
        self.assertIn("modle", joined)
        self.assertIn("[deploy]", joined)
        self.assertTrue(all("user config" in p for p in problems), problems)

    def test_project_layer_problems_are_labelled_with_the_file(self):
        self.project_file(MINIMAL + '\n[review]\nmodle = "x"\nfoo = 1\n[deploy]\nx = 1\n')
        problems = _problems(load_project_config, self.repo)
        keyed = [p for p in problems if "modle" in p or "foo" in p or "[deploy]" in p]
        self.assertEqual(len(keyed), 3, problems)
        for p in keyed:
            self.assertIn("revali.toml", p)

    def test_both_layers_reported_in_one_pass(self):
        problems = _problems(parse_project_config, MINIMAL + '\n[review]\nfoo = 1\n',
                             user_sections={"review": {"bar": 1}})
        self.assertTrue(any("user config" in p and "bar" in p for p in problems), problems)
        self.assertTrue(any("revali.toml" in p and "foo" in p for p in problems), problems)


class AC4Engines(unittest.TestCase):
    def test_retired_values_point_at_strategy(self):
        for old in ("prompt", "hybrid"):
            problems = _problems(parse_project_config, MINIMAL + '\n[review]\nengine = "%s"\n' % old)
            self.assertTrue(any("engine" in p and "strategy" in p for p in problems), problems)

    def test_unknown_engine_lists_available(self):
        for role in ("review", "validate"):
            problems = _problems(parse_project_config, MINIMAL + '\n[%s]\nengine = "codex"\n' % role)
            hits = [p for p in problems if "codex" in p]
            self.assertTrue(hits, problems)
            self.assertIn("claude", hits[0])

    def test_engine_must_name_a_table(self):
        text = MINIMAL + '\n[engines.codex]\ntiers = ["mini", "max"]\n[validate]\nengine = "codex"\n'
        cfg = parse_project_config(text)
        self.assertEqual(cfg.validate.engine, "codex")
        self.assertEqual(cfg.engines["codex"].tiers, ["mini", "max"])
        self.assertEqual(cfg.review.engine, "claude")


class AC5UserFile(IsolatedHome):
    def test_accepts_project_sections_plus_top_keys(self):
        self.user_file('checklist = "mine.md"\nhistory_path = "h.jsonl"\n'
                       '[project]\ntest_dir = "spec"\n[review]\neffort = "low"\n[validate]\nbudget_usd = 2.5\n'
                       '[validate.linux]\nrunner = "local"\n[merge]\nwait_for_checks = false\n'
                       '[paths]\nlogs_dir = "out"\n[engines.claude]\ntiers = ["x"]\n')
        u = load_user_config()
        self.assertEqual(u.checklist, os.path.join(self.home, "mine.md"))
        self.assertEqual(u.history_path, "h.jsonl")
        self.project_file(MINIMAL)
        cfg = load_project_config(self.repo, u)
        self.assertEqual(cfg.project.test_dir, "spec")
        self.assertEqual(cfg.review.effort, "low")
        self.assertEqual(cfg.validate.budget_usd, 2.5)
        self.assertEqual(cfg.validate.platforms["linux"].runner, "local")
        self.assertFalse(cfg.merge.wait_for_checks)
        self.assertEqual(cfg.paths.logs_dir, "out")
        self.assertEqual(cfg.engines["claude"].tiers, ["x"])

    def test_retired_keys_name_their_new_place(self):
        self.user_file('review_model = "opus"\nvalidate_model = "sonnet"\n')
        problems = _problems(load_user_config)
        joined = "\n".join(problems)
        self.assertIn("[review] model", joined)
        self.assertIn("[validate] model", joined)


class AC6Paths(IsolatedHome):
    def test_state_and_logs_dir_single_component(self):
        for key in ("state_dir", "logs_dir"):
            for bad in ('"a/b"', "'a\\b'", '""', '"."', '".."'):
                problems = _problems(parse_project_config, MINIMAL + "\n[paths]\n%s = %s\n" % (key, bad))
                self.assertTrue(any(key in p for p in problems), (key, bad, problems))
        cfg = parse_project_config(MINIMAL + '\n[paths]\nstate_dir = ".rv"\nlogs_dir = "out"\n')
        self.assertEqual((cfg.paths.state_dir, cfg.paths.logs_dir), (".rv", "out"))

    def test_file_overrides_must_exist_under_the_repo(self):
        for section, key in (("review", "prompt"), ("review", "schema"), ("review", "checklist_builtin"),
                             ("validate", "prompt"), ("validate", "schema")):
            self.project_file(MINIMAL + '\n[%s]\n%s = "docs/%s_%s.txt"\n' % (section, key, section, key))
            problems = _problems(load_project_config, self.repo)
            self.assertTrue(any("%s.%s" % (section, key) in p for p in problems), (key, problems))
            _write(os.path.join(self.repo, "docs", "%s_%s.txt" % (section, key)), "x")
            cfg = load_project_config(self.repo)
            self.assertEqual(getattr(getattr(cfg, section), key), "docs/%s_%s.txt" % (section, key))

    def test_empty_override_means_shipped_file(self):
        self.project_file(MINIMAL + '\n[review]\nprompt = ""\nschema = ""\nchecklist_builtin = ""\n')
        cfg = load_project_config(self.repo)
        self.assertEqual((cfg.review.prompt, cfg.review.schema, cfg.review.checklist_builtin), ("", "", ""))


class AC7Docs(IsolatedHome):
    def test_project_template_parses_and_describes_layers(self):
        with open(os.path.join(ROOT, "templates", "revali.toml"), "r", encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("defaults.toml", text)
        self.assertIn("config.toml", text)
        cfg = parse_project_config(text)
        self.assertEqual(cfg.review.engine, "claude")
        self.assertNotIn("review_model", text)

    def test_user_template_loads_and_describes_layers(self):
        with open(os.path.join(ROOT, "templates", "user-config.toml"), "r", encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("defaults.toml", text)
        self.assertNotIn("\nreview_model", text)
        self.user_file(text)
        u = load_user_config()   # the uncommented keys must be accepted as written
        self.assertEqual(u.checklist, "")

    def test_readme_has_configuration_section(self):
        with open(os.path.join(ROOT, "docs", "configuration.md"), "r", encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("# Configuration", text)
        for name in ("defaults.toml", "config.toml", "revali.toml"):
            self.assertIn(name, text)


if __name__ == "__main__":
    unittest.main()
