"""Acceptance tests for feature/bench-resume (M3.3.3): AC-1 to AC-5 and AC-7.

AC-1: before a session starts, results.json holds the case as `started` with the session cap
charged. AC-2: Ctrl-C during a session is `interrupted` with the cap charged and the cost said
to be unknown; before the session it charges nothing. AC-3: `--resume DIR` keeps the cases
that have a result, runs the others, and counts what the file already charged against
`--budget-usd`. AC-4: what `--resume` refuses, and case names narrowing the run. AC-5: the
summary covers kept and new cases and a kept case's line says so. AC-7: a case named twice.

The tool's `main` is called with a command line, as `python tools/review_bench.py` would. The
reviewer is a probe standing in for `claude`: it copies results.json as it is when the session
starts, then hands over to the fake claude. "The machine went down during session n" is that
copy put back as results.json: what a kill with no handler leaves behind. Ctrl-C is a
KeyboardInterrupt raised where the tool starts the session (`spawn_reviewer`) or builds the
prompt (`build_prompt`), the two revali functions the tool's own docstring names.

The fixture repository's session cap is $1.00 and a fake session costs $0.50.
"""

import contextlib
import importlib.util
import io
import json
import os
import sys
import unittest
from unittest import mock

from tests.fixtures.make_sample_repo import CHANGE_MD
from tests.helpers import CLAUDE_STUB, ROOT, RepoCase, approve_response, claude_entry, git

SPEC = importlib.util.spec_from_file_location(
    "review_bench", os.path.join(ROOT, "tools", "review_bench.py")
)
bench = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bench)

PROBE = """\
import os
import shutil
import subprocess
import sys

src = os.environ["BENCH_RESUME_RESULTS"]
dst = os.environ["BENCH_RESUME_SNAPSHOTS"]
target = os.path.join(dst, "%03d.json" % (len(os.listdir(dst)) + 1))
if os.path.isfile(src):
    shutil.copyfile(src, target)
else:
    with open(target, "w", encoding="utf-8") as fh:
        fh.write("")
sys.exit(subprocess.call([sys.executable, os.environ["BENCH_RESUME_STUB"]] + sys.argv[1:]))
"""

SCORED = ("caught-strict", "caught-loose", "missed")
GOAL = "`mul` multiplies two integers."


def quoted(path):
    if " " not in path:
        return path
    return '"%s"' % path if os.name == "nt" else "'%s'" % path


def marker(name):
    return "[bench case %s]" % name


def finding(file="src/calc.py", line=12):
    return {
        "id": "F1",
        "file": file,
        "line": line,
        "severity": "medium",
        "kind": "correctness",
        "text": "mul is wrong for large operands",
        "suggestion": "fix it",
    }


def answer(*findings):
    return claude_entry(approve_response(verdict="CHANGES_REQUESTED", findings=list(findings)))


