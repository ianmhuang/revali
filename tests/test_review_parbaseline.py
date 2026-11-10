"""Reviewer acceptance tests for the parallel baseline (feature/parallel-baseline).

On a first pass the existing-suite baseline starts at the end of preflight and runs while the
PR stage and the round 1 reviewer run (AC-1), on the commit preflight saw (AC-2). A passing
baseline is recorded by the main thread and reused by validation (AC-3). A failing one ends
the run with exit 1 and "branch is broken before review": no reviewer when it failed first
(AC-4), the reviewer killed when it fails mid-session (AC-5), the answer discarded and its
cost kept when the reviewer answered first (AC-6). A run that ends for another reason waits
for the baseline, and when the baseline failed too its failure is reported with the other
reason on the line below (AC-7). Stopping the reviewer takes bounded time even when a
descendant of the session keeps its pipes open (AC-5, as amended after round 1), an orphaned
one included (round 2 F8). `parallel_baseline = false` keeps the old order, and a dry run, a
second pass and a docs change run no baseline (AC-8). The history row keeps the baseline out
of `stage_s.preflight` (AC-9).

The fake runner holds the baseline until a file exists (`block_until`) and the fake reviewer
writes one when it starts and one when it has answered, so "the two overlapped" is observed
through `held` and wall-clock stamps, never through a sleep that happens to be long enough.
"""

import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from revali import EXIT_ACTION, EXIT_ERROR, EXIT_OK, procs
from revali.preflight import Stop
from revali.state import State, read_history
from tests.helpers import RepoCase, approve_response, claude_entry, git, run_cli

BROKEN = "branch is broken before review"
REVIEW_FILE = "tests/test_review_mul.py"
BRANCH = "feature/mul"
LOG = ".revali/feature__mul/logs/revali.log"
RUN = ["run", "--foreground"]


def blocking_finding():
    return {
        "id": "F1",
        "file": "src/calc.py",
        "line": 12,
        "severity": "high",
        "kind": "correctness",
        "text": "mul ignores negative numbers",
        "suggestion": "handle the sign",
    }


class ParallelCase(RepoCase):
    def setUp(self):
        super().setUp()
        self.started = os.path.join(self.tmp, "reviewer-started")
        self.answered = os.path.join(self.tmp, "reviewer-answered")

    def baseline_runner(self, exit_code=0, until="", delay=0.0, timeout=30.0):
        """The fake sandbox: the baseline's `test` step exits `exit_code`; with `until` the
        session is held until that file exists (at most `timeout` s), then `delay` s more."""
        data = {"default": 0, "results": {"baseline": {"test": exit_code}}}
        if until:
            data["block_until"] = {"baseline": until}
            data["block_timeout_s"] = timeout
        if delay:
            data["delay_after_block_s"] = {"baseline": delay}
        self.runner_scenario(data)

    def reviewer(self, data=None, sleep=0.0, **kw):
        """A fake reviewer answer that stamps `started` when the session begins and writes
        `answered` just before it prints its answer (`sleep` seconds in between)."""
        entry = claude_entry(data, **kw)
        entry["touch"] = self.started
        entry["done_marker"] = self.answered
        if sleep:
            entry["sleep"] = sleep
        return entry

    def set_parallel(self, value):
        toml = self.read("revali.toml")
        self.assertIn("[validate]\n", toml)
        line = "parallel_baseline = %s\n" % ("true" if value else "false")
        self.write("revali.toml", toml.replace("[validate]\n", "[validate]\n" + line, 1))
        self.commit_all("set parallel_baseline")

    def runner_calls(self, label):
        try:
            calls = self.fake_calls("runner")
        except ValueError:  # a line the baseline thread is still writing
            return []
        return [c for c in calls if c["label"] == label]

    def baseline_call(self):
        calls = self.runner_calls("baseline")
        self.assertEqual(len(calls), 1, calls)
        return calls[0]

    def reviewer_started_at(self):
        with open(self.started, "r", encoding="utf-8") as fh:
            return float(json.load(fh))

    def gh_calls(self, *prefix):
        return [
            c for c in self.fake_calls("gh") if c.get("argv", [])[: len(prefix)] == list(prefix)
        ]

    def head(self):
        return git(["rev-parse", "HEAD"], self.repo).strip()

    def remote_head(self):
        out = git(["ls-remote", "--heads", "origin", BRANCH], self.repo).strip()
        return out.split()[0] if out else ""

    def history_row(self):
        rows = read_history(os.path.join(self.home, "history.jsonl"))
        self.assertTrue(rows, "no history row")
        return rows[-1]

    def assert_ended_broken(self, code, out, head_before):
        """The run ended the way a failed baseline always did, and left nothing of a round:
        no round record, no test commit, no push of one, no review comment, a clean tree."""
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertIn("ERROR: " + BROKEN, out)
        state = State.load(self.rdir())
        self.assertEqual(state.rounds, [])
        self.assertEqual(state.test_commits, [])
        self.assertEqual((state.stage, state.last_exit), ("error", EXIT_ERROR))
        self.assertEqual(self.gh_calls("pr", "comment"), [])
        self.assertEqual(self.head(), head_before)  # no test commit
        self.assertIn(self.remote_head(), ("", head_before))  # nothing newer was pushed
        self.assertFalse(self.exists(REVIEW_FILE))  # the reviewer's file is gone
        self.assertEqual(git(["status", "--porcelain"], self.repo).strip(), "")
        row = self.history_row()
        self.assertEqual((row["exit"], row["rounds"]), (EXIT_ERROR, 0))
        return state


