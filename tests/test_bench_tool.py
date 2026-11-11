"""feature/review-bench (M3.3.1): tools/review_bench.py runs one reviewer session per case on a
throwaway clone and scores whether a finding names the case's bug. Offline: the fixture
repository is the case's repository and the fake claude answers."""

import contextlib
import importlib.util
import io
import json
import os
import unittest

from tests.helpers import ROOT, RepoCase, approve_response, claude_entry, git

SPEC = importlib.util.spec_from_file_location(
    "review_bench", os.path.join(ROOT, "tools", "review_bench.py")
)
bench = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bench)

EXPECTED_LINE = 13  # `def mul` in the fixture's src/calc.py on feature/mul


def finding(
    fid="F1", file="src/calc.py", line=EXPECTED_LINE, severity="medium", kind="correctness"
):
    return {
        "id": fid,
        "file": file,
        "line": line,
        "severity": severity,
        "kind": kind,
        "text": "mul is wrong",
        "suggestion": "fix it",
    }


def answer(*findings, verdict="CHANGES_REQUESTED"):
    return approve_response(verdict=verdict, findings=list(findings))


class BenchCase(RepoCase):
    def setUp(self):
        super().setUp()
        self.cases = os.path.join(self.tmp, "cases")
        self.out = os.path.join(self.tmp, "out")
        self.base = git(["rev-parse", "main"], self.repo).strip()
        self.head = git(["rev-parse", "feature/mul"], self.repo).strip()
        self.heads_before = git(["for-each-ref", "--format=%(refname)"], self.repo)
        self.origin_before = git(["ls-remote", "--heads", "origin"], self.repo)

    def make_case(self, name, expected=None, drop=(), change_md=None, extra=""):
        exp = {"file": "src/calc.py", "lines": "[13, 14]", "description": '"mul is wrong"'}
        exp.update(expected or {})
        fields = {
            "case": {
                "repo": json.dumps(self.repo),
                "fetch": "[]",
                "base": '"%s"' % self.base,
                "head": '"%s"' % self.head,
                "branch": '"bench/mul"',
            },
            "expected": {k: (v if k != "file" else json.dumps(v)) for k, v in exp.items()},
            "source": {"pr": "1", "round": "1", "finding": '"F1"'},
        }
        lines = []
        for table, values in fields.items():
            lines.append("[%s]" % table)
            for key, value in values.items():
                if "%s.%s" % (table, key) not in drop:
                    lines.append("%s = %s" % (key, value))
        path = os.path.join(self.cases, name)
        os.makedirs(path)
        with open(os.path.join(path, "case.toml"), "w", encoding="utf-8", newline="\n") as fh:
            fh.write("\n".join(lines) + "\n" + extra)
        text = self.read(".revali/feature__mul/change.md") if change_md is None else change_md
        if text is not False:
            with open(os.path.join(path, "change.md"), "w", encoding="utf-8", newline="\n") as fh:
                fh.write(text)
        return path

    def run_bench(self, *extra, budget="10"):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = bench.main(
                ["--cases", self.cases, "--out", self.out, "--budget-usd", budget] + list(extra)
            )
        with open(os.path.join(self.out, "results.json"), encoding="utf-8") as fh:
            results = json.load(fh)
        return code, out.getvalue(), {c["case"]: c for c in results["cases"]}, results

    def assert_nothing_pushed(self):
        self.assertEqual(self.fake_calls("gh"), [])
        self.assertEqual(git(["ls-remote", "--heads", "origin"], self.repo), self.origin_before)
        self.assertEqual(git(["for-each-ref", "--format=%(refname)"], self.repo), self.heads_before)


