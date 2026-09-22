"""AC-1..AC-6 of fix/reviewer-format-cost-encoding: revali formats the reviewer's test files
with `[project] format` before the lint gate, counts every reviewer attempt's cost, and never
dies printing a stage message the console encoding cannot represent."""

import json
import os
import subprocess
import sys
import unittest
from unittest import mock

from revali import EXIT_ACTION, EXIT_ERROR, EXIT_OK, PROMPT_VERSION
from revali.preflight import preflight
from revali.procs import ProcTimeout
from revali.review import build_prompt, format_files
from revali.state import State
from tests.fixtures.make_sample_repo import PY
from tests.helpers import TEST_REVIEW_MUL, RepoCase, approve_response, claude_entry, git, run_cli

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Fails when any reviewer file carries the marker, the way black --check fails on a file it
# would reformat; prints black's summary line, emoji included.
LINTCHECK = """import glob
import sys

bad = []
for path in sorted(glob.glob("tests/test_review_*.py")):
    with open(path, encoding="utf-8") as fh:
        if "# BAD" in fh.read():
            bad.append(path.replace("\\\\", "/"))
for path in bad:
    print("would reformat " + path)
if bad:
    print("Oh no! \\U0001f4a5 \\U0001f494 \\U0001f4a5")
sys.exit(1 if bad else 0)
"""
# The formatter: drops the marker from each file it is given, records its arguments in the
# file REVALI_TEST_FMT_LOG names (outside the repository), exits REVALI_TEST_FMT_EXIT.
FORMATTER = """import json
import os
import sys

for path in sys.argv[1:]:
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    if os.environ.get("REVALI_TEST_FMT_FIX", "1") == "1":
        text = text.replace("  # BAD", "")
    with open(path, "w", encoding="utf-8", newline="\\n") as fh:
        fh.write(text)
with open(os.environ["REVALI_TEST_FMT_LOG"], "a", encoding="utf-8") as fh:
    fh.write(json.dumps(sys.argv[1:]) + "\\n")
sys.exit(int(os.environ.get("REVALI_TEST_FMT_EXIT", "0")))
"""
BAD_TEST = TEST_REVIEW_MUL.replace("import unittest", "import unittest  # BAD")
FILE = "tests/test_review_mul.py"


def entry(text, data=None, cost=0.5, **kw):
    e = claude_entry(data, write_tests=False, cost=cost, **kw)
    e["write_files"] = {FILE: text}
    return e


def review_lines(log, needle):
    return [line for line in log.splitlines() if "] review: " in line and needle in line]


