"""Acceptance tests for feature/validate-every-platform, AC-1 to AC-7: the baseline and the
validation run on every platform of `project.platforms`, in list order.

Black box through `revali run` with the fake gh, claude and runner. The fake runner answers
per label and records each session's label, commit, steps and commands, so a label that
carries the platform lets one platform fail while the other passes. The windows table has a
`new_test` command of its own, which tells the two platforms' sessions apart.
"""

import json
import os
import unittest

from revali import EXIT_ACTION, EXIT_ERROR, EXIT_OK, STATE_VERSION
from revali.state import State, read_history
from tests.fixtures.make_sample_repo import LOCAL_TEST, toml_str
from tests.helpers import RepoCase, claude_entry, git, run_cli

RDIR = ".revali/feature__mul"
TESTS_MD = RDIR + "/tests.md"
LOGS = RDIR + "/logs"
TEST_ID = "tests/test_review_mul.py::MulTests::test_product"
WIN_NEW_TEST = "run-the-windows-new-tests"
LINUX_SUITE = "test = %s" % toml_str(LOCAL_TEST)
LINUX_OUT = "LINUX-BRANCH-OUT 12 != 7"
WINDOWS_OUT = "WINDOWS-BRANCH-OUT 12 != 7"
LINUX_BASE_OUT = "LINUX-BASE-OUT no mul"
WINDOWS_BASE_OUT = "WINDOWS-BASE-OUT no mul"
LINUX_SUMMARY = "the linux sandbox shows mul adding"
WINDOWS_SUMMARY = "the windows sandbox shows a wrong expectation"
ENTRY_KEYS = {"name", "result", "failed_step", "cause", "introduced_by", "base_rerun", "issue"}


WINDOWS_HEAD = '\n[validate.windows]\nrunner = "ssh"\nhost = "winbox"\n'


def windows_table(test=LOCAL_TEST, extra=""):
    body = "test = %s\nnew_test = %s\n" % (toml_str(test), toml_str(WIN_NEW_TEST))
    return WINDOWS_HEAD + extra + body


def diagnosis(summary=LINUX_SUMMARY, cause="code", introduced_by="branch"):
    return {
        "summary": summary,
        "cause": cause,
        "introduced_by": introduced_by,
        "failures": [
            {"test": TEST_ID, "cause": cause, "introduced_by": introduced_by, "note": "12 != 7"}
        ],
        "recommendation": "return a * b",
    }


def diagnoser(cost=0.5, **kw):
    return claude_entry(diagnosis(**kw), write_tests=False, cost=cost)


def failing(*names, base=()):
    """A runner scenario: new_test fails in validation on `names`, and on base for `base`."""
    results, outputs = {}, {}
    texts = {"linux": (LINUX_OUT, LINUX_BASE_OUT), "windows": (WINDOWS_OUT, WINDOWS_BASE_OUT)}
    for name in ("linux", "windows"):
        outputs["validate-r1-" + name] = {"new_test": texts[name][0] + "\n"}
        outputs["base-r1-" + name] = {"new_test": texts[name][1] + "\n"}
    for name in names:
        results["validate-r1-" + name] = {"new_test": 1}
    for name in base:
        results["base-r1-" + name] = {"new_test": 1}
    return {"default": 0, "results": results, "outputs": outputs}


