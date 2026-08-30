"""Config layers and model resolution end to end: state_dir, prompt override, auto models."""
import json
import os
import unittest

from tests.helpers import RepoCase, approve_response, claude_entry, git, run_cli
from revali import EXIT_ACTION, EXIT_OK
from revali.state import State
from tests.test_validate import diagnosis


def argv_after(argv, flag):
    return argv[argv.index(flag) + 1]


class StateDirTests(RepoCase):
    def test_state_dir_override(self):
        self.write("revali.toml", self.read("revali.toml") + '\n[paths]\nstate_dir = ".rv"\nlogs_dir = "out"\n')
        self.commit_all("state dir")
        self.write(".rv/feature__mul/change.md", self.read(".revali/feature__mul/change.md"))
        self.claude(claude_entry())
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertTrue(self.exists(".rv/feature__mul/review-1.md"))
        self.assertTrue(self.exists(".rv/feature__mul/out/revali.log"))
        self.assertTrue(self.exists(".rv/feature__mul/out/review-r1-1.raw.json"))
        self.assertFalse(self.exists(".revali/feature__mul/review-1.md"))
        self.assertIn(".rv/", self.read(".gitignore"))
        self.assertIn("chore: ignore .rv/", git(["log", "--format=%s"], self.repo))
        code, out = run_cli(["status"])
        self.assertEqual(code, EXIT_OK)
        self.assertIn("ready_to_merge", out)


class ModelResolutionTests(RepoCase):
    def set_author(self, model):
        import re
        text = self.read(".revali/feature__mul/change.md")
        self.write(".revali/feature__mul/change.md", re.sub(r"author_model: .*", "author_model: " + model, text, count=1))

    def test_unknown_author_gives_top_tier(self):
        self.claude(claude_entry())
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        argv = self.fake_calls("claude")[0]["argv"]
        self.assertEqual(argv_after(argv, "--model"), "fable")
        self.assertEqual(argv_after(argv, "--fallback-model"), "opus,sonnet,haiku")
        self.assertIn("not on the ladder", out)
        review_md = self.read(".revali/feature__mul/review-1.md")
        self.assertIn("model_reason: auto:", review_md)

    def test_reviewer_one_above_author(self):
        self.set_author("claude-opus-5")
        self.claude(claude_entry())
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        argv = self.fake_calls("claude")[0]["argv"]
        self.assertEqual(argv_after(argv, "--model"), "fable")
        self.assertIn("one tier above author claude-opus-5", out)
        self.set_author("claude-sonnet-5")
        run_cli(["reset"])
        self.claude(claude_entry())
        run_cli(["run", "--foreground"])
        argv = self.fake_calls("claude")[-1]["argv"]
        self.assertEqual(argv_after(argv, "--model"), "opus")
        self.assertEqual(argv_after(argv, "--fallback-model"), "sonnet,haiku")

    def test_diagnoser_one_below_author(self):
        self.set_author("claude-fable-5")
        self.runner_scenario({"default": 0, "results": {"validate-r1": {"new_test": 1}},
                              "outputs": {"validate-r1": {"new_test": "AssertionError: 12 != 7"}}})
        self.claude(claude_entry(), claude_entry(diagnosis(), write_tests=False, model="claude-opus-5", cost=0.2))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ACTION, out)
        reviewer, diag = [c["argv"] for c in self.fake_calls("claude")]
        self.assertEqual(argv_after(reviewer, "--model"), "fable")
        self.assertEqual(argv_after(diag, "--model"), "opus")
        self.assertEqual(argv_after(diag, "--fallback-model"), "sonnet,haiku")
        self.assertIn("--tools", diag)
        meta = json.loads(self.read(".revali/feature__mul/diagnose-1.json"))["meta"]
        self.assertIn("one tier below author claude-fable-5", meta["model_reason"])

    def test_project_pin_beats_user_layer(self):
        with open(os.path.join(self.home, "config.toml"), "w", encoding="utf-8") as fh:
            fh.write('[review]\nmodel = "opus"\nfallback_model = ""\n')
        self.claude(claude_entry())
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        argv = self.fake_calls("claude")[0]["argv"]
        self.assertEqual(argv_after(argv, "--model"), "opus")
        self.assertNotIn("--fallback-model", argv)
        self.assertIn("model_reason: explicit", self.read(".revali/feature__mul/review-1.md"))
        # project layer pins sonnet, user layer says opus
        self.write("revali.toml", self.read("revali.toml").replace("[review]\n", '[review]\nmodel = "sonnet"\n'))
        self.commit_all("pin")
        self.claude(claude_entry())
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        argv = self.fake_calls("claude")[-1]["argv"]
        self.assertEqual(argv_after(argv, "--model"), "sonnet")

    def test_dry_run_prints_resolved_model(self):
        self.set_author("claude-sonnet-5")
        code, out = run_cli(["run", "--dry-run"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("run reviewer opus (auto: one tier above author claude-sonnet-5)", out)


class PromptOverrideTests(RepoCase):
    def test_project_prompt_and_checklist_override(self):
        self.write("docs/review-prompt.md", "CUSTOM PROMPT for $branch\n\n$checklist\n")
        self.write("docs/builtin.md", "PROJECT BUILTIN LIST\n")
        self.write("revali.toml", self.read("revali.toml").replace(
            "[review]\n", '[review]\nprompt = "docs/review-prompt.md"\nchecklist_builtin = "docs/builtin.md"\n'))
        self.commit_all("prompt override")
        self.claude(claude_entry())
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        prompt = self.fake_calls("claude")[0]["prompt"]
        self.assertTrue(prompt.startswith("CUSTOM PROMPT for feature/mul"))
        self.assertIn("PROJECT BUILTIN LIST", prompt)
        self.assertNotIn("Behaviour changes have tests", prompt)

    def test_config_error_does_not_add_change_md_noise(self):
        broken = self.read("revali.toml").replace("[review]\n", '[review]\nengine = "nope"\n') + '\n[paths]\nstate_dir = ".rv"\n'
        self.write("revali.toml", broken)
        self.write(".rv/feature__mul/change.md", self.read(".revali/feature__mul/change.md"))
        self.commit_all("broken")
        code, out = run_cli(["preflight"])
        self.assertEqual(code, 1)
        self.assertIn("engine 'nope' is unknown", out)
        self.assertNotIn("change.md", out)

    def test_missing_override_file_is_a_preflight_error(self):
        self.write("revali.toml", self.read("revali.toml").replace("[review]\n", '[review]\nprompt = "docs/none.md"\n'))
        self.commit_all("bad override")
        code, out = run_cli(["preflight"])
        self.assertEqual(code, 1)
        self.assertIn("review.prompt: file not found: docs/none.md", out)


if __name__ == "__main__":
    unittest.main()
