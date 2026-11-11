"""Acceptance tests for feature/bench-cases (M3.3.2): the case builder (AC-1, AC-2, AC-4, AC-7).

AC-1: `python tools/bench_case.py <archive dir> --round N --finding FID --out DIR` writes a
case the benchmark accepts, taken from the archived round, and refuses, naming the reason,
whatever does not make a case.
AC-2: `--request TEXT` replaces the body of the `## Request` section; without it the section
is copied as it was.
AC-4: `--list` prints one repository's candidates in selection order: PRs newest first, one
per PR, the first qualifying finding of the earliest round that has one.
AC-7: `--config PATH` puts a `revali.toml` in the case, which the benchmark uses.

Offline: the fixture repository stands for the case's repository (`--repo`), and the archive
is written by the test in the layout `revali merge` leaves.
"""

import json
import os
import re
import subprocess
import sys
import tomllib

from tests.helpers import ROOT, RepoCase, git

BUILDER = os.path.join(ROOT, "tools", "bench_case.py")
BENCH = os.path.join(ROOT, "tools", "review_bench.py")

# the user's own words, not in English ("add mul to calc")
USER_WORDS = "把 mul 加進 calc"
REQUEST_BODY = USER_WORDS + "\n\n(M9 in the author's plan, approved 2026-01-02.)"

CHANGE_MD = """---
title: Add mul to calc
kind: feature
author_model: fixture
---

## Request
%s

## Goal
`mul` multiplies two integers.

## Acceptance criteria
- AC-1: mul(a, b) returns the product of a and b for integers
- AC-2: mul with zero returns zero

## Out of scope
Division.

## Dependencies
none

## What
Added `mul(a, b)` to `src/calc.py`.%s

## Why
The calculator only had add and sub.
"""

PROMPT = """You are the independent reviewer for a pull request.

# What you are reviewing

Repository: current working directory (a git checkout of the feature branch).
Branch `feature/mul` against `main`. Change kind: `feature`. Round 1.

## The author's description (change.md)

%s

## The diff (base...HEAD; files matching `test_review_{topic}.py` and `*.lock` are omitted)

```diff
+def mul(a, b):
+    return a + b
```

Files changed but omitted from the diff above: (none)

# How to review

1. Read the diff first, then the description.
"""


def change_md(extra=""):
    return CHANGE_MD % (REQUEST_BODY, extra)


def finding(fid="F1", severity="medium", kind="correctness", line=13):
    return {
        "id": fid,
        "file": "src/calc.py",
        "line": line,
        "severity": severity,
        "kind": kind,
        "text": "mul adds instead of multiplying. The test only checks zero.",
        "suggestion": "multiply",
    }


def resolved(*ids):
    return [{"id": fid, "status": "resolved", "note": "fixed"} for fid in ids]


def squeeze(text):
    """Blank-line runs and the ends do not matter when two descriptions are compared."""
    return re.sub(r"\n{2,}", "\n\n", text).strip()


class BuilderCase(RepoCase):
    def setUp(self):
        super().setUp()
        self.base = git(["rev-parse", "main"], self.repo).strip()
        self.head = git(["rev-parse", "feature/mul"], self.repo).strip()
        git(["update-ref", "refs/pull/7/head", self.head], self.repo)  # what GitHub keeps
        self.archive_root = os.path.join(self.tmp, "archive", "owner__sample")
        self.cases = os.path.join(self.tmp, "built")

    def put(self, path, data):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write(data if isinstance(data, str) else json.dumps(data, indent=1))

    def text(self, path):
        with open(path, "r", encoding="utf-8", newline="") as fh:
            return fh.read().replace("\r\n", "\n")

    def archive(self, pr=7, rounds=None, heads=None, docs=None, root=None):
        """One PR's archive. rounds: {n: (findings, previous_findings)}; heads and docs give
        a round its own head commit and description."""
        d = os.path.join(root or self.archive_root, "%d-feature__mul" % pr)
        self.put(
            os.path.join(d, "state.json"),
            {"repo": "owner/sample", "branch": "feature/mul", "base": "main", "pr_number": pr},
        )
        if rounds is None:
            rounds = {1: ([finding()], []), 2: ([], resolved("F1"))}
        for n, (findings, previous) in rounds.items():
            verdict = "CHANGES_REQUESTED" if findings else "APPROVE"
            self.put(
                os.path.join(d, "review-%d.json" % n),
                {
                    "verdict": verdict,
                    "head_sha": (heads or {}).get(n, self.head),
                    "data": {
                        "verdict": verdict,
                        "findings": findings,
                        "previous_findings": previous,
                    },
                },
            )
            self.put(
                os.path.join(d, "logs", "prompt-r%d-1.md" % n),
                PROMPT % (docs or {}).get(n, change_md()),
            )
        return d

    def run_tool(self, argv):
        return subprocess.run(
            [sys.executable] + argv,
            cwd=self.tmp,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=600,
        )

    def build(self, archive, name="case", round_no=1, fid="F1", extra=()):
        out = os.path.join(self.cases, name)
        argv = [BUILDER, archive, "--round", str(round_no), "--finding", fid, "--out", out]
        return self.run_tool(argv + ["--repo", self.repo] + list(extra)), out

    def case_toml(self, out):
        with open(os.path.join(out, "case.toml"), "rb") as fh:
            return tomllib.load(fh)

    def dry_run(self, cases_dir):
        out = os.path.join(self.tmp, "dry-out")
        res = self.run_tool([BENCH, "--dry-run", "--cases", cases_dir, "--out", out])
        with open(os.path.join(out, "results.json"), encoding="utf-8") as fh:
            results = {c["case"]: c for c in json.load(fh)["cases"]}
        return res, results, out


