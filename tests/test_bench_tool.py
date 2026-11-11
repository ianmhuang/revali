"""feature/review-bench (M3.3.1): tools/review_bench.py runs one reviewer session per case on a
throwaway clone and scores whether a finding names the case's bug. Offline: the fixture
repository is the case's repository and the fake claude answers."""

import contextlib
import importlib.util
import io
import json
import os
import unittest
from unittest import mock

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

    def edit_case(self, name, old, new):
        toml = os.path.join(self.cases, name, "case.toml")
        with open(toml, encoding="utf-8", newline="") as fh:
            text = fh.read()
        self.assertIn(old, text)
        with open(toml, "w", encoding="utf-8", newline="") as fh:
            fh.write(text.replace(old, new))

    def run_bench(self, *extra, budget="10"):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = bench.main(
                ["--cases", self.cases, "--out", self.out, "--budget-usd", budget] + list(extra)
            )
        with open(os.path.join(self.out, "results.json"), encoding="utf-8") as fh:
            results = json.load(fh)
        self.assert_nothing_pushed()  # every run
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
        self.make_case("f-one-number", expected={"lines": "188"})  # round 1 F2
        self.make_case("g-a-string", expected={"lines": '"ab"'})
        path = self.make_case("h-not-utf8")
        with open(os.path.join(path, "change.md"), "wb") as fh:
            fh.write(b"## Request\n\xff\xfe\n")
        path = self.make_case("i-toml-not-utf8")  # round 2 F10
        with open(os.path.join(path, "case.toml"), "ab") as fh:
            fh.write(b"# caf\xe9 in a legacy code page\n")
        self.claude(claude_entry(answer(finding())))
        code, out, cases, _ = self.run_bench()
        self.assertEqual(code, 1, out)
        self.assertIn("expected.file", cases["a-no-file"]["message"])
        self.assertIn("expected.lines", cases["b-reversed"]["message"])
        self.assertIn("no change.md", cases["c-no-change-md"]["message"])
        self.assertIn("case.head, source.pr", cases["d-no-head"]["message"])
        self.assertIn("expected.lines", cases["f-one-number"]["message"])
        self.assertEqual(cases["g-a-string"]["message"].count("expected.lines"), 1)
        self.assertIn("cannot read change.md", cases["h-not-utf8"]["message"])
        self.assertIn("case.toml is not valid TOML", cases["i-toml-not-utf8"]["message"])
        for name in (
            "i-toml-not-utf8",
            "a-no-file",
            "b-reversed",
            "c-no-change-md",
            "d-no-head",
            "f-one-number",
            "g-a-string",
            "h-not-utf8",
        ):
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


class Isolation(BenchCase):
    """round 1 F1: the clone holds the base branch at the case's base, the case's branch, and
    no ref or reflog that leads to a later commit"""

    def test_nothing_past_the_round_is_reachable(self):
        fix = git(
            ["commit-tree", self.base + "^{tree}", "-p", self.base, "-m", "the fix"], self.repo
        ).strip()
        git(["update-ref", "refs/heads/main", fix], self.repo)
        git(["tag", "v-fix", fix], self.repo)
        path = self.make_case("a-iso")
        self.edit_case("a-iso", "fetch = []", 'fetch = ["refs/heads/main"]')
        case = bench.load_case(path)
        tmp = os.path.join(self.tmp, "iso")
        os.makedirs(tmp)
        clone, default = bench.prepare_clone(case, tmp)
        ctx = bench.locate_case(case, clone, default)
        self.assertEqual(ctx.base, "main")
        refs = git(["for-each-ref", "--format=%(refname) %(objectname)"], clone).splitlines()
        self.assertEqual(
            sorted(refs), ["refs/heads/bench/mul " + self.head, "refs/heads/main " + self.base]
        )
        self.assertNotIn(fix, git(["log", "--all", "--reflog", "--format=%H"], clone))
        self.assertFalse(os.path.exists(os.path.join(clone, ".git", "FETCH_HEAD")))
        self.assertEqual(git(["rev-parse", "HEAD"], clone).strip(), self.head)

    def test_a_case_on_the_base_branch_is_an_error(self):
        self.make_case("a-main")
        self.edit_case("a-main", 'branch = "bench/mul"', 'branch = "main"')
        code, _, cases, _ = self.run_bench()
        self.assertEqual(code, 1)
        self.assertEqual(cases["a-main"]["outcome"], "error")
        self.assertIn("'main'", cases["a-main"]["message"])
        self.assertEqual(self.fake_calls("claude"), [])


