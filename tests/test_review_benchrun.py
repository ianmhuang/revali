"""Acceptance tests for feature/review-bench (AC-2, AC-3, AC-4, AC-5, the offline part of AC-7).

`python tools/review_bench.py` is run as a subprocess, as a user runs it, against cases whose
repository is the fixture repository and whose reviewer is the fake claude. A case's head is
an older commit of the fixture's feature branch, so the tests can tell the clone from the
source checkout. Read from the run: the exit code, the printed lines, `results.json` and the
per-case files under `--out`, and the fake log (which sessions ran, where, with which prompt).
"""

import json
import os
import re
import subprocess
import sys
import unittest

from tests.fixtures.make_sample_repo import CHANGE_MD
from tests.helpers import ROOT, RepoCase, approve_response, claude_entry, git

TOOL = os.path.join(ROOT, "tools", "review_bench.py")
MARKER = "bench case text 7f3a"  # only in the case's change.md, not in the source checkout's
CASE_CHANGE_MD = CHANGE_MD.replace(
    "`mul` multiplies two integers.", "`mul` multiplies two integers (%s)." % MARKER
)
EXPECTED_FILE = "src/calc.py"
EXPECTED_LINES = (12, 13)  # `def mul` and its body on the fixture's feature branch
SCORED = ("caught-strict", "caught-loose", "missed")


def finding(fid="F1", file=EXPECTED_FILE, line=12, severity="medium", kind="correctness"):
    return {
        "id": fid,
        "file": file,
        "line": line,
        "severity": severity,
        "kind": kind,
        "text": "mul is wrong for large operands",
        "suggestion": "fix it",
    }


def answer(*findings, verdict="CHANGES_REQUESTED"):
    return claude_entry(approve_response(verdict=verdict, findings=list(findings)))


def refs(repo):
    return git(["for-each-ref", "--format=%(objectname) %(refname)"], repo)