class PlatformCase(RepoCase):
    def list_platforms(self, order=("linux", "windows"), windows=None, edit=None):
        before = self.read("revali.toml")
        after = before.replace(
            'platforms = ["linux"]', "platforms = [%s]" % ", ".join('"%s"' % p for p in order)
        )
        self.assertNotEqual(before, after, "fixture platforms line not found")
        if edit is not None:
            edited = edit(after)
            self.assertNotEqual(edited, after, "the edit changed nothing in revali.toml")
            after = edited
        self.write("revali.toml", after + (windows_table() if windows is None else windows))
        self.commit_all("list %s" % ", ".join(order))

    def validate_key(self, line):
        before = self.read("revali.toml")
        self.assertIn("[validate]\n", before)
        self.write("revali.toml", before.replace("[validate]\n", "[validate]\n%s\n" % line, 1))
        self.commit_all("config: " + line)

    def go(self, expected):
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, expected, out)
        self.assertNotIn("Traceback", out)
        return out

    def sessions(self):
        return self.fake_calls("runner")

    def labels(self):
        return [c["label"] for c in self.sessions()]

    def session(self, label):
        found = [c for c in self.sessions() if c["label"] == label]
        self.assertEqual(len(found), 1, self.labels())
        return found[0]

    def state(self):
        return State.load(self.rdir())

    def record(self):
        validations = self.state().validations
        self.assertEqual(len(validations), 1, validations)
        return validations[0]

    def error_heads(self, out):
        """Every `ERROR:` line of the output up to the log path it points at."""
        heads = [ln.split("; see ")[0] for ln in out.splitlines() if ln.startswith("ERROR: ")]
        self.assertTrue(heads, out)
        return heads

    def assert_error_names(self, out, *words):
        heads = self.error_heads(out)
        self.assertTrue(any(all(w in head for w in words) for head in heads), heads)

    def log_lines(self):
        return self.read(LOGS + "/revali.log").splitlines()


class Baseline(PlatformCase):
    """AC-1"""

    def test_inline_every_platform_in_list_order_on_the_preflight_commit(self):
        self.sequential_baseline()
        self.list_platforms(("windows", "linux"))
        head = git(["rev-parse", "HEAD"], self.repo).strip()
        self.claude(claude_entry())
        self.go(EXIT_OK)
        first = self.sessions()[:2]
        self.assertEqual([c["label"] for c in first], ["baseline-windows", "baseline-linux"])
        for call in first:
            self.assertEqual(call["steps"], ["test"])
            self.assertEqual(call["ref"][:10], head[:10])
        self.assertEqual(self.state().baseline_sha[:10], head[:10])

    def test_parallel_one_thread_runs_them_one_after_another(self):
        self.list_platforms()
        self.claude(claude_entry())
        self.go(EXIT_OK)
        base = [c for c in self.sessions() if c["label"].startswith("baseline")]
        self.assertEqual([c["label"] for c in base], ["baseline-linux", "baseline-windows"])
        self.assertLessEqual(base[0]["t1"], base[1]["t0"])
        self.assertEqual(base[0]["ref"], base[1]["ref"])
        self.assertTrue(self.state().baseline_sha)

    def test_first_failure_stops_the_later_platform_and_the_review(self):
        self.sequential_baseline()
        self.list_platforms()
        self.runner_scenario({"default": 0, "results": {"baseline-linux": {"test": 1}}})
        self.claude(claude_entry())
        out = self.go(EXIT_ERROR)
        self.assertEqual(self.labels(), ["baseline-linux"])
        self.assertEqual(self.fake_calls("claude"), [])
        self.assert_error_names(out, "linux")
        state = self.state()
        self.assertEqual(state.baseline_sha if state else "", "")

    def test_a_later_platform_failing_inline_records_no_baseline(self):
        self.sequential_baseline()
        self.list_platforms()
        self.runner_scenario({"default": 0, "results": {"baseline-windows": {"test": 1}}})
        self.claude(claude_entry())
        out = self.go(EXIT_ERROR)
        self.assertEqual(self.labels(), ["baseline-linux", "baseline-windows"])
        self.assert_error_names(out, "windows")
        state = self.state()
        self.assertEqual(state.baseline_sha if state else "", "")

    def test_a_later_platform_failing_on_the_thread_ends_the_run(self):
        self.list_platforms()
        self.runner_scenario({"default": 0, "results": {"baseline-windows": {"test": 1}}})
        self.claude(claude_entry())
        out = self.go(EXIT_ERROR)
        self.assert_error_names(out, "windows")
        self.assertEqual([lb for lb in self.labels() if lb.startswith("validate-")], [])
        self.assertEqual(self.state().baseline_sha, "")
        self.assertEqual(self.state().validations, [])

    def test_a_platform_without_a_suite_is_skipped_by_name(self):
        self.sequential_baseline()
        self.list_platforms(windows=windows_table(test=""))
        self.claude(claude_entry())
        self.go(EXIT_OK)
        labels = self.labels()
        self.assertIn("baseline-linux", labels)
        self.assertNotIn("baseline-windows", labels)
        named = [
            ln for ln in self.log_lines() if "windows" in ln and "baseline" in ln and "skip" in ln
        ]
        self.assertTrue(named, self.log_lines())
        self.assertTrue(self.state().baseline_sha)
        self.assertEqual(self.session("validate-r1-windows")["steps"], ["new_test"])

    def test_nothing_to_run_only_when_no_platform_has_a_suite(self):
        self.sequential_baseline()
        self.list_platforms(
            windows=windows_table(test=""),
            edit=lambda toml: toml.replace(LINUX_SUITE, 'test = ""'),
        )
        self.claude(claude_entry())
        self.go(EXIT_OK)
        labels = self.labels()
        self.assertEqual([lb for lb in labels if lb.startswith("baseline")], [])
        self.assertEqual(self.state().baseline_sha, "")
        self.assertIn("validate-r1-linux", labels)
        self.assertIn("validate-r1-windows", labels)