class BaselineOverlapsTheReview(ParallelCase):
    """AC-1, AC-2, AC-3, AC-6 (the passing side)"""

    def test_a_slow_baseline_and_a_slow_reviewer_run_side_by_side(self):
        # the baseline can only end 2 s after the reviewer session began, and the reviewer
        # takes 2 s itself: run one after the other they could not overlap at all
        self.baseline_runner(until=self.started, delay=2.0)
        self.claude(self.reviewer(sleep=2.0))
        head_before = self.head()
        code, out = run_cli(RUN)
        self.assertEqual(code, EXIT_OK, out)
        call = self.baseline_call()
        self.assertTrue(call["held"], call)  # AC-1: the reviewer started while it ran
        self.assertEqual(call["ref"], head_before)  # AC-2: the commit preflight saw, by sha
        base_t0, base_t1 = call["t0"], call["t1"]
        rev_t0, rev_t1 = self.reviewer_started_at(), os.path.getmtime(self.answered)
        self.assertLess(base_t0, rev_t0)  # AC-1: started before the reviewer session
        self.assertLess(rev_t0, base_t1)  # AC-1: still running while the reviewer ran
        together = min(base_t1, rev_t1) - max(base_t0, rev_t0)
        self.assertGreater(together, 1.5, (call, rev_t0, rev_t1))
        # AC-1: the two took about as long as the longer one, not their sum
        span = max(base_t1, rev_t1) - min(base_t0, rev_t0)
        total = (base_t1 - base_t0) + (rev_t1 - rev_t0)
        self.assertLess(span, total - 1.5, (call, rev_t0, rev_t1))

    def test_the_baseline_starts_before_the_pr_stage_and_outlasts_it(self):
        self.baseline_runner(until=self.started)
        self.claude(self.reviewer())
        code, out = run_cli(RUN)
        self.assertEqual(code, EXIT_OK, out)
        self.assertTrue(self.baseline_call()["held"])
        lines = self.read(LOG).splitlines()
        first_baseline = min(i for i, ln in enumerate(lines) if "baseline" in ln)
        first_pr = min(i for i, ln in enumerate(lines) if "] pr: " in ln)
        self.assertLess(first_baseline, first_pr, "\n".join(lines))  # AC-1: before the PR stage
        # the fake runner logs a session when it ends, gh when it is called: the draft PR
        # was created while the baseline was still running
        calls = self.fake_calls()
        created = min(i for i, c in enumerate(calls) if c.get("argv", [])[:2] == ["pr", "create"])
        ended = min(i for i, c in enumerate(calls) if c.get("label") == "baseline")
        self.assertLess(created, ended, calls)

    def test_a_passing_baseline_is_recorded_by_the_main_thread_and_reused(self):
        savers = []
        real_save = State.save

        def save(state, rdir):
            savers.append(threading.current_thread())
            return real_save(state, rdir)

        self.baseline_runner(until=self.started)
        self.claude(self.reviewer())
        head_before = self.head()
        with mock.patch.object(State, "save", save):
            code, out = run_cli(RUN)
        self.assertEqual(code, EXIT_OK, out)
        self.assertTrue(self.baseline_call()["held"])  # it did run alongside the reviewer
        state = State.load(self.rdir())
        self.assertEqual(state.baseline_sha, head_before)  # AC-3: that commit
        self.assertTrue(savers)
        # AC-3: every state write came from the thread that runs the pipeline
        self.assertEqual(set(savers), {threading.current_thread()})
        validation = self.runner_calls("validate-r1")
        self.assertEqual(len(validation), 1, validation)
        self.assertNotIn("test", validation[0]["steps"])  # AC-3: reused exactly as before
        self.assertIn("new_test", validation[0]["steps"])
        self.assertEqual(len(state.rounds), 1)

    def test_an_answer_that_came_first_is_acted_on_only_after_the_baseline_passed(self):
        # AC-6: the baseline cannot end before the reviewer has answered, plus 1 s
        self.baseline_runner(until=self.answered, delay=1.0)
        self.claude(self.reviewer())
        code, out = run_cli(RUN)
        self.assertEqual(code, EXIT_OK, out)
        baseline = self.baseline_call()
        self.assertTrue(baseline["held"], baseline)  # the reviewer had answered by then
        smoke = self.runner_calls("smoke-r1-1")
        self.assertEqual(len(smoke), 1, smoke)
        self.assertGreaterEqual(smoke[0]["t0"], baseline["t1"])  # smoke run only afterwards
        state = State.load(self.rdir())
        self.assertEqual(len(state.rounds), 1)
        self.assertEqual(len(state.test_commits), 1)
        self.assertTrue(self.gh_calls("pr", "comment"))  # the review comment was posted
        self.assertTrue(self.exists(REVIEW_FILE))