def read_repo_file(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


class FormatCase(RepoCase):
    format_line = "%s fmt.py {files}" % PY

    def setUp(self):
        super().setUp()
        self.fmt_log = os.path.join(self.tmp, "fmt.log")
        os.environ["REVALI_TEST_FMT_LOG"] = self.fmt_log
        self.write("lintcheck.py", LINTCHECK)
        self.write("fmt.py", FORMATTER)
        toml = self.read("revali.toml").replace('lint = ""', 'lint = "%s lintcheck.py"' % PY)
        if self.format_line:
            toml = toml.replace("[project]\n", '[project]\nformat = "%s"\n' % self.format_line, 1)
        self.write("revali.toml", toml)
        self.commit_all("lint and format lines")

    def log(self):
        return self.read(".revali/feature__mul/logs/revali.log")

    def fmt_calls(self):
        if not os.path.isfile(self.fmt_log):
            return []
        with open(self.fmt_log, encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def history(self):
        with open(os.path.join(self.home, "history.jsonl"), encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]


class Format(FormatCase):
    def test_a_file_that_only_needs_formatting_passes_without_a_bounce(self):
        self.claude(entry(BAD_TEST))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("READY TO MERGE", out)
        self.assertEqual(len(self.fake_calls("claude")), 1)  # AC-1: no model turn spent
        self.assertEqual(self.fmt_calls(), [[FILE]])  # only the reviewer's file
        self.assertNotIn("# BAD", git(["show", "HEAD:" + FILE], self.repo))
        log = self.log()
        fmt = review_lines(log, "format of")
        self.assertEqual(len(fmt), 1, log)
        self.assertIn("fmt.py", fmt[0])
        self.assertIn("exit 0", fmt[0])
        lines = log.splitlines()
        self.assertLess(lines.index(fmt[0]), lines.index(review_lines(log, "lint of")[0]))

    def test_format_runs_once_per_attempt(self):
        partial = approve_response(
            tests=[{"path": FILE, "purpose": "p", "covers": ["AC-1"], "expected": "e"}]
        )
        self.claude(entry(BAD_TEST, partial), entry(BAD_TEST))
        code, out = run_cli(["run", "--foreground"])
        self.assertIn("READY TO MERGE", out)
        self.assertEqual(len(self.fake_calls("claude")), 2)  # the AC gap still bounces
        self.assertEqual(self.fmt_calls(), [[FILE], [FILE]])
        self.assertEqual(len(review_lines(self.log(), "format of")), 2)

    def test_a_failing_format_line_is_logged_and_lint_decides(self):
        os.environ["REVALI_TEST_FMT_EXIT"] = "3"
        os.environ["REVALI_TEST_FMT_FIX"] = "0"
        self.claude(entry(BAD_TEST), entry(TEST_REVIEW_MUL))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)  # AC-2: the format exit does not stop the run
        self.assertEqual(len(self.fake_calls("claude")), 2)  # lint was red, so one bounce
        fmt = review_lines(self.log(), "format of")
        self.assertIn("exit 3", fmt[0])

    def test_a_format_timeout_is_logged_not_raised(self):
        ctx = preflight(self.repo)
        with mock.patch("revali.review.run_shell", side_effect=ProcTimeout("timed out")):
            format_files(ctx, [FILE], None)  # AC-2: no Stop

    def test_no_files_runs_no_format(self):
        ctx = preflight(self.repo)
        with mock.patch("revali.review.run_shell") as rs:
            format_files(ctx, [], None)
        rs.assert_not_called()

    def test_a_path_with_a_space_is_quoted(self):
        ctx = preflight(self.repo)
        with mock.patch("revali.review.run_shell") as rs:
            rs.return_value = mock.Mock(returncode=0, ok=True, text="")
            format_files(ctx, ["tests/test_review_a b.py", FILE], None)
        cmd = rs.call_args[0][0]
        self.assertIn('"tests/test_review_a b.py"', cmd)
        self.assertIn(FILE, cmd)
        self.assertNotIn("{files}", cmd)

    def test_the_prompt_says_revali_formats(self):
        ctx = preflight(self.repo)
        prompt = build_prompt(ctx, State(), self.rdir(), 1)
        self.assertIn("formats your test files", prompt)  # AC-3
        self.assertIn("fmt.py", prompt)
        self.assertGreaterEqual(int(PROMPT_VERSION), 9)  # AC-6


class NoFormat(FormatCase):
    format_line = ""

    def test_without_format_the_old_bounce_stands(self):
        self.claude(entry(BAD_TEST), entry(TEST_REVIEW_MUL))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertEqual(len(self.fake_calls("claude")), 2)
        self.assertEqual(review_lines(self.log(), "format of"), [])
        self.assertEqual(self.fmt_calls(), [])

    def test_the_prompt_says_nothing_about_formatting(self):
        ctx = preflight(self.repo)
        prompt = build_prompt(ctx, State(), self.rdir(), 1)
        self.assertNotIn("formats your test files", prompt)  # AC-3


class FormatWithoutFiles(FormatCase):
    format_line = "%s fmt.py" % PY

    def test_preflight_refuses_it(self):
        for argv in (["preflight"], ["run", "--foreground"]):
            code, out = run_cli(argv)
            self.assertEqual(code, EXIT_ACTION, out)  # AC-2
            self.assertIn("{files}", out)
            self.assertIn("format", out)
        self.assertEqual(self.fake_calls("claude"), [])


class Cost(FormatCase):
    format_line = ""

    def status_cost(self):
        code, out = run_cli(["status"])
        line = [x for x in out.splitlines() if "cost: $" in x][0]
        return line.split("cost: $")[1].split()[0].rstrip(",")

    def test_a_round_that_stops_on_lint_keeps_both_attempts_cost(self):
        self.claude(entry(BAD_TEST, cost=5.25), entry(BAD_TEST, cost=2.0))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertAlmostEqual(State.load(self.rdir()).cost_usd, 7.25)  # AC-4
        self.assertEqual(self.status_cost(), "7.25")
        self.assertAlmostEqual(self.history()[-1]["cost_usd"], 7.25)

    def test_an_engine_error_counts_the_reported_cost(self):
        self.claude(entry(TEST_REVIEW_MUL, cost=0.75, is_error=True, exit=1))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertAlmostEqual(State.load(self.rdir()).cost_usd, 0.75)  # AC-4
        self.assertAlmostEqual(self.history()[-1]["cost_usd"], 0.75)

    def test_output_off_the_schema_counts_the_cost(self):
        self.claude(entry(TEST_REVIEW_MUL, data={"verdict": "MAYBE"}, cost=0.4))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertIn("schema", out)
        self.assertAlmostEqual(State.load(self.rdir()).cost_usd, 0.4)  # AC-4

    def test_a_completed_round_counts_each_attempt_once(self):
        self.claude(entry(BAD_TEST, cost=1.5), entry(TEST_REVIEW_MUL, cost=0.25))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        state = State.load(self.rdir())
        self.assertAlmostEqual(state.cost_usd, 1.75)  # AC-4: not 3.5
        self.assertAlmostEqual(state.rounds[0]["cost_usd"], 1.75)
        meta = json.loads(self.read(".revali/feature__mul/review-1.json"))["meta"]
        self.assertEqual(meta["cost_usd"], "1.7500")


class Encoding(FormatCase):
    format_line = ""

    def revali(self, *argv):
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "cp950"  # a Traditional Chinese Windows console
        env.pop("PYTHONUTF8", None)
        return subprocess.run(
            [sys.executable, os.path.join(ROOT, "revali.py")] + list(argv),
            cwd=self.repo,
            env=env,
            capture_output=True,
            timeout=120,
        )

    def test_wait_and_status_print_an_emoji_message(self):
        self.claude(entry(BAD_TEST), entry(BAD_TEST))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertIn("\U0001f4a5", State.load(self.rdir()).message)  # the fixture holds
        for argv, expected in ((["wait"], EXIT_ERROR), (["status"], EXIT_OK)):
            res = self.revali(*argv)
            stderr = res.stderr.decode("utf-8", "replace")
            self.assertNotIn("UnicodeEncodeError", stderr)  # AC-5
            self.assertEqual(res.returncode, expected, stderr)
            self.assertIn(b"would reformat", res.stdout)


class Docs(unittest.TestCase):
    def test_docs_and_template_describe_format(self):
        conf = read_repo_file("docs/configuration.md")
        self.assertIn("`[project] format`", conf)  # AC-6
        self.assertIn("{files}", conf.split("`[project] format`")[1].split("\n\n")[0])
        side = read_repo_file("docs/side-effects.md")
        self.assertEqual(len([b for b in side.split("\n- ") if "`[project] format`" in b]), 1)
        template = [
            line
            for line in read_repo_file("templates/revali.toml").splitlines()
            if line.startswith("format =")
        ]
        self.assertEqual(len(template), 1)
        self.assertIn("{files}", template[0])


if __name__ == "__main__":
    unittest.main()
