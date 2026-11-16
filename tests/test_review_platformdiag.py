"""Acceptance tests for feature/platform-in-diagnosis: the diagnosis prompt names the platform
and the runner that failed and says the base rerun ran there too (AC-1), lists how the other
listed platforms went (AC-2), the issue body for a pre-existing bug names the platform and the
runner on both Evidence lines and a project template gets `$platform` (AC-3), and the names of
a one-platform run stay as they were while PROMPT_VERSION moves (AC-4).

Black box through `revali run` with the fake gh, claude and runner: the prompt is read from
`logs/prompt-diagnose-<validation>[-<platform>].md`, the issue body from
`logs/issue-<validation>[-<platform>].md`. The assertions look for the names, not for the
sentences around them.
"""

import os
import re
import unittest

from revali import EXIT_ACTION, PROMPT_VERSION
from tests.fixtures.make_sample_repo import LOCAL_NEW_TEST, LOCAL_TEST, toml_str
from tests.helpers import RepoCase, claude_entry, run_cli

RDIR = ".revali/feature__mul"
LOGS = RDIR + "/logs"
TESTS_MD = RDIR + "/tests.md"
TEST_ID = "tests/test_review_mul.py::MulTests::test_product"

WINDOWS_TABLE = '\n[validate.windows]\nrunner = "ssh"\nhost = "winbox"\n' + (
    "test = %s\nnew_test = %s\n" % (toml_str(LOCAL_TEST), toml_str(LOCAL_NEW_TEST))
)


def diagnosis(introduced_by="branch"):
    return {
        "summary": "mul adds instead of multiplying",
        "cause": "code",
        "introduced_by": introduced_by,
        "failures": [
            {"test": TEST_ID, "cause": "code", "introduced_by": introduced_by, "note": "12 != 7"}
        ],
        "recommendation": "return a * b",
    }


def lines_with(text, *words):
    """The lines of `text` that carry every one of `words` as a whole word."""
    return [
        ln
        for ln in text.splitlines()
        if all(re.search(r"(?<![\w-])%s(?![\w-])" % re.escape(w), ln) for w in words)
    ]


class Case(RepoCase):
    def two_platforms(self, order=("linux", "windows")):
        before = self.read("revali.toml")
        after = before.replace(
            'platforms = ["linux"]', "platforms = [%s]" % ", ".join('"%s"' % p for p in order)
        )
        self.assertNotEqual(before, after, "fixture platforms line not found")
        self.write("revali.toml", after + WINDOWS_TABLE)
        self.commit_all("list %s" % ", ".join(order))

    def validate_key(self, line):
        before = self.read("revali.toml")
        self.assertIn("[validate]\n", before)
        self.write("revali.toml", before.replace("[validate]\n", "[validate]\n%s\n" % line, 1))
        self.commit_all("config: " + line)

    def fail(self, results, introduced_by="branch", diagnoses=1):
        """One `revali run` whose validation FAILs as `results` says, with `diagnoses`
        diagnosis answers queued after the reviewer's."""
        self.runner_scenario({"default": 0, "results": results})
        self.claude(
            claude_entry(),
            *[claude_entry(diagnosis(introduced_by), write_tests=False) for _ in range(diagnoses)],
        )
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ACTION, out)
        self.assertNotIn("Traceback", out)
        return out

    def prompt(self, tag):
        return self.read("%s/prompt-diagnose-%s.md" % (LOGS, tag))

    def intro(self, tag):
        """The prompt before its first section: where it says what failed."""
        return self.prompt(tag).split("\n## ", 1)[0]

    def base_section(self, tag):
        """The prompt's section about the base branch, up to the instructions."""
        prompt = self.prompt(tag)
        self.assertIn("on the base branch", prompt)
        return prompt.split("on the base branch", 1)[1].split("\n# ", 1)[0]

    def body(self, tag):
        return self.read("%s/issue-%s.md" % (LOGS, tag))

    def evidence(self, tag):
        return self.body(tag).split("## Evidence", 1)[1].split("\n## ", 1)[0]

    def labels(self):
        return [c["label"] for c in self.fake_calls("runner")]


