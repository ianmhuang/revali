"""Validate stage: baseline, PASS/FAIL, diagnoser, WSL runner script (run through Git Bash)."""
import os
import sys
import unittest

from tests.helpers import FAKE_BIN, RepoCase, _quote, approve_response, claude_entry, git, run_cli
from tests.fixtures.make_sample_repo import LOCAL_NEW_TEST, LOCAL_TEST, PY, toml_str
from revali import EXIT_ACTION, EXIT_ERROR, EXIT_OK
from revali.config import PlatformCfg
from revali.runners import WslRunner
from revali.state import State, read_history

WSL_STUB = os.path.join(FAKE_BIN, "wsl_stub.py")


def diagnosis(cause="code", **kw):
    data = {"summary": "mul returns a + b instead of a * b, so the product test fails.",
            "cause": cause,
            "failures": [{"test": "tests/test_review_mul.py::MulTests::test_product", "cause": cause,
                          "note": "expected 12, got 7"}],
            "recommendation": "return a * b"}
    data.update(kw)
    return data


class BaselineTests(RepoCase):
    def test_broken_baseline_stops_before_review(self):
        self.runner_scenario({"default": 0, "results": {"baseline": {"test": 1}},
                              "outputs": {"baseline": {"test": "FAIL: test_add"}}})
        self.claude(claude_entry())
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertIn("broken before review", out)
        self.assertIn("FAIL: test_add", out)
        self.assertEqual(self.fake_calls("claude"), [])
        self.assertFalse(any(c["argv"][:2] == ["pr", "create"] for c in self.fake_calls("gh")))

    def test_baseline_skipped_without_existing_suite(self):
        self.write("revali.toml", self.read("revali.toml").replace('test = %s' % toml_str(LOCAL_TEST), 'test = ""'))
        self.commit_all("no suite")
        self.claude(claude_entry())
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        labels = [r["label"] for r in self.fake_calls("runner")]
        self.assertNotIn("baseline", labels)
        self.assertIn("validate-r1", labels)

    def test_baseline_only_on_first_pass(self):
        cr = approve_response(verdict="CHANGES_REQUESTED", findings=[{"id": "F1", "file": "src/calc.py", "line": 1,
                                                                        "severity": "high", "kind": "correctness",
                                                                        "text": "t", "suggestion": ""}])
        self.claude(claude_entry(cr), claude_entry(write_tests=False))
        run_cli(["run", "--foreground"])
        self.write("src/calc.py", self.read("src/calc.py") + "\n# fix\n")
        self.commit_all("fix")
        run_cli(["run", "--foreground"])
        labels = [r["label"] for r in self.fake_calls("runner")]
        self.assertEqual(labels.count("baseline"), 1)


