"""Acceptance tests for `[review] tests = "archive"`: the configuration key (AC-1), a round
that archives instead of committing (AC-2), later rounds (AC-3), the sandbox files (AC-4)
and the wording and merge behaviour (AC-8). Black-box through the CLI on the fixture repo."""

import os
import unittest

from revali import EXIT_ACTION, EXIT_OK
from revali.config import ConfigError, parse_project_config
from tests.helpers import TEST_REVIEW_MUL, RepoCase, approve_response, claude_entry, git, run_cli

FILE = "tests/test_review_mul.py"
SECOND = "tests/test_review_zero.py"
SECOND_TEXT = TEST_REVIEW_MUL.replace("MulTests", "ZeroTests")
DATA = "tests/review_data/mul.json"  # under test_dir, off test_file_pattern
TRAILER = "Revali-Round:"


def read(path):
    with open(path, "r", encoding="utf-8", newline="") as fh:
        return fh.read()


def approving(files):
    """An approving reviewer answer that writes exactly `files` (path -> text)."""
    tests = [
        {"path": p, "purpose": "acceptance", "covers": ["AC-1", "AC-2"], "expected": "per AC"}
        for p in files
        if p.endswith(".py")
    ]
    entry = claude_entry(approve_response(tests=tests), write_tests=False)
    entry["write_files"] = dict(files)
    return entry


def requesting_changes(files):
    entry = approving(files)
    entry["structured_output"]["verdict"] = "CHANGES_REQUESTED"
    entry["structured_output"]["findings"] = [
        {
            "id": "F1",
            "file": "src/calc.py",
            "line": 3,
            "severity": "high",
            "kind": "correctness",
            "text": "mul mishandles negative numbers",
            "suggestion": "handle them",
        }
    ]
    return entry


def needing_info(files):
    entry = approving(files)
    entry["structured_output"]["verdict"] = "NEEDS_INFO"
    entry["structured_output"]["questions"] = ["Which integers?"]
    entry["structured_output"]["tests"] = []
    return entry


class ArchiveModeCase(RepoCase):
    def setUp(self):
        super().setUp()
        os.environ["REVALI_POLL_SECONDS"] = "0.01"
        self.set_tests_key("archive")

    def set_tests_key(self, value):
        text = self.read("revali.toml")
        if "\ntests = " in text:
            lines = [ln for ln in text.splitlines(True) if not ln.startswith("tests = ")]
            text = "".join(lines)
        text = text.replace(
            'exclude = ["*.lock"]\n', 'exclude = ["*.lock"]\ntests = "%s"\n' % value
        )
        self.write("revali.toml", text)
        self.commit_all("review.tests = %s" % value)

    def head(self):
        return git(["rev-parse", "HEAD"], self.repo).strip()

    def status(self):
        return git(["status", "--porcelain"], self.repo).strip()

    def trailers(self):
        log = git(["log", "--format=%B", "main..HEAD"], self.repo)
        return [ln for ln in log.splitlines() if ln.startswith(TRAILER)]

    def archived(self, rel):
        return os.path.join(self.rdir(), "tests", rel)

    def state(self):
        from revali.state import State

        return State.load(self.rdir())

    def runner_calls(self, prefix):
        return [c for c in self.fake_calls("runner") if c.get("label", "").startswith(prefix)]

    def fix_and_commit(self, note):
        self.write("src/calc.py", self.read("src/calc.py") + "\n# %s\n" % note)
        self.commit_all(note)


