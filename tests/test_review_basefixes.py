"""Acceptance tests for the two PR #30 follow-ups shipped with the base rerun: the skipped
reason when nothing runs (AC-9) and one shared test_dir check for baseline reuse (AC-10)."""

import os
import types
import unittest

from revali import EXIT_OK
from revali.state import State
from tests.fixtures.make_sample_repo import LOCAL_NEW_TEST, LOCAL_TEST, PY, toml_str
from tests.helpers import RepoCase, approve_response, claude_entry, git, run_cli

TESTS_MD = ".revali/feature__mul/tests.md"
REVALI_LOG = ".revali/feature__mul/logs/revali.log"


class SkippedReason(RepoCase):
    """AC-9: an empty `test` plus a new_test skipped for naming no file."""

    def configure(self, new_test):
        cfg = self.read("revali.toml").replace("test = %s" % toml_str(LOCAL_TEST), 'test = ""')
        cfg = cfg.replace("new_test = %s" % toml_str(LOCAL_NEW_TEST), "new_test = %s" % new_test)
        self.write("revali.toml", cfg)
        self.commit_all("no existing suite")

    def no_file_review(self):
        return claude_entry(
            approve_response(
                tests=[],
                not_testable=[
                    {"ac": "AC-1", "reason": "checked by hand"},
                    {"ac": "AC-2", "reason": "checked by hand"},
                ],
            ),
            write_tests=False,
        )

    def test_reason_names_both(self):
        self.configure(toml_str(PY + " -m unittest {files}"))
        self.claude(self.no_file_review())
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        expected = "nothing to run: no test command and new_test names no test file"
        self.assertIn(expected, self.read(TESTS_MD))
        self.assertIn(expected, self.read(REVALI_LOG))
        self.assertIn(expected, out)
        self.assertEqual(self.fake_calls("runner"), [])  # nothing ran, not even a baseline


class SharedTestDirCheck(RepoCase):
    """AC-10: baseline_reusable decides "under test_dir" with review's helper, so every
    spelling of test_dir gives the same answer in both places."""

    def test_helper_is_importable_and_normalises(self):
        from revali.review import under_test_dir

        for spelling in ("tests", "tests/", "tests\\", "tests//"):
            self.assertTrue(under_test_dir("tests/test_a.py", spelling), spelling)
            self.assertTrue(under_test_dir("tests\\test_a.py", spelling), spelling)
        self.assertFalse(under_test_dir("tests_extra/test_a.py", "tests"))
        self.assertFalse(under_test_dir("tests", "tests"))
        self.assertFalse(under_test_dir("src/x.py", "tests"))

    def test_baseline_reuse_agrees_for_every_spelling(self):
        from revali.validate import baseline_reusable

        st = State()
        st.baseline_sha = git(["rev-parse", "HEAD"], self.repo).strip()
        path = os.path.join(self.repo, "tests", "test_review_x.py")
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("# reviewer file\n")
        git(["add", "--", "tests/test_review_x.py"], self.repo)
        git(["commit", "-q", "-m", "test: x\n\nRevali-Round: 1\n"], self.repo)
        head = git(["rev-parse", "HEAD"], self.repo).strip()

        def ctx(test_dir):
            return types.SimpleNamespace(
                repo_root=self.repo,
                head_sha=head,
                cfg=types.SimpleNamespace(
                    validate=types.SimpleNamespace(reuse_baseline=True),
                    project=types.SimpleNamespace(test_dir=test_dir),
                ),
            )

        for spelling in ("tests", "tests/", "tests\\", "tests//"):
            self.assertEqual(baseline_reusable(ctx(spelling), st), st.baseline_sha, spelling)
        # a commit outside test_dir still forbids the reuse, for every spelling
        with open(os.path.join(self.repo, "src", "calc.py"), "a", encoding="utf-8") as fh:
            fh.write("# touched\n")
        git(["add", "--", "src/calc.py"], self.repo)
        git(["commit", "-q", "-m", "test: y\n\nRevali-Round: 1\n"], self.repo)
        head = git(["rev-parse", "HEAD"], self.repo).strip()
        for spelling in ("tests", "tests/", "tests\\"):
            self.assertEqual(baseline_reusable(ctx(spelling), st), "", spelling)


if __name__ == "__main__":
    unittest.main()
