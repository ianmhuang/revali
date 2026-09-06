"""Validation reruns the reviewer's test files on the base branch when they fail on the
branch, so the diagnosis session sees both outputs and answers `introduced_by`."""

import json
import os
import types
import unittest

from revali import EXIT_ACTION, EXIT_OK, PROMPT_VERSION
from revali.review import under_test_dir
from revali.state import State
from revali.validate import BaseRerun, baseline_reusable, rerun_on_base
from tests.fixtures.make_sample_repo import LOCAL_NEW_TEST, LOCAL_TEST, PY, toml_str
from tests.helpers import ROOT, RepoCase, approve_response, claude_entry, git, run_cli

TESTS_MD = ".revali/feature__mul/tests.md"
REVALI_LOG = ".revali/feature__mul/logs/revali.log"
BRANCH_OUT = "AssertionError: 12 != 7 (branch)"
BASE_OUT = "AssertionError: 12 != 7 (base)"
SUITE_REASON = "existing suite failed; base rerun applies to the reviewer's tests only"


def diagnosis(cause="code", introduced_by="base", **kw):
    data = {
        "summary": "mul returns a + b instead of a * b, so the product test fails.",
        "cause": cause,
        "introduced_by": introduced_by,
        "failures": [
            {
                "test": "tests/test_review_mul.py::MulTests::test_product",
                "cause": cause,
                "introduced_by": introduced_by,
                "note": "expected 12, got 7",
            }
        ],
        "recommendation": "return a * b",
    }
    data.update(kw)
    return data


def failing_new_test(extra=None):
    sc = {
        "default": 0,
        "results": {"validate-r1": {"new_test": 1}, "base-r1": {"new_test": 1}},
        "outputs": {"validate-r1": {"new_test": BRANCH_OUT}, "base-r1": {"new_test": BASE_OUT}},
    }
    if extra:
        for key, value in extra.items():
            sc.setdefault(key, {}).update(value)
    return sc


def read(*parts):
    with open(os.path.join(ROOT, *parts), "r", encoding="utf-8") as fh:
        return fh.read()