class BaselineFails(ParallelCase):
    """AC-4, AC-5, AC-6 (the failing side)"""

    def test_failed_before_the_review_stage_no_reviewer_is_started(self):
        from revali import pr as prstage

        real_ensure_pr = prstage.ensure_pr
        seen = []

        def ensure_pr_then_let_the_baseline_fail(*args, **kw):
            real_ensure_pr(*args, **kw)
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline and not self.runner_calls("baseline"):
                time.sleep(0.05)
            seen.append(len(self.runner_calls("baseline")))
            time.sleep(0.5)  # the session has ended; its failure is known well before the review

        self.baseline_runner(exit_code=1)
        self.claude(self.reviewer())
        head_before = self.head()
        with mock.patch("revali.pr.ensure_pr", ensure_pr_then_let_the_baseline_fail):
            code, out = run_cli(RUN)
        # the run was past preflight when the baseline failed: that is the parallel order
        self.assertEqual(seen, [1], out)
        self.assert_ended_broken(code, out, head_before)
        self.assertEqual(self.fake_calls("claude"), [])  # AC-4: no reviewer session
        self.assertFalse(os.path.exists(self.started))

    def test_failing_while_the_reviewer_runs_kills_the_session(self):
        reviewer_s = 10.0
        self.baseline_runner(exit_code=1, until=self.started)
        self.claude(self.reviewer(sleep=reviewer_s))
        head_before = self.head()
        code, out = run_cli(RUN)
        returned = time.time()
        state = self.assert_ended_broken(code, out, head_before)
        self.assertTrue(self.baseline_call()["held"])  # it failed with the reviewer running
        started = self.reviewer_started_at()
        # AC-5: stopped within a few seconds, not after the session's own 10 s
        self.assertLess(returned - started, 7.0, out)
        note = [
            ln
            for ln in out.splitlines()
            if "reviewer" in ln and "stopped" in ln and "cost" in ln and "not known" in ln
        ]
        self.assertTrue(note, out)  # AC-5: one line says what became of the reviewer
        self.assertEqual(state.cost_usd, 0)  # nothing reported, nothing counted
        # the process is gone, not just ignored: it never gets to write its answer marker
        time.sleep(max(0.0, started + reviewer_s + 1.5 - time.time()))
        self.assertFalse(os.path.exists(self.answered))

    def test_failing_after_the_reviewer_answered_discards_the_answer_and_keeps_its_cost(self):
        self.baseline_runner(exit_code=1, until=self.answered, delay=1.0)
        self.claude(self.reviewer(cost=0.42))
        head_before = self.head()
        code, out = run_cli(RUN)
        state = self.assert_ended_broken(code, out, head_before)
        self.assertTrue(self.baseline_call()["held"])  # the answer was there first
        self.assertTrue(os.path.exists(self.answered))
        self.assertEqual(len(self.fake_calls("claude")), 1)
        self.assertAlmostEqual(state.cost_usd, 0.42)  # AC-6: the reported cost is counted
        self.assertEqual(self.runner_calls("smoke-r1-1"), [])  # AC-6: the answer was not used


