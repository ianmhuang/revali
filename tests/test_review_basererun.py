"""Acceptance tests for the base rerun: after the reviewer's tests fail in validation, the
same files run once more on the base tip and the diagnosis carries `introduced_by`.
Covers AC-1, AC-2, AC-3, AC-4, AC-6, AC-7. Every case drives `revali run` end to end on
the fixture repository with the fake runner and the fake claude."""

import json
import os
import shutil
import tempfile
import types
import unittest

from revali import EXIT_ACTION, EXIT_OK
from revali.config import PlatformCfg
from revali.state import State
from revali.validate import rerun_on_base
from tests.fixtures.make_sample_repo import LOCAL_NEW_TEST, PY, toml_str
from tests.helpers import RepoCase, approve_response, claude_entry, git, run_cli

RDIR = ".revali/feature__mul"
TESTS_MD = RDIR + "/tests.md"
REVALI_LOG = RDIR + "/logs/revali.log"
REVIEWER_FILE = "tests/test_review_mul.py"
BRANCH_OUT = "AssertionError: 12 != 7 [on the branch]"
BASE_OUT = "ImportError: cannot import name 'mul' [on base]"
SUITE_REASON = "existing suite failed; base rerun applies to the reviewer's tests only"


def diagnosis(introduced_by="base", cause="code"):
    return {
        "summary": "the product test fails because mul is wrong.",
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


def scenario(base_results=None, base_outputs=None, errors=None, label="r1"):
    """validate-<label> fails in new_test; base-<label> fails in new_test unless overridden."""
    sc = {
        "default": 0,
        "results": {
            "validate-" + label: {"new_test": 1},
            "base-" + label: dict(base_results or {"new_test": 1}),
        },
        "outputs": {
            "validate-" + label: {"new_test": BRANCH_OUT + "\n"},
            "base-" + label: dict(base_outputs or {"new_test": BASE_OUT + "\n"}),
        },
    }
    if errors:
        sc["errors"] = errors
    return sc


class BaseRerunCase(RepoCase):
    def base_sha(self):
        return git(["rev-parse", "origin/main"], self.repo).strip()

    def head_sha(self):
        return git(["rev-parse", "HEAD"], self.repo).strip()

    def runner_calls(self, label):
        return [c for c in self.fake_calls("runner") if c["label"] == label]

    def validation_section(self, n=1):
        text = self.read(TESTS_MD)
        self.assertIn("## Validation %d" % n, text)
        return text.split("## Validation %d" % n, 1)[1]

    def set_validate_key(self, line):
        cfg = self.read("revali.toml").replace("[validate]\n", "[validate]\n%s\n" % line)
        self.write("revali.toml", cfg)
        self.commit_all("validate key")

    def run_failing(self, diag=None, sc=None):
        self.runner_scenario(sc or scenario())
        self.claude(claude_entry(), claude_entry(diag or diagnosis(), write_tests=False))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ACTION, out)
        return out


