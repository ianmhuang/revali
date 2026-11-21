"""Reviewer acceptance tests for `tools/review_bench.py --effort` (feature/reviewer-effort).

Black box, on the bench tests' fixture: the fixture repository is the case's repository, the
fake claude answers ($0.50 a session, a $1.00 session cap) and records the argv of every
session. The case repository's committed revali.toml and a case's own revali.toml each set a
`[review] effort` of their own here, so no expectation depends on the default in
defaults.toml.
"""

import contextlib
import io
import json
import os
from unittest import mock

from tests.helpers import claude_entry, git
from tests.test_bench_tool import BenchCase, answer, bench, finding

LEVELS = ("low", "medium", "high", "xhigh", "max")  # AC-2
REPO_EFFORT = "xhigh"  # what the case repository's committed revali.toml asks for
OWN_EFFORT = "low"  # what the case's own revali.toml asks for


def with_effort(config, effort):
    assert "[review]\n" in config, config
    return config.replace("[review]\n", '[review]\neffort = "%s"\n' % effort, 1)


class EffortCase(BenchCase):
    def setUp(self):
        super().setUp()
        self.plain_config = self.read("revali.toml")
        self.write("revali.toml", with_effort(self.plain_config, REPO_EFFORT))
        git(["add", "revali.toml"], self.repo)
        git(["commit", "-q", "-m", "the repository sets its reviewer effort"], self.repo)
        self.head = git(["rev-parse", "HEAD"], self.repo).strip()

    def two_cases(self):
        """`a-repo` takes the repository's configuration, `b-own` carries its own."""
        self.make_case("a-repo")
        path = self.make_case("b-own")
        with open(os.path.join(path, "revali.toml"), "w", encoding="utf-8", newline="") as fh:
            fh.write(with_effort(self.plain_config, OWN_EFFORT))

    def efforts_passed(self):
        passed = []
        for call in self.fake_calls("claude"):
            argv = call["argv"]
            passed.append(argv[argv.index("--effort") + 1])
        return passed

    def results_file(self):
        with open(os.path.join(self.out, "results.json"), encoding="utf-8") as fh:
            return json.load(fh)

    def words_of_line(self, out, name):
        lines = [ln for ln in out.splitlines() if ln.startswith(name + " ")]
        self.assertEqual(len(lines), 1, out)
        return lines[0].split()

    def resume(self, *extra):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = bench.main(["--resume", self.out, "--budget-usd", "10"] + list(extra))
        self.assert_nothing_pushed()
        return code, out.getvalue(), err.getvalue()

    def stop_after_first(self, *extra):
        """A first run that scores `a-one` and leaves `b-two` not run (the budget)."""
        self.make_case("a-one")
        self.make_case("b-two")
        self.claude(claude_entry(answer(finding())))
        code, out, cases, _ = self.run_bench(*extra, budget="1.2")
        self.assertEqual(code, 1, out)
        self.assertEqual(cases["a-one"]["outcome"], "caught-strict", out)
        self.assertEqual(cases["b-two"]["outcome"], "not-run", out)
        self.assertEqual(len(self.fake_calls("claude")), 1)


