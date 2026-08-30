"""AC-1 / AC-2 / AC-3: the Engine seam, the claude mapping of abstract permissions,
and the engine registry. Black-box through the CLI where the AC is about behaviour,
white-box on the module surface where the AC names it."""
import importlib.util
import os
import unittest
from dataclasses import fields

from tests.helpers import ROOT, RepoCase, claude_entry, run_cli
from tests.test_validate import diagnosis
from revali import EXIT_ACTION, EXIT_OK
from revali.config import ConfigError, EngineCfg

CLAUDE_FLAGS = ("--permission-mode", "--allowedTools", "--tools", "--json-schema",
                "--max-budget-usd", "--fallback-model", "--output-format", "--effort")


def argv_after(argv, flag):
    return argv[argv.index(flag) + 1]


def package_files():
    """Every .py under revali/, path relative to the package, recursive."""
    pkg = os.path.join(ROOT, "revali")
    out = {}
    for dirpath, _dirs, names in os.walk(pkg):
        for name in names:
            if name.endswith(".py"):
                full = os.path.join(dirpath, name)
                rel = os.path.relpath(full, pkg).replace("\\", "/")
                with open(full, "r", encoding="utf-8") as fh:
                    out[rel] = fh.read()
    return out


class AC1BaseInterface(unittest.TestCase):
    def test_request_result_and_engine_surface(self):
        from revali.engines.base import Engine, EngineRequest, EngineResult
        req_fields = {f.name for f in fields(EngineRequest)}
        for name in ("prompt", "schema_text", "model", "fallback_model", "effort", "budget_usd",
                     "timeout_s", "cwd", "raw_path", "may_write", "read_only", "shell_allow"):
            self.assertIn(name, req_fields, name)
        res_fields = {f.name for f in fields(EngineResult)}
        for name in ("data", "model_requested", "model_actual", "fallback", "cost", "denials", "duration_ms"):
            self.assertIn(name, res_fields, name)
        for attr in ("name", "supports_schema", "run", "model_family", "pick_actual_model"):
            self.assertTrue(hasattr(Engine, attr), attr)
        eng = Engine(EngineCfg(name="x", tiers=["mini", "max"], helper_prefix=""))
        self.assertEqual(eng.tiers, ["mini", "max"])
        with self.assertRaises(NotImplementedError):
            eng.run(EngineRequest(role="reviewer", prompt="p", schema_text="{}", model="max",
                                  fallback_model="", effort="high", budget_usd=1.0, timeout_s=1,
                                  cwd=os.getcwd(), raw_path="raw.json"))

    def test_family_and_actual_model_follow_the_configured_ladder(self):
        # A non-claude ladder: nothing in base.py may assume claude names.
        from revali.engines.base import Engine
        eng = Engine(EngineCfg(name="x", tiers=["mini", "max"], helper_prefix="x-helper"))
        self.assertEqual(eng.model_family("x-max-2"), "max")
        self.assertEqual(eng.model_family("mini"), "mini")
        self.assertEqual(eng.model_family("unknown-id"), "unknown-id")
        usage = {"x-helper-1": {"costUSD": 0.01}, "x-max-2": {"costUSD": 0.4}}
        self.assertEqual(eng.pick_actual_model(usage, "max"), ("x-max-2", False))
        usage = {"x-helper-1": {"costUSD": 0.01}, "x-mini-2": {"costUSD": 0.3}}
        self.assertEqual(eng.pick_actual_model(usage, "max"), ("x-mini-2", True))
        self.assertEqual(eng.pick_actual_model({"x-helper-1": {"costUSD": 0.01}}, "max"), ("", False))


