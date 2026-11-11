"""Acceptance tests for feature/review-bench (AC-1, AC-6): what a benchmark case is.

AC-1: `python tools/review_bench.py` refuses a case directory with a missing or malformed
field, names the field, runs no reviewer session for it, and still runs a well-formed case.
AC-6: `bench/cases/` holds the three pilot cases (PRs #43, #61 and #69), each a `case.toml`
with every field of the format and the round's `change.md`, which revali's own change.md
parser accepts. The pilot cases are read from disk only: running them needs the network.
"""

import json
import os
import re
import subprocess
import sys
import tomllib
import unittest

from revali import V1_KINDS, changedoc
from tests.fixtures.make_sample_repo import CHANGE_MD
from tests.helpers import ROOT, RepoCase, approve_response, claude_entry, git

TOOL = os.path.join(ROOT, "tools", "review_bench.py")
PILOT_DIR = os.path.join(ROOT, "bench", "cases")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")

# name -> (the TOML line to drop or replace, its replacement or None, the field to be named).
# The names carry no field word, so a match in the message is the tool naming the field.
BROKEN = {
    "bad-01": ("repo = ", None, "repo"),
    "bad-02": ("base = ", None, "base"),
    "bad-03": ("head = ", None, "head"),
    "bad-04": ("branch = ", None, "branch"),
    "bad-05": ("file = ", None, "file"),
    "bad-06": ("lines = ", None, "lines"),
    "bad-07": ("description = ", None, "description"),
    "bad-08": ("pr = ", None, "pr"),
    "bad-09": ("finding = ", None, "finding"),
    "bad-10": ("lines = ", "lines = [13, 12]", "lines"),  # reversed
    "bad-11": ("lines = ", "lines = [12, 13, 14]", "lines"),  # not a pair
    "bad-12": ("lines = ", 'lines = ["12", "13"]', "lines"),  # not integers
    "bad-13": ("lines = ", "lines = [0, 13]", "lines"),  # lines are 1-based
    "bad-14": ("head = ", "head = 5", "head"),  # not a string
    "bad-15": ("file = ", 'file = ""', "file"),  # empty
    "bad-16": ("fetch = ", 'fetch = "refs/pull/7/head"', "fetch"),  # not a list
    "bad-17": ("pr = ", 'pr = "seven"', "pr"),  # not a number
    "bad-18": ("lines = ", "lines = 13", "lines"),  # one number, not a pair (round 1 F2)
    "bad-19": ("lines = ", 'lines = "ab"', "lines"),  # a string of length two
    "bad-20": ("lines = ", "lines = 1.5", "lines"),  # a float
}