class Validation(PlatformCase):
    """AC-2"""

    def test_both_platforms_run_in_list_order_and_reuse_the_baseline(self):
        self.list_platforms()
        self.claude(claude_entry())
        self.go(EXIT_OK)
        labels = self.labels()
        self.assertLess(labels.index("validate-r1-linux"), labels.index("validate-r1-windows"))
        self.assertEqual(self.session("validate-r1-linux")["steps"], ["new_test"])
        windows = self.session("validate-r1-windows")
        self.assertEqual(windows["steps"], ["new_test"])
        self.assertEqual(windows["cmds"]["new_test"], WIN_NEW_TEST)
        record = self.record()
        self.assertEqual(record["result"], "PASS")
        self.assertEqual(
            [(p["name"], p["result"]) for p in record["platforms"]],
            [("linux", "PASS"), ("windows", "PASS")],
        )

    def test_reversed_list_validates_windows_first(self):
        self.list_platforms(("windows", "linux"))
        self.claude(claude_entry())
        self.go(EXIT_OK)
        labels = self.labels()
        self.assertLess(labels.index("validate-r1-windows"), labels.index("validate-r1-linux"))
        self.assertEqual([p["name"] for p in self.record()["platforms"]], ["windows", "linux"])

    def test_without_reuse_the_suite_runs_on_every_platform(self):
        self.validate_key("reuse_baseline = false")
        self.list_platforms()
        self.claude(claude_entry())
        self.go(EXIT_OK)
        for name in ("linux", "windows"):
            self.assertEqual(self.session("validate-r1-" + name)["steps"], ["test", "new_test"])

    def test_a_fail_on_the_first_platform_still_runs_the_second(self):
        self.list_platforms()
        self.runner_scenario(failing("linux"))
        self.claude(claude_entry(), diagnoser())
        self.go(EXIT_ACTION)
        labels = self.labels()
        self.assertLess(labels.index("validate-r1-linux"), labels.index("validate-r1-windows"))
        record = self.record()
        self.assertEqual(record["result"], "FAIL")
        self.assertEqual([p["result"] for p in record["platforms"]], ["FAIL", "PASS"])
        self.assertEqual(self.state().last_verdict, "FAIL")

    def test_a_fail_on_the_last_platform_alone_fails_the_run(self):
        self.list_platforms()
        self.runner_scenario(failing("windows"))
        self.claude(claude_entry(), diagnoser(summary=WINDOWS_SUMMARY))
        self.go(EXIT_ACTION)
        record = self.record()
        self.assertEqual(record["result"], "FAIL")
        self.assertEqual([p["result"] for p in record["platforms"]], ["PASS", "FAIL"])

    def test_setup_failing_on_a_later_platform_is_an_environment_error(self):
        self.list_platforms(windows=windows_table(extra='setup = "prepare-windows"\n'))
        scenario = failing("linux")
        scenario["results"]["validate-r1-windows"] = {"setup": 1}
        self.runner_scenario(scenario)
        self.claude(claude_entry(), diagnoser())
        out = self.go(EXIT_ERROR)
        self.assert_error_names(out, "setup", "windows")
        self.assertEqual(self.state().validations, [])
        self.assertNotIn("ACTION NEEDED", out)

    def test_build_failing_on_the_first_platform_stops_at_once(self):
        self.list_platforms(
            edit=lambda toml: toml.replace('setup = ""\n', 'setup = ""\nbuild = "make-it"\n')
        )
        self.runner_scenario({"default": 0, "results": {"validate-r1-linux": {"build": 1}}})
        self.claude(claude_entry())
        out = self.go(EXIT_ERROR)
        self.assert_error_names(out, "build", "linux")
        self.assertNotIn("validate-r1-windows", self.labels())
        self.assertEqual(self.state().validations, [])