class AC2ClaudeFlagsConfined(unittest.TestCase):
    def test_old_module_is_gone(self):
        self.assertIsNone(importlib.util.find_spec("revali.claude"))
        self.assertFalse(os.path.isfile(os.path.join(ROOT, "revali", "claude.py")))

    def test_no_claude_flag_outside_engines(self):
        for rel, text in package_files().items():
            if rel.startswith("engines/"):
                continue
            for flag in CLAUDE_FLAGS:
                self.assertNotIn('"%s"' % flag, text, "%s still carries %s" % (rel, flag))
                self.assertNotIn("'%s'" % flag, text, "%s still carries %s" % (rel, flag))
            self.assertNotIn("acceptEdits", text, rel)
            self.assertNotIn("Read,Grep,Glob", text, rel)
            self.assertNotIn("Bash(", text, rel)


class AC2PermissionMappingThroughThePipeline(RepoCase):
    def test_reviewer_gets_edits_and_readonly_git(self):
        self.claude(claude_entry())
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        argv = self.fake_calls("claude")[0]["argv"]
        self.assertEqual(argv_after(argv, "--permission-mode"), "acceptEdits")
        allowed = argv_after(argv, "--allowedTools")
        for prefix in ("git diff", "git log", "git show"):
            self.assertIn("Bash(%s *)" % prefix, allowed)
        self.assertNotIn("--tools", argv)
        self.assertEqual(argv_after(argv, "--max-budget-usd"), "1.0")   # [review] budget_usd of the fixture
        self.assertIn("--json-schema", argv)
        self.assertIn("via claude", out)

    def test_diagnoser_is_read_only(self):
        self.runner_scenario({"default": 0, "results": {"validate-r1": {"new_test": 1}},
                              "outputs": {"validate-r1": {"new_test": "AssertionError: 12 != 7"}}})
        self.claude(claude_entry(), claude_entry(diagnosis(), write_tests=False))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ACTION, out)
        diag = self.fake_calls("claude")[1]["argv"]
        self.assertEqual(argv_after(diag, "--tools"), "Read,Grep,Glob")
        self.assertNotIn("--permission-mode", diag)
        self.assertNotIn("--allowedTools", diag)
        self.assertEqual(argv_after(diag, "--max-budget-usd"), "0.5")   # [validate] budget_usd of the fixture

    def test_helper_prefix_comes_from_the_engine_table(self):
        # With claude-opus declared the helper, the only non-helper usage row is haiku,
        # which is outside the requested family: the run must be flagged as a fallback.
        with open(os.path.join(self.home, "config.toml"), "w", encoding="utf-8") as fh:
            fh.write('[review]\nmodel = "opus"\nfallback_model = ""\n'
                     '[engines.claude]\nhelper_prefix = "claude-opus"\n')
        self.claude(claude_entry(model="claude-opus-5"))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("fallback model", out)
        self.assertIn("fallback: True", self.read(".revali/feature__mul/review-1.md"))


class AC3Registry(unittest.TestCase):
    def test_available_and_unknown_engine(self):
        from revali.engines import available, get_engine, for_role
        from revali.engines.claude import ClaudeEngine
        cfg = EngineCfg(name="claude", tiers=["haiku", "sonnet", "opus", "fable"], helper_prefix="claude-haiku")
        self.assertEqual(available(), ["claude"])
        eng = get_engine("claude", cfg)
        self.assertIsInstance(eng, ClaudeEngine)
        self.assertEqual(eng.name, "claude")
        self.assertEqual(eng.tiers, ["haiku", "sonnet", "opus", "fable"])
        with self.assertRaises(ConfigError) as cm:
            get_engine("codex", EngineCfg(name="codex", tiers=["mini", "max"]))
        msg = cm.exception.problems[0]
        self.assertIn("codex", msg)
        self.assertIn("available: claude", msg)
        # for_role picks the engine named in [review] / [validate]
        from revali.config import parse_project_config
        from tests.test_review_config import MINIMAL
        cfg_all = parse_project_config(MINIMAL)
        self.assertEqual(for_role(cfg_all, "review").name, "claude")
        self.assertEqual(for_role(cfg_all, "validate").name, "claude")


if __name__ == "__main__":
    unittest.main()