class TheRerun(BaseRerunCase):
    def test_runs_the_reviewer_files_on_the_base_tip(self):
        # AC-1: one sandbox session labelled base-r1 on base_sha (not HEAD), with the
        # reviewer's file as an extra file, running setup, build, new_test
        self.run_failing()
        calls = self.runner_calls("base-r1")
        self.assertEqual(len(calls), 1, self.fake_calls("runner"))
        call = calls[0]
        self.assertEqual(call["ref"], self.base_sha())
        self.assertNotEqual(call["ref"], self.head_sha())
        self.assertEqual(call["extra_files"], [REVIEWER_FILE])
        self.assertIn("new_test", call["steps"])
        self.assertNotIn("test", call["steps"])  # the existing suite is not part of it
        self.assertEqual(call["scope"], "feature__mul")
        # it follows the failing validation session
        labels = [c["label"] for c in self.fake_calls("runner")]
        self.assertLess(labels.index("validate-r1"), labels.index("base-r1"))

    def test_files_placeholder_names_the_reviewer_files(self):
        # AC-1: `{files}` in new_test expands to the same file set on base
        cfg = self.read("revali.toml").replace(
            "new_test = %s" % toml_str(LOCAL_NEW_TEST),
            "new_test = %s" % toml_str(PY + " -m unittest {files}"),
        )
        self.write("revali.toml", cfg)
        self.commit_all("new_test with {files}")
        self.run_failing()
        calls = self.runner_calls("base-r1")
        self.assertEqual(len(calls), 1)
        self.assertIn(REVIEWER_FILE, calls[0]["cmds"]["new_test"])
        self.assertNotIn("{files}", calls[0]["cmds"]["new_test"])

    def test_label_follows_the_round(self):
        # AC-1 / AC-7: a validation in round 2 reruns under base-r2
        cr = approve_response(
            verdict="CHANGES_REQUESTED",
            findings=[
                {
                    "id": "F1",
                    "file": "src/calc.py",
                    "line": 1,
                    "severity": "high",
                    "kind": "correctness",
                    "text": "wrong",
                    "suggestion": "fix it",
                }
            ],
        )
        self.runner_scenario(scenario(label="r2"))
        self.claude(claude_entry(cr))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ACTION, out)
        self.write("src/calc.py", self.read("src/calc.py") + "\n# fix\n")
        self.commit_all("fix")
        self.claude(claude_entry(write_tests=False), claude_entry(diagnosis(), write_tests=False))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ACTION, out)
        self.assertEqual(len(self.runner_calls("base-r2")), 1, self.fake_calls("runner"))
        self.assertEqual(self.runner_calls("base-r1"), [])
        self.assertTrue(self.exists(RDIR + "/logs/base-r2-new_test.log"))
        self.assertIn("### Base rerun (%s)" % self.base_sha()[:10], self.validation_section(1))

    def test_never_changes_the_result(self):
        # AC-3 (and the Goal): a green base rerun still leaves the validation FAIL, exit 2
        self.run_failing(
            diagnosis("branch"), scenario(base_results={"new_test": 0}, base_outputs={})
        )
        st = State.load(self.rdir())
        self.assertEqual(st.last_verdict, "FAIL")
        self.assertEqual(st.validations[0]["result"], "FAIL")
        self.assertEqual(st.validations[0]["failed_step"], "new_test")
        self.assertIn("| new_test | 0 | `base-r1-new_test.log` |", self.validation_section())


class WhenItDoesNotRun(BaseRerunCase):
    def test_not_after_a_pass(self):
        self.claude(claude_entry())
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertEqual(self.runner_calls("base-r1"), [])  # AC-2
        self.assertNotIn("Base rerun", self.read(TESTS_MD))

    def test_not_when_the_existing_suite_failed(self):
        self.set_validate_key("reuse_baseline = false")
        self.runner_scenario(
            {
                "default": 0,
                "results": {"validate-r1": {"test": 1}},
                "outputs": {"validate-r1": {"test": "FAILED test_add\n"}},
            }
        )
        self.claude(claude_entry(), claude_entry(diagnosis("unknown"), write_tests=False))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ACTION, out)
        self.assertEqual(self.runner_calls("base-r1"), [])  # AC-2
        self.assertIn(SUITE_REASON, self.validation_section())
        self.assertIn(SUITE_REASON, self.read(REVALI_LOG))
        self.assertEqual(State.load(self.rdir()).validations[0]["failed_step"], "test")

    def test_not_when_the_key_is_false(self):
        self.set_validate_key("rerun_on_base = false")
        self.run_failing(diagnosis("unknown"))
        self.assertEqual(self.runner_calls("base-r1"), [])  # AC-2
        self.assertIn("rerun_on_base", self.validation_section())
        self.assertIn("rerun_on_base", self.read(REVALI_LOG))

    def test_not_when_the_reviewer_wrote_no_file(self):
        self.runner_scenario(scenario())
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
            claude_entry(diagnosis("unknown"), write_tests=False),
        )
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ACTION, out)
        self.assertEqual(State.load(self.rdir()).test_files, [])
        self.assertEqual(self.runner_calls("base-r1"), [])  # AC-2
        self.assertIn("no reviewer test file", self.validation_section())
        self.assertIn("no reviewer test file", self.read(REVALI_LOG))


