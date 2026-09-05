"""End-to-end through the CLI with fake gh, fake claude, and the fake or real local runner."""

import json
import os
import unittest

from revali import EXIT_ACTION, EXIT_ERROR, EXIT_HUMAN, EXIT_OK
from revali.state import State, read_history
from tests.helpers import RepoCase, approve_response, claude_entry, git, run_cli


def finding(sev="high", kind="correctness", fid="F1"):
    return {
        "id": fid,
        "file": "src/calc.py",
        "line": 3,
        "severity": sev,
        "kind": kind,
        "text": "mul ignores negative numbers",
        "suggestion": "handle them",
    }


class ApprovePath(RepoCase):
    def test_full_round(self):
        self.claude(claude_entry())
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("READY TO MERGE", out)
        state = State.load(self.rdir())
        self.assertEqual(state.stage, "ready_to_merge")
        self.assertEqual(len(state.rounds), 1)
        self.assertEqual(state.rounds[0]["verdict"], "APPROVE")
        self.assertEqual(state.last_verdict, "PASS")
        self.assertEqual(state.pr_number, 7)
        self.assertEqual(state.test_files, ["tests/test_review_mul.py"])
        self.assertEqual(len(state.test_commits), 1)
        self.assertAlmostEqual(state.cost_usd, 0.5)
        self.assertEqual(state.models_used, ["claude-fable-5"])
        self.assertFalse(state.fallback)
        # the test file is committed with the trailers, and pushed
        log = git(["log", "-1", "--format=%B"], self.repo)
        self.assertIn("test: review tests (round 1)", log)
        self.assertIn("Co-Authored-By: Claude", log)
        self.assertIn("Revali-Round: 1", log)
        self.assertEqual(git(["status", "--porcelain"], self.repo).strip(), "")
        remote_head = git(["rev-parse", "feature/mul"], self.info["remote"]).strip()
        self.assertEqual(remote_head, git(["rev-parse", "HEAD"], self.repo).strip())
        # files
        self.assertTrue(self.exists(".revali/feature__mul/review-1.md"))
        self.assertTrue(self.exists(".revali/feature__mul/review-1.json"))
        tests_md = self.read(".revali/feature__mul/tests.md")
        self.assertIn("AC-1", tests_md)
        self.assertIn("`tests/test_review_mul.py`", tests_md)
        review_md = self.read(".revali/feature__mul/review-1.md")
        self.assertIn("model_actual: claude-fable-5", review_md)
        self.assertIn("# Review round 1: APPROVE", review_md)
        # gh: draft PR created, comment posted, body updated
        gh = [c["argv"] for c in self.fake_calls("gh")]
        create = [a for a in gh if a[:2] == ["pr", "create"]]
        self.assertEqual(len(create), 1)
        self.assertIn("--draft", create[0])
        self.assertIn("Add mul to calc", create[0])
        self.assertTrue(any(a[:2] == ["pr", "comment"] for a in gh))
        self.assertTrue(any(a[:2] == ["pr", "edit"] for a in gh))
        # claude: invoked once with the prompt on stdin and the allowlist
        cl = self.fake_calls("claude")
        self.assertEqual(len(cl), 1)
        self.assertIn("--json-schema", cl[0]["argv"])
        self.assertIn("acceptEdits", cl[0]["argv"])
        self.assertIn("add a mul(a, b) function", cl[0]["prompt"])
        # runner: baseline, smoke run with the new file, full validation
        rn = self.fake_calls("runner")
        self.assertEqual([r["label"] for r in rn], ["baseline", "smoke-r1-1", "validate-r1"])
        self.assertEqual(rn[0]["steps"], ["test"])
        self.assertEqual(rn[1]["extra_files"], ["tests/test_review_mul.py"])
        self.assertEqual(rn[1]["steps"], ["new_test"])
        self.assertEqual(rn[2]["steps"], ["test", "new_test"])
        self.assertTrue(any(a[:2] == ["pr", "ready"] for a in gh))
        # history
        rows = read_history(os.path.join(self.home, "history.jsonl"))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["last_verdict"], "PASS")
        self.assertEqual(rows[0]["exit"], 0)

    def test_real_local_runner_smoke(self):
        self.use_real_local_runner()
        self.claude(claude_entry())
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("READY TO MERGE", out)
        log = self.read(".revali/feature__mul/logs/smoke-r1-1-new_test.log")
        self.assertIn("(exit 0)", log)
        self.assertIn("(exit 0)", self.read(".revali/feature__mul/logs/validate-r1-new_test.log"))
        self.assertIn("(exit 0)", self.read(".revali/feature__mul/logs/baseline-test.log"))
        self.assertEqual(git(["worktree", "list"], self.repo).strip().count("\n"), 0)

    def test_dry_run(self):
        code, out = run_cli(["run", "--foreground", "--dry-run"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("DRY RUN OK", out)
        self.assertEqual(self.fake_calls("claude"), [])
        self.assertFalse(any(c["argv"][:2] == ["pr", "create"] for c in self.fake_calls("gh")))

    def test_gitignore_added_when_missing(self):
        self.write(".gitignore", ".venv/\n")
        self.commit_all("drop ignore")
        self.claude(claude_entry())
        run_cli(["run", "--foreground"])
        self.assertIn(".revali/", self.read(".gitignore"))
        self.assertIn("chore: ignore .revali/", git(["log", "--format=%s"], self.repo))

    def test_gitignore_untouched_when_git_already_ignores(self):
        # .git/info/exclude ignores the state dir; .gitignore must stay as the project left it
        self.write(".gitignore", ".venv/\n")
        self.commit_all("drop ignore")
        exclude = os.path.join(self.repo, ".git", "info", "exclude")
        with open(exclude, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(".revali/\n")
        self.claude(claude_entry())
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertEqual(self.read(".gitignore"), ".venv/\n")
        self.assertNotIn("chore: ignore", git(["log", "--format=%s"], self.repo))

    def test_existing_open_pr_reused(self):
        self.scenario(
            {
                "prs_open": [
                    {"number": 12, "url": "https://x/pull/12", "isDraft": True, "title": "t"}
                ]
            }
        )
        self.claude(claude_entry())
        run_cli(["run", "--foreground"])
        self.assertEqual(State.load(self.rdir()).pr_number, 12)
        self.assertFalse(any(c["argv"][:2] == ["pr", "create"] for c in self.fake_calls("gh")))

    def test_closed_pr_refused(self):
        self.scenario({"prs_all": [{"number": 3, "url": "u", "state": "CLOSED"}]})
        self.claude(claude_entry())
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR)
        self.assertIn("PR #3", out)
        self.assertEqual(self.fake_calls("claude"), [])

    def test_docs_kind_needs_no_tests(self):
        self.write(
            ".revali/feature__mul/change.md",
            self.read(".revali/feature__mul/change.md").replace("kind: feature", "kind: docs"),
        )
        data = approve_response(
            tests=[],
            not_testable=[
                {"ac": "AC-1", "reason": "kind docs"},
                {"ac": "AC-2", "reason": "kind docs"},
            ],
        )
        self.claude(claude_entry(data, write_tests=False))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("READY TO MERGE", out)
        self.assertEqual(self.fake_calls("runner"), [])
        self.assertEqual(State.load(self.rdir()).test_files, [])
        self.assertIn("kind docs: nothing to run", self.read(".revali/feature__mul/tests.md"))


class ChangesRequestedLoop(RepoCase):
    def test_changes_requested_then_fix_then_approve(self):
        cr = approve_response(verdict="CHANGES_REQUESTED", findings=[finding()])
        ok = approve_response(
            previous_findings=[{"id": "F1", "status": "resolved", "note": "fixed"}]
        )
        self.claude(claude_entry(cr), claude_entry(ok, write_tests=False))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ACTION, out)
        self.assertIn("changes requested", out)
        self.assertIn("F1", out)
        self.assertIn("response-1.md", out)
        state = State.load(self.rdir())
        self.assertEqual(state.stage, "needs_action")
        self.assertEqual(state.fixes, 0)
        self.assertEqual(
            len(state.test_commits), 1
        )  # tests are committed even on CHANGES_REQUESTED

        # rerun without changing anything: refused before spending tokens
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ACTION)
        self.assertIn("nothing changed", out)
        self.assertEqual(len(self.fake_calls("claude")), 1)

        # fix, answer, rerun
        self.write("src/calc.py", self.read("src/calc.py") + "\n# handles negatives\n")
        self.commit_all("fix negatives")
        self.write(".revali/feature__mul/response-1.md", "- F1: fixed in the last commit\n")
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("READY TO MERGE", out)
        state = State.load(self.rdir())
        self.assertEqual(state.fixes, 1)
        self.assertEqual(len(state.rounds), 2)
        self.assertEqual(state.rounds[1]["verdict"], "APPROVE")
        prompt2 = self.fake_calls("claude")[1]["prompt"]
        self.assertIn("Previous round (1)", prompt2)
        self.assertIn("fixed in the last commit", prompt2)
        self.assertIn("earlier rounds", prompt2)
        self.assertIn("## Previous findings", self.read(".revali/feature__mul/review-2.md"))

    def test_max_fixes_exhausted(self):
        cr = approve_response(verdict="CHANGES_REQUESTED", findings=[finding()])
        self.claude(
            claude_entry(cr),
            claude_entry(cr, write_tests=False),
            claude_entry(cr, write_tests=False),
        )
        self.write(
            "revali.toml", self.read("revali.toml").replace("max_fixes = 2", "max_fixes = 1")
        )
        self.commit_all("limit")
        self.assertEqual(run_cli(["run", "--foreground"])[0], EXIT_ACTION)
        self.write("src/calc.py", self.read("src/calc.py") + "\n# try 1\n")
        self.commit_all("try 1")
        self.assertEqual(run_cli(["run", "--foreground"])[0], EXIT_ACTION)
        self.write("src/calc.py", self.read("src/calc.py") + "\n# try 2\n")
        self.commit_all("try 2")
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_HUMAN, out)
        self.assertIn("fix cycles", out)
        self.assertEqual(State.load(self.rdir()).stage, "needs_human")
        self.assertEqual(len(self.fake_calls("claude")), 2)

    def test_needs_info_once_then_changes_requested(self):
        q = approve_response(
            verdict="NEEDS_INFO", questions=["Should mul accept floats?"], tests=[]
        )
        self.claude(claude_entry(q, write_tests=False), claude_entry(q, write_tests=False))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ACTION, out)
        self.assertIn("questions", out)
        self.assertIn("accept floats", out)
        state = State.load(self.rdir())
        self.assertTrue(state.needs_info_used)
        self.assertEqual(state.fixes, 0)
        self.assertEqual(state.test_commits, [])
        self.write(".revali/feature__mul/response-1.md", "- floats: yes\n")
        code, out = run_cli(["run", "--foreground"])  # no code change needed after a question
        self.assertEqual(code, EXIT_ACTION, out)
        self.assertIn("unanswered", out)
        self.assertEqual(State.load(self.rdir()).fixes, 0)

    def test_history_rewrite_restarts(self):
        self.claude(
            claude_entry(approve_response(verdict="CHANGES_REQUESTED", findings=[finding()])),
            claude_entry(),
        )
        run_cli(["run", "--foreground"])
        git(["reset", "-q", "--hard", "HEAD~1"], self.repo)  # drop the reviewer's test commit
        self.write("src/calc.py", self.read("src/calc.py") + "\n# redo\n")
        self.commit_all("redo")
        code, out = run_cli(["run", "--foreground"])
        self.assertIn("starts over", out)
        state = State.load(self.rdir())
        self.assertEqual(state.fixes, 0)
        self.assertEqual(len(state.rounds), 1)


