"""The baseline and the validation run on every listed platform (feature/validate-every-platform,
M3.7.2). The fake runner answers per label, so a label that carries the platform lets each
platform pass or fail on its own."""

import json
import os
import subprocess
import sys
import unittest

from revali import EXIT_ACTION, EXIT_ERROR, EXIT_OK, STATE_VERSION
from revali.state import State, read_history
from tests.fixtures.make_sample_repo import LOCAL_NEW_TEST, LOCAL_TEST, toml_str
from tests.helpers import FAKE_BIN, ROOT, RepoCase, claude_entry, run_cli

TESTS_MD = ".revali/feature__mul/tests.md"
LOGS = ".revali/feature__mul/logs"
TEST_ID = "tests/test_review_mul.py::MulTests::test_product"

WINDOWS = """
[validate.windows]
runner = "ssh"
host = "winbox"
test = %s
new_test = %s
""" % (
    toml_str(LOCAL_TEST),
    toml_str(LOCAL_NEW_TEST),
)


def diagnosis(introduced_by="branch", summary="mul adds instead of multiplying"):
    return {
        "summary": summary,
        "cause": "code",
        "introduced_by": introduced_by,
        "failures": [
            {"test": TEST_ID, "cause": "code", "introduced_by": introduced_by, "note": "12 != 7"}
        ],
        "recommendation": "return a * b",
    }


class PlatformCase(RepoCase):
    def platforms(self, order=("linux", "windows"), windows=WINDOWS):
        toml = self.read("revali.toml").replace(
            'platforms = ["linux"]', "platforms = [%s]" % ", ".join('"%s"' % p for p in order)
        )
        self.write("revali.toml", toml + windows)
        self.commit_all("list %s" % ", ".join(order))

    def labels(self):
        return [c["label"] for c in self.fake_calls("runner")]

    def steps_of(self, label):
        return [c["steps"] for c in self.fake_calls("runner") if c["label"] == label]

    def claude_roles(self):
        return len(self.fake_calls("claude"))

    def go(self):
        return run_cli(["run", "--foreground"])

    def state(self):
        return State.load(self.rdir())


class Baseline(PlatformCase):
    """AC-1"""

    def test_every_platform_runs_in_list_order_inline(self):
        self.sequential_baseline()
        self.platforms()
        self.claude(claude_entry())
        code, out = self.go()
        self.assertEqual(code, EXIT_OK, out)
        labels = self.labels()
        self.assertEqual(labels[:2], ["baseline-linux", "baseline-windows"])
        self.assertEqual(self.steps_of("baseline-windows"), [["test"]])
        self.assertTrue(self.state().baseline_sha)

    def test_every_platform_runs_on_the_parallel_thread(self):
        self.platforms(("windows", "linux"))
        self.claude(claude_entry())
        code, out = self.go()
        self.assertEqual(code, EXIT_OK, out)
        base = [lb for lb in self.labels() if lb.startswith("baseline")]
        self.assertEqual(base, ["baseline-windows", "baseline-linux"])
        self.assertTrue(self.state().baseline_sha)

    def test_the_first_failure_stops_and_names_the_platform(self):
        self.sequential_baseline()
        self.platforms()
        self.runner_scenario({"default": 0, "results": {"baseline-linux": {"test": 1}}})
        self.claude(claude_entry())
        code, out = self.go()
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertIn("broken before review on linux", out)
        self.assertNotIn("baseline-windows", self.labels())
        self.assertEqual(self.claude_roles(), 0)

    def test_a_later_platform_failing_records_no_baseline(self):
        self.sequential_baseline()
        self.platforms()
        self.runner_scenario({"default": 0, "results": {"baseline-windows": {"test": 1}}})
        self.claude(claude_entry())
        code, out = self.go()
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertIn("broken before review on windows", out)
        self.assertEqual(self.labels(), ["baseline-linux", "baseline-windows"])
        self.assertEqual(self.state().baseline_sha, "")

    def test_a_platform_failing_on_the_thread_cancels_the_review(self):
        self.platforms()
        self.runner_scenario({"default": 0, "results": {"baseline-windows": {"test": 1}}})
        self.claude(claude_entry())
        code, out = self.go()
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertIn("broken before review on windows", out)
        self.assertNotIn("validate-r1-linux", self.labels())

    def test_a_platform_without_a_suite_is_skipped_by_name(self):
        self.sequential_baseline()
        self.platforms(windows=WINDOWS.replace("test = %s" % toml_str(LOCAL_TEST), 'test = ""'))
        self.claude(claude_entry())
        code, out = self.go()
        self.assertEqual(code, EXIT_OK, out)
        self.assertNotIn("baseline-windows", self.labels())
        self.assertIn("baseline-linux", self.labels())
        self.assertIn("no existing test command on windows", out)
        self.assertTrue(self.state().baseline_sha)