class PerPlatformDiagnosis(PlatformCase):
    """AC-3"""

    def both_fail(self):
        self.list_platforms()
        self.runner_scenario(failing("linux", "windows"))
        self.claude(
            claude_entry(),
            diagnoser(cost=0.25),
            diagnoser(cost=0.75, summary=WINDOWS_SUMMARY, cause="test"),
        )
        return self.go(EXIT_ACTION)

    def test_each_failing_platform_gets_its_own_rerun_and_diagnosis(self):
        self.both_fail()
        self.assertEqual(self.session("base-r1-linux")["steps"], ["new_test"])
        self.assertEqual(self.session("base-r1-windows")["cmds"]["new_test"], WIN_NEW_TEST)
        self.assertEqual(len(self.fake_calls("claude")), 3)  # the reviewer and two diagnoses
        for name, summary in (("linux", LINUX_SUMMARY), ("windows", WINDOWS_SUMMARY)):
            path = os.path.join(self.rdir(), "diagnose-1-%s.json" % name)
            self.assertTrue(os.path.isfile(path), path)
            with open(path, "r", encoding="utf-8") as fh:
                self.assertEqual(json.load(fh)["data"]["summary"], summary)
        self.assertFalse(os.path.exists(os.path.join(self.rdir(), "diagnose-1.json")))
        linux = self.read(LOGS + "/prompt-diagnose-1-linux.md")
        windows = self.read(LOGS + "/prompt-diagnose-1-windows.md")
        self.assertIn(LINUX_OUT, linux)
        self.assertIn(LINUX_BASE_OUT, linux)
        self.assertNotIn(WINDOWS_OUT, linux)
        self.assertIn(WINDOWS_OUT, windows)
        self.assertIn(WINDOWS_BASE_OUT, windows)
        self.assertNotIn(LINUX_OUT, windows)
        self.assertIn(WIN_NEW_TEST, windows)  # the command that failed is the platform's own

    def test_the_cost_is_the_sum_of_the_diagnoses(self):
        self.both_fail()
        state = self.state()
        self.assertAlmostEqual(state.validations[0]["cost_usd"], 1.0)
        self.assertAlmostEqual(state.cost_usd, 1.5)  # the reviewer's 0.5 and the two diagnoses

    def test_a_platform_that_failed_in_the_suite_gets_a_diagnosis_too(self):
        self.validate_key("reuse_baseline = false")
        self.list_platforms()
        self.runner_scenario({"default": 0, "results": {"validate-r1-windows": {"test": 1}}})
        self.claude(claude_entry(), diagnoser(summary=WINDOWS_SUMMARY, introduced_by="unknown"))
        self.go(EXIT_ACTION)
        record = self.record()
        self.assertEqual(record["failed_step"], "test")
        linux, windows = record["platforms"]
        self.assertEqual((linux["result"], linux["failed_step"]), ("PASS", ""))
        self.assertEqual((windows["result"], windows["failed_step"]), ("FAIL", "test"))
        self.assertEqual(windows["cause"], "code")
        self.assertIsNotNone(windows["base_rerun"])
        self.assertFalse(windows["base_rerun"]["ran"])  # the rerun is for the reviewer's tests
        self.assertEqual([lb for lb in self.labels() if lb.startswith("base-")], [])
        self.assertTrue(os.path.isfile(os.path.join(self.rdir(), "diagnose-1-windows.json")))
        self.assertEqual(len(self.fake_calls("claude")), 2)

    def test_a_base_bug_on_two_platforms_opens_one_issue(self):
        self.list_platforms()
        self.runner_scenario(failing("linux", "windows", base=("linux", "windows")))
        self.claude(
            claude_entry(),
            diagnoser(introduced_by="base"),
            diagnoser(summary=WINDOWS_SUMMARY, introduced_by="base"),
        )
        self.go(EXIT_ACTION)
        creates = [c for c in self.fake_calls("gh") if c["argv"][:2] == ["issue", "create"]]
        self.assertEqual(len(creates), 1, [c["argv"] for c in self.fake_calls("gh")])
        state = self.state()
        self.assertEqual(len(state.issues), 1)
        numbers = [p["issue"] for p in state.validations[0]["platforms"]]
        self.assertEqual(numbers, [state.issues[0]["number"]] * 2)
        self.assertTrue(numbers[0])