class MalformedCases(RepoCase):
    """AC-1"""

    def setUp(self):
        super().setUp()
        self.cases = os.path.join(self.tmp, "cases")
        self.out = os.path.join(self.tmp, "out")
        self.base = git(["rev-parse", "main"], self.repo).strip()
        self.head = git(["rev-parse", "feature/mul"], self.repo).strip()

    def toml_lines(self):
        return [
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
        ]

    def make_case(self, name, toml=None, change_md=CHANGE_MD):
        path = os.path.join(self.cases, name)
        os.makedirs(path)
        if toml is None:
            toml = "\n".join(self.toml_lines()) + "\n"
        if toml is not False:
            with open(os.path.join(path, "case.toml"), "w", encoding="utf-8", newline="\n") as fh:
                fh.write(toml)
        if isinstance(change_md, bytes):
            with open(os.path.join(path, "change.md"), "wb") as fh:
                fh.write(change_md)
        elif change_md is not False:
            with open(os.path.join(path, "change.md"), "w", encoding="utf-8", newline="\n") as fh:
                fh.write(change_md)

    def make_broken(self, name, prefix, replacement):
        lines = self.toml_lines()
        hits = [i for i, line in enumerate(lines) if line.startswith(prefix)]
        self.assertEqual(len(hits), 1, prefix)
        if replacement is None:
            del lines[hits[0]]
        else:
            lines[hits[0]] = replacement
        self.make_case(name, "\n".join(lines) + "\n")

    def bench(self):
        res = subprocess.run(
            [sys.executable, TOOL, "--cases", self.cases, "--out", self.out, "--budget-usd", "50"],
            cwd=self.tmp,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=600,
        )
        with open(os.path.join(self.out, "results.json"), encoding="utf-8") as fh:
            data = json.load(fh)
        return res, {c["case"]: c for c in data["cases"]}, data["summary"]

    def answer(self):
        finding = {
            "id": "F1",
            "file": "src/calc.py",
            "line": 12,
            "severity": "medium",
            "kind": "correctness",
            "text": "mul is wrong for large operands",
            "suggestion": "fix it",
        }
        return claude_entry(approve_response(verdict="CHANGES_REQUESTED", findings=[finding]))

    def test_each_missing_or_malformed_field_is_named_and_the_good_case_runs(self):
        for name, (prefix, replacement, _field) in BROKEN.items():
            self.make_broken(name, prefix, replacement)
        self.make_case("z-good")
        self.claude(self.answer())
        res, cases, summary = self.bench()
        self.assertNotEqual(res.returncode, 0, res.stdout)
        self.assertNotIn("Traceback", res.stderr)
        for name, (_prefix, _replacement, field) in BROKEN.items():
            with self.subTest(case=name):
                self.assertEqual(cases[name]["outcome"], "malformed")
                self.assertRegex(cases[name]["message"], r"\b%s\b" % field)
                line = [ln for ln in res.stdout.splitlines() if ln.startswith(name)]
                self.assertEqual(len(line), 1, res.stdout)
                self.assertIn("malformed", line[0])
                self.assertRegex(line[0], r"\b%s\b" % field)
        # the well-formed case is the only one that reached a reviewer, and the only one scored
        self.assertEqual(cases["z-good"]["outcome"], "caught-strict")
        self.assertEqual(len(self.fake_calls("claude")), 1)
        self.assertEqual(self.fake_calls("gh"), [])
        self.assertEqual((summary["scored"], summary["strict"]), (1, 1))

    def test_two_bad_fields_are_both_named(self):
        lines = [
            ln for ln in self.toml_lines() if not ln.startswith(("branch = ", "description = "))
        ]
        self.make_case("m-two", "\n".join(lines) + "\n")
        res, cases, _ = self.bench()
        self.assertNotEqual(res.returncode, 0, res.stdout)
        self.assertEqual(cases["m-two"]["outcome"], "malformed")
        self.assertRegex(cases["m-two"]["message"], r"\bbranch\b")
        self.assertRegex(cases["m-two"]["message"], r"\bdescription\b")
        self.assertEqual(self.fake_calls("claude"), [])

    def test_a_case_without_one_of_its_two_files_or_with_broken_toml_is_refused(self):
        self.make_case("m-no-change-md", change_md=False)
        self.make_case("m-empty-change-md", change_md="  \n")
        self.make_case("m-no-case-toml", toml=False)
        self.make_case("m-not-toml", toml="[case\nrepo = \n")
        # round 1 F2: a description that is not UTF-8 is a refusal, not a traceback
        self.make_case("m-not-utf8-change-md", change_md=b"## Request\n\xff\xfe mul\n")
        res, cases, summary = self.bench()
        self.assertNotEqual(res.returncode, 0, res.stdout)
        self.assertNotIn("Traceback", res.stderr)
        for name, named in (
            ("m-no-change-md", "change.md"),
            ("m-empty-change-md", "change.md"),
            ("m-not-utf8-change-md", "change.md"),
            ("m-no-case-toml", "case.toml"),
            ("m-not-toml", "case.toml"),
        ):
            with self.subTest(case=name):
                self.assertEqual(cases[name]["outcome"], "malformed")
                self.assertIn(named, cases[name]["message"])
        self.assertEqual(self.fake_calls("claude"), [])
        self.assertEqual(summary["scored"], 0)


class PilotCases(unittest.TestCase):
    """AC-6"""

    # AC-6 names the file of #43's bug; the other two are a module of the package
    EXPECTED_FILE = {43: r"^revali/pr\.py$", 61: r"^revali/\w+\.py$", 69: r"^revali/\w+\.py$"}

    def load(self):
        cases = {}
        for name in sorted(os.listdir(PILOT_DIR)):
            path = os.path.join(PILOT_DIR, name)
            if os.path.isdir(path):
                with open(os.path.join(path, "case.toml"), "rb") as fh:
                    cases[name] = (path, tomllib.load(fh))
        return cases

    def test_three_cases_from_prs_43_61_and_69(self):
        cases = self.load()
        self.assertEqual(len(cases), 3, sorted(cases))
        self.assertEqual(sorted(data["source"]["pr"] for _, data in cases.values()), [43, 61, 69])
        for name, (_path, data) in cases.items():
            with self.subTest(case=name):
                pr = data["source"]["pr"]
                case, expected, source = data["case"], data["expected"], data["source"]
                self.assertTrue(case["repo"].strip())
                self.assertIn("refs/pull/%d/head" % pr, case["fetch"])
                self.assertRegex(case["base"], SHA_RE)
                self.assertRegex(case["head"], SHA_RE)
                self.assertNotEqual(case["base"], case["head"])
                self.assertTrue(case["branch"].strip())
                self.assertRegex(expected["file"], self.EXPECTED_FILE[pr])
                first, last = expected["lines"]
                self.assertTrue(1 <= first <= last, expected["lines"])
                self.assertTrue(expected["description"].strip())
                self.assertEqual(len(expected["description"].splitlines()), 1)
                self.assertEqual(source["round"], 1)
                self.assertRegex(source["finding"], r"^F\d+$")

    def test_each_case_carries_a_change_md_revali_accepts(self):
        for name, (path, _data) in self.load().items():
            with self.subTest(case=name):
                doc = changedoc.load(os.path.join(path, "change.md"))
                self.assertEqual(changedoc.validate(doc, V1_KINDS), [])
                for heading in ("## Request", "## Goal", "## Acceptance criteria", "## What"):
                    self.assertIn(heading, doc.raw)


if __name__ == "__main__":
    unittest.main()