class WhenItIsUnavailable(BaseRerunCase):
    """AC-3: the rerun's own problems are recorded, the run goes on to the diagnosis, the
    result stays FAIL with exit 2 and no environment-problem stop."""

    def test_setup_failing_on_base(self):
        self.write(
            "revali.toml",
            self.read("revali.toml").replace('setup = ""', 'setup = "pip install nothing"'),
        )
        self.commit_all("setup step")
        out = self.run_failing(diagnosis("unknown"), scenario(base_results={"setup": 1}))
        self.assertNotIn("environment problem", out)
        section = self.validation_section()
        self.assertIn("### Base rerun", section)
        self.assertIn("unavailable", section)
        self.assertIn("setup failed on base (exit 1)", section)
        self.assertNotIn(BASE_OUT, section)
        self.assertEqual(len(self.fake_calls("claude")), 2)  # the diagnosis still ran
        self.assertIn("setup failed on base", self.read(RDIR + "/logs/prompt-diagnose-1.md"))
        self.assertEqual(State.load(self.rdir()).validations[0]["result"], "FAIL")

    def test_build_failing_on_base(self):
        self.write(
            "revali.toml",
            self.read("revali.toml").replace('setup = ""', 'setup = ""\nbuild = "make"'),
        )
        self.commit_all("build step")
        self.run_failing(diagnosis("unknown"), scenario(base_results={"build": 2}))
        section = self.validation_section()
        self.assertIn("build failed on base (exit 2)", section)
        self.assertIn("unavailable", section)
        self.assertEqual(len(self.fake_calls("claude")), 2)

    def test_sandbox_error_on_base(self):
        self.run_failing(diagnosis("unknown"), scenario(errors={"base-r1": "clone blew up"}))
        section = self.validation_section()
        self.assertIn("unavailable", section)
        self.assertIn("clone blew up", section)
        self.assertIn("clone blew up", self.read(RDIR + "/logs/prompt-diagnose-1.md"))
        self.assertEqual(len(self.fake_calls("claude")), 2)
        self.assertEqual(State.load(self.rdir()).last_verdict, "FAIL")

    def test_new_test_timing_out_on_base(self):
        self.run_failing(diagnosis("unknown"), scenario(base_results={"new_test": 124}))
        section = self.validation_section()
        self.assertIn("unavailable", section)
        self.assertIn("timed out", section)
        self.assertNotIn("new_test output on base", section)
        self.assertEqual(len(self.fake_calls("claude")), 2)


class TestsMdAndComments(BaseRerunCase):
    def test_tests_md_block(self):
        # AC-4: header with the 10-char sha, the step table with the log file, the output
        self.run_failing()
        section = self.validation_section()
        base = self.base_sha()
        self.assertIn("### Base rerun (%s)" % base[:10], section)
        self.assertIn("| new_test | 1 | `base-r1-new_test.log` |", section)
        self.assertIn(BASE_OUT, section)
        self.assertIn(BRANCH_OUT, section)  # the branch output is still there too
        # the base block sits between the branch failure output and the diagnosis
        self.assertLess(section.index("### Failure output"), section.index("### Base rerun"))
        self.assertLess(section.index("### Base rerun"), section.index("### Diagnosis"))

    def test_private_comment_carries_the_full_block(self):
        self.run_failing()
        comment = self.read(RDIR + "/logs/comment-validate-1.md")
        self.assertIn("### Base rerun (%s)" % self.base_sha()[:10], comment)
        self.assertIn(BASE_OUT, comment)

    def test_log_file_and_timing(self):
        # AC-7
        self.run_failing()
        self.assertTrue(self.exists(RDIR + "/logs/base-r1-new_test.log"))
        self.assertIn(BASE_OUT, self.read(RDIR + "/logs/base-r1-new_test.log"))
        log = self.read(REVALI_LOG)
        timing = [ln for ln in log.splitlines() if "run: timing" in ln]
        self.assertEqual(len(timing), 1, log)
        self.assertRegex(timing[0], r"sandbox .*\bbase-r1 \d+[smh]")
        self.assertRegex(timing[0], r"sandbox .*\bvalidate-r1 \d+[smh]")