class Validation(PlatformCase):
    """AC-2, AC-3"""

    def test_both_pass(self):
        self.platforms()
        self.claude(claude_entry())
        code, out = self.go()
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("validate-r1-linux", self.labels())
        self.assertIn("validate-r1-windows", self.labels())
        record = self.state().validations[0]
        self.assertEqual(record["result"], "PASS")
        self.assertEqual(
            [(p["name"], p["result"]) for p in record["platforms"]],
            [("linux", "PASS"), ("windows", "PASS")],
        )

    def test_the_suite_is_reused_on_every_platform(self):
        self.platforms()
        self.claude(claude_entry())
        code, out = self.go()
        self.assertEqual(code, EXIT_OK, out)
        for name in ("linux", "windows"):
            self.assertEqual(self.steps_of("validate-r1-" + name), [["new_test"]], name)

    def test_a_fail_on_the_first_still_runs_the_second(self):
        self.platforms()
        self.runner_scenario({"default": 0, "results": {"validate-r1-linux": {"new_test": 1}}})
        self.claude(claude_entry(), claude_entry(diagnosis(), write_tests=False))
        code, out = self.go()
        self.assertEqual(code, EXIT_ACTION, out)
        labels = self.labels()
        self.assertLess(labels.index("validate-r1-linux"), labels.index("validate-r1-windows"))
        self.assertIn("base-r1-linux", labels)
        self.assertNotIn("base-r1-windows", labels)
        self.assertEqual(self.claude_roles(), 2)  # reviewer + one diagnoser
        self.assertTrue(os.path.isfile(os.path.join(self.rdir(), "diagnose-1-linux.json")))
        self.assertTrue(os.path.isfile(os.path.join(self.repo, LOGS, "prompt-diagnose-1-linux.md")))
        record = self.state().validations[0]
        self.assertEqual(record["result"], "FAIL")
        self.assertEqual(record["failed_step"], "new_test")
        self.assertEqual(record["platforms"][0]["result"], "FAIL")
        self.assertEqual(record["platforms"][0]["cause"], "code")
        self.assertEqual(record["platforms"][1]["result"], "PASS")
        self.assertIn("new_test on linux", out)

    def test_a_fail_on_the_second_only(self):
        self.platforms()
        self.runner_scenario({"default": 0, "results": {"validate-r1-windows": {"new_test": 1}}})
        self.claude(claude_entry(), claude_entry(diagnosis(), write_tests=False))
        code, out = self.go()
        self.assertEqual(code, EXIT_ACTION, out)
        record = self.state().validations[0]
        self.assertEqual(record["result"], "FAIL")
        self.assertEqual(
            [(p["name"], p["result"]) for p in record["platforms"]],
            [("linux", "PASS"), ("windows", "FAIL")],
        )
        self.assertEqual(record["cause"], "code")  # top level: the first failing platform
        self.assertIn("new_test on windows", out)

    def test_both_fail_get_a_diagnosis_each_and_the_cost_adds_up(self):
        self.platforms()
        self.runner_scenario(
            {
                "default": 0,
                "results": {
                    "validate-r1-linux": {"new_test": 1},
                    "validate-r1-windows": {"new_test": 1},
                },
            }
        )
        self.claude(
            claude_entry(),
            claude_entry(diagnosis(summary="linux side"), write_tests=False, cost=0.25),
            claude_entry(diagnosis(summary="windows side"), write_tests=False, cost=0.75),
        )
        code, out = self.go()
        self.assertEqual(code, EXIT_ACTION, out)
        self.assertEqual(self.claude_roles(), 3)
        self.assertIn("base-r1-linux", self.labels())
        self.assertIn("base-r1-windows", self.labels())
        self.assertIn("new_test on linux", out)
        self.assertIn("new_test on windows", out)
        self.assertIn("linux side", out)
        self.assertIn("windows side", out)
        state = self.state()
        self.assertAlmostEqual(state.validations[0]["cost_usd"], 1.0)
        self.assertAlmostEqual(state.cost_usd, 1.5)  # reviewer 0.5 + 1.0
        md = self.read(TESTS_MD)
        self.assertIn("### linux: FAIL", md)
        self.assertIn("### windows: FAIL", md)
        self.assertLess(md.index("linux side"), md.index("### windows: FAIL"))

    def test_setup_failing_on_a_platform_stops_without_a_diagnosis(self):
        self.platforms()
        self.runner_scenario(
            {
                "default": 0,
                "results": {
                    "validate-r1-linux": {"new_test": 1},
                    "validate-r1-windows": {"setup": 1},
                },
            }
        )
        self.write(
            "revali.toml",
            self.read("revali.toml").replace(
                'host = "winbox"\n', 'host = "winbox"\nsetup = "true"\n'
            ),
        )
        self.commit_all("windows setup")
        self.claude(claude_entry(), claude_entry(diagnosis(), write_tests=False))
        code, out = self.go()
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertIn("setup step failed on windows", out)
        self.assertEqual(self.claude_roles(), 1)
        self.assertNotIn("base-r1-linux", self.labels())

    def test_a_base_bug_on_both_platforms_opens_one_issue(self):
        self.platforms()
        self.runner_scenario(
            {
                "default": 0,
                "results": {
                    "validate-r1-linux": {"new_test": 1},
                    "validate-r1-windows": {"new_test": 1},
                    "base-r1-linux": {"new_test": 1},
                    "base-r1-windows": {"new_test": 1},
                },
            }
        )
        self.claude(
            claude_entry(),
            claude_entry(diagnosis("base"), write_tests=False),
            claude_entry(diagnosis("base"), write_tests=False),
        )
        code, out = self.go()
        self.assertEqual(code, EXIT_ACTION, out)
        creates = [c for c in self.fake_calls("gh") if c["argv"][:2] == ["issue", "create"]]
        self.assertEqual(len(creates), 1)
        self.assertTrue(os.path.isfile(os.path.join(self.repo, LOGS, "issue-1-linux.md")))
        state = self.state()
        self.assertEqual(len(state.issues), 1)
        self.assertEqual([p["issue"] for p in state.validations[0]["platforms"]], [41, 41])