class Scoring(BenchCase):
    """AC-2, AC-4, AC-5"""

    def test_a_caught_case(self):
        self.make_case("a-caught")
        self.claude(claude_entry(answer(finding())))
        code, out, cases, results = self.run_bench()
        self.assertEqual(code, 0, out)
        r = cases["a-caught"]
        self.assertEqual(r["outcome"], "caught-strict")
        self.assertEqual(
            (r["finding"], r["severity"], r["verdict"]), ("F1", "medium", "CHANGES_REQUESTED")
        )
        self.assertEqual(r["cost"], 0.5)
        self.assertEqual(results["summary"]["strict"], 1)
        self.assertIn("caught: strict 1/1 (100%), loose 1/1 (100%); cost $0.50", out)
        # one session, on a clone that is gone afterwards, with the case's change.md
        calls = self.fake_calls("claude")
        self.assertEqual(len(calls), 1)
        self.assertFalse(os.path.exists(calls[0]["cwd"]))
        self.assertNotEqual(os.path.normcase(calls[0]["cwd"]), os.path.normcase(self.repo))
        with open(os.path.join(self.out, "a-caught", "prompt.md"), encoding="utf-8") as fh:
            prompt = fh.read()
        self.assertIn("Add mul to calc", prompt)
        self.assertIn("def mul(a, b):", prompt)  # the diff base..head
        self.assertTrue(os.path.isfile(os.path.join(self.out, "a-caught", "answer.json")))
        self.assertTrue(os.path.isfile(os.path.join(self.out, "a-caught", "review.raw.json")))
        self.assert_nothing_pushed()

    def test_loose_when_low_or_approved(self):
        self.make_case("a-low")
        self.make_case("b-approve")
        self.claude(
            claude_entry(answer(finding(severity="low"))),
            claude_entry(answer(finding(), verdict="APPROVE")),
        )
        code, out, cases, results = self.run_bench()
        self.assertEqual(code, 0, out)
        self.assertEqual(cases["a-low"]["outcome"], "caught-loose")
        self.assertEqual(cases["b-approve"]["outcome"], "caught-loose")
        self.assertEqual((results["summary"]["strict"], results["summary"]["loose"]), (0, 2))

    def test_missed_outside_the_range_elsewhere_or_a_convention(self):
        self.make_case("a-far")
        self.make_case("b-other-file")
        self.make_case("c-convention")
        self.make_case("d-no-line")
        self.claude(
            claude_entry(answer(finding(line=EXPECTED_LINE + 1 + 15 + 1))),
            claude_entry(answer(finding(file="src/other.py"))),
            claude_entry(answer(finding(kind="convention"))),
            claude_entry(answer(finding(line=0))),
        )
        code, out, cases, _ = self.run_bench()
        self.assertEqual(code, 0, out)
        for name in ("a-far", "b-other-file", "c-convention", "d-no-line"):
            with self.subTest(case=name):
                self.assertEqual(cases[name]["outcome"], "missed")
                self.assertEqual(len(cases[name]["other_findings"]), 1)
        self.assertIn("caught: strict 0/4 (0%), loose 0/4 (0%)", out)

    def test_the_margin_is_an_option(self):
        self.make_case("a-far")
        self.claude(claude_entry(answer(finding(line=EXPECTED_LINE + 25))))
        _, _, cases, _ = self.run_bench("--line-margin", "30")
        self.assertEqual(cases["a-far"]["outcome"], "caught-strict")

    def test_a_failed_session_does_not_stop_the_others(self):
        self.make_case("a-fails")
        self.make_case("b-runs")
        self.claude(
            claude_entry(is_error=True, cost=0.25, write_tests=False),
            claude_entry(answer(finding())),
        )
        code, out, cases, results = self.run_bench()
        self.assertEqual(code, 1, out)
        self.assertEqual(cases["a-fails"]["outcome"], "error")
        self.assertTrue(cases["a-fails"]["message"])
        self.assertEqual(cases["b-runs"]["outcome"], "caught-strict")
        self.assertEqual(results["summary"]["cost"], 0.75)  # the failed session's cost counts

    def test_model_is_passed_to_the_session(self):
        self.make_case("a-caught")
        self.claude(claude_entry(answer(finding())))
        self.run_bench("--model", "claude-sonnet-5-5")
        argv = self.fake_calls("claude")[0]["argv"]
        self.assertEqual(argv[argv.index("--model") + 1], "claude-sonnet-5-5")