class Names(PlatformCase):
    """AC-4"""

    def test_one_platform_keeps_its_names(self):
        self.sequential_baseline()
        self.runner_scenario({"default": 0, "results": {"validate-r1": {"new_test": 1}}})
        self.claude(claude_entry(), diagnoser())
        out = self.go(EXIT_ACTION)
        expected = ["baseline", "smoke-r1-1", "validate-r1", "base-r1"]
        self.assertEqual(self.labels(), expected)
        self.assertTrue(os.path.isfile(os.path.join(self.rdir(), "diagnose-1.json")))
        self.assertTrue(self.exists(LOGS + "/prompt-diagnose-1.md"))
        self.assertTrue(self.exists(LOGS + "/validate-r1-new_test.log"))
        rows = read_history(os.path.join(self.home, "history.jsonl"))
        self.assertEqual(sorted(rows[-1]["sandbox_s"]), sorted(expected))
        # AC-5: the text of a single platform has no platform block
        md = self.read(TESTS_MD)
        self.assertNotIn("### linux", md)
        self.assertIn("### Failure output (new_test, last 40 lines)", md)
        self.assertIn("### Base rerun (", md)
        self.assertIn("### Diagnosis (", md)
        self.assertIn("validation 1 FAILED at step new_test\n", out)
        baseline = [ln for ln in self.log_lines() if "] preflight: baseline" in ln]
        self.assertTrue(baseline)
        self.assertEqual([ln for ln in baseline if "linux" in ln], [])
        # AC-6: one platform is one entry
        record = self.record()
        self.assertEqual([p["name"] for p in record["platforms"]], ["linux"])
        self.assertEqual(record["platforms"][0]["failed_step"], "new_test")

    def test_two_platforms_never_share_a_label_or_a_log_file(self):
        self.sequential_baseline()
        self.list_platforms()
        self.runner_scenario(failing("linux", "windows"))
        self.claude(claude_entry(), diagnoser(), diagnoser(summary=WINDOWS_SUMMARY))
        self.go(EXIT_ACTION)
        labels = self.labels()
        expected = ["smoke-r1-1"] + [
            "%s-%s" % (label, name)
            for label in ("baseline", "validate-r1", "base-r1")
            for name in ("linux", "windows")
        ]
        self.assertEqual(sorted(labels), sorted(expected))
        logs = os.listdir(os.path.join(self.repo, LOGS))
        for name in ("linux", "windows"):
            self.assertIn("baseline-%s-test.log" % name, logs)
            self.assertIn("validate-r1-%s-new_test.log" % name, logs)
            self.assertIn("base-r1-%s-new_test.log" % name, logs)
            self.assertIn("prompt-diagnose-1-%s.md" % name, logs)
        rows = read_history(os.path.join(self.home, "history.jsonl"))
        self.assertEqual(sorted(rows[-1]["sandbox_s"]), sorted(expected))