class ReviewerMisbehaviour(RepoCase):
    def test_guard_reverts_outside_test_dir(self):
        entry = claude_entry()
        entry["write_files"]["src/evil.py"] = "print('hi')\n"
        entry["write_files"]["README.md"] = "# hacked\n"
        self.claude(entry)
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertIn("outside tests", out)
        self.assertFalse(self.exists("src/evil.py"))
        self.assertEqual(self.read("README.md"), "# sample\n\nFixture project for revali.\n")

    def test_malformed_json(self):
        self.claude({"raw_stdout": "not json at all", "exit": 0})
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR)
        self.assertIn("invalid JSON", out)
        self.assertTrue(self.exists(".revali/feature__mul/logs/review-r1-1.raw.json"))

    def test_schema_mismatch(self):
        self.claude(claude_entry({"verdict": "APPROVE"}, write_tests=False))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR)
        self.assertIn("does not match the schema", out)

    def test_session_error(self):
        self.claude(claude_entry(is_error=True, exit=1))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR)
        self.assertIn("reviewer session failed", out)

    def test_unjustified_test_change_forces_changes_requested(self):
        self.write("tests/test_calc.py", self.read("tests/test_calc.py").replace("5)", "6)"))
        self.commit_all("weaken")
        data = approve_response(
            test_changes=[
                {"file": "tests/test_calc.py", "justified": False, "reason": "assertion changed"}
            ]
        )
        self.claude(claude_entry(data))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ACTION, out)
        self.assertIn("without justification", out)
        self.assertIn("Reviewer said APPROVE", self.read(".revali/feature__mul/review-1.md"))

    def test_ac_gap_bounces_once(self):
        partial = approve_response(
            tests=[
                {
                    "path": "tests/test_review_mul.py",
                    "purpose": "p",
                    "covers": ["AC-1"],
                    "expected": "e",
                }
            ]
        )
        self.claude(claude_entry(partial), claude_entry(write_tests=False))
        code, out = run_cli(["run", "--foreground"])
        self.assertIn("READY TO MERGE", out)
        cl = self.fake_calls("claude")
        self.assertEqual(len(cl), 2)
        self.assertIn("Corrections required", cl[1]["prompt"])
        self.assertIn("AC-2", cl[1]["prompt"])
        self.assertEqual(json.loads(self.read(".revali/feature__mul/review-1.json"))["bounces"], 1)

    def test_ac_gap_persisting_forces_changes_requested(self):
        partial = approve_response(
            tests=[
                {
                    "path": "tests/test_review_mul.py",
                    "purpose": "p",
                    "covers": ["AC-1"],
                    "expected": "e",
                }
            ]
        )
        self.claude(claude_entry(partial), claude_entry(partial, write_tests=False))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ACTION, out)
        self.assertIn("AC-2", out)

    def test_smoke_bounce_then_ok(self):
        self.runner_scenario(
            {
                "default": 0,
                "results": {"smoke-r1-1": {"new_test": 2}},
                "outputs": {"smoke-r1-1": {"new_test": "ImportError: no module named nothing"}},
            }
        )
        self.claude(claude_entry(), claude_entry())
        code, out = run_cli(["run", "--foreground"])
        self.assertIn("READY TO MERGE", out)
        cl = self.fake_calls("claude")
        self.assertEqual(len(cl), 2)
        self.assertIn("could not run", cl[1]["prompt"])
        self.assertIn("ImportError", cl[1]["prompt"])

    def test_smoke_failing_twice_is_error(self):
        self.runner_scenario(
            {
                "default": 0,
                "results": {"smoke-r1-1": {"new_test": 2}, "smoke-r1-2": {"new_test": 2}},
            }
        )
        self.claude(claude_entry(), claude_entry())
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertIn("still cannot run", out)

    def test_smoke_assertion_failure_is_not_a_bounce(self):
        self.runner_scenario({"default": 0, "results": {"smoke-r1-1": {"new_test": 1}}})
        self.claude(claude_entry())
        code, out = run_cli(["run", "--foreground"])
        self.assertIn("READY TO MERGE", out)
        self.assertEqual(len(self.fake_calls("claude")), 1)

    def test_setup_failure_is_pipeline_error(self):
        self.write(
            "revali.toml",
            self.read("revali.toml").replace('setup = ""', 'setup = "pip install nothing"'),
        )
        self.commit_all("setup")
        self.runner_scenario({"default": 0, "results": {"smoke-r1-1": {"setup": 1}}})
        self.claude(claude_entry())
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertIn("sandbox setup step failed", out)

    def test_fallback_model_flagged(self):
        self.claude(claude_entry(model="claude-opus-5"))
        run_cli(["run", "--foreground"])
        state = State.load(self.rdir())
        self.assertTrue(state.fallback)
        self.assertIn("fallback: True", self.read(".revali/feature__mul/review-1.md"))
        self.assertIn("FALLBACK", self.read(".revali/feature__mul/logs/revali.log"))

    def test_comment_with_secret_withheld(self):
        data = approve_response(summary="Found key AKIAIOSFODNN7EXAMPLE in config, otherwise fine.")
        self.claude(claude_entry(data))
        run_cli(["run", "--foreground"])
        posted = self.read(".revali/feature__mul/logs/comment-review-1.md")
        self.assertNotIn("AKIAIOSFODNN7EXAMPLE", posted)
        self.assertIn("withheld", posted)
        self.assertIn("AKIAIOSFODNN7EXAMPLE", self.read(".revali/feature__mul/review-1.md"))


