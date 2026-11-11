"""feature/bench-cases (M3.3.2): tools/bench_case.py builds a benchmark case from an archived
revali round, and lists one repository's candidates in selection order. Offline: the fixture
repository is the case's repository, the archive is written by the test."""

import contextlib
import importlib.util
import io
import json
import os
import sys

from tests.helpers import ROOT, RepoCase, git

sys.path.insert(0, os.path.join(ROOT, "tools"))
SPEC = importlib.util.spec_from_file_location(
    "bench_case", os.path.join(ROOT, "tools", "bench_case.py")
)
builder = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(builder)
bench = builder.bench

CHANGE_MD = """---
title: Add mul to calc
kind: feature
author_model: claude-opus-5-5
---

## Request
把 mul 加進去

## Goal
calc has mul.

## Acceptance criteria
- AC-1: mul(2, 3) is 6

## Out of scope
none

## Dependencies
none
"""


def finding(fid="F1", severity="medium", kind="correctness", line=13, text=None):
    return {
        "id": fid,
        "file": "src/calc.py",
        "line": line,
        "severity": severity,
        "kind": kind,
        "text": text or "mul adds instead of multiplying. The test only checks 0.",
        "suggestion": "multiply",
    }


class Archive(RepoCase):
    def setUp(self):
        super().setUp()
        self.base = git(["rev-parse", "main"], self.repo).strip()
        self.head = git(["rev-parse", "feature/mul"], self.repo).strip()
        git(["update-ref", "refs/pull/7/head", self.head], self.repo)
        self.root = os.path.join(self.tmp, "archive", "owner__sample")
        self.out = os.path.join(self.tmp, "cases")

    def write(self, path, data):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write(data if isinstance(data, str) else json.dumps(data))

    def archive(self, pr=7, rounds=None, prompt=True, head=None):
        """rounds: {n: (findings, previous_findings)}"""
        d = os.path.join(self.root, "%d-feature__mul" % pr)
        self.write(
            os.path.join(d, "state.json"),
            {"repo": "owner/sample", "branch": "feature/mul", "base": "main", "pr_number": pr},
        )
        if rounds is None:
            rounds = {1: ([finding()], []), 2: ([], [{"id": "F1", "status": "resolved"}])}
        for n, (findings, prev) in rounds.items():
            self.write(
                os.path.join(d, "review-%d.json" % n),
                {
                    "verdict": "CHANGES_REQUESTED" if findings else "APPROVE",
                    "head_sha": head or self.head,
                    "data": {"findings": findings, "previous_findings": prev},
                },
            )
            if prompt:
                self.write(
                    os.path.join(d, "logs", "prompt-r%d-1.md" % n),
                    "# Review\n\n## The author's description (change.md)\n\n"
                    + CHANGE_MD
                    + "\n\n## The diff\n\n```\n...\n```\n",
                )
        return d

    def build(self, d, n=1, fid="F1", name="c", **kw):
        return builder.build(d, n, fid, os.path.join(self.out, name), repo=self.repo, **kw)

    def refused(self, d, needle, **kw):
        with self.assertRaises(builder.BuildError) as cm:
            self.build(d, **kw)
        self.assertIn(needle, str(cm.exception))
        self.assertFalse(os.path.exists(os.path.join(self.out, kw.get("name", "c"))))