class Build(BuilderCase):
    """AC-1, AC-2, AC-7"""

    def test_a_case_from_an_archived_round(self):
        res, out = self.build(self.archive())
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        data = self.case_toml(out)
        self.assertEqual(data["case"]["repo"], self.repo)
        self.assertEqual(data["case"]["head"], self.head)  # review-1.json
        self.assertEqual(data["case"]["base"], self.base)  # merge-base with main
        self.assertEqual(data["case"]["fetch"], ["refs/pull/7/head"])
        self.assertEqual(data["case"]["branch"], "feature/mul")  # the archive's state
        self.assertEqual(data["expected"]["file"], "src/calc.py")
        self.assertEqual(data["expected"]["lines"], [13, 13])
        description = data["expected"]["description"]
        self.assertTrue(description.startswith("mul adds instead of multiplying"), description)
        self.assertNotIn("only checks zero", description)  # the first sentence only
        self.assertEqual(data["source"], {"pr": 7, "round": 1, "finding": "F1"})
        # change.md is the description of the round's prompt, the Request as it was (AC-2)
        text = self.text(os.path.join(out, "change.md"))
        self.assertEqual(squeeze(text), squeeze(change_md()))
        self.assertIn(USER_WORDS, text)
        self.assertNotIn("The diff", text)
        self.assertNotIn("independent reviewer", text)
        self.assertFalse(os.path.exists(os.path.join(out, "revali.toml")))  # no --config
        # the benchmark accepts the case: its dry run gets as far as the prompt
        dry, results, _ = self.dry_run(self.cases)
        self.assertEqual(dry.returncode, 0, dry.stdout + dry.stderr)
        self.assertEqual(results["case"]["outcome"], "ready", results)
        self.assertEqual(self.fake_calls("gh"), [])
        self.assertEqual(self.fake_calls("claude"), [])

    def test_a_later_round_gives_that_rounds_head_and_description(self):
        archive = self.archive(
            rounds={
                1: ([finding("F1", severity="low")], []),
                2: ([finding("F2")], resolved("F1")),
                3: ([], resolved("F2")),
            },
            heads={1: self.base},
            docs={2: change_md("\nRound 1 F1: renamed the helper.")},
        )
        res, out = self.build(archive, round_no=2, fid="F2")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        data = self.case_toml(out)
        self.assertEqual(data["case"]["head"], self.head)
        self.assertEqual(data["case"]["base"], self.base)
        self.assertEqual(data["source"], {"pr": 7, "round": 2, "finding": "F2"})
        self.assertIn("Round 1 F1: renamed the helper.", self.text(os.path.join(out, "change.md")))

    def test_request_replaces_the_body_of_the_request_section_only(self):
        res, out = self.build(self.archive(), extra=["--request", "Add mul to calc."])
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        text = self.text(os.path.join(out, "change.md"))
        self.assertEqual(squeeze(text), squeeze(CHANGE_MD % ("Add mul to calc.", "")))
        self.assertEqual([c for c in text if ord(c) > 127], [])
        self.assertNotIn("approved 2026-01-02", text)  # the whole body went, not one line

    def test_config_puts_a_revali_toml_in_the_case_and_the_benchmark_uses_it(self):
        own = self.text(os.path.join(self.repo, "revali.toml"))
        self.assertIn('test_dir = "tests"\n', own)
        self.assertNotIn("test_bench_", own)
        own = own.replace(
            'test_dir = "tests"\n',
            'test_dir = "tests"\ntest_file_pattern = "test_bench_{topic}.py"\n',
        )
        config = os.path.join(self.tmp, "local", "revali.toml")
        self.put(config, own)
        res, out = self.build(self.archive(), extra=["--config", config])
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertEqual(self.text(os.path.join(out, "revali.toml")), own)
        dry, results, dry_out = self.dry_run(self.cases)
        self.assertEqual(dry.returncode, 0, dry.stdout + dry.stderr)
        self.assertEqual(results["case"]["outcome"], "ready", results)
        # the prompt names the pattern of the case's configuration, not the clone's
        self.assertIn(
            "test_bench_{topic}.py", self.text(os.path.join(dry_out, "case", "prompt.md"))
        )