class Failures(BenchCase):
    """round 1 F3, F4, F7, F8"""

    def test_a_session_without_a_cost_counts_its_cap(self):
        self.make_case("a-garbled")
        self.make_case("b-next")
        self.claude(
            claude_entry(raw_stdout="not json", write_tests=False),
            claude_entry(answer(finding())),
        )
        code, out, cases, results = self.run_bench(budget="1.6")
        self.assertEqual(code, 1, out)
        a = cases["a-garbled"]
        self.assertEqual((a["outcome"], a["cost"], a["charged"]), ("error", 0, 1.0))
        self.assertIn("cost unknown", a["message"])
        self.assertEqual(cases["b-next"]["outcome"], "not-run")  # 1.0 + the 1.0 cap > 1.6
        self.assertEqual(len(self.fake_calls("claude")), 1)
        self.assertEqual((results["summary"]["cost"], results["summary"]["charged"]), (0, 1.0))
        self.assertIn("counted against the budget $1.00", out)

    def test_a_git_failure_is_an_error_and_the_next_case_runs(self):
        self.make_case("a-git")
        self.make_case("b-next")
        self.claude(claude_entry(answer(finding())))
        real = bench.build_prompt
        calls = []

        def broken_once(*a, **kw):
            calls.append(1)
            if len(calls) == 1:
                raise bench.gitops.GitError("git diff failed: no merge base")
            return real(*a, **kw)

        with mock.patch.object(bench, "build_prompt", broken_once):
            code, out, cases, _ = self.run_bench()
        self.assertEqual(code, 1, out)
        self.assertEqual(cases["a-git"]["outcome"], "error")
        self.assertIn("no merge base", cases["a-git"]["message"])
        self.assertEqual(cases["a-git"]["charged"], 0)  # no session ran
        self.assertEqual(cases["b-next"]["outcome"], "caught-strict")

    def test_an_interrupted_run_keeps_its_results(self):
        for name in ("a-one", "b-two", "c-three"):
            self.make_case(name)
        self.claude(claude_entry(answer(finding())))
        real = bench.spawn_reviewer
        calls = []

        def interrupted_second(*a, **kw):
            calls.append(1)
            if len(calls) == 2:
                raise KeyboardInterrupt
            return real(*a, **kw)

        with mock.patch.object(bench, "spawn_reviewer", interrupted_second):
            code, out, cases, _ = self.run_bench()
        self.assertEqual(code, 1, out)
        self.assertEqual(
            [cases[n]["outcome"] for n in ("a-one", "b-two", "c-three")],
            ["caught-strict", "interrupted", "not-run"],  # M3.3.3 AC-2: was "error"
        )
        self.assertIn("interrupted", cases["b-two"]["message"])

    def test_a_clone_left_behind_is_reported(self):
        self.make_case("a-kept")
        self.claude(claude_entry(answer(finding())))
        real, left = bench._rmtree, []
        with mock.patch.object(bench, "_rmtree", left.append):
            _, _, cases, _ = self.run_bench()
        for path in left:
            self.addCleanup(real, path)
        self.assertEqual(len(left), 1)
        self.assertIn("clone left at " + left[0], cases["a-kept"]["message"])

    def test_no_case_is_an_error(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = bench.main(["--cases", self.cases, "--out", self.out, "--budget-usd", "5"])
        self.assertEqual(code, 1)
        self.assertIn("no case directory", err.getvalue())

    def test_a_negative_margin_is_refused(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                bench.main(["--cases", self.cases, "--budget-usd", "5", "--line-margin", "-1"])


class DryRunAndDirectories(BenchCase):
    """M3.3.2 AC-3, AC-5"""

    def test_a_dry_run_builds_prompts_and_runs_no_session(self):
        self.make_case("a-ok")
        self.make_case("b-gone")
        self.edit_case("b-gone", self.head, "0" * 40)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = bench.main(["--dry-run", "--cases", self.cases, "--out", self.out])
        self.assertEqual(code, 1, out.getvalue())
        with open(os.path.join(self.out, "results.json"), encoding="utf-8") as fh:
            results = json.load(fh)
        cases = {c["case"]: c for c in results["cases"]}
        self.assertEqual(cases["a-ok"]["outcome"], "ready")
        self.assertEqual(cases["b-gone"]["outcome"], "error")
        self.assertTrue(results["dry_run"])
        self.assertIn("ready: 1/2; no session run", out.getvalue())
        self.assertTrue(os.path.isfile(os.path.join(self.out, "a-ok", "prompt.md")))
        self.assertEqual(self.fake_calls("claude"), [])
        self.assert_nothing_pushed()

    def test_without_a_dry_run_the_budget_is_still_required(self):
        self.make_case("a-ok")
        with contextlib.redirect_stderr(io.StringIO()) as err:
            with self.assertRaises(SystemExit):
                bench.main(["--cases", self.cases])
        self.assertIn("--dry-run", err.getvalue())

    def test_several_case_directories_share_one_budget(self):
        self.make_case("a-one")
        self.cases, first = os.path.join(self.tmp, "private"), self.cases
        self.make_case("b-two")
        self.make_case("c-three")
        self.claude(claude_entry(answer(finding())))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = bench.main(
                ["--cases", first, "--cases", self.cases, "--out", self.out, "--budget-usd", "2"]
            )  # 0+1, 0.5+1, 1.0+1 fit; a fourth would not
        self.assertEqual(code, 0, out.getvalue())
        self.assertEqual(len(self.fake_calls("claude")), 3)
        self.assertIn("caught: strict 3/3", out.getvalue())

    def test_a_name_in_two_directories_is_refused(self):
        self.make_case("a-one")
        self.cases, first = os.path.join(self.tmp, "private"), self.cases
        self.make_case("a-one")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = bench.main(
                ["--cases", first, "--cases", self.cases, "--out", self.out, "--budget-usd", "5"]
            )
        self.assertEqual(code, 1)
        self.assertIn("a case name is in two directories: a-one", err.getvalue())
        self.assertEqual(self.fake_calls("claude"), [])

    def test_a_case_carries_its_own_revali_toml(self):
        """a project that keeps revali.toml out of git: the case's copy goes into the clone"""
        path = self.make_case("a-own-config")
        config = self.read("revali.toml").replace("budget_usd = 1.0", "budget_usd = 0.7", 1)
        self.assertNotEqual(config, self.read("revali.toml"))
        with open(os.path.join(path, "revali.toml"), "w", encoding="utf-8", newline="") as fh:
            fh.write(config)
        self.make_case("b-bad-config")
        with open(os.path.join(self.cases, "b-bad-config", "revali.toml"), "w") as fh:
            fh.write("[review\n")
        self.claude(claude_entry(answer(finding())))
        code, out, cases, _ = self.run_bench()
        self.assertEqual(cases["a-own-config"]["outcome"], "caught-strict", out)
        argv = self.fake_calls("claude")[0]["argv"]
        self.assertEqual(argv[argv.index("--max-budget-usd") + 1], "0.7")
        self.assertEqual(cases["b-bad-config"]["outcome"], "malformed")
        self.assertIn("revali.toml is not valid TOML", cases["b-bad-config"]["message"])
        self.assertEqual(code, 1)

    def test_a_named_case_is_found_in_any_directory(self):
        self.cases, first = os.path.join(self.tmp, "private"), self.cases
        self.make_case("b-two")
        paths = bench.case_dirs([first, self.cases], ["b-two"])
        self.assertEqual(paths, [os.path.join(self.cases, "b-two")])

    def test_a_named_case_in_two_directories_is_refused(self):
        """round 1 F2"""
        self.make_case("a-one")
        self.cases, first = os.path.join(self.tmp, "private"), self.cases
        self.make_case("a-one")
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            code = bench.main(
                ["--cases", first, "--cases", self.cases, "--budget-usd", "5", "a-one"]
            )
        self.assertEqual(code, 1)
        self.assertIn("a case name is in two directories: a-one", err.getvalue())
        self.assertEqual(self.fake_calls("claude"), [])

    def test_a_cases_value_that_is_not_a_directory_stops_the_run(self):
        """round 1 F1: one missing directory of several would drop part of the set unnoticed"""
        self.make_case("a-one")
        missing = os.path.join(self.tmp, "no-such-dir")
        for argv in (
            ["--dry-run", "--cases", self.cases, "--cases", missing],
            ["--budget-usd", "5", "--cases", self.cases, "--cases", missing],
        ):
            with self.subTest(argv=argv[0]):
                err = io.StringIO()
                with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
                    code = bench.main(argv + ["--out", self.out])
                self.assertEqual(code, 1)
                self.assertIn("no case directory under %s" % missing, err.getvalue())
                self.assertFalse(os.path.exists(os.path.join(self.out, "results.json")))
        self.assertEqual(self.fake_calls("claude"), [])

    def test_a_tilde_in_a_cases_value_is_expanded(self):
        self.cases = os.path.join(self.tmp, "home", "private")
        self.make_case("a-one")
        home = os.path.join(self.tmp, "home")
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"HOME": home, "USERPROFILE": home}):
            with contextlib.redirect_stdout(out):
                code = bench.main(["--dry-run", "--cases", "~/private", "--out", self.out])
        self.assertEqual(code, 0, out.getvalue())
        self.assertIn("ready: 1/1", out.getvalue())

    def test_an_empty_case_revali_toml_still_replaces_the_clones(self):
        """AC-7: the case's file replaces the clone's, whatever it holds"""
        path = self.make_case("a-empty-config")
        with open(os.path.join(path, "revali.toml"), "w", encoding="utf-8", newline="") as fh:
            fh.write("")
        self.assertEqual(bench.load_case(path).config, "")
        self.assertIsNone(bench.load_case(self.make_case("b-none")).config)
        clone = os.path.join(self.tmp, "clone")
        git(["clone", "--quiet", self.repo, clone], self.tmp)
        with self.assertRaisesRegex(Exception, "before revali can run"):  # revali refuses it
            bench.locate_case(bench.load_case(path), clone, "main")
        with open(os.path.join(clone, "revali.toml"), encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "")

    def test_a_case_named_twice_is_refused(self):
        """M3.3.3 AC-7"""
        self.make_case("a-one")
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            code = bench.main(["--cases", self.cases, "--budget-usd", "5", "a-one", "a-one"])
        self.assertEqual(code, 1)
        self.assertIn("case named twice: a-one", err.getvalue())
        self.assertNotIn("two directories", err.getvalue())
        self.assertEqual(self.fake_calls("claude"), [])

    def test_two_directories_of_one_name_are_not_named_twice(self):
        """PR #72 round 1 F3"""
        first = self.make_case("x")
        self.cases = os.path.join(self.tmp, "private")
        second = self.make_case("x")
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            code = bench.main(["--budget-usd", "5", first, second])
        self.assertEqual(code, 1)
        self.assertIn(
            "a case name is in two directories: x (%s and %s)" % (first, second), err.getvalue()
        )
        self.assertNotIn("named twice", err.getvalue())