class Text(PlatformCase):
    """AC-5"""

    def test_tests_md_has_one_block_per_platform(self):
        self.list_platforms()
        self.runner_scenario(failing("linux"))
        self.claude(claude_entry(), diagnoser())
        out = self.go(EXIT_ACTION)
        md = self.read(TESTS_MD)
        self.assertIn("## Validation 1: FAIL", md)
        top, bottom = md.index("### linux: FAIL"), md.index("### windows: PASS")
        self.assertLess(top, bottom)
        linux, windows = md[top:bottom], md[bottom:]
        self.assertIn("validate-r1-linux-new_test.log", linux)
        self.assertIn(LINUX_OUT, linux)
        self.assertIn("Base rerun", linux)
        self.assertIn(LINUX_BASE_OUT, linux)
        self.assertIn(LINUX_SUMMARY, linux)
        self.assertIn("validate-r1-windows-new_test.log", windows)
        self.assertNotIn(LINUX_SUMMARY, windows)
        self.assertNotIn("Base rerun", windows)
        action = out[out.index("ACTION NEEDED:") :].splitlines()[0]
        self.assertIn("linux", action)

    def test_the_action_text_names_every_failing_platform(self):
        self.list_platforms()
        self.runner_scenario(failing("linux", "windows"))
        self.claude(claude_entry(), diagnoser(), diagnoser(summary=WINDOWS_SUMMARY))
        out = self.go(EXIT_ACTION)
        action = out[out.index("ACTION NEEDED:") :]
        first = action.splitlines()[0]
        self.assertIn("linux", first)
        self.assertIn("windows", first)
        self.assertLess(action.index(LINUX_SUMMARY), action.index(WINDOWS_SUMMARY))
        self.assertIn("validate-r1-linux-new_test.log", action)
        self.assertIn("validate-r1-windows-new_test.log", action)
        md = self.read(TESTS_MD)
        top, bottom = md.index("### linux: FAIL"), md.index("### windows: FAIL")
        self.assertIn(LINUX_SUMMARY, md[top:bottom])
        self.assertIn(WINDOWS_SUMMARY, md[bottom:])
        self.assertIn(WINDOWS_OUT, md[bottom:])

    def test_the_public_summary_has_one_block_per_platform(self):
        self.scenario({"visibility": "PUBLIC"})
        self.list_platforms()
        self.runner_scenario(failing("linux"))
        self.claude(claude_entry(), diagnoser())
        self.go(EXIT_ACTION)
        comment = self.read(LOGS + "/comment-validate-1.md")
        self.assertIn("## Validation 1: FAIL", comment)
        top, bottom = comment.index("### linux: FAIL"), comment.index("### windows: PASS")
        self.assertLess(top, bottom)
        self.assertIn("| new_test | 1 |", comment[top:bottom])
        self.assertIn("| new_test | 0 |", comment[bottom:])
        self.assertNotIn(LINUX_OUT, comment)  # still a summary: no failure output
        self.assertNotIn(LINUX_SUMMARY, comment)

    def test_the_log_names_the_platform_of_baseline_and_validate_lines(self):
        self.sequential_baseline()
        self.list_platforms()
        self.claude(claude_entry())
        self.go(EXIT_OK)
        lines = self.log_lines()
        for marker in ("] preflight: baseline", "] validate: "):
            for name in ("linux", "windows"):
                self.assertTrue(
                    [ln for ln in lines if marker in ln and name in ln], (marker, name, lines)
                )