class Refusals(BuilderCase):
    """AC-1: what does not make a case is refused, and the reason is named"""

    def assert_refused(self, res, out, reason):
        self.assertEqual(res.returncode, 1, res.stdout + res.stderr)
        self.assertNotIn("Traceback", res.stderr)
        self.assertRegex(res.stdout + res.stderr, reason)
        self.assertFalse(os.path.exists(os.path.join(out, "case.toml")))

    def test_a_round_finding_or_resolution_that_does_not_qualify(self):
        good = {1: ([finding()], []), 2: ([], resolved("F1"))}
        unresolved = [{"id": "F1", "status": "unresolved", "note": "still there"}]
        table = (
            ("no-such-round", good, {"round_no": 3}, r"round 3|review-3"),
            ("no-such-finding", good, {"fid": "F9"}, r"F9"),
            ("low", {1: ([finding(severity="low")], []), 2: ([], resolved("F1"))}, {}, r"low"),
            (
                "convention",
                {1: ([finding(kind="convention")], []), 2: ([], resolved("F1"))},
                {},
                r"convention",
            ),
            ("no-line", {1: ([finding(line=0)], []), 2: ([], resolved("F1"))}, {}, r"line"),
            ("unresolved", {1: ([finding()], []), 2: ([], unresolved)}, {}, r"resolved"),
            ("other-finding", {1: ([finding()], []), 2: ([], resolved("F2"))}, {}, r"resolved"),
            ("last-round", {1: ([finding()], [])}, {}, r"resolved"),
        )
        for label, rounds, how, reason in table:
            with self.subTest(label=label):
                archive = self.archive(rounds=rounds, root=os.path.join(self.tmp, "a-" + label))
                res, out = self.build(archive, name=label, **how)
                self.assert_refused(res, out, reason)
                self.assertFalse(os.path.exists(out))

    def test_a_head_the_repository_cannot_reach(self):
        gone = "1234567890" * 4
        archive = self.archive(heads={1: gone})
        res, out = self.build(archive)
        self.assert_refused(res, out, r"(?i)reach")
        self.assertFalse(os.path.exists(out))

    def test_an_output_directory_that_exists(self):
        archive = self.archive()
        out = os.path.join(self.cases, "case")
        self.put(os.path.join(out, "notes.txt"), "mine\n")
        res, out = self.build(archive)
        self.assert_refused(res, out, r"(?i)exist")
        self.assertEqual(os.listdir(out), ["notes.txt"])  # left as it was


class Listing(BuilderCase):
    """AC-4: the order the selection rule takes candidates in"""

    def test_newest_pr_first_one_per_pr_earliest_round_first_finding(self):
        self.archive(pr=7)  # round 1, F1
        self.archive(  # the earliest round with a qualifying finding is 2; F2 comes before F3
            pr=12,
            rounds={
                1: ([finding("F1", severity="low")], []),
                2: ([finding("F2"), finding("F3")], resolved("F1")),
                3: ([], resolved("F2", "F3")),
            },
        )
        self.archive(  # two rounds qualify: the earlier one is taken
            pr=20,
            rounds={
                1: ([finding("F1")], []),
                2: ([finding("F5")], resolved("F1")),
                3: ([], resolved("F5")),
            },
        )
        self.archive(pr=100)  # newest by number, not by name
        self.archive(  # nothing that qualifies: not a candidate
            pr=9, rounds={1: ([finding(kind="convention")], []), 2: ([], resolved("F1"))}
        )
        os.makedirs(os.path.join(self.archive_root, "notes"))
        self.put(os.path.join(self.archive_root, "13-readme.txt"), "not an archive\n")
        res = self.run_tool([BUILDER, "--list", self.archive_root])
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        lines = [line for line in res.stdout.splitlines() if line.strip()]
        expected = [(100, "F1"), (20, "F1"), (12, "F2"), (7, "F1")]
        self.assertEqual(len(lines), len(expected), res.stdout)
        for line, (pr, fid) in zip(lines, expected, strict=True):
            with self.subTest(pr=pr):
                self.assertIn("%s%d-feature__mul" % (os.sep, pr), line)
                self.assertRegex(line, r"(^|\s)%s(\s|$)" % fid)
                self.assertNotRegex(line, r"(^|\s)F[35](\s|$)")  # a later finding or round
        self.assertNotIn("%s9-feature__mul" % os.sep, res.stdout)