class Names(PlatformCase):
    """AC-4, AC-5: one platform keeps today's names and text"""

    def test_one_platform_keeps_its_labels_and_files(self):
        self.sequential_baseline()
        self.runner_scenario({"default": 0, "results": {"validate-r1": {"new_test": 1}}})
        self.claude(claude_entry(), claude_entry(diagnosis(), write_tests=False))
        code, out = self.go()
        self.assertEqual(code, EXIT_ACTION, out)
        self.assertEqual(self.labels(), ["baseline", "smoke-r1-1", "validate-r1", "base-r1"])
        self.assertTrue(os.path.isfile(os.path.join(self.rdir(), "diagnose-1.json")))
        md = self.read(TESTS_MD)
        self.assertNotIn("### linux", md)
        self.assertIn("### Failure output (new_test, last 40 lines)", md)
        self.assertIn("validation 1 FAILED at step new_test\n", out)
        record = self.state().validations[0]
        self.assertEqual([p["name"] for p in record["platforms"]], ["linux"])
        rows = read_history(os.path.join(self.home, "history.jsonl"))
        self.assertEqual(
            sorted(rows[-1]["sandbox_s"]), ["base-r1", "baseline", "smoke-r1-1", "validate-r1"]
        )

    def test_two_platforms_carry_the_platform_in_every_label(self):
        self.sequential_baseline()
        self.platforms()
        self.claude(claude_entry())
        code, out = self.go()
        self.assertEqual(code, EXIT_OK, out)
        for label in self.labels():
            if not label.startswith("smoke-"):  # the smoke run stays on the first platform
                self.assertTrue(label.endswith(("-linux", "-windows")), label)
        logs = os.listdir(os.path.join(self.repo, LOGS))
        self.assertIn("validate-r1-linux-new_test.log", logs)
        self.assertIn("validate-r1-windows-new_test.log", logs)
        md = self.read(TESTS_MD)
        self.assertIn("### linux: PASS", md)
        self.assertIn("### windows: PASS", md)


class StateFile(PlatformCase):
    """AC-6"""

    def test_state_version_bumped(self):
        self.assertGreaterEqual(STATE_VERSION, 9)

    def test_a_record_without_platforms_still_loads(self):
        os.makedirs(self.rdir(), exist_ok=True)
        old = {
            "version": 8,
            "validations": [{"number": 1, "result": "PASS", "failed_step": "", "round": 1}],
        }
        with open(os.path.join(self.rdir(), "state.json"), "w", encoding="utf-8") as fh:
            json.dump(old, fh)
        state = State.load(self.rdir())
        self.assertEqual(state.validations[0]["result"], "PASS")
        code, out = run_cli(["status"])
        self.assertNotIn("ERROR", out)