class ResumeCase(RepoCase):
    def setUp(self):
        super().setUp()
        self.assertIn(GOAL, CHANGE_MD)
        self.cases = os.path.join(self.tmp, "cases")
        self.out = os.path.join(self.tmp, "bench out")
        self.results_path = os.path.join(self.out, "results.json")
        self.base = git(["rev-parse", "main"], self.repo).strip()
        self.head = git(["rev-parse", "feature/mul"], self.repo).strip()
        self.names = []
        self.stderr = ""
        self.snap_dir = os.path.join(self.tmp, "snapshots")
        os.makedirs(self.snap_dir)
        probe = os.path.join(self.tmp, "probe_claude.py")
        with open(probe, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(PROBE)
        os.environ["BENCH_RESUME_RESULTS"] = self.results_path
        os.environ["BENCH_RESUME_SNAPSHOTS"] = self.snap_dir
        os.environ["BENCH_RESUME_STUB"] = CLAUDE_STUB
        os.environ["REVALI_CLAUDE_CMD"] = "%s %s" % (quoted(sys.executable), quoted(probe))

    def make_case(self, name, malformed=False):
        """A case on the fixture repository; its change.md carries the case's name, so the
        prompt of a session tells which case it was for."""
        path = os.path.join(self.cases, name)
        os.makedirs(path)
        lines = [
            "[case]",
            "repo = %s" % json.dumps(self.repo),
            "fetch = []",
            "base = %s" % json.dumps(self.base),
            "head = %s" % json.dumps(self.head),
            'branch = "bench/mul"',
            "",
            "[expected]",
            'file = "src/calc.py"',
            "lines = [12, 13]",
            'description = "mul is wrong for large operands"',
            "",
            "[source]",
            "pr = 7",
            "round = 1",
            'finding = "F1"',
            "",
        ]
        if malformed:
            lines.remove('file = "src/calc.py"')
        with open(os.path.join(path, "case.toml"), "w", encoding="utf-8", newline="\n") as fh:
            fh.write("\n".join(lines))
        with open(os.path.join(path, "change.md"), "w", encoding="utf-8", newline="\n") as fh:
            fh.write(CHANGE_MD.replace(GOAL, "%s %s" % (GOAL, marker(name))))
        self.names.append(name)
        return path

    def run_tool(self, *argv):
        """(exit code, stdout); stderr is kept in self.stderr."""
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = bench.main([str(a) for a in argv])
            except SystemExit as exc:  # argparse: an option this tool does not have
                code = exc.code
        self.stderr = err.getvalue()
        self.assertEqual(self.fake_calls("gh"), [])
        return code, out.getvalue()

    def first_run(self, *extra, budget="10"):
        return self.run_tool(
            "--cases", self.cases, "--out", self.out, "--budget-usd", budget, *extra
        )

    def resume(self, *extra, budget="10", directory=None):
        return self.run_tool("--resume", directory or self.out, "--budget-usd", budget, *extra)

    def ctrl_c_at_session(self, n):
        """Patch: the n-th session of the run is interrupted instead of started."""
        real, calls = bench.spawn_reviewer, []

        def spawn(*args, **kwargs):
            calls.append(1)
            if len(calls) == n:
                raise KeyboardInterrupt
            return real(*args, **kwargs)

        return mock.patch.object(bench, "spawn_reviewer", spawn)

    def snapshots(self):
        """results.json as each session so far found it, oldest first ("" when there was none)."""
        texts = []
        for name in sorted(os.listdir(self.snap_dir)):
            with open(os.path.join(self.snap_dir, name), encoding="utf-8") as fh:
                texts.append(fh.read())
        return texts

    def parsed(self, text):
        self.assertTrue(text.strip(), "no results.json existed when the session started")
        data = json.loads(text)
        return {c["case"]: c for c in data["cases"]}, data

    def machine_went_down_at(self, snapshot_no):
        """Leave results.json as the snapshot_no-th session found it."""
        text = self.snapshots()[snapshot_no - 1]
        self.assertTrue(text.strip(), "no results.json existed when the session started")
        with open(self.results_path, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)

    def results(self):
        with open(self.results_path, encoding="utf-8") as fh:
            return self.parsed(fh.read())

    def file_text(self):
        with open(self.results_path, encoding="utf-8", newline="") as fh:
            return fh.read()

    def sessions(self):
        return len(self.fake_calls("claude"))

    def ran(self, since):
        """The cases the sessions after the first `since` were for, in order."""
        names = []
        for call in self.fake_calls("claude")[since:]:
            hits = [n for n in self.names if marker(n) in call["prompt"]]
            self.assertEqual(len(hits), 1, hits)
            names.append(hits[0])
        return names

    def line_of(self, out, name):
        lines = [ln for ln in out.splitlines() if ln.startswith(name + " ")]
        self.assertEqual(len(lines), 1, out)
        return lines[0]


class StartedRecord(ResumeCase):
    """AC-1"""

    def test_results_json_names_the_case_and_its_cap_before_the_session_runs(self):
        self.make_case("a-one")
        self.make_case("b-two")
        self.claude(answer(finding()))
        code, out = self.first_run()
        self.assertEqual(code, 0, out + self.stderr)
        texts = self.snapshots()
        self.assertEqual(len(texts), 2)
        first, _ = self.parsed(texts[0])
        self.assertEqual(first["a-one"]["outcome"], "started")
        self.assertAlmostEqual(first["a-one"]["charged"], 1.0)  # the session cap
        self.assertNotIn(first.get("b-two", {}).get("outcome"), SCORED)
        second, _ = self.parsed(texts[1])
        self.assertEqual(second["a-one"]["outcome"], "caught-strict")
        self.assertAlmostEqual(second["a-one"]["charged"], 0.5)
        self.assertEqual(second["b-two"]["outcome"], "started")
        self.assertAlmostEqual(second["b-two"]["charged"], 1.0)
        # when the session ends, the entry is replaced by its result
        cases, data = self.results()
        self.assertEqual([cases[n]["outcome"] for n in ("a-one", "b-two")], ["caught-strict"] * 2)
        for name in ("a-one", "b-two"):
            self.assertAlmostEqual(cases[name]["charged"], 0.5)
        self.assertAlmostEqual(data["summary"]["charged"], 1.0)

    def test_the_cap_recorded_is_the_one_the_session_runs_under(self):
        self.make_case("a-one")
        self.claude(answer(finding()))
        code, out = self.first_run("--session-budget-usd", "0.6")
        self.assertEqual(code, 0, out + self.stderr)
        first, _ = self.parsed(self.snapshots()[0])
        self.assertEqual(first["a-one"]["outcome"], "started")
        self.assertAlmostEqual(first["a-one"]["charged"], 0.6)


class CtrlC(ResumeCase):
    """AC-2"""

    def test_during_a_session_the_case_is_interrupted_and_its_cap_is_charged(self):
        for name in ("a-one", "b-two", "c-three"):
            self.make_case(name)
        self.claude(answer(finding()))
        with self.ctrl_c_at_session(2):
            code, out = self.first_run()
        self.assertEqual(code, 1, out + self.stderr)
        cases, data = self.results()
        self.assertEqual(cases["a-one"]["outcome"], "caught-strict")
        b = cases["b-two"]
        self.assertEqual(b["outcome"], "interrupted")
        self.assertAlmostEqual(b["cost"], 0.0)
        self.assertAlmostEqual(b["charged"], 1.0)
        self.assertRegex(b["message"], r"(?i)unknown")
        self.assertIn("interrupted", self.line_of(out, "b-two"))
        self.assertEqual(cases["c-three"]["outcome"], "not-run")
        self.assertAlmostEqual(cases["c-three"]["charged"], 0.0)
        self.assertAlmostEqual(data["summary"]["cost"], 0.5)
        self.assertAlmostEqual(data["summary"]["charged"], 1.5)
        self.assertEqual(self.sessions(), 1)

    def test_before_the_session_nothing_is_charged(self):
        self.make_case("a-one")
        self.make_case("b-two")
        self.claude(answer(finding()))
        with mock.patch.object(bench, "build_prompt", side_effect=KeyboardInterrupt):
            code, out = self.first_run()
        self.assertEqual(code, 1, out + self.stderr)
        cases, data = self.results()
        self.assertEqual(cases["a-one"]["outcome"], "interrupted")
        self.assertAlmostEqual(cases["a-one"]["charged"], 0.0)
        self.assertEqual(cases["b-two"]["outcome"], "not-run")
        self.assertAlmostEqual(data["summary"]["charged"], 0.0)
        self.assertEqual(self.sessions(), 0)


class Resume(ResumeCase):
    """AC-3, AC-5"""

    def test_after_the_machine_went_down_the_scored_case_is_kept_and_the_rest_run(self):
        for name in ("a-one", "b-two", "c-three"):
            self.make_case(name)
        self.claude(answer(finding()))
        self.first_run()
        self.machine_went_down_at(2)  # a-one scored, b-two's session running, c-three unknown
        earlier, _ = self.results()
        self.assertEqual(earlier["b-two"]["outcome"], "started")
        before = self.sessions()
        code, out = self.resume()
        self.assertEqual(code, 0, out + self.stderr)
        self.assertEqual(self.ran(before), ["b-two", "c-three"])  # a-one is not paid for again
        cases, data = self.results()  # the same directory
        self.assertEqual(sorted(cases), ["a-one", "b-two", "c-three"])
        for name in cases:
            self.assertEqual(cases[name]["outcome"], "caught-strict", name)
        # b-two: the new session's cost, and the interrupted session's cap still charged
        self.assertAlmostEqual(cases["b-two"]["cost"], 0.5)
        self.assertAlmostEqual(cases["b-two"]["charged"], 1.5)
        self.assertRegex(cases["b-two"]["message"], r"(?i)unknown")
        self.assertAlmostEqual(cases["a-one"]["charged"], 0.5)
        self.assertAlmostEqual(cases["c-three"]["charged"], 0.5)
        # AC-5: the summary covers kept and new cases, the charged total the lost session's cap
        summary = data["summary"]
        self.assertEqual((summary["scored"], summary["strict"]), (3, 3))
        self.assertAlmostEqual(summary["cost"], 1.5)
        self.assertAlmostEqual(summary["charged"], 2.5)
        self.assertRegex(out, r"strict 3/3")
        self.assertIn("2.50", out.strip().splitlines()[-1])
        self.assertRegex(self.line_of(out, "a-one"), r"(?i)\bkept\b")
        self.assertNotRegex(self.line_of(out, "c-three"), r"(?i)\bkept\b")

    def test_after_ctrl_c_only_the_cases_without_a_result_run(self):
        self.make_case("a-err")
        self.make_case("b-missed")
        self.make_case("c-bad", malformed=True)
        self.make_case("d-cut")
        self.make_case("e-later")
        self.claude(
            claude_entry(is_error=True, cost=0.25, write_tests=False),
            answer(finding(file="src/other.py")),
            answer(finding()),
        )
        with self.ctrl_c_at_session(3):  # a-err and b-missed ran; d-cut's session is cut
            self.first_run()
        earlier, _ = self.results()
        self.assertEqual(
            {n: c["outcome"] for n, c in earlier.items()},
            {
                "a-err": "error",
                "b-missed": "missed",
                "c-bad": "malformed",
                "d-cut": "interrupted",
                "e-later": "not-run",
            },
        )
        self.make_case("f-new")  # in the case directory, not in the file
        before = self.sessions()
        code, out = self.resume()
        self.assertEqual(self.ran(before), ["d-cut", "e-later", "f-new"])
        cases, data = self.results()
        self.assertEqual(sorted(cases), sorted(self.names))
        self.assertEqual(cases["a-err"]["outcome"], "error")
        self.assertAlmostEqual(cases["a-err"]["cost"], 0.25)
        self.assertEqual(cases["b-missed"]["outcome"], "missed")
        self.assertEqual(cases["c-bad"]["outcome"], "malformed")
        for name in ("d-cut", "e-later", "f-new"):
            self.assertEqual(cases[name]["outcome"], "caught-strict", name)
        self.assertAlmostEqual(cases["d-cut"]["charged"], 1.5)  # the cut session's cap + 0.5
        self.assertEqual(code, 1, out + self.stderr)  # a-err and c-bad are still not scored
        summary = data["summary"]
        self.assertEqual((summary["scored"], summary["strict"]), (4, 3))
        self.assertAlmostEqual(summary["cost"], 0.25 + 0.5 * 4)
        self.assertAlmostEqual(summary["charged"], 0.25 + 0.5 + 1.5 + 0.5 + 0.5)
        self.assertRegex(out, r"strict 3/4")
        for name in ("a-err", "b-missed", "c-bad"):
            self.assertRegex(self.line_of(out, name), r"(?i)\bkept\b")

    def test_the_budget_is_the_total_of_every_invocation(self):
        self.make_case("a-one")
        self.make_case("b-two")
        self.claude(answer(finding()))
        self.first_run()
        self.machine_went_down_at(2)  # charged so far: a-one $0.50, b-two's cap $1.00
        before = self.sessions()
        code, out = self.resume(budget="2.4")  # 1.5 + the next session's cap 1.0 passes 2.4
        self.assertEqual(code, 1, out + self.stderr)
        self.assertEqual(self.sessions(), before)
        cases, data = self.results()
        self.assertEqual(cases["a-one"]["outcome"], "caught-strict")
        self.assertEqual(cases["b-two"]["outcome"], "not-run")
        self.assertAlmostEqual(data["summary"]["charged"], 1.5)  # the lost cap, counted once
        code, out = self.resume(budget="2.5")  # 1.5 + 1.0 fits
        self.assertEqual(code, 0, out + self.stderr)
        self.assertEqual(self.ran(before), ["b-two"])
        cases, data = self.results()
        self.assertEqual(cases["b-two"]["outcome"], "caught-strict")
        self.assertAlmostEqual(cases["b-two"]["charged"], 1.5)
        self.assertAlmostEqual(data["summary"]["charged"], 2.0)

    def test_a_second_stop_during_the_resumed_run_loses_nothing(self):
        for name in ("a-one", "b-two", "c-three"):
            self.make_case(name)
        self.claude(answer(finding()))
        with self.ctrl_c_at_session(2):  # a-one $0.50, b-two interrupted $1.00, c-three not-run
            self.first_run()
        before = self.sessions()
        code, out = self.resume()
        self.assertEqual(code, 0, out + self.stderr)
        self.assertEqual(self.ran(before), ["b-two", "c-three"])
        # what the file held while b-two's second session ran
        during, _ = self.parsed(self.snapshots()[1])
        self.assertEqual(during["a-one"]["outcome"], "caught-strict")
        self.assertEqual(during["b-two"]["outcome"], "started")
        self.assertIn("c-three", during)
        self.assertAlmostEqual(sum(c["charged"] for c in during.values()), 0.5 + 1.0 + 1.0)
        # the machine goes down right there, and the run is continued once more
        self.machine_went_down_at(2)
        before = self.sessions()
        code, out = self.resume()
        self.assertEqual(code, 0, out + self.stderr)
        self.assertEqual(self.ran(before), ["b-two", "c-three"])
        cases, data = self.results()
        for name in ("a-one", "b-two", "c-three"):
            self.assertEqual(cases[name]["outcome"], "caught-strict", name)
        self.assertAlmostEqual(cases["b-two"]["charged"], 2.5)  # two lost caps and one session
        self.assertAlmostEqual(data["summary"]["cost"], 1.5)
        self.assertAlmostEqual(data["summary"]["charged"], 3.5)


class ResumeSettingsAndRefusals(ResumeCase):
    """AC-4"""

    def stopped_run(self, *extra):
        self.make_case("a-one")
        self.make_case("b-two")
        self.claude(answer(finding()))
        self.first_run(*extra)
        self.machine_went_down_at(2)

    def assert_refused(self, code, out, reason, before, text=None):
        self.assertEqual(code, 1, out + self.stderr)
        self.assertRegex(self.stderr, reason)
        self.assertNotIn("Traceback", self.stderr)
        self.assertEqual(self.sessions(), before)
        if text is not None:
            self.assertEqual(self.file_text(), text)  # nothing started, nothing rewritten

    def test_a_setting_that_differs_from_the_recorded_one_is_refused_and_named(self):
        self.stopped_run()
        other = os.path.join(self.tmp, "other-cases")
        os.makedirs(other)
        before, text = self.sessions(), self.file_text()
        for extra, reason in (
            (["--model", "claude-opus-5-5"], r"(?i)model"),
            (["--session-budget-usd", "2"], r"(?i)session|cap"),
            (["--line-margin", "5"], r"(?i)margin"),
            (["--cases", other], r"other-cases"),
            (["--out", other], r"--out"),
        ):
            with self.subTest(extra=extra[0]):
                code, out = self.resume(*extra)
                self.assert_refused(code, out, reason, before, text)

    def test_a_directory_without_a_usable_results_json_is_refused(self):
        self.make_case("a-one")
        self.claude(answer(finding()))
        code, out = self.resume(directory=os.path.join(self.tmp, "no-such-run"))
        self.assert_refused(code, out, r"results\.json", 0)
        os.makedirs(self.out)
        code, out = self.resume()
        self.assert_refused(code, out, r"results\.json", 0)
        with open(self.results_path, "w", encoding="utf-8", newline="") as fh:
            fh.write("{not json")
        code, out = self.resume()
        self.assert_refused(code, out, r"results\.json", 0, "{not json")

    def test_a_dry_run_is_not_resumed(self):
        self.make_case("a-one")
        self.claude(answer(finding()))
        code, out = self.run_tool("--dry-run", "--cases", self.cases, "--out", self.out)
        self.assertEqual(code, 0, out + self.stderr)
        text = self.file_text()
        code, out = self.resume()
        self.assert_refused(code, out, r"(?i)dry", 0, text)

    def test_the_resumed_sessions_run_with_the_recorded_settings(self):
        self.stopped_run(
            "--model", "claude-sonnet-5-5", "--session-budget-usd", "0.6", "--line-margin", "3"
        )
        earlier, _ = self.results()
        self.assertAlmostEqual(earlier["b-two"]["charged"], 0.6)  # AC-1 with a given cap
        # two lines past the recorded margin of 3, well inside the default margin
        self.claude(answer(finding(line=13 + 3 + 2)))
        before = self.sessions()
        code, out = self.resume()
        self.assertEqual(code, 0, out + self.stderr)
        self.assertEqual(self.ran(before), ["b-two"])
        argv = self.fake_calls("claude")[-1]["argv"]
        self.assertEqual(argv[argv.index("--model") + 1], "claude-sonnet-5-5")
        self.assertAlmostEqual(float(argv[argv.index("--max-budget-usd") + 1]), 0.6)
        cases, _ = self.results()
        self.assertEqual(cases["b-two"]["outcome"], "missed")
        self.assertAlmostEqual(cases["b-two"]["charged"], 0.6 + 0.5)
        # the same settings given again are no difference
        code, out = self.resume(
            "--model",
            "claude-sonnet-5-5",
            "--session-budget-usd",
            "0.6",
            "--line-margin",
            "3",
            "--cases",
            self.cases,
        )
        self.assertEqual(code, 0, out + self.stderr)
        self.assertEqual(self.sessions(), before + 1)

    def test_case_names_narrow_the_run_and_a_kept_case_is_not_run_again(self):
        for name in ("a-one", "b-two", "c-three"):
            self.make_case(name)
        self.claude(answer(finding()))
        with self.ctrl_c_at_session(2):  # a-one scored, b-two interrupted, c-three not-run
            self.first_run()
        before = self.sessions()
        code, out = self.resume("b-two")
        self.assertEqual(self.ran(before), ["b-two"])
        cases, data = self.results()
        self.assertEqual(
            {n: c["outcome"] for n, c in cases.items()},
            {"a-one": "caught-strict", "b-two": "caught-strict", "c-three": "not-run"},
        )
        self.assertEqual(code, 1, out + self.stderr)  # c-three is still not run
        # AC-5: the summary is of the whole set, the lost cap included
        self.assertEqual((data["summary"]["scored"], data["summary"]["strict"]), (2, 2))
        self.assertAlmostEqual(data["summary"]["charged"], 0.5 + 1.5)
        self.assertRegex(out, r"strict 2/2")
        before = self.sessions()
        code, out = self.resume("a-one")  # kept: reported, not run
        self.assertEqual(self.sessions(), before)
        self.assertRegex(self.line_of(out, "a-one"), r"(?i)\bkept\b")
        self.assertIn("caught-strict", self.line_of(out, "a-one"))
        cases, _ = self.results()
        self.assertEqual(
            {n: c["outcome"] for n, c in cases.items()},
            {"a-one": "caught-strict", "b-two": "caught-strict", "c-three": "not-run"},
        )


class NamedTwice(ResumeCase):
    """AC-7"""

    def test_the_same_case_named_twice_is_refused_as_named_twice(self):
        self.make_case("a-one")
        self.make_case("b-two")
        self.claude(answer(finding()))
        for extra in (["--budget-usd", "5"], ["--dry-run"]):
            with self.subTest(extra=extra[0]):
                code, out = self.run_tool(
                    "--cases", self.cases, "--out", self.out, *extra, "a-one", "b-two", "a-one"
                )
                self.assertEqual(code, 1, out + self.stderr)
                self.assertIn("case named twice: a-one", self.stderr)
                self.assertNotIn("two directories", self.stderr)
                self.assertNotIn("b-two", self.stderr)
                self.assertEqual(self.sessions(), 0)


if __name__ == "__main__":
    unittest.main()