class PromptOnePlatform(Case):
    def test_names_the_only_platform_lists_no_others_and_keeps_the_names(self):
        self.fail({"validate-r1": {"new_test": 1}})
        intro, base = self.intro("1"), self.base_section("1")
        # AC-1: holds with one platform listed too
        self.assertTrue(lines_with(intro, "linux"), intro)
        self.assertTrue(lines_with(intro, "local"), intro)
        self.assertTrue(lines_with(base, "linux"), base)
        # AC-2: with one platform there is no list of other platforms
        self.assertFalse(lines_with(intro, "windows"), intro)
        self.assertNotRegex(intro, r"\b(PASS|FAIL)\b")
        self.assertFalse(lines_with(self.prompt("1"), "linux", "PASS"))
        self.assertNotRegex(self.prompt("1"), r"\$[a-z_]+")  # every placeholder filled
        # AC-4: labels, file names and tests.md are those of a one-platform run
        self.assertEqual(
            sorted(self.labels()), sorted(["baseline", "smoke-r1-1", "validate-r1", "base-r1"])
        )
        self.assertTrue(os.path.isfile(os.path.join(self.rdir(), "diagnose-1.json")))
        logs = os.listdir(os.path.join(self.repo, LOGS))
        self.assertIn("validate-r1-new_test.log", logs)
        self.assertIn("base-r1-new_test.log", logs)
        md = self.read(TESTS_MD)
        self.assertIn("## Validation 1", md)
        self.assertNotIn("### linux", md)
        self.assertIn("### Failure output (new_test, last 40 lines)", md)


class PromptTwoPlatforms(Case):
    def test_the_failing_platform_is_named_and_the_other_listed_as_pass(self):
        self.two_platforms()
        self.fail({"validate-r1-windows": {"new_test": 1}})
        self.assertFalse(
            os.path.exists(os.path.join(self.repo, LOGS, "prompt-diagnose-1-linux.md"))
        )
        intro, base = self.intro("1-windows"), self.base_section("1-windows")
        prompt = self.prompt("1-windows")
        # AC-1: the platform and runner of the sandbox that failed, not the first listed one
        self.assertTrue(lines_with(intro, "windows"), intro)
        self.assertTrue(lines_with(intro, "ssh"), intro)
        self.assertFalse(lines_with(intro, "local"), intro)
        self.assertTrue(lines_with(base, "windows"), base)
        self.assertFalse(lines_with(base, "linux"), base)
        # AC-2: the other platform with its result; the failed one is not said to pass
        self.assertTrue(lines_with(prompt, "linux", "PASS"), prompt)
        self.assertFalse(lines_with(prompt, "windows", "PASS"), prompt)
        self.assertFalse(lines_with(prompt, "linux", "FAIL"), prompt)
        self.assertNotRegex(prompt, r"\$[a-z_]+")

    def test_the_first_platform_failing_lists_the_second_as_pass(self):
        self.two_platforms()
        self.fail({"validate-r1-linux": {"new_test": 1}})
        intro, prompt = self.intro("1-linux"), self.prompt("1-linux")
        self.assertTrue(lines_with(intro, "linux"), intro)
        self.assertTrue(lines_with(intro, "local"), intro)
        self.assertFalse(lines_with(intro, "ssh"), intro)
        self.assertTrue(lines_with(self.base_section("1-linux"), "linux"))
        # the second platform ran after the first failed, and its result is in the prompt
        self.assertTrue(lines_with(prompt, "windows", "PASS"), prompt)
        self.assertFalse(lines_with(prompt, "linux", "PASS"), prompt)

    def test_the_listed_order_does_not_decide_which_platform_is_named(self):
        self.two_platforms(order=("windows", "linux"))
        self.fail({"validate-r1-linux": {"new_test": 1}})
        intro = self.intro("1-linux")
        self.assertTrue(lines_with(intro, "linux"), intro)
        self.assertTrue(lines_with(intro, "local"), intro)
        self.assertTrue(lines_with(self.prompt("1-linux"), "windows", "PASS"))

    def test_both_failing_list_each_other_as_fail_with_the_step(self):
        self.two_platforms()
        self.fail(
            {"validate-r1-linux": {"new_test": 1}, "validate-r1-windows": {"new_test": 1}},
            diagnoses=2,
        )
        linux, windows = self.prompt("1-linux"), self.prompt("1-windows")
        self.assertTrue(lines_with(self.intro("1-linux"), "local"))
        self.assertTrue(lines_with(self.intro("1-windows"), "ssh"))
        # AC-2: FAIL with the failed step
        self.assertTrue(lines_with(linux, "windows", "FAIL", "new_test"), linux)
        self.assertTrue(lines_with(windows, "linux", "FAIL", "new_test"), windows)
        self.assertFalse(lines_with(linux, "windows", "PASS"), linux)
        self.assertFalse(lines_with(windows, "linux", "PASS"), windows)

    def test_the_step_listed_is_the_other_platforms_own(self):
        # linux fails in the existing suite, windows in the reviewer's tests: each prompt
        # names the step the other platform failed at, not its own
        self.validate_key("reuse_baseline = false")
        self.two_platforms()
        self.fail(
            {"validate-r1-linux": {"test": 1}, "validate-r1-windows": {"new_test": 1}},
            diagnoses=2,
        )
        linux, windows = self.prompt("1-linux"), self.prompt("1-windows")
        about_linux = lines_with(windows, "linux", "FAIL", "test")
        self.assertTrue(about_linux, windows)
        self.assertFalse(lines_with(windows, "linux", "FAIL", "new_test"), windows)
        self.assertTrue(lines_with(linux, "windows", "FAIL", "new_test"), linux)
        # no base rerun for a suite failure, and the prompt still names where it failed
        self.assertNotIn("base-r1-linux", self.labels())
        self.assertTrue(lines_with(self.intro("1-linux"), "linux"))
        self.assertTrue(lines_with(self.intro("1-linux"), "local"))