class HistoryRepoTests(RepoCase):
    """A run that stops in preflight still names its repo in history (stats grouped it as
    unknown)."""

    def test_preflight_stop_records_repo_from_origin(self):
        git(["remote", "set-url", "origin", "https://github.com/Me/Sample.git"], self.repo)
        self.write("src/calc.py", "# dirty\n")
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR, out)
        rows = read_history(os.path.join(self.home, "history.jsonl"))
        self.assertEqual(rows[-1]["repo"], "me/sample")
        code, out = run_cli(["stats"])
        self.assertNotIn("(unknown repo)", out)
        self.assertIn("me/sample", out)

    def test_ssh_style_origin(self):
        git(["remote", "set-url", "origin", "git@github.com:me/sample.git"], self.repo)
        self.write("src/calc.py", "# dirty\n")
        run_cli(["run", "--foreground"])
        rows = read_history(os.path.join(self.home, "history.jsonl"))
        self.assertEqual(rows[-1]["repo"], "me/sample")

    def test_local_origin_stays_blank(self):
        # the fixture's origin is a bare directory on disk
        self.write("src/calc.py", "# dirty\n")
        run_cli(["run", "--foreground"])
        rows = read_history(os.path.join(self.home, "history.jsonl"))
        self.assertEqual(rows[-1]["repo"], "")


if __name__ == "__main__":
    unittest.main()