class BenchRun(RepoCase):
    def setUp(self):
        super().setUp()
        self.assertIn(MARKER, CASE_CHANGE_MD)
        self.cases = os.path.join(self.tmp, "cases")
        self.out = os.path.join(self.tmp, "bench out")
        self.base = git(["rev-parse", "main"], self.repo).strip()
        self.head = git(["rev-parse", "feature/mul"], self.repo).strip()
        # the source branch moves on after the commit the case was built from
        self.write(
            "src/calc.py", self.read("src/calc.py") + "\n\ndef div(a, b):\n    return a / b\n"
        )
        self.commit_all("Add div")
        self.source_head = git(["rev-parse", "HEAD"], self.repo).strip()
        self.assertNotEqual(self.source_head, self.head)

    def snapshot(self):
        self.before = (
            refs(self.repo),
            refs(self.info["remote"]),
            git(["status", "--porcelain"], self.repo),
            git(["remote", "-v"], self.repo),
            git(["rev-parse", "HEAD"], self.repo),
        )

    def assert_source_untouched(self):
        after = (
            refs(self.repo),
            refs(self.info["remote"]),
            git(["status", "--porcelain"], self.repo),
            git(["remote", "-v"], self.repo),
            git(["rev-parse", "HEAD"], self.repo),
        )
        self.assertEqual(after, self.before)
        self.assertFalse(self.exists("tests/test_review_mul.py"))

    def make_case(self, name, head=None, fetch=(), lines=EXPECTED_LINES, file=EXPECTED_FILE):
        path = os.path.join(self.cases, name)
        os.makedirs(path)
        text = "\n".join(
            [
                "[case]",
                "repo = %s" % json.dumps(self.repo),
                "fetch = %s" % json.dumps(list(fetch)),
                "base = %s" % json.dumps(self.base),
                "head = %s" % json.dumps(head or self.head),
                'branch = "bench/mul"',
                "",
                "[expected]",
                "file = %s" % json.dumps(file),
                "lines = [%d, %d]" % tuple(lines),
                'description = "mul is wrong for large operands"',
                "",
                "[source]",
                "pr = 7",
                "round = 1",
                'finding = "F1"',
                "",
            ]
        )
        with open(os.path.join(path, "case.toml"), "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        with open(os.path.join(path, "change.md"), "w", encoding="utf-8", newline="\n") as fh:
            fh.write(CASE_CHANGE_MD)
        return path

    def bench(self, *extra, budget="10"):
        """Run the tool; (exit code, stdout, {case: its results.json entry}, summary)."""
        argv = [sys.executable, TOOL, "--cases", self.cases, "--out", self.out]
        if budget is not None:
            argv += ["--budget-usd", budget]
        self.snapshot()
        res = subprocess.run(
            argv + list(extra),
            cwd=self.tmp,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=600,
        )
        self.stderr = res.stderr
        cases, summary = {}, {}
        path = os.path.join(self.out, "results.json")
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            cases = {c["case"]: c for c in data["cases"]}
            summary = data["summary"]
        return res.returncode, res.stdout, cases, summary

    def line_of(self, out, name):
        lines = [ln for ln in out.splitlines() if ln.startswith(name)]
        self.assertEqual(len(lines), 1, out)
        return lines[0]

    def out_file(self, case, name):
        with open(os.path.join(self.out, case, name), encoding="utf-8") as fh:
            return fh.read()


class OneSessionOnAThrowawayClone(BenchRun):
    """AC-2, AC-5, AC-7"""

    def test_a_caught_case_end_to_end(self):
        self.make_case("a-caught")
        self.claude(answer(finding()))
        code, out, cases, summary = self.bench()
        self.assertEqual(code, 0, out + self.stderr)

        # AC-5: the case's line and the summary, the same in results.json
        r = cases["a-caught"]
        self.assertEqual(r["outcome"], "caught-strict")
        self.assertEqual(
            (r["finding"], r["severity"], r["verdict"]), ("F1", "medium", "CHANGES_REQUESTED")
        )
        self.assertAlmostEqual(r["cost"], 0.5)
        self.assertTrue(r["model"])
        line = self.line_of(out, "a-caught")
        for word in ("caught-strict", "F1", "medium", "CHANGES_REQUESTED", r["model"], "0.50"):
            self.assertIn(word, line)
        self.assertEqual((summary["strict"], summary["loose"], summary["scored"]), (1, 1, 1))
        self.assertAlmostEqual(summary["cost"], 0.5)
        self.assertRegex(out, r"strict 1/1.*loose 1/1")

        # AC-2: exactly one session, in a clone that is not the source checkout and is gone
        self.assertEqual({c["exe"] for c in self.fake_calls()}, {"claude"})  # no gh, no sandbox
        calls = self.fake_calls("claude")
        self.assertEqual(len(calls), 1)
        cwd = calls[0]["cwd"]
        self.assertNotEqual(
            os.path.normcase(os.path.realpath(cwd)), os.path.normcase(os.path.realpath(self.repo))
        )
        self.assertFalse(os.path.exists(cwd))

        # the round-1 prompt, built from the case's change.md and the diff base...head
        for prompt in (calls[0]["prompt"], self.out_file("a-caught", "prompt.md")):
            self.assertIn(MARKER, prompt)
            self.assertIn("Round 1", prompt)
            self.assertIn("bench/mul", prompt)
            self.assertIn("+def mul(a, b):", prompt)
            self.assertNotIn("def div", prompt)  # the head commit, not the source's branch tip

        # the raw answer and the parsed one are kept
        self.assertTrue(self.out_file("a-caught", "review.raw.json").strip())
        kept = json.loads(self.out_file("a-caught", "answer.json"))
        self.assertEqual(kept["verdict"], "CHANGES_REQUESTED")
        self.assertEqual([f["id"] for f in kept["findings"]], ["F1"])
        self.assert_source_untouched()

    def test_listed_refs_are_fetched_and_a_failure_is_one_cases_error(self):
        tree = git(["rev-parse", "%s^{tree}" % self.head], self.repo).strip()
        pull = git(
            ["commit-tree", tree, "-p", self.base, "-m", "head of a deleted branch"], self.repo
        ).strip()
        git(["update-ref", "refs/pull/7/head", pull], self.repo)
        self.make_case("a-no-such-ref", fetch=["refs/pull/999/head"])
        self.make_case("b-no-such-head", head="0" * 40)
        self.make_case("c-pull-ref", head=pull, fetch=["refs/pull/7/head"])
        self.claude(answer(finding()))
        code, out, cases, summary = self.bench()
        self.assertNotEqual(code, 0, out)
        self.assertEqual(cases["a-no-such-ref"]["outcome"], "error")
        self.assertIn("refs/pull/999/head", cases["a-no-such-ref"]["message"])
        self.assertEqual(cases["b-no-such-head"]["outcome"], "error")
        self.assertTrue(cases["b-no-such-head"]["message"])
        self.assertIn("error", self.line_of(out, "a-no-such-ref"))
        self.assertEqual(cases["c-pull-ref"]["outcome"], "caught-strict")
        calls = self.fake_calls("claude")
        self.assertEqual(len(calls), 1)  # only the case that could be prepared
        self.assertIn("+def mul(a, b):", calls[0]["prompt"])
        self.assertEqual(summary["scored"], 1)  # errors are not scored
        self.assert_source_untouched()

    def test_a_failed_session_is_an_error_and_the_next_case_runs(self):
        self.make_case("a-fails")
        self.make_case("b-runs")
        self.claude(claude_entry(is_error=True, cost=0.25, write_tests=False), answer(finding()))
        code, out, cases, summary = self.bench()
        self.assertNotEqual(code, 0, out)
        self.assertEqual(cases["a-fails"]["outcome"], "error")
        self.assertTrue(cases["a-fails"]["message"].strip())
        self.assertIn(cases["a-fails"]["message"].strip().splitlines()[0][:40], out)
        self.assertIn("error", self.line_of(out, "a-fails"))
        self.assertEqual(cases["b-runs"]["outcome"], "caught-strict")
        self.assertEqual(len(self.fake_calls("claude")), 2)
        # the failed session's cost is in the total
        self.assertAlmostEqual(summary["cost"], 0.75)
        self.assertRegex(out, r"strict 1/1.*loose 1/1")
        self.assertIn("0.75", out)
        for call in self.fake_calls("claude"):
            self.assertFalse(os.path.exists(call["cwd"]))
        self.assert_source_untouched()


class Scoring(BenchRun):
    """AC-4"""

    def outcomes(self, entries, *extra):
        names = []
        for i, _ in enumerate(entries):
            names.append("%s-case" % "abcdefghij"[i])
            self.make_case(names[-1])
        self.claude(*entries)
        code, out, cases, summary = self.bench(*extra)
        self.assertEqual(code, 0, out + self.stderr)  # every case ran and was scored
        self.assertEqual(len(self.fake_calls("claude")), len(entries))
        self.assertEqual(self.fake_calls("gh"), [])
        return [cases[n] for n in names], summary, out

    def test_strict_and_loose(self):
        got, summary, out = self.outcomes(
            [
                answer(finding(severity="high", kind="security")),
                answer(finding(severity="low")),
                answer(finding(), verdict="APPROVE"),
                answer(finding("F1", severity="low"), finding("F2", line=13, severity="high")),
            ]
        )
        self.assertEqual(
            [r["outcome"] for r in got],
            ["caught-strict", "caught-loose", "caught-loose", "caught-strict"],
        )
        self.assertEqual((got[0]["severity"], got[0]["verdict"]), ("high", "CHANGES_REQUESTED"))
        self.assertEqual((got[1]["finding"], got[1]["severity"]), ("F1", "low"))
        self.assertEqual((got[2]["severity"], got[2]["verdict"]), ("medium", "APPROVE"))
        # the medium-or-high finding is the one reported, and what makes the case strict
        self.assertEqual(got[3]["finding"], "F2")
        self.assertEqual((summary["strict"], summary["loose"], summary["scored"]), (2, 4, 4))
        self.assertRegex(out, r"strict 2/4.*loose 4/4")

    def test_missed_and_findings_elsewhere_are_listed(self):
        got, summary, out = self.outcomes(
            [
                answer(finding("F7", file="src/other.py")),
                answer(finding("F8", kind="convention", severity="high")),
                answer(),
                answer(finding("F1"), finding("F9", file="README.md", line=1, severity="low")),
            ]
        )
        self.assertEqual(
            [r["outcome"] for r in got], ["missed", "missed", "missed", "caught-strict"]
        )
        self.assertIn("F7", json.dumps(got[0]))  # listed, not scored
        self.assertIn("F8", json.dumps(got[1]))
        self.assertEqual(got[3]["finding"], "F1")
        self.assertIn("F9", json.dumps(got[3]))
        self.assertEqual((summary["strict"], summary["loose"], summary["scored"]), (1, 1, 4))
        for r in got[:3]:
            self.assertIn("missed", self.line_of(out, r["case"]))

    def test_the_default_margin_is_fifteen_lines(self):
        last = EXPECTED_LINES[1]
        got, _, _ = self.outcomes(
            [
                answer(finding(line=last + 15)),
                answer(finding(line=last + 16)),
                answer(finding(line=1)),
            ]
        )
        self.assertEqual([r["outcome"] for r in got], ["caught-strict", "missed", "caught-strict"])

    def test_line_margin_narrows_and_widens_the_range(self):
        first, last = EXPECTED_LINES
        got, _, _ = self.outcomes(
            [
                answer(finding(line=first - 2)),
                answer(finding(line=first - 3)),
                answer(finding(line=last + 2)),
                answer(finding(line=last + 3)),
            ],
            "--line-margin",
            "2",
        )
        self.assertEqual(
            [r["outcome"] for r in got], ["caught-strict", "missed", "caught-strict", "missed"]
        )

    def test_a_wider_margin_catches_what_the_default_misses(self):
        got, _, _ = self.outcomes(
            [answer(finding(line=EXPECTED_LINES[1] + 25))], "--line-margin", "30"
        )
        self.assertEqual(got[0]["outcome"], "caught-strict")


class BudgetAndModel(BenchRun):
    """AC-3: the fixture repository's session cap is $1.00; a fake session costs $0.50"""

    def three_cases(self):
        for name in ("a-one", "b-two", "c-three"):
            self.make_case(name)
        return ("a-one", "b-two", "c-three")

    def test_stops_before_the_session_whose_cap_could_pass_the_budget(self):
        names = self.three_cases()
        self.claude(answer(finding()))
        # 0 + 1.0 and 0.5 + 1.0 fit 1.6; 1.0 + 1.0 does not
        code, out, cases, summary = self.bench(budget="1.6")
        self.assertNotEqual(code, 0, out)
        self.assertEqual(
            [cases[n]["outcome"] for n in names], ["caught-strict", "caught-strict", "not-run"]
        )
        self.assertEqual(len(self.fake_calls("claude")), 2)
        self.assertTrue(cases["c-three"]["message"].strip())
        self.assertIn("not-run", self.line_of(out, "c-three"))
        self.assertLessEqual(summary["cost"], 1.6)
        self.assertEqual(summary["scored"], 2)
        self.assert_source_untouched()

    def test_a_budget_below_one_session_cap_runs_nothing(self):
        names = self.three_cases()
        code, out, cases, summary = self.bench(budget="0.9")
        self.assertNotEqual(code, 0, out)
        self.assertEqual([cases[n]["outcome"] for n in names], ["not-run"] * 3)
        self.assertEqual(self.fake_calls("claude"), [])
        self.assertAlmostEqual(summary["cost"], 0.0)

    def test_a_failed_sessions_cost_counts_towards_the_budget(self):
        names = self.three_cases()
        self.claude(claude_entry(is_error=True, cost=0.7, write_tests=False), answer(finding()))
        code, out, cases, summary = self.bench(budget="1.6")  # 0.7 + 1.0 passes 1.6
        self.assertNotEqual(code, 0, out)
        self.assertEqual([cases[n]["outcome"] for n in names], ["error", "not-run", "not-run"], out)
        self.assertEqual(len(self.fake_calls("claude")), 1)
        self.assertAlmostEqual(summary["cost"], 0.7)

    def test_a_lower_session_cap_reaches_the_session_and_lets_more_cases_run(self):
        names = self.three_cases()
        self.claude(answer(finding()))
        # 0+0.6, 0.5+0.6 and 1.0+0.6 all fit 1.7; with the $1.00 cap 1.0+1.0 would not
        code, out, cases, _ = self.bench("--session-budget-usd", "0.6", budget="1.7")
        self.assertEqual(code, 0, out + self.stderr)
        self.assertEqual([cases[n]["outcome"] for n in names], ["caught-strict"] * 3)
        calls = self.fake_calls("claude")
        self.assertEqual(len(calls), 3)
        for call in calls:
            argv = call["argv"]
            self.assertAlmostEqual(float(argv[argv.index("--max-budget-usd") + 1]), 0.6)

    def test_the_budget_is_required(self):
        self.make_case("a-one")
        code, out, cases, _ = self.bench(budget=None)
        self.assertNotEqual(code, 0)
        self.assertRegex(self.stderr, r"--budget-usd")
        self.assertEqual(self.fake_calls(), [])
        self.assertEqual(cases, {})

    def test_model_sets_the_reviewer_model_of_every_case(self):
        self.make_case("a-one")
        self.make_case("b-two")
        self.claude(answer(finding()))
        code, out, _, _ = self.bench("--model", "claude-sonnet-5-5")
        self.assertEqual(code, 0, out + self.stderr)
        calls = self.fake_calls("claude")
        self.assertEqual(len(calls), 2)
        for call in calls:
            argv = call["argv"]
            self.assertEqual(argv[argv.index("--model") + 1], "claude-sonnet-5-5")

    def test_without_model_the_case_repositorys_configuration_chooses(self):
        toml = self.read("revali.toml")
        self.assertIn("[review]\n", toml)
        self.write("revali.toml", toml.replace("[review]\n", '[review]\nmodel = "sonnet"\n', 1))
        self.commit_all("pin the reviewer model")
        configured = git(["rev-parse", "HEAD"], self.repo).strip()
        self.make_case("a-one", head=configured)
        self.claude(answer(finding()))
        code, out, cases, _ = self.bench()
        self.assertEqual(code, 0, out + self.stderr)
        argv = self.fake_calls("claude")[0]["argv"]
        self.assertEqual(argv[argv.index("--model") + 1], "sonnet")
        self.assertTrue(re.search(r"\bcaught-strict\b", self.line_of(out, "a-one")))


if __name__ == "__main__":
    unittest.main()