class StateFile(PlatformCase):
    """AC-6"""

    def test_the_record_lists_each_platform_and_leads_with_the_first_that_failed(self):
        self.list_platforms()
        self.runner_scenario(failing("windows"))
        self.claude(claude_entry(), diagnoser(summary=WINDOWS_SUMMARY, cause="test"))
        self.go(EXIT_ACTION)
        record = self.record()
        linux, windows = record["platforms"]
        for entry in (linux, windows):
            self.assertLessEqual(ENTRY_KEYS, set(entry))
        self.assertEqual((linux["name"], windows["name"]), ("linux", "windows"))
        self.assertEqual(
            [linux[k] for k in ("result", "failed_step", "cause", "introduced_by", "issue")],
            ["PASS", "", "", "", 0],
        )
        self.assertIsNone(linux["base_rerun"])
        self.assertEqual(
            [windows[k] for k in ("result", "failed_step", "cause", "introduced_by", "issue")],
            ["FAIL", "new_test", "test", "branch", 0],
        )
        self.assertTrue(windows["base_rerun"]["ran"])
        for key in ("result", "failed_step", "cause", "introduced_by", "base_rerun", "issue"):
            self.assertEqual(record[key], windows[key], key)

    def test_two_failures_lead_with_the_first_in_list_order(self):
        self.list_platforms()
        self.runner_scenario(failing("linux", "windows"))
        self.claude(claude_entry(), diagnoser(), diagnoser(summary=WINDOWS_SUMMARY, cause="test"))
        self.go(EXIT_ACTION)
        record = self.record()
        self.assertEqual([p["cause"] for p in record["platforms"]], ["code", "test"])
        self.assertEqual(record["cause"], "code")

    def test_all_passing_leads_with_the_first_platform_and_saves_the_new_version(self):
        self.list_platforms()
        self.claude(claude_entry())
        self.go(EXIT_OK)
        record = self.record()
        self.assertEqual(
            (record["result"], record["failed_step"], record["issue"]), ("PASS", "", 0)
        )
        self.assertGreaterEqual(STATE_VERSION, 9)
        with open(os.path.join(self.rdir(), "state.json"), "r", encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["version"], STATE_VERSION)

    def test_a_record_written_before_the_change_loads(self):
        os.makedirs(self.rdir(), exist_ok=True)
        old = {
            "version": 8,
            "validations": [
                {"number": 1, "result": "FAIL", "failed_step": "new_test", "round": 1, "issue": 0}
            ],
        }
        path = os.path.join(self.rdir(), "state.json")
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(old, fh)
        state = State.load(self.rdir())
        self.assertEqual(state.validations[0]["failed_step"], "new_test")
        self.assertNotIn("platforms", state.validations[0])
        self.assertGreater(STATE_VERSION, 8)


class SmokeAndNote(PlatformCase):
    """AC-7"""

    def test_the_smoke_run_stays_on_the_first_platform(self):
        self.list_platforms(("windows", "linux"))
        self.claude(claude_entry())
        self.go(EXIT_OK)
        smoke = [c for c in self.sessions() if c["label"].startswith("smoke")]
        self.assertEqual([c["label"] for c in smoke], ["smoke-r1-1"])
        self.assertEqual(smoke[0]["cmds"]["new_test"], WIN_NEW_TEST)
        self.assertIn("validate-r1-linux", self.labels())  # the later platform is validated

    def test_no_note_says_a_listed_platform_is_not_validated(self):
        self.list_platforms()
        code, out = run_cli(["run", "--dry-run"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertNotIn("not validated", out)
        self.assertNotIn("validates on", out)


if __name__ == "__main__":
    unittest.main()