class Build(Archive):
    """AC-1, AC-2, AC-3"""

    def test_a_case_from_an_archived_round(self):
        path = self.build(self.archive())
        case = bench.load_case(path)
        self.assertEqual((case.head, case.base), (self.head, self.base))
        self.assertEqual(case.fetch, ["refs/pull/7/head"])
        self.assertEqual(case.branch, "feature/mul")
        self.assertEqual((case.file, case.lines), ("src/calc.py", [13, 13]))
        self.assertEqual(case.description, "mul adds instead of multiplying")
        self.assertEqual((case.pr, case.round, case.finding), (7, 1, "F1"))
        self.assertEqual(case.change_md, CHANGE_MD)
        self.assertEqual(case.repo, self.repo)
        self.assertEqual(self.fake_calls("gh"), [])

    def test_the_request_is_replaced_when_given(self):
        path = self.build(self.archive(), request="Add mul.")
        text = bench.load_case(path).change_md
        self.assertIn("## Request\nAdd mul.\n\n## Goal", text)
        self.assertNotIn("把 mul", text)

    def test_a_config_goes_into_the_case(self):
        config = os.path.join(self.tmp, "local-revali.toml")
        self.write(config, self.read("revali.toml") + "# the project's local copy\n")
        path = self.build(self.archive(), config=config)
        self.assertEqual(
            bench.load_case(path).config, self.read("revali.toml") + "# the project's local copy\n"
        )
        self.refused(self.archive(), "cannot read", name="d", config=config + ".missing")

    def test_the_built_case_passes_a_dry_run(self):
        self.build(self.archive(), name="pr7")
        out = os.path.join(self.tmp, "bench-out")
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = bench.main(["--dry-run", "--cases", self.out, "--out", out])
        self.assertEqual(code, 0, stdout.getvalue())
        self.assertIn("ready: 1/1; no session run", stdout.getvalue())
        self.assertTrue(os.path.isfile(os.path.join(out, "pr7", "prompt.md")))
        self.assertEqual(self.fake_calls("claude"), [])

    def test_a_description_is_one_line_and_bounded(self):
        long = "word " * 80 + ". Second."
        path = self.build(
            self.archive(
                rounds={
                    1: ([finding(text='A "quoted"\nbackslash \\ ' + long)], []),
                    2: ([], [{"id": "F1", "status": "resolved"}]),
                }
            )
        )
        desc = bench.load_case(path).description
        self.assertEqual(
            builder.first_sentence("It needs `x`, i.e. the y. `z` is next."),
            "It needs `x`, i.e. the y",
        )
        self.assertEqual(builder.first_sentence("Use a, e.g. `b`. Then c."), "Use a, e.g. `b`")
        self.assertEqual(len(desc.splitlines()), 1)
        self.assertLessEqual(len(desc), builder.DESCRIPTION_MAX)
        self.assertTrue(desc.startswith('A "quoted" backslash \\ word'))


class Refusals(Archive):
    """AC-1"""

    def test_an_existing_output_directory(self):
        d = self.archive()
        os.makedirs(os.path.join(self.out, "c"))
        with self.assertRaises(builder.BuildError) as cm:
            self.build(d)
        self.assertIn("already exists", str(cm.exception))

    def test_a_round_or_finding_not_in_the_archive(self):
        d = self.archive()
        self.refused(d, "review-3.json", n=3)
        self.refused(d, "no finding F9", fid="F9")

    def test_a_finding_that_does_not_qualify(self):
        for f, needle in (
            (finding(severity="low"), "low correctness"),
            (finding(kind="convention"), "medium convention"),
            (finding(line=0), "line 0"),
        ):
            with self.subTest(needle=needle):
                self.root = os.path.join(self.tmp, "archive-" + needle.replace(" ", "-"))
                d = self.archive(
                    rounds={1: ([f], []), 2: ([], [{"id": "F1", "status": "resolved"}])}
                )
                self.refused(d, needle)

    def test_a_finding_the_next_round_did_not_resolve(self):
        for prev, label in (([{"id": "F1", "status": "unresolved"}], "unresolved"), (None, "last")):
            with self.subTest(label=label):
                self.root = os.path.join(self.tmp, "archive-" + label)
                rounds = {1: ([finding()], [])}
                if prev is not None:
                    rounds[2] = ([], prev)
                self.refused(self.archive(rounds=rounds), "does not mark F1 resolved")

    def test_a_head_the_repository_cannot_reach(self):
        self.refused(self.archive(head="0" * 40), "is not reachable")

    def test_a_round_without_its_prompt(self):
        self.refused(self.archive(prompt=False), "prompt-r1-1.md")

    def test_the_command_line_says_refused(self):
        d = self.archive()
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = builder.main(
                [
                    d,
                    "--round",
                    "1",
                    "--finding",
                    "F9",
                    "--out",
                    os.path.join(self.out, "c"),
                    "--repo",
                    self.repo,
                ]
            )
        self.assertEqual(code, 1)
        self.assertIn("refused: round 1 has no finding F9", err.getvalue())


class Candidates(Archive):
    """AC-4: PRs newest first, one per PR, the earliest round with a qualifying finding"""

    def test_selection_order(self):
        resolved = [{"id": "F2", "status": "resolved"}]
        self.archive(pr=7)
        self.archive(
            pr=12,
            rounds={
                1: ([finding("F1", severity="low")], []),
                2: ([finding("F2"), finding("F3")], [{"id": "F1", "status": "resolved"}]),
                3: ([], resolved + [{"id": "F3", "status": "resolved"}]),
            },
        )
        self.archive(pr=9, rounds={1: ([finding(kind="convention")], []), 2: ([], [])})
        os.makedirs(os.path.join(self.root, "notes"))
        got = [(pr, n, fid) for pr, _, n, fid in builder.candidates(self.root)]
        self.assertEqual(got, [(12, 2, "F2"), (7, 1, "F1")])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(builder.main(["--list", self.root]), 0)
        self.assertEqual(len(out.getvalue().splitlines()), 2)
