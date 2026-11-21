"""M4.4.1: `tools/review_bench.py --effort` runs every case at one reviewer effort, whatever
the case's configuration says, and records it, so two runs that differ only in effort can be
compared case by case. Offline, on the bench tests' fixture (fake claude, $0.50 a session,
a $1.00 session cap)."""

import contextlib
import io
import json
import os
import unittest

from revali.engines.claude import EFFORTS
from tests.helpers import ROOT, claude_entry
from tests.test_bench_tool import BenchCase, answer, bench, finding


def effort_of(argv):
    return argv[argv.index("--effort") + 1]


class Effort(BenchCase):
    def efforts_passed(self):
        return [effort_of(c["argv"]) for c in self.fake_calls("claude")]

    def case_with_own_effort(self, name, effort):
        path = self.make_case(name)
        config = self.read("revali.toml")
        self.assertIn("budget_usd = 1.0\n", config)
        config = config.replace(
            "budget_usd = 1.0\n", 'budget_usd = 1.0\neffort = "%s"\n' % effort, 1
        )
        with open(os.path.join(path, "revali.toml"), "w", encoding="utf-8", newline="") as fh:
            fh.write(config)
        return path

    def line_of(self, out, name):
        lines = [ln for ln in out.splitlines() if ln.startswith(name)]
        self.assertEqual(len(lines), 1, out)
        return lines[0]

    def test_without_effort_the_case_config_chooses(self):
        """AC-1"""
        self.make_case("a-default")
        self.case_with_own_effort("b-own", "low")
        self.claude(claude_entry(answer(finding())))
        code, out, cases, results = self.run_bench()
        self.assertEqual(code, 0, out)
        self.assertEqual(self.efforts_passed(), ["high", "low"])  # defaults.toml, the case's
        self.assertEqual(cases["a-default"]["effort"], "high")
        self.assertEqual(cases["b-own"]["effort"], "low")
        self.assertEqual(results["effort"], "")

    def test_effort_replaces_every_case_config(self):
        """AC-1, AC-3"""
        self.make_case("a-default")
        self.case_with_own_effort("b-own", "high")
        self.claude(claude_entry(answer(finding())))
        code, out, cases, results = self.run_bench("--effort", "medium")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.efforts_passed(), ["medium", "medium"])
        self.assertEqual([c["effort"] for c in cases.values()], ["medium", "medium"])
        self.assertEqual(results["effort"], "medium")
        for name in ("a-default", "b-own"):
            self.assertIn(" medium ", self.line_of(out, name))

    def test_a_level_the_engine_does_not_take(self):
        """AC-2: a usage error before anything runs"""
        self.make_case("a-default")
        self.claude(claude_entry(answer(finding())))
        for bad in ("hgih", "", "HIGH"):
            with self.subTest(bad=bad):
                err = io.StringIO()
                with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
                    with self.assertRaises(SystemExit) as ctx:
                        bench.main(
                            ["--cases", self.cases, "--out", self.out, "--budget-usd", "10"]
                            + ["--effort", bad]
                        )
                self.assertEqual(ctx.exception.code, 2)
                self.assertIn("--effort", err.getvalue())
        self.assertEqual(self.fake_calls("claude"), [])
        self.assertFalse(os.path.exists(os.path.join(self.out, "results.json")))

    def test_the_levels_are_the_engines(self):
        """AC-2: listed once, in the claude engine"""
        self.assertEqual(EFFORTS, ("low", "medium", "high", "xhigh", "max"))
        with open(os.path.join(ROOT, "tools", "review_bench.py"), encoding="utf-8") as fh:
            text = fh.read()
        self.assertNotIn('"xhigh"', text)
        self.make_case("case-max")
        self.claude(claude_entry(answer(finding())))
        code, out, cases, _ = self.run_bench("--effort", "max")
        self.assertEqual(code, 0, out)
        self.assertEqual(cases["case-max"]["effort"], "max")

    def test_the_dry_run_names_the_effort(self):
        """AC-5"""
        self.make_case("a-default")
        self.case_with_own_effort("b-own", "xhigh")
        code, out, cases, _ = self.run_bench("--dry-run")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.fake_calls("claude"), [])
        self.assertEqual(cases["a-default"]["effort"], "high")
        self.assertEqual(cases["b-own"]["effort"], "xhigh")
        self.assertIn(" xhigh ", self.line_of(out, "b-own"))
        self.assertTrue(os.path.isfile(os.path.join(self.out, "b-own", "prompt.md")))
        code, out, cases, _ = self.run_bench("--dry-run", "--effort", "low")
        self.assertEqual(code, 0, out)
        self.assertEqual([c["effort"] for c in cases.values()], ["low", "low"])