class ConfigKeyTests(RepoCase):
    """AC-1 against parse_project_config, the public entry point for revali.toml."""

    def parse(self, review_line="", extra=""):
        text = self.read("revali.toml").replace(
            'exclude = ["*.lock"]\n', 'exclude = ["*.lock"]\n' + review_line
        )
        return parse_project_config(text + extra)

    def test_default_is_commit(self):
        # AC-1: the key is absent from the fixture, so defaults.toml decides
        self.assertEqual(self.parse().review.tests, "commit")

    def test_both_values_are_accepted(self):
        # AC-1
        self.assertEqual(self.parse('tests = "archive"\n').review.tests, "archive")
        self.assertEqual(self.parse('tests = "commit"\n').review.tests, "commit")

    def test_other_values_are_a_configuration_error(self):
        # AC-1
        for bad in ('tests = "branch"\n', 'tests = ""\n', 'tests = "Archive"\n'):
            with self.assertRaises(ConfigError, msg=bad) as caught:
                self.parse(bad)
            self.assertTrue(
                any("review.tests" in p for p in caught.exception.problems),
                caught.exception.problems,
            )

    def test_archive_needs_a_non_empty_archive_dir(self):
        # AC-1: both keys named, so the reader knows which pair to fix
        with self.assertRaises(ConfigError) as caught:
            self.parse('tests = "archive"\n', '\n[paths]\narchive_dir = ""\n')
        problems = caught.exception.problems
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("review.tests", problems[0])
        self.assertIn("paths.archive_dir", problems[0])
        # whitespace only is empty too
        with self.assertRaises(ConfigError):
            self.parse('tests = "archive"\n', '\n[paths]\narchive_dir = "  "\n')
        # commit mode does not care
        self.parse('tests = "commit"\n', '\n[paths]\narchive_dir = ""\n')


class FirstRoundTests(ArchiveModeCase):
    def test_round_archives_and_commits_nothing(self):
        # AC-2, AC-4, AC-8
        head = self.head()
        self.claude(approving({FILE: TEST_REVIEW_MUL, DATA: "{}\n"}))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertEqual(self.head(), head)
        self.assertEqual(self.status(), "")
        self.assertEqual(self.trailers(), [])
        self.assertFalse(self.exists(FILE))
        self.assertFalse(self.exists(DATA))
        self.assertEqual(read(self.archived(FILE)), TEST_REVIEW_MUL)
        self.assertEqual(read(self.archived(DATA)), "{}\n")
        state = self.state()
        self.assertEqual(sorted(state.test_files), [DATA, FILE])
        self.assertEqual(state.test_commits, [])
        self.assertEqual(state.pending_test_files, [])
        self.assertEqual(state.rounds[-1]["test_commit"], "")
        self.assertEqual(state.tests_mode, "archive")
        # the smoke run read the files from the working tree, before they were archived
        smoke = self.runner_calls("smoke-r1")
        self.assertEqual(len(smoke), 1, smoke)
        self.assertEqual(smoke[0]["extra_files"], [DATA, FILE])
        # validation gets the archived files as extra files at their repository paths
        validate = self.runner_calls("validate-r1")
        self.assertEqual(len(validate), 1, validate)
        self.assertEqual(validate[0]["extra_files"], [DATA, FILE])
        self.assertEqual(validate[0]["ref"], "HEAD")
        # HEAD is still the baseline commit: the existing suite is not rerun
        self.assertNotIn("test", validate[0]["steps"])
        self.assertIn("new_test", validate[0]["steps"])
        self.assertIn("  tests archived: %s, %s" % (DATA, FILE), out)
        self.assertNotIn("tests landing", out)
        # the remote got the branch as it was, nothing on top
        self.assertEqual(git(["rev-parse", "feature/mul"], self.info["remote"]).strip(), head)

    def test_commit_mode_still_commits(self):
        # AC-1 (default behaviour unchanged), AC-8
        self.set_tests_key("commit")
        self.claude(approving({FILE: TEST_REVIEW_MUL}))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertEqual(len(self.trailers()), 1)
        self.assertTrue(self.exists(FILE))
        self.assertFalse(os.path.exists(self.archived(FILE)))
        self.assertIn("  tests landing: " + FILE, out)
        self.assertNotIn("tests archived", out)
        state = self.state()
        self.assertEqual(state.tests_mode, "commit")
        self.assertEqual(len(state.test_commits), 1)


