"""Acceptance tests for feature/bench-cases (M3.3.2): the benchmark side (AC-2, AC-3, AC-4,
AC-5, AC-7).

AC-3: `review_bench.py --dry-run` goes as far as each case's prompt, runs no session, needs
no `--budget-usd`, reports each case as `ready` or `error` with a message, and exits 1 when a
case is not ready; a case whose `revali.toml` this revali refuses shows up there.
AC-5: `--cases` may be given more than once; every directory's cases run in one invocation
under one budget, and a name present in two directories is refused.
AC-7: a case directory may hold a `revali.toml`, which replaces the clone's own before the
configuration is read; one that is not valid TOML makes the case malformed, named.
AC-4 / AC-2: the committed set is 6 revali cases (the 3 pilot ones among them) and 4
bmc-toolkit cases, one per PR, and no committed Request holds a non-ASCII character.

Offline: the fixture repository is the case's repository and the fake claude answers. The
committed cases are read from disk only: running them needs the network.
"""

import json
import os
import re
import subprocess
import sys
import tomllib
import unittest

from tests.fixtures.make_sample_repo import CHANGE_MD
from tests.helpers import ROOT, RepoCase, approve_response, claude_entry, git

TOOL = os.path.join(ROOT, "tools", "review_bench.py")
COMMITTED = os.path.join(ROOT, "bench", "cases")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")