class EffortResume(BenchCase):
    """AC-4"""

    def resume(self, *extra):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = bench.main(["--resume", self.out, "--budget-usd", "10"] + list(extra))
        return code, out.getvalue(), err.getvalue()

    def stop_after_first(self, *extra):
        """A first run whose second case is not run (the budget), so a resume has work."""
        self.make_case("a-one")
        self.make_case("b-two")
        self.claude(claude_entry(answer(finding())))
        code, out, cases, _ = self.run_bench(*extra, budget="1.2")
        self.assertEqual(cases["b-two"]["outcome"], "not-run", out)

    def test_the_effort_of_the_earlier_run_is_kept(self):
        self.stop_after_first("--effort", "medium")
        code, out, err = self.resume()
        self.assertEqual(code, 0, out + err)
        argv = self.fake_calls("claude")[-1]["argv"]
        self.assertEqual(effort_of(argv), "medium")
        with open(os.path.join(self.out, "results.json"), encoding="utf-8") as fh:
            results = json.load(fh)
        self.assertEqual(results["effort"], "medium")
        self.assertEqual([c["effort"] for c in results["cases"]], ["medium", "medium"])

    def test_the_same_effort_given_again_resumes(self):
        self.stop_after_first("--effort", "medium")
        code, out, err = self.resume("--effort", "medium")
        self.assertEqual(code, 0, out + err)

    def test_a_different_effort_is_refused(self):
        self.stop_after_first("--effort", "medium")
        calls = len(self.fake_calls("claude"))
        code, out, err = self.resume("--effort", "high")
        self.assertEqual(code, 1, out + err)
        self.assertIn("effort", err)
        self.assertIn("'high'", err)
        self.assertIn("'medium'", err)
        self.assertEqual(len(self.fake_calls("claude")), calls)

    def test_effort_on_a_run_that_gave_none_is_refused(self):
        self.stop_after_first()
        code, out, err = self.resume("--effort", "medium")
        self.assertEqual(code, 1, out + err)
        self.assertIn("effort", err)

    def test_a_results_file_from_before_effort(self):
        self.stop_after_first()
        path = os.path.join(self.out, "results.json")
        with open(path, encoding="utf-8") as fh:
            results = json.load(fh)
        del results["effort"]
        for c in results["cases"]:
            del c["effort"]
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(results, fh)
        code, out, err = self.resume()
        self.assertEqual(code, 0, out + err)
        code, out, err = self.resume("--effort", "medium")  # nothing left to run, still refused
        self.assertEqual(code, 1, out + err)

    def test_a_bad_recorded_effort_is_not_a_results_file(self):
        self.stop_after_first()
        path = os.path.join(self.out, "results.json")
        with open(path, encoding="utf-8") as fh:
            results = json.load(fh)
        results["effort"] = 3
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(results, fh)
        code, out, err = self.resume()
        self.assertEqual(code, 1, out + err)
        self.assertIn("not a benchmark results.json", err)


class Readme(unittest.TestCase):
    """AC-6"""

    def test_the_readme(self):
        with open(os.path.join(ROOT, "bench", "README.md"), encoding="utf-8") as fh:
            text = " ".join(fh.read().split())
        self.assertIn("--effort", text)
        self.assertIn("`effort`", text)
        self.assertIn("same `PROMPT_VERSION`", text)