class TheRunEndsForAnotherReason(ParallelCase):
    """AC-7"""

    def test_a_reviewer_error_waits_for_the_baseline_and_is_the_reported_reason(self):
        self.baseline_runner(until=self.answered, delay=1.5)
        self.claude(self.reviewer(is_error=True))
        code, out = run_cli(RUN)
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertNotIn(BROKEN, out)
        errors = [ln for ln in out.splitlines() if ln.startswith("ERROR: ")]
        self.assertTrue(errors, out)
        self.assertIn("reviewer", errors[0])  # the original reason, as before
        call = self.baseline_call()  # logged when the session ends: it had ended by exit
        self.assertTrue(call["held"], call)  # and it was running when the reviewer failed
        self.assertEqual(State.load(self.rdir()).rounds, [])

    def test_a_failing_pr_stage_waits_for_the_baseline_and_is_the_reported_reason(self):
        pushed = os.path.join(self.tmp, "pr-stage-failed")

        def failing_pr_stage(*args, **kw):
            with open(pushed, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(time.time()))
            raise Stop(EXIT_ERROR, "git push failed: the remote said no")

        self.baseline_runner(until=pushed, delay=1.0)
        self.claude(self.reviewer())
        with mock.patch("revali.pr.ensure_pr", failing_pr_stage):
            code, out = run_cli(RUN)
        returned = time.time()
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertIn("ERROR: git push failed: the remote said no", out)
        self.assertNotIn(BROKEN, out)
        call = self.baseline_call()  # present: the session ended before the run did
        self.assertTrue(call["held"], call)  # it was still running when the PR stage failed
        with open(pushed, "r", encoding="utf-8") as fh:
            failed_at = float(json.load(fh))
        self.assertGreaterEqual(returned - failed_at, 0.9)  # the run waited for it
        self.assertEqual(self.fake_calls("claude"), [])

    def line_below_the_baseline_failure(self, out):
        lines = out.splitlines()
        at = [i for i, ln in enumerate(lines) if ln.startswith("ERROR: " + BROKEN)]
        self.assertEqual(len(at), 1, out)  # the baseline's failure is the reported one
        self.assertLess(at[0] + 1, len(lines), out)
        return lines[at[0] + 1]

    def test_a_failing_pr_stage_and_a_failing_baseline_report_the_baseline_first(self):
        # AC-7 as amended: the old order would have stopped at the baseline, so that is the
        # reported failure (exit 1), and the PR stage's own reason is on the line below it
        pushed = os.path.join(self.tmp, "pr-stage-failed")

        def failing_pr_stage(*args, **kw):
            with open(pushed, "w", encoding="utf-8") as fh:
                fh.write("failed\n")
            raise Stop(EXIT_ERROR, "git push failed: the remote said no")

        # the baseline can only fail 1 s after the PR stage has
        self.baseline_runner(exit_code=1, until=pushed, delay=1.0)
        self.claude(self.reviewer())
        with mock.patch("revali.pr.ensure_pr", failing_pr_stage):
            code, out = run_cli(RUN)
        self.assertEqual(code, EXIT_ERROR, out)
        below = self.line_below_the_baseline_failure(out)
        self.assertIn("git push failed: the remote said no", below)
        self.assertFalse([ln for ln in out.splitlines() if ln.startswith("ERROR: git push")], out)
        call = self.baseline_call()  # the run waited for the session to end
        self.assertTrue(call["held"], call)  # which was running when the PR stage failed
        self.assertEqual(self.fake_calls("claude"), [])
        state = State.load(self.rdir())
        self.assertEqual((state.stage, state.last_exit), ("error", EXIT_ERROR))
        self.assertTrue(state.message.startswith(BROKEN), state.message)
        self.assertEqual(state.rounds, [])

    def test_a_reviewer_error_and_a_failing_baseline_report_the_baseline_first(self):
        # the baseline can only fail 1 s after the reviewer session has ended in an error
        self.baseline_runner(exit_code=1, until=self.answered, delay=1.0)
        self.claude(self.reviewer(is_error=True))
        head_before = self.head()
        code, out = run_cli(RUN)
        self.assert_ended_broken(code, out, head_before)
        self.assertTrue(self.baseline_call()["held"])  # the reviewer had failed by then
        below = self.line_below_the_baseline_failure(out)
        self.assertIn("reviewer", below)  # AC-7: the other reason, under the baseline's
        self.assertNotIn("not known", below)  # it ended by itself, nothing was stopped


