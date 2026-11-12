"""Acceptance tests for feature/bench-resume (M3.3.3), AC-6: `tools/bench_case.py` ends in
`refused: <reason>`, exit 1, with no traceback when the output directory cannot be created
or written and when an archive field has the wrong type, and leaves no half-written case.

The builder runs as a subprocess, as a user runs it, against an archive written by the test
in the layout `revali merge` leaves; the fixture repository stands for the case's repository
(`--repo`). One failure cannot be produced from outside on every platform, a write that fails
after the directory was created: for that one the builder's `main` is called in-process with
`open` refusing to write the case's change.md.
"""

import builtins
import contextlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import unittest
from unittest import mock

from tests.helpers import ROOT, RepoCase, git

BUILDER = os.path.join(ROOT, "tools", "bench_case.py")

SPEC = importlib.util.spec_from_file_location("bench_case", BUILDER)
builder = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(builder)

CHANGE_MD = """---
title: Add mul to calc
kind: feature
author_model: fixture
---

## Request
Add mul to calc.

## Goal
`mul` multiplies two integers.

## Acceptance criteria
- AC-1: mul(a, b) returns the product of a and b for integers

## Out of scope
Division.

## Dependencies
none
"""

PROMPT = """You are the independent reviewer for a pull request.

# What you are reviewing

Branch `feature/mul` against `main`. Change kind: `feature`. Round 1.

## The author's description (change.md)

%s

## The diff (base...HEAD)

```diff
+def mul(a, b):
+    return a + b
```

# How to review
"""


def finding(fid="F1"):
    return {
        "id": fid,
        "file": "src/calc.py",
        "line": 13,
        "severity": "medium",
        "kind": "correctness",
        "text": "mul adds instead of multiplying. The test only checks zero.",
        "suggestion": "multiply",
    }


class BuilderCase(RepoCase):
    def setUp(self):
        super().setUp()
        self.head = git(["rev-parse", "feature/mul"], self.repo).strip()
        git(["update-ref", "refs/pull/7/head", self.head], self.repo)  # what GitHub keeps
        self.archive_root = os.path.join(self.tmp, "archive", "owner__sample")
        self.cases = os.path.join(self.tmp, "built")

    def put(self, path, data):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write(data if isinstance(data, str) else json.dumps(data, indent=1))

    def archive(self, pr=7, root=None, state=None, review=None, review_data=None, next_data=None):
        """One PR's archive: round 1 names F1, round 2 marks it resolved. `state` and `review`
        overwrite keys of state.json and review-1.json; `review_data` and `next_data` replace
        the `data` of review-1.json and review-2.json."""
        d = os.path.join(root or self.archive_root, "%d-feature__mul" % pr)
        base = {"repo": "owner/sample", "branch": "feature/mul", "base": "main", "pr_number": pr}
        base.update(state or {})
        self.put(os.path.join(d, "state.json"), base)
        first = {
            "verdict": "CHANGES_REQUESTED",
            "head_sha": self.head,
            "data": {"findings": [finding()], "previous_findings": []},
        }
        if review_data is not None:
            first["data"] = review_data
        first.update(review or {})
        self.put(os.path.join(d, "review-1.json"), first)
        second = {
            "verdict": "APPROVE",
            "head_sha": self.head,
            "data": {"findings": [], "previous_findings": [{"id": "F1", "status": "resolved"}]},
        }
        if next_data is not None:
            second["data"] = next_data
        self.put(os.path.join(d, "review-2.json"), second)
        for n in (1, 2):
            self.put(os.path.join(d, "logs", "prompt-r%d-1.md" % n), PROMPT % CHANGE_MD)
        return d

    def run_tool(self, argv):
        return subprocess.run(
            [sys.executable, BUILDER] + argv,
            cwd=self.tmp,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=600,
        )

    def build(self, archive, out):
        return self.run_tool(
            [archive, "--round", "1", "--finding", "F1", "--out", out, "--repo", self.repo]
        )

    def assert_refused(self, res, reason):
        self.assertNotIn("Traceback", res.stderr)
        self.assertEqual(res.returncode, 1, res.stdout + res.stderr)
        self.assertIn("refused: ", res.stderr)
        self.assertRegex(res.stderr, reason)