class EffortOption(EffortCase):
    def test_without_the_option_each_configuration_chooses(self):
        """AC-1, AC-3"""
        self.two_cases()
        self.claude(claude_entry(answer(finding())))
        code, out, cases, results = self.run_bench()
        self.assertEqual(code, 0, out)
        self.assertEqual(self.efforts_passed(), [REPO_EFFORT, OWN_EFFORT])
        self.assertEqual(cases["a-repo"]["effort"], REPO_EFFORT)
        self.assertEqual(cases["b-own"]["effort"], OWN_EFFORT)
        self.assertEqual(results["effort"], "")
        self.assertIn(REPO_EFFORT, self.words_of_line(out, "a-repo"))
        self.assertIn(OWN_EFFORT, self.words_of_line(out, "b-own"))

    def test_the_option_replaces_both_configurations(self):
        """AC-1, AC-3"""
        self.two_cases()
        self.claude(claude_entry(answer(finding())))
        code, out, cases, results = self.run_bench("--effort", "max")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.efforts_passed(), ["max", "max"])
        self.assertEqual(cases["a-repo"]["effort"], "max")
        self.assertEqual(cases["b-own"]["effort"], "max")
        self.assertEqual(results["effort"], "max")
        for name in ("a-repo", "b-own"):
            words = self.words_of_line(out, name)
            self.assertIn("max", words)
            self.assertNotIn(REPO_EFFORT, words)
            self.assertNotIn(OWN_EFFORT, words)

    def test_only_the_engines_levels_are_taken(self):
        """AC-2"""
        self.make_case("a-repo")
        self.claude(claude_entry(answer(finding())))
        for bad in ("hgih", "HIGH", "", "none", "medium,high"):
            with self.subTest(bad=bad):
                err = io.StringIO()
                with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
                    with self.assertRaises(SystemExit) as raised:
                        bench.main(
                            ["--cases", self.cases, "--out", self.out, "--budget-usd", "10"]
                            + ["--effort", bad]
                        )
                self.assertEqual(raised.exception.code, 2)
                self.assertIn("--effort", err.getvalue())
        self.assertEqual(self.fake_calls("claude"), [])
        self.assertFalse(os.path.exists(os.path.join(self.out, "results.json")))
        for level in LEVELS:
            with self.subTest(level=level):
                code, out, cases, results = self.run_bench("--dry-run", "--effort", level)
                self.assertEqual(code, 0, out)
                self.assertEqual(cases["a-repo"]["effort"], level)
                self.assertEqual(results["effort"], level)
        self.assertEqual(self.fake_calls("claude"), [])

    def test_a_dry_run_reports_the_effort(self):
        """AC-5"""
        self.two_cases()
        code, out, cases, results = self.run_bench("--dry-run", "--effort", "max")
        self.assertEqual(code, 0, out)
        self.assertTrue(results["dry_run"])
        self.assertEqual(results["effort"], "max")
        for name in ("a-repo", "b-own"):
            self.assertEqual(cases[name]["outcome"], "ready")
            self.assertEqual(cases[name]["effort"], "max")
            self.assertIn("max", self.words_of_line(out, name))
            self.assertTrue(os.path.isfile(os.path.join(self.out, name, "prompt.md")))
        code, out, cases, results = self.run_bench("--dry-run")
        self.assertEqual(code, 0, out)
        self.assertEqual(results["effort"], "")
        self.assertEqual(cases["a-repo"]["effort"], REPO_EFFORT)
        self.assertEqual(cases["b-own"]["effort"], OWN_EFFORT)
        self.assertIn(REPO_EFFORT, self.words_of_line(out, "a-repo"))
        self.assertIn(OWN_EFFORT, self.words_of_line(out, "b-own"))
        self.assertEqual(self.fake_calls("claude"), [])


class EffortOfASessionThatDidNotScore(EffortCase):
    """AC-3: a session that failed, is in progress or was interrupted ran at an effort too."""

    def test_a_failed_session_records_its_effort(self):
        self.two_cases()
        failing = claude_entry(is_error=True, cost=0.25, write_tests=False)
        self.claude(failing)
        code, out, cases, results = self.run_bench()
        self.assertEqual(code, 1, out)
        self.assertEqual([cases[n]["outcome"] for n in ("a-repo", "b-own")], ["error", "error"])
        self.assertEqual(self.efforts_passed(), [REPO_EFFORT, OWN_EFFORT])
        self.assertEqual(cases["a-repo"]["effort"], REPO_EFFORT)
        self.assertEqual(cases["b-own"]["effort"], OWN_EFFORT)
        self.assertEqual(results["effort"], "")
        self.assertIn(REPO_EFFORT, self.words_of_line(out, "a-repo"))

        self.claude(failing)
        code, out, cases, results = self.run_bench("--effort", "medium")
        self.assertEqual(code, 1, out)
        self.assertEqual([cases[n]["outcome"] for n in ("a-repo", "b-own")], ["error", "error"])
        self.assertEqual(self.efforts_passed()[-2:], ["medium", "medium"])
        for name in ("a-repo", "b-own"):
            self.assertEqual(cases[name]["effort"], "medium")
            self.assertIn("medium", self.words_of_line(out, name))

    def test_a_started_and_an_interrupted_session_record_the_effort(self):
        self.make_case("a-repo")
        self.claude(claude_entry(answer(finding())))
        seen = []

        def interrupted(*args, **kwargs):
            seen.append(self.results_file()["cases"][0])  # what a kill here would leave
            raise KeyboardInterrupt

        with mock.patch.object(bench, "spawn_reviewer", interrupted):
            code, out, cases, _ = self.run_bench()
        self.assertEqual(code, 1, out)
        self.assertEqual(len(seen), 1)
        self.assertEqual((seen[0]["outcome"], seen[0]["effort"]), ("started", REPO_EFFORT))
        self.assertEqual(cases["a-repo"]["outcome"], "interrupted")
        self.assertEqual(cases["a-repo"]["effort"], REPO_EFFORT)
        self.assertIn(REPO_EFFORT, self.words_of_line(out, "a-repo"))