class Budget(BenchCase):
    """AC-3: the fixture's session cap is $1.00, each fake session costs $0.50"""

    def test_a_budget_below_one_session_runs_nothing(self):
        self.make_case("a-one")
        self.make_case("b-two")
        code, out, cases, results = self.run_bench(budget="0.9")
        self.assertEqual(code, 1, out)
        self.assertEqual([cases[n]["outcome"] for n in ("a-one", "b-two")], ["not-run", "not-run"])
        self.assertIn("would pass the budget $0.90", cases["a-one"]["message"])
        self.assertEqual(self.fake_calls("claude"), [])
        self.assertEqual(results["summary"]["cost"], 0)

    def test_stops_before_the_session_that_could_pass_it(self):
        for name in ("a-one", "b-two", "c-three"):
            self.make_case(name)
        self.claude(claude_entry(answer(finding())))
        code, out, cases, _ = self.run_bench(
            budget="1.6"
        )  # 0.5 spent + 1.0 cap fits, 1.0 + 1.0 not
        self.assertEqual(code, 1, out)
        self.assertEqual(
            [cases[n]["outcome"] for n in ("a-one", "b-two", "c-three")],
            ["caught-strict", "caught-strict", "not-run"],
        )
        self.assertEqual(len(self.fake_calls("claude")), 2)

    def test_a_lower_session_cap_lets_more_cases_run(self):
        for name in ("a-one", "b-two", "c-three"):
            self.make_case(name)
        self.claude(claude_entry(answer(finding())))
        code, out, cases, _ = self.run_bench("--session-budget-usd", "0.6", budget="1.6")
        self.assertEqual(code, 0, out)  # 0 + 0.6, 0.5 + 0.6, 1.0 + 0.6 all fit
        self.assertEqual(len(self.fake_calls("claude")), 3)
        argv = self.fake_calls("claude")[0]["argv"]
        self.assertEqual(argv[argv.index("--max-budget-usd") + 1], "0.6")

    def test_the_budget_is_required_and_positive(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                bench.main(["--cases", self.cases])
            with self.assertRaises(SystemExit):
                bench.main(["--cases", self.cases, "--budget-usd", "0"])


class Malformed(BenchCase):
    """AC-1"""

    def test_each_bad_field_is_named_and_the_rest_run(self):
        self.make_case("a-no-file", drop=("expected.file",))
        self.make_case("b-reversed", expected={"lines": "[14, 13]"})
        self.make_case("c-no-change-md", change_md=False)
        self.make_case("d-no-head", drop=("case.head", "source.pr"))
        self.make_case("e-good")
        self.claude(claude_entry(answer(finding())))
        code, out, cases, _ = self.run_bench()
        self.assertEqual(code, 1, out)
        self.assertIn("expected.file", cases["a-no-file"]["message"])
        self.assertIn("expected.lines", cases["b-reversed"]["message"])
        self.assertIn("no change.md", cases["c-no-change-md"]["message"])
        self.assertIn("case.head, source.pr", cases["d-no-head"]["message"])
        for name in ("a-no-file", "b-reversed", "c-no-change-md", "d-no-head"):
            self.assertEqual(cases[name]["outcome"], "malformed")
        self.assertEqual(cases["e-good"]["outcome"], "caught-strict")
        self.assertEqual(len(self.fake_calls("claude")), 1)

    def test_a_head_the_repository_lacks_is_an_error(self):
        self.make_case("a-gone")
        toml = os.path.join(self.cases, "a-gone", "case.toml")
        with open(toml, encoding="utf-8") as fh:
            text = fh.read().replace(self.head, "0" * 40)
        with open(toml, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        code, _, cases, _ = self.run_bench()
        self.assertEqual(code, 1)
        self.assertEqual(cases["a-gone"]["outcome"], "error")
        self.assertIn("cat-file", cases["a-gone"]["message"])
        self.assertEqual(self.fake_calls("claude"), [])


class ScoreUnit(unittest.TestCase):
    def case(self):
        return bench.Case(
            name="x",
            path="",
            repo="",
            fetch=[],
            base="",
            head="",
            branch="",
            file="pkg/a.py",
            lines=[40, 50],
            description="",
            pr=1,
            round=1,
            finding="F1",
            change_md="",
        )

    def test_the_strongest_matching_finding_is_reported(self):
        data = answer(
            finding("F1", file="pkg/a.py", line=45, severity="low"),
            finding("F2", file=".\\pkg\\a.py", line=30, severity="high"),
            finding("F3", file="pkg/b.py", line=45),
        )
        r = bench.score(self.case(), data, 15)
        self.assertEqual((r.outcome, r.finding, r.severity), ("caught-strict", "F2", "high"))
        self.assertEqual(len(r.other_findings), 2)

    def test_the_range_edges(self):
        for line, outcome in (
            (25, "caught-strict"),
            (24, "missed"),
            (65, "caught-strict"),
            (66, "missed"),
        ):
            with self.subTest(line=line):
                data = answer(finding(file="./pkg/a.py", line=line))
                self.assertEqual(bench.score(self.case(), data, 15).outcome, outcome)


class PilotCases(unittest.TestCase):
    """AC-6: the shipped cases load"""

    def test_the_three_pilot_cases(self):
        dirs = bench.case_dirs(bench.CASES_DIR, [])
        names = [os.path.basename(d) for d in dirs]
        self.assertEqual(len(names), 3, names)
        for path in dirs:
            with self.subTest(case=os.path.basename(path)):
                case = bench.load_case(path)
                self.assertEqual(len(case.head), 40)
                self.assertEqual(len(case.base), 40)
                self.assertTrue(case.fetch)
                self.assertIn("## Acceptance criteria", case.change_md)


if __name__ == "__main__":
    unittest.main()