# A stand-in for a reviewer session that has started a helper process: the helper inherits the
# session's stdout / stderr and stays alive after the session is stopped. It ends when the
# test lets it go (the `release` file, or its directory disappearing), at the latest after 45 s.
DESCENDANT = (
    "import os, sys, time\n"
    "up, release = sys.argv[1], sys.argv[2]\n"
    "open(up, 'w').close()\n"
    "end = time.time() + 45\n"
    "while time.time() < end and os.path.isdir(os.path.dirname(up)):\n"
    "    if os.path.exists(release):\n"
    "        break\n"
    "    time.sleep(0.1)\n"
)
SESSION = (
    "import subprocess, sys, time\n"
    "subprocess.Popen([sys.executable, '-c', sys.argv[1], sys.argv[2], sys.argv[3]])\n"
    "time.sleep(60)\n"
)
# The same, with the helper started through a process that exits at once: the helper is then
# an orphan, which a kill of the session's process tree (Windows) does not reach either. The
# session writes `orphaned` once that process in between is gone.
MIDDLE = (
    "import subprocess, sys\n"
    "subprocess.Popen([sys.executable, '-c', sys.argv[1], sys.argv[2], sys.argv[3]])\n"
)
ORPHANING_SESSION = (
    "import subprocess, sys, time\n"
    "subprocess.run([sys.executable, '-c', sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]])\n"
    "open(sys.argv[5], 'w').close()\n"
    "time.sleep(60)\n"
)


class StoppingTheSessionIsBounded(unittest.TestCase):
    """AC-5 (as amended): a stopped session whose descendant holds its pipes"""

    def let_go(self, release):
        if os.path.isdir(os.path.dirname(release)):
            with open(release, "w", encoding="utf-8") as fh:
                fh.write("go\n")

    def stop_took(self, orphan):
        """Seconds between "the baseline failed" and the cancelled session's `run` returning,
        with a descendant of the session alive and holding its pipes throughout."""
        tmp = tempfile.mkdtemp(prefix="revali-cancel-")
        self.addCleanup(shutil.rmtree, tmp, True)
        up = os.path.join(tmp, "descendant-up")
        release = os.path.join(tmp, "release")
        orphaned = os.path.join(tmp, "orphaned")
        self.addCleanup(self.let_go, release)  # runs before the rmtree above
        if orphan:
            cmd = [sys.executable, "-c", ORPHANING_SESSION, MIDDLE, DESCENDANT, up, release]
            cmd.append(orphaned)
            markers = [up, orphaned]
        else:
            cmd = [sys.executable, "-c", SESSION, DESCENDANT, up, release]
            markers = [up]
        cancel = threading.Event()
        fired = []

        def fire():  # "the baseline failed", once the descendant exists
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline and not all(os.path.exists(m) for m in markers):
                time.sleep(0.05)
            fired.append(time.monotonic())
            cancel.set()

        threading.Thread(target=fire, daemon=True).start()
        with self.assertRaises(procs.ProcCancelled):
            procs.run(cmd, cancel=cancel)
        returned = time.monotonic()
        for marker in markers:
            self.assertTrue(os.path.exists(marker), "never happened: %s" % marker)
        return returned - fired[0]

    def test_a_descendant_holding_the_pipes_delays_the_stop_by_seconds_not_its_lifetime(self):
        # AC-5: the pipes are given up after at most about 15 s; the descendant lives 45 s
        self.assertLess(self.stop_took(orphan=False), 20.0)

    def test_an_orphaned_descendant_holding_the_pipes_does_not_delay_it_longer(self):
        # PR #55 round 2 F8: no kill of the session's tree reaches this descendant on either
        # platform, so the stop has to return without the pipes ever closing
        self.assertLess(self.stop_took(orphan=True), 20.0)


