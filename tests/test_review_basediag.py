"""Acceptance tests for what the diagnosis session and the user see of the base rerun:
the prompt section and `introduced_by` definition, the schema, PROMPT_VERSION (AC-5),
the config key in defaults / template / docs and the doc pages (AC-8), and the `revali
reset` wording (AC-11)."""

import json
import os
import tomllib
import unittest

from revali import EXIT_ACTION, PROMPT_VERSION
from tests.fixtures.make_sample_repo import LOCAL_NEW_TEST, PY, toml_str
from tests.helpers import ROOT, RepoCase, claude_entry, git, run_cli

RDIR = ".revali/feature__mul"
PROMPT = RDIR + "/logs/prompt-diagnose-1.md"
BASE_OUT = "ImportError: cannot import name 'mul' [on base]"
BRANCH_OUT = "AssertionError: 12 != 7 [on the branch]"


def repo_file(*parts):
    with open(os.path.join(ROOT, *parts), "r", encoding="utf-8") as fh:
        return fh.read()


def diagnosis(introduced_by="unknown"):
    return {
        "summary": "the product test fails.",
        "cause": "code",
        "introduced_by": introduced_by,
        "failures": [
            {
                "test": "tests/test_review_mul.py::MulTests::test_product",
                "cause": "code",
                "introduced_by": introduced_by,
                "note": "expected 12, got 7",
            }
        ],
        "recommendation": "return a * b",
    }


class DiagnosisPrompt(RepoCase):
    def failing(self, base_exit=1, errors=None):
        sc = {
            "default": 0,
            "results": {"validate-r1": {"new_test": 1}, "base-r1": {"new_test": base_exit}},
            "outputs": {
                "validate-r1": {"new_test": BRANCH_OUT + "\n"},
                "base-r1": {"new_test": BASE_OUT + "\n"},
            },
        }
        if errors:
            sc["errors"] = errors
        self.runner_scenario(sc)
        self.claude(claude_entry(), claude_entry(diagnosis(), write_tests=False))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ACTION, out)
        return self.read(PROMPT)

    def test_prompt_carries_the_base_output(self):
        # AC-5: base name and sha, the exit code, the output tail, the definitions
        prompt = self.failing()
        base = git(["rev-parse", "origin/main"], self.repo).strip()
        self.assertIn(base, prompt)
        self.assertIn("`main`", prompt)
        self.assertIn("exited 1 on base", prompt)
        self.assertIn("last 200 lines", prompt)
        self.assertIn(BASE_OUT, prompt)
        self.assertIn(BRANCH_OUT, prompt)
        self.assertIn("introduced_by", prompt)
        for token in ("`branch`", "`base`", "`unknown`"):
            self.assertIn(token, prompt)
        self.assertIn("import error", prompt.lower())
        # the same prompt is what the diagnosis session received
        sent = self.fake_calls("claude")[1]["prompt"]
        self.assertIn(BASE_OUT, sent)
        self.assertIn("introduced_by", sent)

    def test_prompt_carries_a_green_base_result(self):
        prompt = self.failing(base_exit=0)
        self.assertIn("exited 0 on base", prompt)

    def test_prompt_says_why_there_is_no_base_output(self):
        # AC-5: the reason replaces the output, and the answer is pinned to unknown
        prompt = self.failing(errors={"base-r1": "clone blew up"})
        self.assertIn("clone blew up", prompt)
        self.assertNotIn(BASE_OUT, prompt)
        self.assertIn("unknown", prompt)
        # round 1 F2: the prompt must not claim the files were copied and run on base
        self.assertNotIn("copied the reviewer's test files", prompt)
        section = prompt.split("## The same test files on the base branch")[1]
        self.assertNotIn("exited", section.split("# What to decide")[0])

    def test_prompt_states_only_what_ran_when_the_rerun_is_off(self):
        # round 1 F2: with the key false nothing ran on base; the section still names the
        # base branch and sha, gives the reason, and does not say anything was copied
        cfg = self.read("revali.toml").replace("[validate]\n", "[validate]\nrerun_on_base = false\n")
        self.write("revali.toml", cfg)
        self.commit_all("rerun off")
        prompt = self.failing()
        base = git(["rev-parse", "origin/main"], self.repo).strip()
        section = prompt.split("## The same test files on the base branch")[1]
        section = section.split("# What to decide")[0]
        self.assertIn(base, section)
        self.assertIn("`main`", section)
        self.assertIn("rerun_on_base", section)
        self.assertNotIn("copied", section)
        self.assertNotIn(BASE_OUT, section)
        self.assertIn("unknown", section)
        self.assertEqual([c for c in self.fake_calls("runner") if c["label"] == "base-r1"], [])

    def test_prompt_claims_the_copy_only_when_it_happened(self):
        prompt = self.failing()
        section = prompt.split("## The same test files on the base branch")[1]
        section = section.split("# What to decide")[0]
        self.assertIn("copied the reviewer's test files", section)
        self.assertIn("exited 1 on base", section)
        self.assertIn(BASE_OUT, section)

    def test_prompt_names_the_reviewer_files_the_rerun_used(self):
        cfg = self.read("revali.toml").replace(
            "new_test = %s" % toml_str(LOCAL_NEW_TEST),
            "new_test = %s" % toml_str(PY + " -m unittest {files}"),
        )
        self.write("revali.toml", cfg)
        self.commit_all("new_test with {files}")
        prompt = self.failing()
        self.assertIn("tests/test_review_mul.py", prompt)
        self.assertIn(BASE_OUT, prompt)