class Rerun(RepoCase):
    def calls(self, label):
        return [c for c in self.fake_calls("runner") if c["label"] == label]

    def base_sha(self):
        return git(["rev-parse", "origin/main"], self.repo).strip()

    def set_key(self, value):
        toml = self.read("revali.toml").replace(
            "[validate]\n", "[validate]\nrerun_on_base = %s\n" % value
        )
        self.write("revali.toml", toml)
        self.commit_all("set rerun_on_base")

    def test_failing_reviewer_tests_rerun_on_the_base_tip(self):
        self.runner_scenario(failing_new_test())
        self.claude(claude_entry(), claude_entry(diagnosis(), write_tests=False))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ACTION, out)
        base = self.base_sha()
        calls = self.calls("base-r1")
        self.assertEqual(len(calls), 1)  # AC-1
        self.assertEqual(calls[0]["ref"], base)
        self.assertEqual(calls[0]["steps"], ["new_test"])
        self.assertEqual(calls[0]["extra_files"], ["tests/test_review_mul.py"])
        # the label's runner call came after the failing validation run
        labels = [c["label"] for c in self.fake_calls("runner")]
        self.assertLess(labels.index("validate-r1"), labels.index("base-r1"))
        tests_md = self.read(TESTS_MD)
        section = tests_md.split("## Validation 1")[1]
        self.assertIn("### Base rerun (%s)" % base[:10], section)  # AC-4
        self.assertIn("| new_test | 1 | `base-r1-new_test.log` |", section)
        self.assertIn(BASE_OUT, section)
        self.assertIn(BRANCH_OUT, section)
        self.assertTrue(self.exists(".revali/feature__mul/logs/base-r1-new_test.log"))  # AC-7
        self.assertRegex(self.read(REVALI_LOG), r"sandbox .*base-r1 \d+s")  # AC-7 timing line
        # AC-5: the diagnoser sees both
        prompt = self.fake_calls("claude")[1]["prompt"]
        self.assertIn(base, prompt)
        self.assertIn("main", prompt)
        self.assertIn(BASE_OUT, prompt)
        self.assertIn(BRANCH_OUT, prompt)
        self.assertIn("`new_test` exited 1 on base", prompt)
        self.assertIn("introduced_by", prompt)
        # AC-6: the answer's introduced_by lands everywhere
        self.assertIn("introduced by: **base**", section)
        self.assertIn("test_product`: code, introduced by base.", section)
        diag = json.loads(self.read(".revali/feature__mul/diagnose-1.json"))
        self.assertEqual(diag["data"]["introduced_by"], "base")
        self.assertEqual(State.load(self.rdir()).validations[0]["introduced_by"], "base")
        self.assertIn("introduced by: base", out)
        self.assertEqual(State.load(self.rdir()).last_verdict, "FAIL")

    def test_files_placeholder_names_the_reviewer_files_on_base(self):
        cfg = self.read("revali.toml").replace(
            "new_test = %s" % toml_str(LOCAL_NEW_TEST),
            "new_test = %s" % toml_str(PY + " -m unittest {files}"),
        )
        self.write("revali.toml", cfg)
        self.commit_all("files placeholder")
        self.runner_scenario(failing_new_test())
        self.claude(claude_entry(), claude_entry(diagnosis(), write_tests=False))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ACTION, out)
        calls = self.calls("base-r1")
        self.assertEqual(len(calls), 1)
        self.assertIn("tests/test_review_mul.py", calls[0]["cmds"]["new_test"])  # AC-1

    def test_pass_does_not_rerun(self):
        self.claude(claude_entry())
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertEqual(self.calls("base-r1"), [])  # AC-2
        self.assertNotIn("Base rerun", self.read(TESTS_MD))

    def test_existing_suite_failure_does_not_rerun(self):
        toml = self.read("revali.toml").replace(
            "[validate]\n", "[validate]\nreuse_baseline = false\n"
        )
        self.write("revali.toml", toml)
        self.commit_all("always run the suite")
        self.runner_scenario({"default": 0, "results": {"validate-r1": {"test": 1}}})
        self.claude(claude_entry(), claude_entry(diagnosis("code", "unknown"), write_tests=False))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ACTION, out)
        self.assertEqual(self.calls("base-r1"), [])  # AC-2
        self.assertIn(SUITE_REASON, self.read(TESTS_MD))
        self.assertIn(SUITE_REASON, self.read(REVALI_LOG))
        self.assertIn(SUITE_REASON, self.fake_calls("claude")[1]["prompt"])

    def test_key_false_does_not_rerun(self):
        self.set_key("false")
        self.runner_scenario(failing_new_test())
        self.claude(claude_entry(), claude_entry(diagnosis("code", "unknown"), write_tests=False))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ACTION, out)
        self.assertEqual(self.calls("base-r1"), [])  # AC-2
        self.assertIn("rerun_on_base", self.read(TESTS_MD))
        self.assertIn("rerun_on_base", self.read(REVALI_LOG))

    def test_no_reviewer_file_does_not_rerun(self):
        # the reviewer covers both AC without a file; new_test still runs its pattern and fails
        self.runner_scenario(failing_new_test())
        self.claude(
            claude_entry(
                approve_response(
                    tests=[],
                    not_testable=[
                        {"ac": "AC-1", "reason": "checked by hand"},
                        {"ac": "AC-2", "reason": "checked by hand"},
                    ],
                ),
                write_tests=False,
            ),
            claude_entry(diagnosis("code", "unknown"), write_tests=False),
        )
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ACTION, out)
        self.assertEqual(State.load(self.rdir()).test_files, [])
        self.assertEqual(self.calls("base-r1"), [])  # AC-2
        self.assertIn("no reviewer test file", self.read(TESTS_MD))

    def test_setup_failure_on_base_is_unavailable_not_stop(self):
        self.write(
            "revali.toml", self.read("revali.toml").replace('setup = ""', 'setup = "pip install x"')
        )
        self.commit_all("setup")
        self.runner_scenario(failing_new_test({"results": {"base-r1": {"setup": 1}}}))
        self.claude(claude_entry(), claude_entry(diagnosis("code", "unknown"), write_tests=False))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ACTION, out)  # AC-3: exit code unchanged
        self.assertNotIn("environment problem", out)
        section = self.read(TESTS_MD).split("## Validation 1")[1]
        self.assertIn("### Base rerun", section)
        self.assertIn("unavailable", section)
        self.assertIn("setup failed on base (exit 1)", section)
        self.assertNotIn(BASE_OUT, section)
        prompt = self.fake_calls("claude")[1]["prompt"]  # the diagnosis still ran
        self.assertIn("setup failed on base", prompt)
        self.assertEqual(State.load(self.rdir()).validations[0]["result"], "FAIL")

    def test_timeout_on_base_is_unavailable(self):
        self.runner_scenario(failing_new_test({"results": {"base-r1": {"new_test": 124}}}))
        self.claude(claude_entry(), claude_entry(diagnosis("code", "unknown"), write_tests=False))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ACTION, out)  # AC-3
        section = self.read(TESTS_MD).split("## Validation 1")[1]
        self.assertIn("unavailable", section)
        self.assertIn("timed out", section)

    def test_sandbox_error_on_base_is_unavailable(self):
        self.runner_scenario(failing_new_test({"errors": {"base-r1": "clone exploded"}}))
        self.claude(claude_entry(), claude_entry(diagnosis("code", "unknown"), write_tests=False))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ACTION, out)  # AC-3
        section = self.read(TESTS_MD).split("## Validation 1")[1]
        self.assertIn("unavailable", section)
        self.assertIn("clone exploded", section)
        self.assertIn("clone exploded", self.fake_calls("claude")[1]["prompt"])
        self.assertEqual(len(self.fake_calls("claude")), 2)

    def test_answer_without_introduced_by_is_a_schema_mismatch(self):
        self.runner_scenario(failing_new_test())
        bad = diagnosis()
        del bad["introduced_by"]
        self.claude(claude_entry(), claude_entry(bad, write_tests=False))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ACTION, out)
        self.assertIn(
            "diagnosis unavailable: diagnoser output did not match the schema", out
        )  # AC-6
        self.assertEqual(State.load(self.rdir()).validations[0]["introduced_by"], "")

    def test_answer_with_an_unknown_value_is_a_schema_mismatch(self):
        self.runner_scenario(failing_new_test())
        self.claude(claude_entry(), claude_entry(diagnosis(introduced_by="me"), write_tests=False))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ACTION, out)
        self.assertIn("did not match the schema", out)  # AC-6