class TheOldOrder(ParallelCase):
    """AC-8"""

    def test_false_ends_the_baseline_before_the_pr_stage_and_the_reviewer(self):
        self.set_parallel(False)
        # nothing can release the hold inside preflight: the session gives up after 2 s
        self.baseline_runner(until=self.started, timeout=2.0)
        self.claude(self.reviewer())
        code, out = run_cli(RUN)
        self.assertEqual(code, EXIT_OK, out)
        call = self.baseline_call()
        self.assertFalse(call["held"], call)  # no reviewer ran while the baseline did
        self.assertLess(call["t1"], self.reviewer_started_at())
        calls = self.fake_calls()
        ended = min(i for i, c in enumerate(calls) if c.get("label") == "baseline")
        created = min(i for i, c in enumerate(calls) if c.get("argv", [])[:2] == ["pr", "create"])
        self.assertLess(ended, created, calls)  # over before anything was pushed

    def test_false_and_a_failing_baseline_pushes_nothing_and_starts_no_reviewer(self):
        self.set_parallel(False)
        self.baseline_runner(exit_code=1)
        self.claude(self.reviewer())
        code, out = run_cli(RUN)
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertIn("ERROR: " + BROKEN, out)
        self.assertEqual(self.fake_calls("claude"), [])
        self.assertEqual(self.gh_calls("pr", "create"), [])
        self.assertEqual(self.remote_head(), "")  # the branch never reached the remote

    def test_a_dry_run_runs_no_baseline_with_the_key_on(self):
        self.set_parallel(True)
        self.baseline_runner()
        code, out = run_cli(RUN + ["--dry-run"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertEqual(self.runner_calls("baseline"), [])

    def test_a_docs_change_runs_no_baseline_with_the_key_on(self):
        self.set_parallel(True)
        change = self.read(".revali/feature__mul/change.md")
        self.write(".revali/feature__mul/change.md", change.replace("kind: feature", "kind: docs"))
        data = approve_response(
            tests=[],
            not_testable=[
                {"ac": "AC-1", "reason": "kind docs"},
                {"ac": "AC-2", "reason": "kind docs"},
            ],
        )
        self.baseline_runner()
        self.claude(self.reviewer(data, write_tests=False))
        code, out = run_cli(RUN)
        self.assertEqual(code, EXIT_OK, out)
        self.assertEqual(self.runner_calls("baseline"), [])

    def test_a_second_pass_runs_no_baseline(self):
        changes = approve_response(verdict="CHANGES_REQUESTED", findings=[blocking_finding()])
        fixed = approve_response(
            previous_findings=[{"id": "F1", "status": "resolved", "note": "fixed"}]
        )
        self.baseline_runner(until=self.started)
        self.claude(self.reviewer(changes), claude_entry(fixed, write_tests=False))
        code, out = run_cli(RUN)
        self.assertEqual(code, EXIT_ACTION, out)
        self.assertTrue(self.baseline_call()["held"])  # the first pass ran it in parallel
        self.write("src/calc.py", self.read("src/calc.py") + "\n# handles negatives\n")
        self.commit_all("fix negatives")
        self.write(".revali/feature__mul/response-1.md", "- F1: fixed in the last commit\n")
        code, out = run_cli(RUN)
        self.assertEqual(len(self.fake_calls("claude")), 2, out)  # the second review ran
        self.assertEqual(len(self.runner_calls("baseline")), 1, out)  # and no second baseline


class HistoryRow(ParallelCase):
    """AC-9"""

    def test_the_baseline_is_in_sandbox_s_and_no_longer_in_preflight(self):
        # the baseline ends 2 s after the reviewer answered: all of that wait is review time
        self.baseline_runner(until=self.answered, delay=2.0)
        self.claude(self.reviewer())
        code, out = run_cli(RUN)
        self.assertEqual(code, EXIT_OK, out)
        row = self.history_row()
        stage, sandbox = row["stage_s"], row["sandbox_s"]
        self.assertGreaterEqual(sandbox["baseline"], 1.9, row)  # still recorded, in full
        self.assertLess(stage["preflight"], sandbox["baseline"], row)  # not inside preflight
        self.assertGreaterEqual(sandbox["baseline"], stage["pr"], row)  # it spans the PR stage
        self.assertGreaterEqual(stage["review"], 1.9, row)  # the wait counts in review


if __name__ == "__main__":
    unittest.main()
