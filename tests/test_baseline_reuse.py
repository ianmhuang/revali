"""Validation reuses the baseline: the existing suite (`test`) is left out when only the
reviewer's test commits followed the commit the baseline passed on, and runs otherwise."""

import os
import types
import unittest

from revali import EXIT_ACTION, EXIT_OK
from revali.state import State
from revali.validate import baseline_reusable
from tests.helpers import RepoCase, approve_response, claude_entry, git, run_cli

REVALI_LOG = ".revali/feature__mul/logs/revali.log"
NOTE = "existing suite not rerun"


def finding():
    return {
        "id": "F1",
        "file": "src/calc.py",
        "line": 3,
        "severity": "high",
        "kind": "correctness",
        "text": "mul ignores negative numbers",
        "suggestion": "handle them",
    }


class BaselineReuse(RepoCase):
    def steps(self, label):
        return [c["steps"] for c in self.fake_calls("runner") if c["label"] == label]

    def test_round_one_leaves_the_existing_suite_out(self):
        self.claude(claude_entry())
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertEqual(self.steps("baseline"), [["test"]])
        self.assertEqual(self.steps("validate-r1"), [["new_test"]])
        state = State.load(self.rdir())
        # the baseline passed on the commit before the reviewer's test commit
        self.assertEqual(state.baseline_sha, git(["rev-parse", "HEAD~1"], self.repo).strip())
        log = self.read(REVALI_LOG)
        self.assertIn(
            "%s: unchanged since the baseline that passed on %s" % (NOTE, state.baseline_sha[:10]),
            log,
        )
        section = self.read(".revali/feature__mul/tests.md").split("## Validation 1")[1]
        self.assertIn(NOTE, section)
        self.assertNotIn("| test |", section)
        self.assertIn("| new_test |", section)
        self.assertIn(NOTE, self.read(".revali/feature__mul/logs/comment-validate-1.md"))

    def test_a_fix_round_runs_the_full_suite(self):
        cr = approve_response(verdict="CHANGES_REQUESTED", findings=[finding()])
        ok = approve_response(
            previous_findings=[{"id": "F1", "status": "resolved", "note": "fixed"}]
        )
        self.claude(claude_entry(cr), claude_entry(ok, write_tests=False))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ACTION, out)
        self.write("src/calc.py", self.read("src/calc.py") + "\n# handles negatives\n")
        self.commit_all("fix negatives")
        self.write(".revali/feature__mul/response-1.md", "- F1: fixed in the last commit\n")
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertEqual(self.steps("validate-r2"), [["test", "new_test"]])
        self.assertNotIn(NOTE, self.read(REVALI_LOG))
        self.assertNotIn(NOTE, self.read(".revali/feature__mul/tests.md"))

    def test_reuse_baseline_false_runs_the_suite_every_time(self):
        toml = self.read("revali.toml").replace(
            "[validate]\n", "[validate]\nreuse_baseline = false\n"
        )
        self.write("revali.toml", toml)
        self.commit_all("always run the suite")
        self.claude(claude_entry())
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertEqual(self.steps("validate-r1"), [["test", "new_test"]])
        self.assertNotIn(NOTE, self.read(REVALI_LOG))
        self.assertTrue(State.load(self.rdir()).baseline_sha)  # recorded, just not used

    def test_kind_docs_records_no_baseline(self):
        change = self.read(".revali/feature__mul/change.md").replace("kind: feature", "kind: docs")
        self.write(".revali/feature__mul/change.md", change)
        self.claude(
            claude_entry(
                approve_response(
                    tests=[],
                    not_testable=[
                        {"ac": "AC-1", "reason": "docs"},
                        {"ac": "AC-2", "reason": "docs"},
                    ],
                ),
                write_tests=False,
            )
        )
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertEqual(self.steps("baseline"), [])
        self.assertEqual(State.load(self.rdir()).baseline_sha, "")


class Decision(RepoCase):
    """baseline_reusable on real commits: the trailer and the touched paths decide."""

    with_remote = False

    def ctx(self, reuse=True, test_dir="tests"):
        return types.SimpleNamespace(
            repo_root=self.repo,
            head_sha=git(["rev-parse", "HEAD"], self.repo).strip(),
            cfg=types.SimpleNamespace(
                validate=types.SimpleNamespace(reuse_baseline=reuse),
                project=types.SimpleNamespace(test_dir=test_dir),
            ),
        )

    def state_at_head(self):
        st = State()
        st.baseline_sha = git(["rev-parse", "HEAD"], self.repo).strip()
        return st

    def commit(self, path, message):
        full = os.path.join(self.repo, path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "a", encoding="utf-8", newline="\n") as fh:
            fh.write("# more\n")
        git(["add", "--", path], self.repo)
        git(["commit", "-q", "-m", message], self.repo)

    def test_same_commit_is_reusable(self):
        st = self.state_at_head()
        self.assertEqual(baseline_reusable(self.ctx(), st), st.baseline_sha)

    def test_reviewer_test_commit_keeps_it_reusable(self):
        st = self.state_at_head()
        self.commit("tests/test_review_x.py", "test: review tests (round 1)\n\nRevali-Round: 1\n")
        self.assertEqual(baseline_reusable(self.ctx(), st), st.baseline_sha)

    def test_a_commit_without_the_trailer_does_not(self):
        st = self.state_at_head()
        self.commit("tests/test_mine.py", "my own test")
        self.assertEqual(baseline_reusable(self.ctx(), st), "")

    def test_a_trailer_commit_outside_test_dir_does_not(self):
        st = self.state_at_head()
        self.commit("src/calc.py", "sneaky\n\nRevali-Round: 1\n")
        self.assertEqual(baseline_reusable(self.ctx(), st), "")

    def test_empty_or_vanished_baseline_does_not(self):
        self.assertEqual(baseline_reusable(self.ctx(), State()), "")
        st = self.state_at_head()
        git(["commit", "-q", "--allow-empty", "--amend", "-m", "rewritten"], self.repo)
        self.assertEqual(baseline_reusable(self.ctx(), st), "")

    def test_config_off_does_not(self):
        st = self.state_at_head()
        self.assertEqual(baseline_reusable(self.ctx(reuse=False), st), "")


if __name__ == "__main__":
    unittest.main()
