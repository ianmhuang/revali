import os
import unittest

from tests.helpers import RepoCase, approve_response, claude_entry, git, run_cli
from revali import EXIT_ACTION, EXIT_ERROR, EXIT_OK
from revali.state import State, read_history
from revali.stats import summarise


class MergeTests(RepoCase):
    def setUp(self):
        super().setUp()
        os.environ["REVALI_POLL_SECONDS"] = "0.01"

    def ready(self):
        self.claude(claude_entry())
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)

    def test_refused_when_not_ready(self):
        code, out = run_cli(["merge"])
        self.assertEqual(code, EXIT_ERROR)
        self.assertIn("not ready to merge", out)
        self.claude(claude_entry(approve_response(verdict="CHANGES_REQUESTED", findings=[
            {"id": "F1", "file": "src/calc.py", "line": 1, "severity": "high", "kind": "correctness", "text": "t", "suggestion": ""}])))
        run_cli(["run", "--foreground"])
        code, out = run_cli(["merge"])
        self.assertEqual(code, EXIT_ERROR)
        self.assertIn("needs_action", out)

    def test_merge_without_checks(self):
        self.ready()
        code, out = run_cli(["merge"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("MERGED: PR #7 into main", out)
        self.assertIn("tests/test_review_mul.py", out)
        gh = [c["argv"] for c in self.fake_calls("gh")]
        merge_calls = [a for a in gh if a[:2] == ["pr", "merge"]]
        self.assertEqual(merge_calls, [["pr", "merge", "7", "--squash", "--delete-branch"]])
        self.assertTrue(any(a[:2] == ["pr", "checks"] for a in gh))
        self.assertEqual(git(["rev-parse", "--abbrev-ref", "HEAD"], self.repo).strip(), "main")
        self.assertNotIn("feature/mul", git(["branch", "--list"], self.repo))
        self.assertFalse(os.path.isdir(self.rdir()))
        rows = read_history(os.path.join(self.home, "history.jsonl"))
        self.assertEqual(rows[-1]["stage"], "merged")
        self.assertEqual(rows[-1]["repo"], "me/sample")

    def test_failing_checks_block(self):
        self.ready()
        self.scenario({"checks": [{"name": "ci", "state": "FAILURE", "bucket": "fail"},
                                  {"name": "lint", "state": "SUCCESS", "bucket": "pass"}]})
        code, out = run_cli(["merge"])
        self.assertEqual(code, EXIT_ACTION, out)
        self.assertIn("CI checks failed: ci", out)
        self.assertEqual(State.load(self.rdir()).stage, "ready_to_merge")
        self.assertFalse(any(c["argv"][:2] == ["pr", "merge"] for c in self.fake_calls("gh")))

    def test_pending_checks_then_green(self):
        self.ready()
        self.scenario({"checks_sequence": [[{"name": "ci", "state": "PENDING", "bucket": "pending"}],
                                           [{"name": "ci", "state": "SUCCESS", "bucket": "pass"}]]})
        code, out = run_cli(["merge"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertGreaterEqual(len([c for c in self.fake_calls("gh") if c["argv"][:2] == ["pr", "checks"]]), 2)

    def test_pending_checks_timeout(self):
        self.write("revali.toml", self.read("revali.toml").replace("[merge]", "[merge]\nchecks_timeout_min = 0"))
        self.commit_all("timeout")
        self.ready()
        self.scenario({"checks": [{"name": "ci", "state": "PENDING", "bucket": "pending"}]})
        code, out = run_cli(["merge"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertIn("still pending", out)

    def test_checks_disabled(self):
        self.write("revali.toml", self.read("revali.toml").replace("[merge]", "[merge]\nwait_for_checks = false"))
        self.commit_all("no wait")
        self.ready()
        self.scenario({"checks": [{"name": "ci", "state": "FAILURE", "bucket": "fail"}]})
        code, out = run_cli(["merge"])
        self.assertEqual(code, EXIT_OK, out)

    def test_head_moved_after_validation(self):
        self.ready()
        self.write("src/calc.py", self.read("src/calc.py") + "\n# late edit\n")
        self.commit_all("late")
        code, out = run_cli(["merge"])
        self.assertEqual(code, EXIT_ACTION, out)
        self.assertIn("HEAD moved", out)

    def test_gh_merge_failure(self):
        self.ready()
        self.scenario({"merge_exit": 1})
        code, out = run_cli(["merge"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertIn("gh pr merge failed", out)
        self.assertEqual(State.load(self.rdir()).stage, "ready_to_merge")
        self.assertEqual(git(["rev-parse", "--abbrev-ref", "HEAD"], self.repo).strip(), "feature/mul")

    def test_merge_method_from_config(self):
        self.write("revali.toml", self.read("revali.toml").replace('method = "squash"', 'method = "rebase"'))
        self.commit_all("rebase")
        self.ready()
        run_cli(["merge"])
        self.assertTrue(any(c["argv"][:4] == ["pr", "merge", "7", "--rebase"] for c in self.fake_calls("gh")))


class StatsTests(RepoCase):
    def test_stats_after_runs(self):
        self.claude(claude_entry())
        run_cli(["run", "--foreground"])
        code, out = run_cli(["stats"])
        self.assertEqual(code, EXIT_OK)
        self.assertIn("me/sample", out)
        self.assertIn("| 1/1 |", out)
        self.assertIn("claude-fable-5", out)

    def test_summarise_empty_and_mixed(self):
        self.assertEqual(summarise([]), "no runs recorded yet")
        # branch a: rejected, fixed, passed on a fallback model, then merged; rows are cumulative
        # branch b: passed first try, not merged yet; repo s: gave up
        rows = [
            {"repo": "r", "branch": "a", "pr": 1, "stage": "needs_action", "fixes": 0, "rounds": 1, "last_verdict": "CHANGES_REQUESTED", "cost_usd": 0.5},
            {"repo": "r", "branch": "a", "pr": 1, "stage": "ready_to_merge", "fixes": 1, "rounds": 2, "last_verdict": "PASS", "cost_usd": 1.0, "fallback": True},
            {"repo": "r", "branch": "a", "pr": 1, "stage": "merged", "fixes": 1, "rounds": 2, "last_verdict": "PASS", "cost_usd": 1.0},
            {"repo": "r", "branch": "b", "pr": 2, "stage": "ready_to_merge", "fixes": 0, "rounds": 1, "last_verdict": "PASS", "cost_usd": 0.7},
            {"repo": "s", "branch": "c", "pr": 3, "stage": "needs_human", "fixes": 3, "rounds": 3, "last_verdict": "FAIL", "cost_usd": 3.0},
        ]
        text = summarise(rows)
        self.assertIn("history rows: 5, pipelines: 3", text)
        self.assertIn("| r | 2 | 2 | 1/2 | 1 | 0 | 1 | 1.5 | $1.70 |", text)
        self.assertIn("| s | 1 | 1 | - | 0 | 1 | 0 | 3.0 | $3.00 |", text)
        self.assertIn("last verdicts: FAIL 1, PASS 2", text)

if __name__ == "__main__":
    unittest.main()