class SchemaAndVersion(unittest.TestCase):
    def test_schema_requires_introduced_by_everywhere(self):
        # AC-5
        schema = json.loads(repo_file("schemas", "diagnose.schema.json"))
        enum = ["branch", "base", "unknown"]
        self.assertIn("introduced_by", schema["required"])
        self.assertEqual(schema["properties"]["introduced_by"]["type"], "string")
        self.assertEqual(schema["properties"]["introduced_by"]["enum"], enum)
        item = schema["properties"]["failures"]["items"]
        self.assertIn("introduced_by", item["required"])
        self.assertEqual(item["properties"]["introduced_by"]["enum"], enum)
        self.assertFalse(item.get("additionalProperties", True))

    def test_prompt_version_bumped(self):
        # AC-5: "6" for this change; a later bump keeps the test true
        self.assertGreaterEqual(int(PROMPT_VERSION), 6)

    def test_prompt_template_defines_the_values(self):
        text = repo_file("prompts", "diagnose.md")
        for token in ("$base_sha", "$base", "introduced_by", "`branch`", "`base`", "`unknown`"):
            self.assertIn(token, text)
        # the section with the base output sits in the template
        self.assertIn("$base_rerun", text)


class ConfigAndDocs(unittest.TestCase):
    def test_key_in_defaults_and_template(self):
        # AC-8: default true, listed in the template
        defaults = tomllib.loads(repo_file("defaults.toml"))
        self.assertIs(defaults["validate"]["rerun_on_base"], True)
        template = tomllib.loads(repo_file("templates", "revali.toml"))
        self.assertIn("rerun_on_base", template["validate"])
        self.assertIsInstance(template["validate"]["rerun_on_base"], bool)

    def test_configuration_page(self):
        text = repo_file("docs", "configuration.md")
        self.assertIn("rerun_on_base", text)
        idx = text.index("rerun_on_base")
        self.assertIn("introduced_by", text[idx : idx + 800])

    def test_sandbox_and_files_pages(self):
        sandbox = repo_file("docs", "sandbox.md")
        self.assertIn("base-r", sandbox)
        self.assertIn("introduced_by", sandbox)
        self.assertIn("rerun_on_base", sandbox)
        files = repo_file("docs", "files.md")
        self.assertIn("Base rerun", files)
        self.assertIn("base-r", files)

    def test_readme_validator_row(self):
        rows = [ln for ln in repo_file("README.md").splitlines() if ln.startswith("| Validator")]
        self.assertEqual(len(rows), 1)
        self.assertIn("base", rows[0])
        self.assertIn("introduced_by", rows[0])

    def test_reset_redoes_the_baseline(self):
        # AC-11
        text = repo_file("docs", "configuration.md")
        self.assertNotIn("`revali reset` all bring the full suite back", text)
        idx = text.index("`revali reset`")
        window = text[idx : idx + 200]
        self.assertIn("baseline", window)


if __name__ == "__main__":
    unittest.main()