class PublicComment(RepoCase):
    def setUp(self):
        super().setUp()
        self.scenario({"visibility": "PUBLIC"})

    def comment(self):
        return self.read(".revali/feature__mul/logs/comment-validate-1.md")

    def test_one_line_and_the_base_flag(self):
        self.runner_scenario(failing_new_test())
        self.claude(claude_entry(), claude_entry(diagnosis(), write_tests=False))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ACTION, out)
        c = self.comment()
        base = git(["rev-parse", "origin/main"], self.repo).strip()
        self.assertIn("on base %s: new_test exit 1" % base[:10], c)  # AC-4
        self.assertNotIn(BASE_OUT, c)
        self.assertNotIn(BRANCH_OUT, c)
        self.assertIn("introduced by **base**", c)  # AC-6

    def test_branch_answer_carries_no_flag(self):
        self.runner_scenario(failing_new_test({"results": {"base-r1": {"new_test": 0}}}))
        self.claude(claude_entry(), claude_entry(diagnosis("code", "branch"), write_tests=False))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ACTION, out)
        c = self.comment()
        self.assertIn("new_test exit 0", c)
        self.assertNotIn("introduced by", c)  # AC-6: only `base` is flagged
        self.assertIn("introduced by: **branch**", self.read(TESTS_MD))

    def test_reason_when_it_did_not_run(self):
        self.runner_scenario(failing_new_test({"errors": {"base-r1": "clone exploded"}}))
        self.claude(claude_entry(), claude_entry(diagnosis("code", "unknown"), write_tests=False))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ACTION, out)
        self.assertIn("on base", self.comment())
        self.assertIn("clone exploded", self.comment())


class SkippedReason(RepoCase):
    """AC-9: an empty `test` with a skipped `new_test` names both reasons."""

    def configure(self, new_test):
        cfg = self.read("revali.toml").replace("test = %s" % toml_str(LOCAL_TEST), 'test = ""')
        cfg = cfg.replace("new_test = %s" % toml_str(LOCAL_NEW_TEST), "new_test = %s" % new_test)
        self.write("revali.toml", cfg)
        self.commit_all("no suite")

    def no_file_review(self):
        return claude_entry(
            approve_response(
                tests=[],
                not_testable=[
                    {"ac": "AC-1", "reason": "checked by hand"},
                    {"ac": "AC-2", "reason": "checked by hand"},
                ],
            ),
            write_tests=False,
        )

    def test_new_test_skipped_for_no_file(self):
        self.configure(toml_str(PY + " -m unittest {files}"))
        self.claude(self.no_file_review())
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn(
            "nothing to run: no test command and new_test names no test file", self.read(TESTS_MD)
        )

    def test_new_test_without_the_placeholder_still_runs(self):
        # an empty `test` with a pattern new_test: the step runs, nothing is skipped, so no
        # "nothing to run" reason at all (preflight requires new_test; `no test command`
        # alone cannot occur)
        self.configure(toml_str(LOCAL_NEW_TEST))
        self.claude(self.no_file_review())
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertNotIn("nothing to run", self.read(TESTS_MD))
        self.assertIn("| new_test | 0 |", self.read(TESTS_MD))