class PublicComment(BaseRerunCase):
    def setUp(self):
        super().setUp()
        self.scenario({"visibility": "PUBLIC"})

    def comment(self):
        return self.read(RDIR + "/logs/comment-validate-1.md")

    def test_one_line_no_output_and_the_base_flag(self):
        self.run_failing()
        c = self.comment()
        self.assertIn("on base %s: new_test exit 1" % self.base_sha()[:10], c)  # AC-4
        self.assertNotIn(BASE_OUT, c)
        self.assertNotIn(BRANCH_OUT, c)
        self.assertNotIn("### Base rerun", c)
        self.assertIn("introduced by **base**", c)  # AC-6

    def test_branch_and_unknown_are_not_flagged(self):
        self.run_failing(diagnosis("branch"), scenario(base_results={"new_test": 0}))
        c = self.comment()
        self.assertIn("new_test exit 0", c)
        self.assertNotIn("introduced by", c)  # AC-6: only `base` is flagged
        self.assertIn("introduced by: **branch**", self.read(TESTS_MD))

    def test_reason_when_unavailable(self):
        self.run_failing(diagnosis("unknown"), scenario(errors={"base-r1": "clone blew up"}))
        c = self.comment()
        self.assertIn("on base %s" % self.base_sha()[:10], c)
        self.assertIn("clone blew up", c)

    def test_reason_when_not_run(self):
        self.set_validate_key("rerun_on_base = false")
        self.run_failing(diagnosis("unknown"))
        self.assertIn("rerun_on_base", self.comment())


class IntroducedBy(BaseRerunCase):
    def test_carried_everywhere(self):
        # AC-6
        out = self.run_failing(diagnosis("base"))
        section = self.validation_section()
        self.assertIn("cause: **code**", section)
        self.assertIn("introduced by: **base**", section)
        self.assertIn(
            "`tests/test_review_mul.py::MulTests::test_product`: code, introduced by base.",
            section,
        )
        diag = json.loads(self.read(RDIR + "/diagnose-1.json"))
        self.assertEqual(diag["data"]["introduced_by"], "base")
        self.assertEqual(diag["data"]["failures"][0]["introduced_by"], "base")
        st = State.load(self.rdir())
        self.assertEqual(st.validations[-1]["introduced_by"], "base")
        self.assertEqual(st.validations[-1]["cause"], "code")
        self.assertIn("validation 1 FAILED at step new_test", out)
        self.assertIn("introduced by: base", out)

    def test_missing_is_a_schema_mismatch(self):
        bad = diagnosis()
        del bad["introduced_by"]
        out = self.run_failing(bad)
        self.assertIn("diagnosis unavailable: diagnoser output did not match the schema", out)
        self.assertIn("diagnoser output did not match the schema", self.read(TESTS_MD))
        st = State.load(self.rdir())
        self.assertEqual(st.validations[0]["introduced_by"], "")
        self.assertEqual(st.last_verdict, "FAIL")

    def test_value_outside_the_enum_is_a_schema_mismatch(self):
        out = self.run_failing(diagnosis("reviewer"))
        self.assertIn("diagnoser output did not match the schema", out)
        self.assertEqual(State.load(self.rdir()).validations[0]["introduced_by"], "")

    def test_unknown_is_a_valid_answer(self):
        out = self.run_failing(diagnosis("unknown"))
        self.assertNotIn("did not match the schema", out)
        self.assertIn("introduced by: **unknown**", self.read(TESTS_MD))
        self.assertEqual(State.load(self.rdir()).validations[0]["introduced_by"], "unknown")