class Notes(PlatformCase):
    """AC-7"""

    def test_no_not_validated_note(self):
        self.platforms()
        code, out = run_cli(["run", "--dry-run"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertNotIn("not validated", out)

    def test_the_smoke_run_stays_on_the_first(self):
        self.platforms(("windows", "linux"))
        self.claude(claude_entry())
        code, out = self.go()
        self.assertEqual(code, EXIT_OK, out)
        smoke = [lb for lb in self.labels() if lb.startswith("smoke")]
        self.assertEqual(smoke, ["smoke-r1-1"])


SSH_STUB = os.path.join(FAKE_BIN, "ssh_stub.py")
SCP_STUB = os.path.join(FAKE_BIN, "scp_stub.py")


class SshStubKnobs(unittest.TestCase):
    """AC-8: a knob names every host ("1") or the hosts it acts on."""

    def setUp(self):
        self.env = dict(os.environ)
        for knob in (
            "REVALI_FAKE_SSH_DOWN",
            "REVALI_FAKE_SSH_BASH_FAILS",
            "REVALI_FAKE_SSH_SCRIPT_DROP",
        ):
            self.env.pop(knob, None)
        self.env.pop("REVALI_FAKE_LOG", None)
        self.env["REVALI_FAKE_REMOTE"] = os.path.join(ROOT, "tests", "fixtures", "no-such-remote")

    def ssh_full(self, host, *words, **env):
        e = dict(self.env, **env)
        return subprocess.run(
            [sys.executable, SSH_STUB, host] + list(words), env=e, capture_output=True, text=True
        )

    def ssh(self, host, *words, **env):
        return self.ssh_full(host, *words, **env).returncode

    def scp(self, src, dst, **env):
        e = dict(self.env, **env)
        return subprocess.run(
            [sys.executable, SCP_STUB, src, dst], env=e, capture_output=True, text=True
        ).returncode

    def test_down_for_every_host(self):
        self.assertEqual(self.ssh("winbox", "true", REVALI_FAKE_SSH_DOWN="1"), 255)
        self.assertEqual(self.ssh("linbox", "true", REVALI_FAKE_SSH_DOWN="1"), 255)

    def test_down_for_one_host(self):
        self.assertEqual(self.ssh("winbox", "true", REVALI_FAKE_SSH_DOWN="winbox"), 255)
        self.assertEqual(self.ssh("linbox", "true", REVALI_FAKE_SSH_DOWN="winbox"), 0)
        self.assertEqual(self.ssh("b", "true", REVALI_FAKE_SSH_DOWN="a,b"), 255)

    def test_scp_down_for_one_host(self):
        self.assertEqual(self.scp("winbox:x", ".", REVALI_FAKE_SSH_DOWN="winbox"), 255)
        # linbox is up, so the copy is tried and fails on the missing file instead
        self.assertEqual(self.scp("linbox:x", ".", REVALI_FAKE_SSH_DOWN="winbox"), 1)

    def test_bash_fails_for_one_host(self):
        knob = {"REVALI_FAKE_SSH_BASH_FAILS": "winbox"}
        res = self.ssh_full("winbox", "bash", "x.sh", **knob)
        self.assertEqual(res.returncode, 127)
        self.assertIn("bash: cannot execute", res.stderr)
        # linbox runs bash, which then fails on the missing script: not the knob's message
        self.assertNotIn(
            "bash: cannot execute", self.ssh_full("linbox", "bash", "x.sh", **knob).stderr
        )


class Docs(unittest.TestCase):
    """AC-9"""

    def read(self, *parts):
        with open(os.path.join(ROOT, *parts), "r", encoding="utf-8", newline="") as fh:
            return " ".join(fh.read().split())

    def test_configuration(self):
        text = self.read("docs", "configuration.md")
        self.assertIn("every listed platform", text)
        self.assertNotIn("not validated", text)
        self.assertIn("smoke run", text)

    def test_files(self):
        text = self.read("docs", "files.md")
        self.assertIn("`platforms`", text)
        self.assertIn("validate-r<n>-<platform>", text)

    def test_sandbox(self):
        text = self.read("docs", "sandbox.md")
        self.assertIn("not been run end to end", text)


if __name__ == "__main__":
    unittest.main()