class Unit(unittest.TestCase):
    """rerun_on_base's decisions on a hand-built context."""

    def ctx(self, key=True):
        return types.SimpleNamespace(
            cfg=types.SimpleNamespace(validate=types.SimpleNamespace(rerun_on_base=key)),
            base="main",
            base_sha="0123456789abcdef",
            repo_root=os.path.join(ROOT, "does-not-exist"),
        )

    def failed(self, name):
        return types.SimpleNamespace(name=name)

    def test_reasons(self):
        st = State()
        st.test_files = ["tests/test_review_x.py"]
        r = rerun_on_base(self.ctx(False), st, self.failed("new_test"), 1, None)
        self.assertIsInstance(r, BaseRerun)
        self.assertFalse(r.available)
        self.assertIn("rerun_on_base", r.reason)
        r = rerun_on_base(self.ctx(), st, self.failed("test"), 1, None)
        self.assertEqual(r.reason, SUITE_REASON)
        r = rerun_on_base(self.ctx(), State(), self.failed("new_test"), 1, None)
        self.assertIn("no reviewer test file", r.reason)
        # files recorded but gone from the working tree count as none
        r = rerun_on_base(self.ctx(), st, self.failed("new_test"), 1, None)
        self.assertIn("no reviewer test file", r.reason)


class UnderTestDir(RepoCase):
    """AC-10: baseline_reusable and review share one test_dir check."""

    with_remote = False

    def test_helper_is_shared_and_normalises(self):
        self.assertTrue(under_test_dir("tests/test_a.py", "tests"))
        self.assertTrue(under_test_dir("tests/test_a.py", "tests/"))
        self.assertTrue(under_test_dir("tests\\test_a.py", "tests\\"))
        self.assertFalse(under_test_dir("tests_extra/test_a.py", "tests"))
        self.assertFalse(under_test_dir("src/x.py", "tests"))

    def test_baseline_reusable_agrees_for_every_spelling(self):
        st = State()
        st.baseline_sha = git(["rev-parse", "HEAD"], self.repo).strip()
        path = os.path.join(self.repo, "tests", "test_review_x.py")
        with open(path, "a", encoding="utf-8", newline="\n") as fh:
            fh.write("# more\n")
        git(["add", "--", "tests/test_review_x.py"], self.repo)
        git(["commit", "-q", "-m", "test: x\n\nRevali-Round: 1\n"], self.repo)
        for spelling in ("tests", "tests/", "tests\\", "tests//"):
            ctx = types.SimpleNamespace(
                repo_root=self.repo,
                head_sha=git(["rev-parse", "HEAD"], self.repo).strip(),
                cfg=types.SimpleNamespace(
                    validate=types.SimpleNamespace(reuse_baseline=True),
                    project=types.SimpleNamespace(test_dir=spelling),
                ),
            )
            self.assertEqual(baseline_reusable(ctx, st), st.baseline_sha, spelling)


class DocsAndSchema(unittest.TestCase):
    def test_prompt_version_is_six(self):
        self.assertGreaterEqual(int(PROMPT_VERSION), 6)  # AC-5

    def test_schema_requires_introduced_by(self):
        schema = json.loads(read("schemas", "diagnose.schema.json"))
        enum = ["branch", "base", "unknown"]
        self.assertIn("introduced_by", schema["required"])  # AC-5
        self.assertEqual(schema["properties"]["introduced_by"]["enum"], enum)
        item = schema["properties"]["failures"]["items"]
        self.assertIn("introduced_by", item["required"])
        self.assertEqual(item["properties"]["introduced_by"]["enum"], enum)

    def test_prompt_defines_the_values(self):
        text = read("prompts", "diagnose.md")
        for token in ("introduced_by", "`branch`", "`base`", "`unknown`", "$base_sha"):
            self.assertIn(token, text)  # AC-5

    def test_key_in_defaults_and_template(self):
        import tomllib

        defaults = tomllib.loads(read("defaults.toml"))
        self.assertIs(defaults["validate"]["rerun_on_base"], True)  # AC-8
        template = tomllib.loads(read("templates", "revali.toml"))
        self.assertIn("rerun_on_base", template["validate"])

    def test_docs_mention_it(self):
        self.assertIn("rerun_on_base", read("docs", "configuration.md"))  # AC-8
        self.assertIn("Base rerun", read("docs", "files.md"))
        sandbox = read("docs", "sandbox.md")
        self.assertIn("base", sandbox)
        self.assertIn("introduced_by", sandbox)
        readme = read("README.md")
        row = [ln for ln in readme.splitlines() if ln.startswith("| Validator")][0]
        self.assertIn("base", row)

    def test_reset_is_described_as_redoing_the_baseline(self):
        text = read("docs", "configuration.md")
        self.assertNotIn("`revali reset` all bring the full suite back", text)  # AC-11
        idx = text.index("revali reset")
        self.assertIn("baseline", text[idx : idx + 200])


if __name__ == "__main__":
    unittest.main()
