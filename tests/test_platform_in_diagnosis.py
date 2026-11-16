"""The diagnosis prompt and the pre-existing-bug issue name the platform and the runner the
failure was seen on (feature/platform-in-diagnosis, M3.7.3)."""

import os
import re
import unittest

from revali import EXIT_ACTION, PROMPT_VERSION
from tests.helpers import ROOT, claude_entry
from tests.test_every_platform import LOGS, PlatformCase, diagnosis

OTHERS = "The same validation on the other listed platforms:"


def evidence_of(body):
    return body.split("## Evidence")[1].split("## ")[0]


class Prompt(PlatformCase):
    """AC-1, AC-2"""

    def failing_run(self, results, diagnoses=1):
        self.runner_scenario({"default": 0, "results": results})
        self.claude(
            claude_entry(),
            *[claude_entry(diagnosis(), write_tests=False) for _ in range(diagnoses)],
        )
        code, out = self.go()
        self.assertEqual(code, EXIT_ACTION, out)

    def prompt(self, tag):
        return self.read(os.path.join(LOGS, "prompt-diagnose-%s.md" % tag))

    def test_one_platform_names_it_and_lists_no_others(self):
        self.failing_run({"validate-r1": {"new_test": 1}})
        prompt = self.prompt("1")
        self.assertIn("on the `linux` platform (runner `local`)", prompt)
        self.assertIn("uses the same `linux` platform (runner `local`)", prompt)
        self.assertNotIn(OTHERS, prompt)
        self.assertNotRegex(prompt, r"\$[a-z_]+")

    def test_two_platforms_name_the_failing_one_and_the_others_result(self):
        self.platforms()
        self.failing_run({"validate-r1-windows": {"new_test": 1}})
        prompt = self.prompt("1-windows")
        self.assertIn("on the `windows` platform (runner `ssh`)", prompt)
        self.assertIn("uses the same `windows` platform (runner `ssh`)", prompt)
        self.assertIn(OTHERS + "\n\n- `linux`: PASS\n", prompt)
        self.assertNotIn("- `windows`:", prompt)

    def test_both_failing_list_each_other_with_the_failed_step(self):
        self.platforms()
        self.failing_run(
            {"validate-r1-linux": {"new_test": 1}, "validate-r1-windows": {"new_test": 1}},
            diagnoses=2,
        )
        linux, windows = self.prompt("1-linux"), self.prompt("1-windows")
        self.assertIn("on the `linux` platform (runner `local`)", linux)
        self.assertIn("- `windows`: FAIL at `new_test`", linux)
        self.assertIn("on the `windows` platform (runner `ssh`)", windows)
        self.assertIn("- `linux`: FAIL at `new_test`", windows)


class Issue(PlatformCase):
    """AC-3"""

    def base_bug(self, results, diagnoses=1):
        self.runner_scenario({"default": 0, "results": results})
        self.claude(
            claude_entry(),
            *[claude_entry(diagnosis("base"), write_tests=False) for _ in range(diagnoses)],
        )
        code, out = self.go()
        self.assertEqual(code, EXIT_ACTION, out)

    def test_one_platform_issue_names_it(self):
        self.base_bug({"validate-r1": {"new_test": 1}, "base-r1": {"new_test": 1}})
        body = self.read(os.path.join(LOGS, "issue-1.md"))
        self.assertIn("- validation 1 on `linux`, revali", body)
        lines = [ln for ln in evidence_of(body).splitlines() if ln.strip()]
        self.assertTrue(lines[0].startswith("On the branch: `new_test` exit 1."), lines)
        self.assertTrue(lines[1].startswith("Base branch `main`, on base"), lines)
        for line in lines[:2]:
            self.assertTrue(line.endswith("Platform `linux`, runner `local`."), line)
        self.assertNotRegex(body, r"\$[a-z_]+")

    def test_the_second_platform_issue_names_the_second(self):
        self.platforms()
        self.base_bug({"validate-r1-windows": {"new_test": 1}, "base-r1-windows": {"new_test": 1}})
        body = self.read(os.path.join(LOGS, "issue-1-windows.md"))
        self.assertIn("- validation 1 on `windows`, revali", body)
        evidence = evidence_of(body)
        self.assertEqual(evidence.count("Platform `windows`, runner `ssh`."), 2, evidence)
        self.assertNotIn("linux", evidence)

    def test_a_project_template_gets_the_platform(self):
        self.platforms()
        self.write("issue.md", "seen on $platform\n\n$evidence\n")
        self.write(
            "revali.toml",
            self.read("revali.toml").replace(
                "[validate]\n", '[validate]\nissue_template = "issue.md"\n'
            ),
        )
        self.commit_all("project issue template")
        self.base_bug({"validate-r1-windows": {"new_test": 1}, "base-r1-windows": {"new_test": 1}})
        body = self.read(os.path.join(LOGS, "issue-1-windows.md"))
        self.assertTrue(body.startswith("seen on windows\n"), body[:80])


class Version(unittest.TestCase):
    """AC-4"""

    def test_prompt_version_bumped(self):
        self.assertEqual(PROMPT_VERSION, "10")

    def test_docs_list_the_placeholder(self):
        with open(os.path.join(ROOT, "docs", "configuration.md"), encoding="utf-8") as f:
            text = f.read()
        placeholders = re.search(r"\$placeholder` template \(([^)]*)\)", text).group(1)
        self.assertIn("`$platform`", placeholders)


if __name__ == "__main__":
    unittest.main()