class WrongTypes(BuilderCase):
    """AC-6: an archive field of the wrong type"""

    def test_a_good_archive_builds(self):
        # the control: what the refusals below change is the one field each
        out = os.path.join(self.cases, "good")
        res = self.build(self.archive(), out)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertTrue(os.path.isfile(os.path.join(out, "case.toml")))

    def test_state_json_repo_branch_or_base_that_is_not_a_string(self):
        for key, value in (
            ("repo", ["owner/sample"]),
            ("branch", 7),
            ("base", {"name": "main"}),
            ("base", ["main"]),
        ):
            label = "%s-%s" % (key, type(value).__name__)
            with self.subTest(field=label):
                archive = self.archive(
                    root=os.path.join(self.tmp, "a-" + label), state={key: value}
                )
                out = os.path.join(self.cases, label)
                res = self.build(archive, out)
                self.assert_refused(res, key)
                self.assertFalse(os.path.exists(out))

    def test_a_head_sha_that_is_not_a_string(self):
        for label, value in (
            ("number", 12345),
            ("list", [self.head]),
            ("object", {"sha": self.head}),
            ("null", None),
        ):
            with self.subTest(head_sha=label):
                archive = self.archive(
                    root=os.path.join(self.tmp, "a-head-" + label), review={"head_sha": value}
                )
                out = os.path.join(self.cases, "head-" + label)
                res = self.build(archive, out)
                self.assert_refused(res, r"head_sha")
                self.assertFalse(os.path.exists(out))

    def test_list_meets_wrong_types_without_a_traceback(self):
        broken = self.archive(pr=7, review_data=["not", "an", "object"])
        self.archive(pr=12, next_data={"findings": [], "previous_findings": 5})
        self.archive(pr=20)
        res = self.run_tool(["--list", self.archive_root])
        self.assertNotIn("Traceback", res.stderr)
        self.assertIn(res.returncode, (0, 1), res.stdout + res.stderr)
        if res.returncode == 1:
            self.assertIn("refused: ", res.stderr)
        else:  # the rounds that cannot be used are passed over, the rest is listed
            self.assertIn("%s20-feature__mul" % os.sep, res.stdout)
            self.assertNotIn("%s7-feature__mul" % os.sep, res.stdout)
            self.assertNotIn("%s12-feature__mul" % os.sep, res.stdout)
        # building from the round whose data is not an object is refused, too
        out = os.path.join(self.cases, "not-an-object")
        res = self.build(broken, out)
        self.assert_refused(res, r"review-1\.json")
        self.assertFalse(os.path.exists(out))


class OutputDirectory(BuilderCase):
    """AC-6: the output directory cannot be created or written"""

    def test_a_parent_that_is_a_file(self):
        blocker = os.path.join(self.tmp, "blocker")
        self.put(blocker, "a file where a directory is wanted\n")
        res = self.build(self.archive(), os.path.join(blocker, "case"))
        self.assert_refused(res, r"blocker")
        self.assertTrue(os.path.isfile(blocker))
        with open(blocker, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "a file where a directory is wanted\n")

    def test_a_write_that_fails_half_way_leaves_no_case_directory(self):
        archive = self.archive()
        out = os.path.join(self.cases, "half")
        real_open, refused = builtins.open, []

        def no_change_md(file, mode="r", *args, **kwargs):
            if "w" in mode and os.path.basename(str(file)) == "change.md":
                refused.append(str(file))
                raise PermissionError(13, "Permission denied", str(file))
            return real_open(file, mode, *args, **kwargs)

        err = io.StringIO()
        argv = [archive, "--round", "1", "--finding", "F1", "--out", out, "--repo", self.repo]
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            with mock.patch("builtins.open", no_change_md):
                try:
                    code = builder.main(argv)
                except OSError as exc:  # what a user would see as a traceback
                    code = "not refused, raised %r" % (exc,)
        self.assertEqual(code, 1, err.getvalue())
        self.assertEqual(len(refused), 1, refused)  # the write was reached and failed
        self.assertIn("refused: ", err.getvalue())
        self.assertNotIn("Traceback", err.getvalue())
        self.assertFalse(os.path.exists(out))  # the half-written case is gone


if __name__ == "__main__":
    unittest.main()