class IssueBody(Case):
    def test_one_platform_names_it_on_the_branch_line_and_the_base_line(self):
        self.fail(
            {"validate-r1": {"new_test": 1}, "base-r1": {"new_test": 1}}, introduced_by="base"
        )
        evidence = self.evidence("1")
        branch = [ln for ln in evidence.splitlines() if ln.startswith("On the branch")]
        base = [ln for ln in evidence.splitlines() if ln.startswith("Base branch")]
        self.assertEqual((len(branch), len(base)), (1, 1), evidence)
        for line in branch + base:  # AC-3: both lines, platform and runner
            self.assertTrue(lines_with(line, "linux"), line)
            self.assertTrue(lines_with(line, "local"), line)
        # the sentences that were there are still there
        self.assertIn("`new_test` exit 1", branch[0])
        self.assertIn("`main`", base[0])
        self.assertIn("new_test exit 1", base[0])
        self.assertNotRegex(self.body("1"), r"\$[a-z_]+")

    def test_the_second_platform_issue_names_the_second_platform(self):
        self.two_platforms()
        self.fail(
            {"validate-r1-windows": {"new_test": 1}, "base-r1-windows": {"new_test": 1}},
            introduced_by="base",
        )
        evidence = self.evidence("1-windows")
        branch = [ln for ln in evidence.splitlines() if ln.startswith("On the branch")]
        base = [ln for ln in evidence.splitlines() if ln.startswith("Base branch")]
        self.assertEqual((len(branch), len(base)), (1, 1), evidence)
        for line in branch + base:
            self.assertTrue(lines_with(line, "windows"), line)
            self.assertTrue(lines_with(line, "ssh"), line)
            self.assertFalse(lines_with(line, "linux"), line)
            self.assertFalse(lines_with(line, "local"), line)

    def test_the_shipped_template_names_the_platform_outside_the_evidence(self):
        # AC-3: templates/issue.md has a `$platform` placeholder of its own
        self.two_platforms()
        self.fail(
            {"validate-r1-windows": {"new_test": 1}, "base-r1-windows": {"new_test": 1}},
            introduced_by="base",
        )
        body = self.body("1-windows")
        rest = body.replace(self.evidence("1-windows"), "")
        self.assertTrue(lines_with(rest, "windows"), rest)
        self.assertNotRegex(body, r"\$[a-z_]+")

    def project_template(self):
        self.write("docs/bug.md", "seen on [$platform]\n\n$evidence\n")
        self.validate_key('issue_template = "docs/bug.md"')

    def test_a_project_template_gets_the_platform_with_one_listed(self):
        self.project_template()
        self.fail(
            {"validate-r1": {"new_test": 1}, "base-r1": {"new_test": 1}}, introduced_by="base"
        )
        body = self.body("1")
        self.assertTrue(body.startswith("seen on [linux]\n"), body[:80])

    def test_a_project_template_gets_the_failing_platform_with_two_listed(self):
        self.project_template()
        self.two_platforms()
        self.fail(
            {"validate-r1-windows": {"new_test": 1}, "base-r1-windows": {"new_test": 1}},
            introduced_by="base",
        )
        body = self.body("1-windows")
        self.assertTrue(body.startswith("seen on [windows]\n"), body[:80])


class Version(unittest.TestCase):
    def test_prompt_version_moved_past_9(self):
        self.assertGreaterEqual(int(PROMPT_VERSION), 10)  # AC-4


if __name__ == "__main__":
    unittest.main()