class UnreadableReviewerFile(unittest.TestCase):
    """Round 1 F3 / AC-3: a reviewer file that exists but cannot be decoded is a reason on
    the result, not an exception out of rerun_on_base. Drives the function directly on a
    temporary tree with the fake runner, so no sandbox and no git are involved."""

    BASE_SHA = "0123456789abcdef0123456789abcdef01234567"

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="revali-base-rerun-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        os.makedirs(os.path.join(self.tmp, "tests"))
        self.logs = os.path.join(self.tmp, "logs")
        self.scenario = os.path.join(self.tmp, "runner.json")
        with open(self.scenario, "w", encoding="utf-8") as fh:
            json.dump({"default": 0, "results": {"base-r1": {"new_test": 1}}}, fh)
        backup = dict(os.environ)

        def restore():
            os.environ.clear()
            os.environ.update(backup)

        self.addCleanup(restore)
        os.environ["REVALI_FAKE_RUNNER"] = self.scenario
        os.environ.pop("REVALI_FAKE_LOG", None)  # no call log outside a RepoCase

    def ctx(self):
        plat = PlatformCfg(name="linux", runner="local", new_test="python -m unittest {files}")
        return types.SimpleNamespace(
            cfg=types.SimpleNamespace(
                validate=types.SimpleNamespace(rerun_on_base=True, platforms={"linux": plat}),
                project=types.SimpleNamespace(platforms=["linux"], test_dir="tests"),
            ),
            base="main",
            base_sha=self.BASE_SHA,
            branch="feature/mul",
            repo_root=self.tmp,
            logs=self.logs,
        )

    def write(self, rel, data):
        with open(os.path.join(self.tmp, rel), "wb") as fh:
            fh.write(data)

    def test_undecodable_file_becomes_a_reason(self):
        rel = "tests/test_review_x.py"
        self.write(rel, b"# \xff\xfe not utf-8\n")
        st = State()
        st.test_files = [rel]
        failed = types.SimpleNamespace(name="new_test")
        r = rerun_on_base(self.ctx(), st, failed, 1, None)  # must not raise
        self.assertFalse(r.ran)
        self.assertFalse(r.available)
        self.assertIn("could not read a reviewer test file", r.reason)
        self.assertTrue(r.one_line().startswith("on base %s: not run, " % self.BASE_SHA[:10]))
        self.assertIn("could not read", r.one_line())
        self.assertFalse(os.path.isfile(os.path.join(self.logs, "base-r1-new_test.log")))

    def test_one_bad_file_among_good_ones_is_still_a_reason(self):
        # the whole file set is what reruns; a single unreadable file means no rerun
        self.write("tests/test_review_a.py", b"# fine\n")
        self.write("tests/test_review_b.py", b"\xff\xfe\n")
        st = State()
        st.test_files = ["tests/test_review_a.py", "tests/test_review_b.py"]
        r = rerun_on_base(self.ctx(), st, types.SimpleNamespace(name="new_test"), 1, None)
        self.assertFalse(r.ran)
        self.assertIn("could not read a reviewer test file", r.reason)

    def test_readable_file_runs(self):
        # the guard does not swallow the normal case: decodable files reach the sandbox,
        # a recorded file missing from the tree is simply left out
        rel = "tests/test_review_x.py"
        self.write(rel, b"# fine\n")
        st = State()
        st.test_files = [rel, "tests/does_not_exist.py"]
        r = rerun_on_base(self.ctx(), st, types.SimpleNamespace(name="new_test"), 1, None)
        self.assertTrue(r.ran)
        self.assertTrue(r.available, r.reason)
        self.assertEqual(r.reason, "")
        self.assertEqual(r.new_test.returncode, 1)
        self.assertIn(rel, r.new_test.cmd)
        self.assertNotIn("does_not_exist", r.new_test.cmd)
        self.assertEqual(r.one_line(), "on base %s: new_test exit 1" % self.BASE_SHA[:10])
        self.assertTrue(os.path.isfile(os.path.join(self.logs, "base-r1-new_test.log")))


if __name__ == "__main__":
    unittest.main()