class EffortResume(EffortCase):
    """AC-4"""

    def test_a_resume_takes_the_recorded_effort(self):
        self.stop_after_first("--effort", "medium")
        code, out, err = self.resume()
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.efforts_passed(), ["medium", "medium"])
        results = self.results_file()
        self.assertEqual(results["effort"], "medium")
        self.assertEqual([c["effort"] for c in results["cases"]], ["medium", "medium"])

    def test_the_same_effort_given_again_resumes(self):
        self.stop_after_first("--effort", "medium")
        code, out, err = self.resume("--effort", "medium")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.efforts_passed(), ["medium", "medium"])
        self.assertEqual(self.results_file()["effort"], "medium")

    def test_a_different_effort_is_refused_before_anything_runs(self):
        self.stop_after_first("--effort", "medium")
        before = self.results_file()
        code, out, err = self.resume("--effort", "low")
        self.assertEqual(code, 1, out + err)
        self.assertIn("effort", err)
        self.assertEqual(len(self.fake_calls("claude")), 1)
        self.assertEqual(self.results_file(), before)

    def test_an_effort_on_a_run_that_gave_none_is_refused(self):
        self.stop_after_first()
        before = self.results_file()
        self.assertEqual(before["effort"], "")
        # the level the first case ran at anyway: the run still gave none
        code, out, err = self.resume("--effort", REPO_EFFORT)
        self.assertEqual(code, 1, out + err)
        self.assertIn("effort", err)
        self.assertEqual(len(self.fake_calls("claude")), 1)
        self.assertEqual(self.results_file(), before)

    def test_a_recorded_level_the_engine_does_not_take_starts_no_session(self):
        """AC-2, AC-4: the recorded effort goes to every session a resume starts"""
        self.stop_after_first("--effort", "medium")
        edited = self.results_file()
        edited["effort"] = "hgih"
        with open(os.path.join(self.out, "results.json"), "w", encoding="utf-8") as fh:
            json.dump(edited, fh)
        code, out, err = self.resume()
        self.assertNotEqual(code, 0, out + err)
        self.assertEqual(len(self.fake_calls("claude")), 1)
        self.assertEqual(self.results_file(), edited)

    def strip_effort(self):
        """results.json as a revali before `--effort` wrote it."""
        results = self.results_file()
        del results["effort"]
        for case in results["cases"]:
            del case["effort"]
        with open(os.path.join(self.out, "results.json"), "w", encoding="utf-8") as fh:
            json.dump(results, fh)
        return results

    def test_an_older_results_file_resumes_as_none_given(self):
        self.stop_after_first()
        self.strip_effort()
        code, out, err = self.resume()
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.efforts_passed(), [REPO_EFFORT, REPO_EFFORT])
        results = self.results_file()
        self.assertEqual(results["effort"], "")
        cases = {c["case"]: c for c in results["cases"]}
        self.assertEqual(cases["a-one"]["outcome"], "caught-strict")
        self.assertEqual(cases["b-two"]["outcome"], "caught-strict")
        self.assertEqual(cases["b-two"]["effort"], REPO_EFFORT)

    def test_an_older_results_file_refuses_an_effort(self):
        self.stop_after_first()
        before = self.strip_effort()
        code, out, err = self.resume("--effort", "medium")
        self.assertEqual(code, 1, out + err)
        self.assertIn("effort", err)
        self.assertEqual(len(self.fake_calls("claude")), 1)
        self.assertEqual(self.results_file(), before)