class ValidationOutcomes(RepoCase):
    def test_pass_marks_ready(self):
        self.claude(claude_entry())
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("READY TO MERGE", out)
        self.assertIn("tests/test_review_mul.py", out)
        state = State.load(self.rdir())
        self.assertEqual(state.stage, "ready_to_merge")
        self.assertEqual(state.last_verdict, "PASS")
        self.assertEqual(len(state.validations), 1)
        self.assertEqual(state.validations[0]["result"], "PASS")
        tests_md = self.read(".revali/feature__mul/tests.md")
        self.assertIn("## Validation 1: PASS", tests_md)
        self.assertIn("| new_test | 0 |", tests_md)
        gh = [c["argv"] for c in self.fake_calls("gh")]
        self.assertTrue(any(a[:2] == ["pr", "ready"] for a in gh))
        self.assertTrue(self.exists(".revali/feature__mul/logs/comment-validate-1.md"))
        self.assertEqual(len(self.fake_calls("claude")), 1)  # no diagnoser on PASS

    def test_fail_diagnoses_then_fix_then_pass(self):
        self.runner_scenario({"default": 0, "results": {"validate-r1": {"new_test": 1}},
                              "outputs": {"validate-r1": {"new_test": "AssertionError: 12 != 7"}}})
        self.claude(claude_entry(), claude_entry(diagnosis(), write_tests=False, model="claude-opus-5", cost=0.2),
                    claude_entry(approve_response(previous_findings=[]), write_tests=False))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ACTION, out)
        self.assertIn("validation 1 FAILED at step new_test", out)
        self.assertIn("cause: code", out)
        self.assertIn("return a * b", out)
        state = State.load(self.rdir())
        self.assertEqual(state.stage, "needs_action")
        self.assertEqual(state.last_verdict, "FAIL")
        self.assertEqual(state.validations[0]["cause"], "code")
        self.assertIn("claude-opus-5", state.models_used)
        self.assertAlmostEqual(state.cost_usd, 0.7)
        tests_md = self.read(".revali/feature__mul/tests.md")
        self.assertIn("## Validation 1: FAIL", tests_md)
        self.assertIn("AssertionError: 12 != 7", tests_md)
        self.assertIn("cause: **code**", tests_md)
        self.assertTrue(self.exists(".revali/feature__mul/diagnose-1.json"))
        cl = self.fake_calls("claude")
        self.assertEqual(len(cl), 2)
        diag = cl[1]
        self.assertIn("--tools", diag["argv"])
        self.assertIn("Read,Grep,Glob", diag["argv"])
        self.assertNotIn("acceptEdits", diag["argv"])
        self.assertIn("AssertionError: 12 != 7", diag["prompt"])
        self.assertIn("step `new_test`", diag["prompt"])
        self.assertIn("tests/test_review_mul.py", diag["prompt"])
        # fix and rerun: counts as a fix cycle, review round 2, validation 2 passes
        self.runner_scenario({"default": 0})
        self.write("src/calc.py", self.read("src/calc.py").replace("return a * b", "return a * b  # fixed"))
        self.commit_all("fix mul")
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        state = State.load(self.rdir())
        self.assertEqual(state.fixes, 1)
        self.assertEqual(len(state.rounds), 2)
        self.assertEqual(len(state.validations), 2)
        self.assertEqual(state.validations[1]["result"], "PASS")
        self.assertIn("## Validation 2: PASS", self.read(".revali/feature__mul/tests.md"))
        rows = read_history(os.path.join(self.home, "history.jsonl"))
        self.assertEqual([r["last_verdict"] for r in rows], ["FAIL", "PASS"])

    def test_existing_suite_failure_is_fail(self):
        self.runner_scenario({"default": 0, "results": {"validate-r1": {"test": 1}}})
        self.claude(claude_entry(), claude_entry(diagnosis("code"), write_tests=False))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ACTION, out)
        self.assertIn("FAILED at step test", out)
        self.assertIn("step `test`", self.fake_calls("claude")[1]["prompt"])

    def test_setup_failure_at_validation_is_error(self):
        self.write("revali.toml", self.read("revali.toml").replace('setup = ""', 'setup = "pip install x"'))
        self.commit_all("setup")
        self.runner_scenario({"default": 0, "results": {"validate-r1": {"setup": 1}}})
        self.claude(claude_entry())
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertIn("environment problem", out)
        self.assertEqual(len(self.fake_calls("claude")), 1)

    def test_diagnoser_failure_still_reports_fail(self):
        self.runner_scenario({"default": 0, "results": {"validate-r1": {"new_test": 1}}})
        self.claude(claude_entry(), {"raw_stdout": "garbage", "exit": 0})
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ACTION, out)
        self.assertIn("diagnosis unavailable", out)
        self.assertIn("diagnosis unavailable", self.read(".revali/feature__mul/tests.md"))
        self.assertEqual(State.load(self.rdir()).last_verdict, "FAIL")

    def test_timed_out_test_is_fail(self):
        self.runner_scenario({"default": 0, "results": {"validate-r1": {"new_test": 124}}})
        self.claude(claude_entry(), claude_entry(diagnosis("env"), write_tests=False))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ACTION, out)
        self.assertIn("cause: env", out)

    def test_fallback_reviewer_noted_in_summary(self):
        self.claude(claude_entry(model="claude-opus-5"))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("fallback model", out)