class Resume(BenchCase):
    """M3.3.3 AC-1 to AC-5: the fixture's session cap is $1.00, each fake session costs $0.50"""

    def results_file(self):
        with open(os.path.join(self.out, "results.json"), encoding="utf-8") as fh:
            return json.load(fh)

    def killed_at(self, n):
        """A spawn_reviewer whose n-th call ends the run the way a power cut does: the
        results.json of that moment is all that is left (no handler runs afterwards)."""
        real, calls, snapshot = bench.spawn_reviewer, [], {}

        def spawn(*a, **kw):
            calls.append(1)
            if len(calls) == n:
                with open(os.path.join(self.out, "results.json"), encoding="utf-8") as fh:
                    snapshot["text"] = fh.read()
                raise KeyboardInterrupt
            return real(*a, **kw)

        def restore():
            with open(os.path.join(self.out, "results.json"), "w", encoding="utf-8") as fh:
                fh.write(snapshot["text"])

        return spawn, restore

    def first_run_killed_at(self, n, *extra, budget="10"):
        spawn, restore = self.killed_at(n)
        with mock.patch.object(bench, "spawn_reviewer", spawn):
            self.run_bench(*extra, budget=budget)
        restore()

    def resume(self, *extra, budget="10"):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = bench.main(["--resume", self.out, "--budget-usd", budget] + list(extra))
        self.assert_nothing_pushed()
        return code, out.getvalue(), err.getvalue()

    def by_name(self, results):
        return {c["case"]: c for c in results["cases"]}

    def test_a_session_is_recorded_as_started_before_it_runs(self):
        """AC-1"""
        self.make_case("a-one")
        self.make_case("b-two")
        self.claude(claude_entry(answer(finding())))
        real, seen = bench.spawn_reviewer, []

        def spawn(*a, **kw):
            seen.append(self.by_name(self.results_file()))
            return real(*a, **kw)

        with mock.patch.object(bench, "spawn_reviewer", spawn):
            code, out, cases, _ = self.run_bench()
        self.assertEqual(code, 0, out)
        self.assertEqual(list(seen[0]), ["a-one"])
        self.assertEqual(seen[0]["a-one"]["outcome"], "started")
        self.assertEqual(seen[0]["a-one"]["charged"], 1.0)
        self.assertEqual(seen[1]["a-one"]["outcome"], "caught-strict")
        self.assertEqual(
            (seen[1]["b-two"]["outcome"], seen[1]["b-two"]["charged"]), ("started", 1.0)
        )
        self.assertEqual(
            [(c["outcome"], c["charged"]) for c in cases.values()],
            [("caught-strict", 0.5), ("caught-strict", 0.5)],
        )

    def test_ctrl_c_charges_the_session_cap(self):
        """AC-2: during the session the cap counts; before it, nothing does"""
        for name in ("a-one", "b-two", "c-three"):
            self.make_case(name)
        self.claude(claude_entry(answer(finding())))
        spawn, _ = self.killed_at(2)
        with mock.patch.object(bench, "spawn_reviewer", spawn):
            code, out, cases, results = self.run_bench()
        self.assertEqual(code, 1, out)
        b = cases["b-two"]
        self.assertEqual((b["outcome"], b["cost"], b["charged"]), ("interrupted", 0, 1.0))
        self.assertIn("cost unknown", b["message"])
        self.assertEqual(cases["c-three"]["outcome"], "not-run")
        self.assertEqual(results["summary"]["charged"], 1.5)

        os.remove(os.path.join(self.out, "results.json"))
        with mock.patch.object(bench, "prepare_clone", side_effect=KeyboardInterrupt):
            code, out, cases, _ = self.run_bench()
        self.assertEqual((cases["a-one"]["outcome"], cases["a-one"]["charged"]), ("interrupted", 0))
        self.assertNotIn("cost unknown", cases["a-one"]["message"])

    def test_resume_after_the_machine_went_down(self):
        """AC-3, AC-5: the scored case is kept, the started one charged once and run again"""
        for name in ("a-one", "b-two", "c-three"):
            self.make_case(name)
        self.claude(claude_entry(answer(finding())))
        self.first_run_killed_at(2)
        self.assertEqual(self.by_name(self.results_file())["b-two"]["outcome"], "started")
        before = len(self.fake_calls("claude"))
        code, out, err = self.resume()
        self.assertEqual(code, 0, out + err)
        self.assertEqual(len(self.fake_calls("claude")) - before, 2)  # b-two and c-three
        results = self.results_file()
        cases = self.by_name(results)
        self.assertEqual(list(cases), ["a-one", "b-two", "c-three"])
        self.assertTrue(all(c["outcome"] == "caught-strict" for c in cases.values()))
        b = cases["b-two"]
        self.assertEqual((b["cost"], b["charged"]), (0.5, 1.5))
        self.assertIn("interrupted", b["message"])
        self.assertEqual(results["summary"]["cost"], 1.5)
        self.assertEqual(results["summary"]["charged"], 2.5)
        self.assertIn("caught: strict 3/3", out)
        self.assertIn("counted against the budget $2.50", out)
        a_line = next(line for line in out.splitlines() if line.startswith("a-one"))
        self.assertIn("kept from the earlier run", a_line)

    def test_resume_after_ctrl_c_runs_only_what_has_no_result(self):
        """AC-3: error, malformed and missed are kept; interrupted, not-run and new cases run"""
        self.make_case("a-err")
        self.make_case("b-missed")
        self.make_case("c-bad", drop=("expected.file",))
        self.make_case("d-cut")
        self.make_case("e-later")
        self.claude(
            claude_entry(is_error=True, cost=0.25, write_tests=False),
            claude_entry(answer(finding(file="src/other.py"))),
            claude_entry(answer(finding())),
        )
        spawn, _ = self.killed_at(3)
        with mock.patch.object(bench, "spawn_reviewer", spawn):
            self.run_bench()
        outcomes = {n: c["outcome"] for n, c in self.by_name(self.results_file()).items()}
        self.assertEqual(
            outcomes,
            {
                "a-err": "error",
                "b-missed": "missed",
                "c-bad": "malformed",
                "d-cut": "interrupted",
                "e-later": "not-run",
            },
        )
        self.make_case("f-new")
        before = len(self.fake_calls("claude"))
        code, out, err = self.resume()
        self.assertEqual(code, 1, out + err)  # a-err and c-bad are still not scored
        self.assertEqual(len(self.fake_calls("claude")) - before, 3)  # d, e, f
        cases = self.by_name(self.results_file())
        self.assertEqual(cases["a-err"]["outcome"], "error")
        self.assertEqual(cases["b-missed"]["outcome"], "missed")
        for name in ("d-cut", "e-later", "f-new"):
            self.assertEqual(cases[name]["outcome"], "caught-strict", name)
        self.assertEqual(cases["d-cut"]["charged"], 1.5)
        self.assertIn("caught: strict 3/4", out)

    def test_the_earlier_charge_counts_against_the_budget(self):
        """AC-3: --budget-usd is the total of every invocation"""
        self.make_case("a-one")
        self.make_case("b-two")
        self.claude(claude_entry(answer(finding())))
        self.first_run_killed_at(2)  # a-one $0.50, b-two started with its $1.00 cap
        before = len(self.fake_calls("claude"))
        code, out, _ = self.resume(budget="2.4")  # 1.5 spent + 1.0 cap > 2.4
        self.assertEqual(code, 1, out)
        self.assertEqual(len(self.fake_calls("claude")), before)
        b = self.by_name(self.results_file())["b-two"]
        self.assertEqual((b["outcome"], b["charged"]), ("not-run", 1.0))  # still charged once
        code, out, _ = self.resume(budget="2.5")
        self.assertEqual(code, 0, out)
        results = self.results_file()
        self.assertEqual(self.by_name(results)["b-two"]["charged"], 1.5)
        self.assertEqual(results["summary"]["charged"], 2.0)
        self.assertEqual(results["budget_usd"], 2.5)

    def test_named_cases_narrow_the_run_and_the_rest_stay(self):
        """AC-4"""
        for name in ("a-one", "b-two", "c-three"):
            self.make_case(name)
        self.claude(claude_entry(answer(finding())))
        spawn, _ = self.killed_at(2)  # Ctrl-C: b-two interrupted, c-three not-run in the file
        with mock.patch.object(bench, "spawn_reviewer", spawn):
            self.run_bench()
        before = len(self.fake_calls("claude"))
        code, out, _ = self.resume("b-two")
        self.assertEqual(len(self.fake_calls("claude")) - before, 1)
        cases = self.by_name(self.results_file())
        self.assertEqual(
            {n: c["outcome"] for n, c in cases.items()},
            {"a-one": "caught-strict", "b-two": "caught-strict", "c-three": "not-run"},
        )
        self.assertEqual(code, 1, out)  # c-three is still not run
        before = len(self.fake_calls("claude"))
        self.resume("a-one")  # kept: reported, not run
        self.assertEqual(len(self.fake_calls("claude")), before)

    def test_settings_come_from_the_earlier_run(self):
        """AC-4: what is not given is taken from results.json"""
        self.make_case("a-one")
        self.make_case("b-two")
        self.claude(claude_entry(answer(finding())))
        self.first_run_killed_at(
            2, "--model", "claude-sonnet-5-5", "--session-budget-usd", "0.6", "--line-margin", "3"
        )
        code, out, err = self.resume()
        self.assertEqual(code, 0, out + err)
        argv = self.fake_calls("claude")[-1]["argv"]
        self.assertEqual(argv[argv.index("--model") + 1], "claude-sonnet-5-5")
        self.assertEqual(argv[argv.index("--max-budget-usd") + 1], "0.6")
        results = self.results_file()
        self.assertEqual(
            (results["model"], results["session_budget_usd"], results["line_margin"]),
            ("claude-sonnet-5-5", 0.6, 3),
        )
        self.assertEqual(results["cases_dirs"], [self.cases])

    def test_refusals(self):
        """AC-4: exit 1, the reason named, no session"""
        self.make_case("a-one")
        self.make_case("b-two")
        self.claude(claude_entry(answer(finding())))
        self.first_run_killed_at(2)
        other = os.path.join(self.tmp, "other-cases")
        os.makedirs(other)
        before = len(self.fake_calls("claude"))
        for extra, needle in (
            (["--model", "claude-opus-5-5"], "model"),
            (["--session-budget-usd", "2"], "session cap"),
            (["--line-margin", "5"], "line margin"),
            (["--cases", other], "case directories"),
            (["--out", other], "--out"),
            (["--dry-run"], "--dry-run"),
        ):
            with self.subTest(needle=needle):
                code, out, err = self.resume(*extra)
                self.assertEqual(code, 1, out)
                self.assertIn(needle, err)
        text = json.dumps(dict(self.results_file(), prompt_version="0"))
        with open(os.path.join(self.out, "results.json"), "w", encoding="utf-8") as fh:
            fh.write(text)
        code, _, err = self.resume()
        self.assertEqual(code, 1)
        self.assertIn("PROMPT_VERSION", err)
        self.assertEqual(len(self.fake_calls("claude")), before)

    def test_a_failed_write_leaves_the_previous_file_whole(self):
        """PR #72 round 1 F1"""
        self.make_case("a-one")
        self.claude(claude_entry(answer(finding())))
        self.run_bench()
        before = self.results_file()
        args = mock.Mock(
            model="",
            dry_run=False,
            cases_dir=[self.cases],
            budget_usd=1.0,
            session_budget_usd=0.0,
            line_margin=15,
        )
        with mock.patch.object(bench.json, "dump", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                bench._write_results(self.out, args, [])
        self.assertEqual(self.results_file(), before)

    def test_a_resume_from_another_directory(self):
        """PR #72 round 1 F2: relative --cases are recorded absolute"""
        self.make_case("a-one")
        self.make_case("b-two")
        self.claude(claude_entry(answer(finding())))
        cwd = os.getcwd()
        self.addCleanup(os.chdir, cwd)
        os.chdir(self.tmp)
        spawn, restore = self.killed_at(2)
        with mock.patch.object(bench, "spawn_reviewer", spawn):
            with contextlib.redirect_stdout(io.StringIO()):
                bench.main(["--cases", "cases", "--out", self.out, "--budget-usd", "10"])
        restore()
        self.assertEqual(self.results_file()["cases_dirs"], [os.path.abspath(self.cases)])
        os.chdir(self.repo)
        code, out, err = self.resume()
        self.assertEqual(code, 0, out + err)
        code, out, err = self.resume("--cases", self.cases)  # the same directory, given again
        self.assertEqual(code, 0, out + err)

    def test_a_mistyped_name_on_resume_is_refused(self):
        """PR #72 round 1 F4"""
        self.make_case("a-one")
        self.make_case("b-two")
        self.claude(claude_entry(answer(finding())))
        self.first_run_killed_at(2)
        before, calls = self.results_file(), len(self.fake_calls("claude"))
        code, _, err = self.resume("b-tow")
        self.assertEqual(code, 1)
        self.assertIn("no case b-tow in the earlier run or the case directories", err)
        self.assertEqual(self.results_file(), before)
        self.assertEqual(len(self.fake_calls("claude")), calls)

    def test_a_tilde_in_the_resume_value_and_bad_settings(self):
        """PR #72 round 1 follow-ups"""
        self.make_case("a-one")
        self.make_case("b-two")
        self.claude(claude_entry(answer(finding())))
        self.first_run_killed_at(2)
        home = os.path.dirname(self.out)
        with mock.patch.dict(os.environ, {"HOME": home, "USERPROFILE": home}):
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = bench.main(
                    ["--resume", "~/" + os.path.basename(self.out), "--budget-usd", "10"]
                )
        self.assertEqual(code, 0, out.getvalue() + err.getvalue())
        good = self.results_file()
        for key, value in (("model", 5), ("session_budget_usd", "8"), ("line_margin", True)):
            with self.subTest(key=key):
                with open(os.path.join(self.out, "results.json"), "w", encoding="utf-8") as fh:
                    json.dump(dict(good, **{key: value}), fh)
                code, _, err = self.resume()
                self.assertEqual(code, 1)
                self.assertIn("not a benchmark results.json", err)

    def test_no_usable_results_file_is_refused(self):
        """AC-4"""
        self.make_case("a-one")
        os.makedirs(self.out)
        code, _, err = self.resume()
        self.assertEqual(code, 1)
        self.assertIn("results.json", err)
        for text in ("{not json", "[]", '{"cases": "x"}'):
            with self.subTest(text=text):
                with open(os.path.join(self.out, "results.json"), "w", encoding="utf-8") as fh:
                    fh.write(text)
                code, _, err = self.resume()
                self.assertEqual(code, 1)
                self.assertIn("results.json", err)
        with contextlib.redirect_stdout(io.StringIO()):
            bench.main(["--dry-run", "--cases", self.cases, "--out", self.out])
        code, _, err = self.resume()
        self.assertEqual(code, 1)
        self.assertIn("dry run", err)
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
    """AC-6 (M3.3.1) and M3.3.2 AC-4: the shipped cases load; 6 from revali (the 3 pilot
    cases among them), 4 from bmc-toolkit, one per PR"""

    def test_the_committed_cases(self):
        dirs = bench.case_dirs(bench.CASES_DIR, [])
        cases = [bench.load_case(d) for d in dirs]
        per_repo = {}
        for case in cases:
            per_repo.setdefault(case.repo.rsplit("/", 1)[-1], []).append(case.pr)
        self.assertEqual(
            {repo: len(prs) for repo, prs in per_repo.items()}, {"revali": 6, "bmc-toolkit": 4}
        )
        self.assertLessEqual({43, 61, 69}, set(per_repo["revali"]))
        for prs in per_repo.values():
            self.assertEqual(len(prs), len(set(prs)))  # one case per PR
        for path in dirs:
            with self.subTest(case=os.path.basename(path)):
                case = bench.load_case(path)
                self.assertEqual(len(case.head), 40)
                self.assertEqual(len(case.base), 40)
                self.assertTrue(case.fetch)
                self.assertIn("## Acceptance criteria", case.change_md)

    def test_no_committed_request_holds_the_users_words(self):
        """M3.3.2 AC-2: a committed case's Request is a paraphrase; the user writes in
        Chinese, so a non-ASCII character there is their own words"""
        for path in bench.case_dirs(bench.CASES_DIR, []):
            with self.subTest(case=os.path.basename(path)):
                text = bench.load_case(path).change_md
                request = text.split("## Request\n", 1)[1].split("\n## ", 1)[0]
                self.assertTrue(request.strip())
                self.assertEqual([c for c in request if ord(c) > 127], [])


if __name__ == "__main__":
    unittest.main()