class DirsCase(RepoCase):
    def setUp(self):
        super().setUp()
        self.public = os.path.join(self.tmp, "public")
        self.private = os.path.join(self.tmp, "private")
        self.base = git(["rev-parse", "main"], self.repo).strip()
        self.head = git(["rev-parse", "feature/mul"], self.repo).strip()
        self.runs = 0

    def make_case(self, root, name, head=None, config=None):
        path = os.path.join(root, name)
        os.makedirs(path)
        lines = [
            "[case]",
            "repo = %s" % json.dumps(self.repo),
            "fetch = []",
            "base = %s" % json.dumps(self.base),
            "head = %s" % json.dumps(head or self.head),
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
        files = {"case.toml": "\n".join(lines) + "\n", "change.md": CHANGE_MD}
        if config is not None:
            files["revali.toml"] = config
        for filename, text in files.items():
            with open(os.path.join(path, filename), "w", encoding="utf-8", newline="\n") as fh:
                fh.write(text)
        return path

    def fixture_config(self):
        with open(os.path.join(self.repo, "revali.toml"), encoding="utf-8") as fh:
            return fh.read()

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

    def bench(self, *argv):
        """Run the tool with a new output directory; (process, cases by name, output dir)."""
        self.runs += 1
        out = os.path.join(self.tmp, "out-%d" % self.runs)
        res = subprocess.run(
            [sys.executable, TOOL, "--out", out] + list(argv),
            cwd=self.tmp,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=600,
        )
        cases = {}
        results = os.path.join(out, "results.json")
        if os.path.isfile(results):
            with open(results, encoding="utf-8") as fh:
                cases = {c["case"]: c for c in json.load(fh)["cases"]}
        return res, cases, out

    def prompt(self, out, name):
        with open(os.path.join(out, name, "prompt.md"), encoding="utf-8") as fh:
            return fh.read()


class DryRun(DirsCase):
    """AC-3"""

    def test_every_case_ready_runs_no_session_and_needs_no_budget(self):
        self.make_case(self.public, "a-ok")
        self.make_case(self.public, "b-ok")
        self.claude(self.answer())
        res, cases, out = self.bench("--dry-run", "--cases", self.public)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertEqual({n: c["outcome"] for n, c in cases.items()}, dict.fromkeys(cases, "ready"))
        self.assertEqual(sorted(cases), ["a-ok", "b-ok"])
        for name in cases:
            self.assertIn("Add mul to calc", self.prompt(out, name))  # the description
            self.assertIn("def mul(a, b):", self.prompt(out, name))  # the diff
            self.assertEqual(cases[name]["cost"], 0)
        self.assertEqual(self.fake_calls("claude"), [])
        self.assertEqual(self.fake_calls("gh"), [])
        # a real run still needs its budget: nothing is spent without one
        res, cases, _ = self.bench("--cases", self.public)
        self.assertNotEqual(res.returncode, 0)
        self.assertEqual(cases, {})
        self.assertEqual(self.fake_calls("claude"), [])

    def test_a_case_that_is_not_ready_is_an_error_with_its_message_and_exit_1(self):
        config = self.fixture_config()
        self.assertIn('method = "squash"', config)
        self.make_case(self.public, "a-ok")
        self.make_case(self.public, "b-head-gone", head="1234567890" * 4)
        self.make_case(  # a configuration this revali refuses
            self.public, "c-refused-config", config=config.replace('"squash"', '"octopus"')
        )
        self.claude(self.answer())
        res, cases, out = self.bench("--dry-run", "--cases", self.public)
        self.assertEqual(res.returncode, 1, res.stdout + res.stderr)
        self.assertNotIn("Traceback", res.stderr)
        self.assertEqual(cases["a-ok"]["outcome"], "ready")
        self.assertTrue(os.path.isfile(os.path.join(out, "a-ok", "prompt.md")))
        for name, named in (("b-head-gone", "1234567890"), ("c-refused-config", "method")):
            with self.subTest(case=name):
                self.assertEqual(cases[name]["outcome"], "error")
                self.assertIn(named, cases[name]["message"])
                self.assertIn(name, res.stdout)
        self.assertEqual(self.fake_calls("claude"), [])


class Directories(DirsCase):
    """AC-5"""

    def setUp(self):
        super().setUp()
        self.make_case(self.public, "a-one")
        self.make_case(self.public, "b-two")
        self.make_case(self.private, "c-three")
        self.claude(self.answer())  # every session: $0.50, the bug named

    def test_the_cases_of_every_directory_run_in_one_invocation(self):
        res, cases, _ = self.bench(
            "--cases", self.public, "--cases", self.private, "--budget-usd", "5"
        )
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertEqual(
            {n: c["outcome"] for n, c in cases.items()},
            {"a-one": "caught-strict", "b-two": "caught-strict", "c-three": "caught-strict"},
        )
        self.assertEqual(len(self.fake_calls("claude")), 3)
        self.assertEqual(self.fake_calls("gh"), [])

    def test_the_directories_share_one_budget(self):
        # caps of $1: 0 + 1 and 0.5 + 1 fit in 1.7; the second directory's case, at 1.0 + 1,
        # does not, which only holds when the first directory's spending counts against it
        res, cases, _ = self.bench(
            "--cases",
            self.public,
            "--cases",
            self.private,
            "--budget-usd",
            "1.7",
            "--session-budget-usd",
            "1",
        )
        self.assertEqual(res.returncode, 1, res.stdout + res.stderr)
        self.assertEqual(cases["a-one"]["outcome"], "caught-strict")
        self.assertEqual(cases["b-two"]["outcome"], "caught-strict")
        self.assertEqual(cases["c-three"]["outcome"], "not-run")
        self.assertEqual(len(self.fake_calls("claude")), 2)

    def test_a_name_in_two_directories_is_refused_before_anything_runs(self):
        self.make_case(self.private, "b-two")
        for extra in (["--budget-usd", "5"], ["--dry-run"]):
            with self.subTest(extra=extra):
                res, cases, _ = self.bench("--cases", self.public, "--cases", self.private, *extra)
                self.assertEqual(res.returncode, 1, res.stdout + res.stderr)
                self.assertNotIn("Traceback", res.stderr)
                self.assertIn("b-two", res.stderr)
                self.assertEqual(cases, {})  # no case was started
        self.assertEqual(self.fake_calls("claude"), [])


class CaseConfig(DirsCase):
    """AC-7"""

    def own_config(self):
        config = self.fixture_config()
        self.assertIn('test_dir = "tests"\n', config)
        self.assertNotIn("test_bench_", config)
        return config.replace(
            'test_dir = "tests"\n',
            'test_dir = "tests"\ntest_file_pattern = "test_bench_{topic}.py"\n',
        )

    def test_a_case_revali_toml_replaces_the_clones_own(self):
        self.make_case(self.public, "a-own-config", config=self.own_config())
        self.make_case(self.public, "b-clone-config")
        res, cases, out = self.bench("--dry-run", "--cases", self.public)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertEqual(cases["a-own-config"]["outcome"], "ready")
        self.assertEqual(cases["b-clone-config"]["outcome"], "ready")
        # the prompt names the configured test file pattern
        self.assertIn("test_bench_{topic}.py", self.prompt(out, "a-own-config"))
        self.assertNotIn("test_bench_{topic}.py", self.prompt(out, "b-clone-config"))
        self.assertEqual(self.fake_calls("claude"), [])

    def test_a_revali_toml_that_is_not_toml_makes_the_case_malformed(self):
        self.make_case(self.public, "a-ok")
        self.make_case(self.public, "b-broken", config="[review\nbudget_usd = 1.0\n")
        self.claude(self.answer())
        for extra in (["--dry-run"], ["--budget-usd", "5"]):
            with self.subTest(extra=extra):
                res, cases, _ = self.bench("--cases", self.public, *extra)
                self.assertEqual(res.returncode, 1, res.stdout + res.stderr)
                self.assertNotIn("Traceback", res.stderr)
                self.assertEqual(cases["b-broken"]["outcome"], "malformed")
                self.assertIn("revali.toml", cases["b-broken"]["message"])
                self.assertIn(cases["a-ok"]["outcome"], ("ready", "caught-strict"))
        self.assertEqual(len(self.fake_calls("claude")), 1)  # the good case's one real session


class CommittedCases(unittest.TestCase):
    """AC-4, AC-2: what is committed under bench/cases/"""

    def load(self):
        cases = {}
        for name in sorted(os.listdir(COMMITTED)):
            path = os.path.join(COMMITTED, name)
            if os.path.isdir(path):
                with open(os.path.join(path, "case.toml"), "rb") as fh:
                    cases[name] = (path, tomllib.load(fh))
        return cases

    def repo_name(self, data):
        name = data["case"]["repo"].rstrip("/").rsplit("/", 1)[-1]
        return name[:-4] if name.endswith(".git") else name

    def test_six_revali_and_four_bmc_toolkit_cases_one_per_pr(self):
        per_repo = {}
        for _name, (_path, data) in self.load().items():
            per_repo.setdefault(self.repo_name(data), []).append(data["source"]["pr"])
        # public repositories only: a private project's case is never committed
        self.assertEqual(
            {repo: len(prs) for repo, prs in per_repo.items()}, {"revali": 6, "bmc-toolkit": 4}
        )
        self.assertLessEqual({43, 61, 69}, set(per_repo["revali"]))  # the pilot cases count
        for repo, prs in per_repo.items():
            with self.subTest(repo=repo):
                self.assertEqual(len(prs), len(set(prs)), prs)

    def test_each_case_has_the_fields_of_a_built_case(self):
        for name, (path, data) in self.load().items():
            with self.subTest(case=name):
                case, expected, source = data["case"], data["expected"], data["source"]
                self.assertTrue(case["repo"].strip())
                self.assertIn("refs/pull/%d/head" % source["pr"], case["fetch"])
                self.assertRegex(case["base"], SHA_RE)
                self.assertRegex(case["head"], SHA_RE)
                self.assertNotEqual(case["base"], case["head"])
                self.assertTrue(case["branch"].strip())
                self.assertTrue(expected["file"].strip())
                first, last = expected["lines"]
                self.assertTrue(1 <= first <= last, expected["lines"])
                self.assertEqual(len(expected["description"].splitlines()), 1)
                self.assertGreaterEqual(source["round"], 1)
                self.assertRegex(source["finding"], r"^F\d+$")
                config = os.path.join(path, "revali.toml")
                if os.path.exists(config):  # AC-7: a case's own configuration parses
                    with open(config, "rb") as fh:
                        tomllib.load(fh)

    def test_no_committed_request_has_a_non_ascii_character(self):
        for name, (path, _data) in self.load().items():
            with self.subTest(case=name):
                with open(os.path.join(path, "change.md"), encoding="utf-8") as fh:
                    text = fh.read()
                match = re.search(r"^## Request\n(.*?)(?=^## |\Z)", text, re.S | re.M)
                self.assertIsNotNone(match, "no ## Request section")
                request = match.group(1)
                self.assertTrue(request.strip())
                self.assertEqual([c for c in request if ord(c) > 127], [])


if __name__ == "__main__":
    unittest.main()