class LaterRoundTests(ArchiveModeCase):
    def test_next_round_updates_adds_and_drops(self):
        # AC-3
        self.claude(requesting_changes({FILE: TEST_REVIEW_MUL, DATA: "{}\n"}))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ACTION, out)
        self.assertEqual(self.status(), "")
        self.fix_and_commit("negatives handled")
        updated = TEST_REVIEW_MUL + "\n# round 2\n"
        entry = approving({FILE: updated, SECOND: SECOND_TEXT})
        entry["delete_files"] = [DATA]
        self.claude(entry)
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        # the reviewer found both earlier files in the working tree
        prompt = self.fake_calls("claude")[-1]["prompt"]
        self.assertIn("- " + FILE, prompt)
        self.assertEqual(read(self.archived(FILE)), updated)
        self.assertEqual(read(self.archived(SECOND)), SECOND_TEXT)
        self.assertFalse(os.path.exists(self.archived(DATA)))
        self.assertEqual(sorted(self.state().test_files), [FILE, SECOND])
        self.assertEqual(self.status(), "")
        self.assertEqual(self.trailers(), [])
        self.assertEqual(self.runner_calls("validate-r2")[-1]["extra_files"], [FILE, SECOND])
        self.assertIn("  tests archived: %s, %s" % (FILE, SECOND), out)

    def test_needs_info_round_is_archived_too(self):
        # AC-3
        self.claude(needing_info({FILE: TEST_REVIEW_MUL}))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ACTION, out)
        self.assertEqual(self.status(), "")
        self.assertFalse(self.exists(FILE))
        self.assertEqual(read(self.archived(FILE)), TEST_REVIEW_MUL)
        state = self.state()
        self.assertEqual(state.pending_test_files, [])
        self.assertEqual(state.test_files, [FILE])
        self.assertNotIn("do not commit", out)
        self.write(".revali/feature__mul/response-1.md", "- any Python int\n")
        self.claude(approving({FILE: TEST_REVIEW_MUL}))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertEqual(self.status(), "")
        self.assertEqual(self.trailers(), [])
        self.assertEqual(read(self.archived(FILE)), TEST_REVIEW_MUL)


class SandboxTests(ArchiveModeCase):
    def test_base_rerun_sends_the_archived_files(self):
        # AC-4
        self.runner_scenario(
            {"default": 0, "results": {"validate-r1": {"new_test": 1}, "base-r1": {"new_test": 0}}}
        )
        diagnosis = {
            "summary": "the product test fails on the branch only.",
            "cause": "code",
            "introduced_by": "branch",
            "failures": [],
            "recommendation": "fix mul",
        }
        self.claude(approving({FILE: TEST_REVIEW_MUL}), claude_entry(diagnosis, write_tests=False))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ACTION, out)
        base = self.runner_calls("base-r1")
        self.assertEqual(len(base), 1, self.fake_calls("runner"))
        self.assertEqual(base[0]["extra_files"], [FILE])
        self.assertEqual(self.status(), "")
        self.assertEqual(read(self.archived(FILE)), TEST_REVIEW_MUL)

    def test_suite_reruns_once_the_author_commits(self):
        # AC-4: the baseline is reused only while HEAD is the commit it passed on
        self.claude(requesting_changes({FILE: TEST_REVIEW_MUL}))
        self.assertEqual(run_cli(["run", "--foreground"])[0], EXIT_ACTION)
        self.fix_and_commit("negatives handled")
        self.claude(approving({FILE: TEST_REVIEW_MUL}))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        validate = self.runner_calls("validate-r2")
        self.assertEqual(len(validate), 1, validate)
        self.assertIn("test", validate[0]["steps"])
        self.assertEqual(validate[0]["extra_files"], [FILE])


class MergeTests(ArchiveModeCase):
    def test_merge_says_archived_and_moves_the_tests(self):
        # AC-8
        self.claude(approving({FILE: TEST_REVIEW_MUL}))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        code, out = run_cli(["merge"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("  tests archived: " + FILE, out)
        self.assertNotIn("tests landed", out)
        self.assertFalse(os.path.exists(self.rdir()))
        found = []
        for dirpath, _, names in os.walk(os.path.join(self.home, "archive")):
            for name in names:
                if name == os.path.basename(FILE):
                    found.append(os.path.join(dirpath, name))
        self.assertEqual(len(found), 1, found)
        self.assertEqual(read(found[0]), TEST_REVIEW_MUL)
        self.assertTrue(found[0].replace("\\", "/").endswith("/tests/" + FILE))

    def test_merge_in_commit_mode_says_landed(self):
        # AC-8
        self.set_tests_key("commit")
        self.claude(approving({FILE: TEST_REVIEW_MUL}))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        code, out = run_cli(["merge"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("  tests landed: " + FILE, out)
        self.assertNotIn("tests archived", out)


if __name__ == "__main__":
    unittest.main()
