import os
import unittest

from tests.helpers import ROOT  # noqa: F401  (sys.path setup)
from revali.config import ConfigError, EngineCfg
from revali.engines import available, get_engine
from revali.engines.base import EngineRequest
from revali.engines.claude import ClaudeEngine, failure_message, permission_args

CFG = EngineCfg(name="claude", tiers=["haiku", "sonnet", "opus", "fable"], helper_prefix="claude-haiku")


def request(**kw):
    base = dict(role="reviewer", prompt="p", schema_text="{}", model="fable", fallback_model="opus",
                effort="high", budget_usd=1.0, timeout_s=60, cwd=os.getcwd(), raw_path="raw.json")
    base.update(kw)
    return EngineRequest(**base)


class RegistryTests(unittest.TestCase):
    def test_available_and_lookup(self):
        self.assertEqual(available(), ["claude"])
        self.assertIsInstance(get_engine("claude", CFG), ClaudeEngine)
        with self.assertRaises(ConfigError) as cm:
            get_engine("codex", CFG)
        self.assertIn("available: claude", cm.exception.problems[0])


class PermissionMappingTests(unittest.TestCase):
    def test_reviewer_permissions(self):
        args = permission_args(request(may_write=["tests"], shell_allow=["git diff", "git log", "git show"]))
        self.assertEqual(args, ["--permission-mode", "acceptEdits",
                                "--allowedTools", "Bash(git diff *) Bash(git log *) Bash(git show *)"])

    def test_read_only_wins(self):
        args = permission_args(request(read_only=True, may_write=["tests"], shell_allow=["git diff"]))
        self.assertEqual(args, ["--tools", "Read,Grep,Glob"])

    def test_nothing_requested(self):
        self.assertEqual(permission_args(request()), [])


class FailureMessageTests(unittest.TestCase):
    def test_budget_exhausted(self):
        payload = {"is_error": True, "subtype": "error_max_budget_usd", "num_turns": 13,
                   "total_cost_usd": 2.17, "errors": ["Reached maximum budget ($2)"], "result": None}
        msg = failure_message("reviewer", payload, 1, 2.0, "raw.json")
        self.assertIn("reviewer ran out of budget ($2.00) after 13 turns, spent $2.17", msg)
        self.assertIn("raise budget_usd", msg)

    def test_other_errors_are_quoted(self):
        payload = {"is_error": True, "subtype": "error_during_execution", "errors": ["boom"]}
        self.assertIn("reviewer session failed (exit 1): boom", failure_message("reviewer", payload, 1, 2.0, "r"))
        self.assertIn("no error text", failure_message("diagnoser", {"is_error": True}, 1, 1.0, "r"))
        self.assertIn("no error text", failure_message("diagnoser", {"is_error": True, "result": None}, 1, 1.0, "r"))


class ModelFamilyTests(unittest.TestCase):
    def setUp(self):
        self.engine = ClaudeEngine(CFG)

    def test_model_family(self):
        self.assertEqual(self.engine.model_family("fable"), "fable")
        self.assertEqual(self.engine.model_family("claude-fable-5"), "fable")
        self.assertEqual(self.engine.model_family("claude-opus-5"), "opus")
        self.assertEqual(self.engine.model_family("something-else"), "something-else")
        self.assertEqual(self.engine.model_family("Claude-Opus-5"), "opus")
        self.assertEqual(self.engine.model_family(""), "")

    def test_model_family_follows_tier_index(self):
        # one lookup for both: a ladder with unusual names must give the same answer in both places
        engine = ClaudeEngine(EngineCfg(name="claude", tiers=["Mini", "Max"], helper_prefix=""))
        self.assertEqual(engine.model_family("vendor-max-2"), "max")
        self.assertEqual(engine.model_family("vendor-mini"), "mini")

    def test_pick_actual_model(self):
        usage = {"claude-haiku-4-5-20251001": {"costUSD": 0.001}, "claude-fable-5": {"costUSD": 0.4}}
        self.assertEqual(self.engine.pick_actual_model(usage, "fable"), ("claude-fable-5", False))
        usage = {"claude-haiku-4-5-20251001": {"costUSD": 0.001}, "claude-opus-5": {"costUSD": 0.3}}
        self.assertEqual(self.engine.pick_actual_model(usage, "fable"), ("claude-opus-5", True))
        self.assertEqual(self.engine.pick_actual_model({}, "fable"), ("", False))

    def test_helper_prefix_from_config(self):
        engine = ClaudeEngine(EngineCfg(name="claude", tiers=["opus", "fable"], helper_prefix="claude-sonnet"))
        usage = {"claude-sonnet-5": {"costUSD": 0.01}, "claude-fable-5": {"costUSD": 0.4}}
        self.assertEqual(engine.pick_actual_model(usage, "fable"), ("claude-fable-5", False))


if __name__ == "__main__":
    unittest.main()