class WslRunnerTests(RepoCase):
    """The generated script is exercised with the host's bash via wsl_stub (no real WSL)."""
    runner = "wsl"

    def setUp(self):
        super().setUp()
        self.use_real_local_runner()
        os.environ["REVALI_WSL_CMD"] = "%s %s" % (_quote(sys.executable), _quote(WSL_STUB))
        # Commands that work under Git Bash on the host.
        cfg = self.read("revali.toml")
        cfg = cfg.replace('setup = "python3 -m venv .venv && .venv/bin/pip install -q -r requirements.txt"', 'setup = "%s --version"' % PY)
        cfg = cfg.replace('test = ".venv/bin/python -m pytest -q"', 'test = %s' % toml_str(LOCAL_TEST))
        cfg = cfg.replace('new_test = ".venv/bin/python -m pytest -q tests"', 'new_test = %s' % toml_str(LOCAL_NEW_TEST))
        self.write("revali.toml", cfg)
        self.commit_all("bash-friendly commands")

    def test_script_shape(self):
        r = WslRunner(PlatformCfg(runner="wsl", distro="Ubuntu"))
        text = r.script("/mnt/d/x y/repo", "/mnt/d/x y/logs", "/mnt/d/x y/extra", "abc123",
                        [("setup", "python3 -m venv .venv"), ("build", ""), ("test", "pytest -q")],
                        "validate-r1", "$HOME/.revali/sandbox/repo/validate-r1", 900)
        self.assertIn('HOST="/mnt/d/x y/repo"', text)
        self.assertIn("git -c safe.directory='*' clone -q --no-checkout", text)
        self.assertIn('git checkout -q --detach "$REF"', text)
        self.assertIn("run_step setup ||", text)
        self.assertIn("run_step test ||", text)
        self.assertNotIn("run_step build", text)
        self.assertIn("STEP_TIMEOUT=900", text)
        self.assertIn('rm -rf "$SB"', text)
        self.assertNotIn("\r", text)

    @unittest.skipUnless(os.name == "nt" or os.path.exists("/bin/bash"), "needs bash")
    def test_run_through_bash(self):
        from revali.runners import WslRunner as W
        from revali.config import load_project_config
        cfg = load_project_config(self.repo)
        plat = cfg.validate.platforms["linux"]
        r = W(plat)
        logs = os.path.join(self.rdir(), "logs")
        head = git(["rev-parse", "HEAD"], self.repo).strip()
        report = r.run(self.repo, head, [("setup", plat.setup), ("test", plat.test), ("new_test", plat.new_test)],
                       {"tests/test_review_mul.py": "import unittest\nfrom src.calc import mul\n\nclass T(unittest.TestCase):\n    def test_m(self):\n        self.assertEqual(mul(2, 3), 6)\n"},
                       logs, "validate-r1")
        self.assertEqual([s.name for s in report.steps], ["setup", "test", "new_test"])
        self.assertTrue(report.ok, [(s.name, s.returncode, s.stdout[-300:]) for s in report.steps])
        self.assertTrue(os.path.isfile(os.path.join(logs, "validate-r1.sh")))
        self.assertTrue(os.path.isfile(os.path.join(logs, "validate-r1-new_test.log")))
        self.assertIn("Ran 1 test", report.step("new_test").stdout)
        sandbox = os.path.join(os.path.expanduser("~"), ".revali", "sandbox", "sample", "validate-r1")
        self.assertFalse(os.path.exists(sandbox))
        self.assertFalse(os.path.isdir(os.path.join(logs, "validate-r1-extra")))

    @unittest.skipUnless(os.name == "nt" or os.path.exists("/bin/bash"), "needs bash")
    def test_run_reports_failing_step(self):
        from revali.runners import WslRunner as W
        r = W(PlatformCfg(runner="wsl", distro="Ubuntu", command_timeout_min=1, sandbox_dir="~/.revali/sandbox"))
        logs = os.path.join(self.rdir(), "logs")
        head = git(["rev-parse", "HEAD"], self.repo).strip()
        report = r.run(self.repo, head, [("setup", "true"), ("test", "exit 3"), ("new_test", "true")], {}, logs, "validate-r2")
        self.assertEqual([s.name for s in report.steps], ["setup", "test"])
        self.assertEqual(report.failed.name, "test")
        self.assertEqual(report.failed.returncode, 3)

    @unittest.skipUnless(os.environ.get("REVALI_TEST_WSL") == "1", "set REVALI_TEST_WSL=1 to run against real WSL")
    def test_real_wsl(self):
        os.environ.pop("REVALI_WSL_CMD", None)
        from revali.runners import WslRunner as W
        r = W(PlatformCfg(runner="wsl", distro="Ubuntu", sandbox_dir="~/.revali/sandbox"))
        logs = os.path.join(self.rdir(), "logs")
        head = git(["rev-parse", "HEAD"], self.repo).strip()
        report = r.run(self.repo, head, [("test", "python3 -m unittest discover -s tests -t . -p 'test_calc*.py'")], {}, logs, "validate-real")
        self.assertTrue(report.ok, [(s.name, s.returncode, s.stdout[-300:]) for s in report.steps])


if __name__ == "__main__":
    unittest.main()
