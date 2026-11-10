"""The baseline runs alongside the first review round (feature/parallel-baseline, M4.2).

The fake runner's baseline can be held until the fake reviewer has started (`block_until`):
if the two did not overlap, the baseline would wait for a reviewer that never starts and
the test fails on its timeout rather than passing by luck.
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

from revali import EXIT_ERROR, EXIT_OK, procs
from revali.state import State, lock_path, read_history
from tests.helpers import (
    RepoCase,
    claude_entry,
    git,
    run_cli,
)

BROKEN = "branch is broken before review"
STOPPED_NOTE = "the reviewer session was stopped when the baseline failed; its cost is not known"
DISCARDED_NOTE = "the reviewer's answer was discarded"
REVIEW_FILE = "tests/test_review_mul.py"


class ParallelCase(RepoCase):
    def setUp(self):
        super().setUp()
        self.started = os.path.join(self.tmp, "reviewer-started")
        self.finished = os.path.join(self.tmp, "reviewer-finished")

    def runner(self, baseline_exit=0, hold=True, extra=None):
        data = {"default": 0, "results": {"baseline": {"test": baseline_exit}}}
        if hold:
            data["block_until"] = {"baseline": self.started}
            data["block_timeout_s"] = 30
        data.update(extra or {})
        self.runner_scenario(data)

    def reviewer(self, sleep=0.0, **kw):
        entry = claude_entry(**kw)
        entry["touch"] = self.started
        entry["done_marker"] = self.finished
        if sleep:
            entry["sleep"] = sleep
        return entry

    def set_validate(self, line):
        path = os.path.join(self.repo, "revali.toml")
        with open(path, "r", encoding="utf-8", newline="") as fh:
            toml = fh.read()
        with open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write(toml.replace("[validate]\n", "[validate]\n%s\n" % line, 1))
        self.commit_all("set %s" % line)

    def calls(self, exe):
        return self.fake_calls(exe)

    def baseline_call(self):
        found = [c for c in self.calls("runner") if c["label"] == "baseline"]
        self.assertEqual(len(found), 1, found)
        return found[0]

    def run_timed(self, argv):
        t0 = time.monotonic()
        code, out = run_cli(argv)
        return code, out, time.monotonic() - t0

    def no_baseline_thread(self):
        alive = [t.name for t in threading.enumerate() if t.name == "revali-baseline"]
        self.assertEqual(alive, [])

    def head(self):
        return git(["rev-parse", "HEAD"], self.repo).strip()


class Overlap(ParallelCase):
    """AC-1, AC-2, AC-3"""

    def test_the_baseline_runs_while_the_reviewer_does(self):
        self.runner(hold=True)
        before = self.head()
        self.claude(self.reviewer())
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        call = self.baseline_call()
        self.assertTrue(call["held"], call)  # it saw the reviewer start before it ended
        self.assertEqual(call["ref"], before)  # AC-2: the commit preflight saw
        state = State.load(self.rdir())
        self.assertEqual(state.baseline_sha, before)  # AC-3
        validate = [c["steps"] for c in self.calls("runner") if c["label"] == "validate-r1"]
        self.assertEqual(len(validate), 1)
        self.assertNotIn("test", validate[0])  # AC-3: the suite the baseline ran is reused
        self.assertIn("new_test", validate[0])
        self.no_baseline_thread()

    def test_the_baseline_starts_before_the_pr_stage(self):
        self.runner(hold=False)
        self.claude(self.reviewer())
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        log = self.read(".revali/feature__mul/logs/revali.log")
        started = log.index("baseline started; it runs alongside the PR stage and the review")
        self.assertLess(started, log.index("opened draft PR"))

    def test_a_reviewer_that_returns_first_waits_for_the_baseline(self):
        # AC-6, passing side: the answer is acted on only once the baseline passed
        self.runner(hold=True, extra={"delay_after_block_s": {"baseline": 1.0}})
        self.claude(self.reviewer())
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("waiting for the baseline", self.read(".revali/feature__mul/logs/revali.log"))
        self.assertTrue(self.exists(REVIEW_FILE))
        self.assertEqual(len(State.load(self.rdir()).rounds), 1)


class BaselineFails(ParallelCase):
    """AC-4, AC-5, AC-6"""

    def assert_broken_run(self, code, out):
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertIn("ERROR: " + BROKEN, out)
        state = State.load(self.rdir())
        self.assertEqual(state.rounds, [])
        self.assertEqual(state.test_commits, [])
        self.assertEqual((state.stage, state.last_exit), ("error", EXIT_ERROR))
        comments = [c for c in self.calls("gh") if "comment" in c.get("argv", [])]
        self.assertEqual(comments, [])
        self.assertFalse(self.exists(REVIEW_FILE))  # the reviewer's file was cleaned up
        self.assertEqual(git(["status", "--porcelain", "--", "tests"], self.repo).strip(), "")
        self.no_baseline_thread()
        return state

    def test_failing_while_the_reviewer_runs_stops_it(self):
        self.runner(baseline_exit=1, hold=True)
        self.claude(self.reviewer(sleep=60))
        code, out, took = self.run_timed(["run", "--foreground"])
        self.assert_broken_run(code, out)
        self.assertIn(STOPPED_NOTE, out)
        self.assertLess(took, 40)  # not the reviewer's 60 s
        self.assertFalse(os.path.exists(self.finished))  # the stub never got to answer

    def test_failing_after_the_reviewer_answered_discards_the_answer(self):
        self.runner(baseline_exit=1, hold=True, extra={"delay_after_block_s": {"baseline": 1.0}})
        entry = self.reviewer(cost=0.42)
        self.claude(entry)
        code, out = run_cli(["run", "--foreground"])
        state = self.assert_broken_run(code, out)
        self.assertIn(DISCARDED_NOTE, out)
        self.assertNotIn(STOPPED_NOTE, out)
        self.assertAlmostEqual(state.cost_usd, 0.42)
        self.assertTrue(os.path.exists(self.finished))

    def test_failing_before_the_reviewer_starts_spawns_none(self):
        from revali import validate

        real_start = validate.BaselineJob.start

        def start_and_finish(ctx, log):
            job = real_start(ctx, log)
            job.finish()  # failed before anything else happens
            return job

        self.runner(baseline_exit=1, hold=False)
        self.claude(self.reviewer())
        with mock.patch.object(validate.BaselineJob, "start", side_effect=start_and_finish):
            code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertIn("ERROR: " + BROKEN, out)
        self.assertEqual(self.calls("claude"), [])
        self.assertNotIn(STOPPED_NOTE, out)
        self.assertNotIn(DISCARDED_NOTE, out)

    def test_a_sandbox_error_counts_as_a_failure(self):
        self.runner_scenario({"default": 0, "errors": {"baseline": "wsl went away"}})
        self.claude(self.reviewer())
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertIn("sandbox failed during baseline: wsl went away", out)
        self.assertEqual(State.load(self.rdir()).rounds, [])
        self.no_baseline_thread()


class OtherStops(ParallelCase):
    """AC-7"""

    def test_a_reviewer_error_waits_for_the_baseline_and_reports_itself(self):
        self.runner(hold=True, extra={"delay_after_block_s": {"baseline": 1.5}})
        entry = self.reviewer()
        entry["is_error"] = True
        self.claude(entry)
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertNotIn(BROKEN, out)
        self.assertIn("reviewer", out.lower())
        self.assertEqual(self.baseline_call()["finished"], True)  # it ran to its end
        self.no_baseline_thread()

    def test_both_failing_reports_the_baseline(self):
        self.runner(baseline_exit=1, hold=True, extra={"delay_after_block_s": {"baseline": 1.0}})
        entry = self.reviewer()
        entry["is_error"] = True
        self.claude(entry)
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertIn("ERROR: " + BROKEN, out)
        self.no_baseline_thread()

    def test_locks_are_released(self):
        self.runner(baseline_exit=1, hold=True)
        self.claude(self.reviewer(sleep=60))
        run_cli(["run", "--foreground"])
        self.assertFalse(os.path.isfile(lock_path(self.rdir())))
        self.assertFalse(os.path.isfile(os.path.join(self.repo, ".revali", "tree.lock")))


class Sequential(ParallelCase):
    """AC-8"""

    def test_parallel_baseline_false_keeps_todays_order(self):
        self.set_validate("parallel_baseline = false")
        self.runner(hold=False)
        self.claude(self.reviewer())
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        call = self.baseline_call()
        self.assertFalse(call["held"])
        with open(self.started, encoding="utf-8") as fh:
            reviewer_started = json.load(fh)
        self.assertLess(call["t1"], reviewer_started)  # baseline ended before
        self.assertNotIn("alongside", self.read(".revali/feature__mul/logs/revali.log"))

    def test_sequential_failure_pushes_nothing_and_spawns_no_reviewer(self):
        self.set_validate("parallel_baseline = false")
        self.runner(baseline_exit=1, hold=False)
        self.claude(self.reviewer())
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertIn(BROKEN, out)
        self.assertEqual(self.calls("claude"), [])
        self.assertEqual([c for c in self.calls("gh") if "create" in c.get("argv", [])], [])

    def test_no_baseline_on_a_dry_run(self):
        self.runner(hold=False)
        code, out = run_cli(["run", "--foreground", "--dry-run"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertEqual([c for c in self.calls("runner") if c["label"] == "baseline"], [])


class HistoryTiming(ParallelCase):
    """AC-9"""

    def test_preflight_no_longer_holds_the_baseline(self):
        self.runner(hold=True, extra={"delay_after_block_s": {"baseline": 1.0}})
        self.claude(self.reviewer())
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        row = read_history(os.path.join(self.home, "history.jsonl"))[-1]
        self.assertIn("baseline", row["sandbox_s"])
        self.assertGreater(row["sandbox_s"]["baseline"], 1.0)
        self.assertLess(row["stage_s"]["preflight"], row["sandbox_s"]["baseline"])


class Docs(unittest.TestCase):
    """AC-10"""

    def read(self, rel):
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(here, rel), "r", encoding="utf-8") as fh:
            return fh.read()

    def test_key_everywhere(self):
        import tomllib

        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(here, "defaults.toml"), "rb") as fh:
            self.assertIs(tomllib.load(fh)["validate"]["parallel_baseline"], True)
        self.assertIn("parallel_baseline", self.read("templates/revali.toml"))
        self.assertIn("parallel_baseline", self.read("docs/configuration.md"))

    def test_side_effects_name_the_push_on_failure(self):
        text = " ".join(self.read("docs/side-effects.md").split())
        self.assertIn("parallel_baseline", text)
        self.assertIn("draft PR", text)

    def test_workflow_and_files(self):
        self.assertIn("parallel_baseline", self.read("docs/workflow.md"))
        files = " ".join(self.read("docs/files.md").split())
        self.assertIn("sandbox_s", files)
        self.assertIn("baseline", files)


class CancellableRun(unittest.TestCase):
    """procs.run(cancel=...): the seam the reviewer session is stopped through."""

    def test_cancel_kills_the_child_and_raises(self):
        cancel = threading.Event()
        threading.Timer(0.5, cancel.set).start()
        t0 = time.monotonic()
        with self.assertRaises(procs.ProcCancelled):
            procs.run([sys.executable, "-c", "import time; time.sleep(60)"], cancel=cancel)
        self.assertLess(time.monotonic() - t0, 20)

    @unittest.skipUnless(os.name == "nt", "POSIX stops the direct child only (see procs.run)")
    def test_the_child_tree_goes_too(self):
        # the grandchild proves its death by never writing `late`; a pid check could be fooled
        # by Windows handing the pid to another process at once
        tmp = tempfile.mkdtemp(prefix="revali cancel ")
        self.addCleanup(shutil.rmtree, tmp, True)
        started, late = os.path.join(tmp, "started"), os.path.join(tmp, "late")
        grandchild = "import sys, time; time.sleep(3); open(sys.argv[1], 'w').close()"
        script = (
            "import subprocess, sys, time\n"
            "subprocess.Popen([sys.executable, '-c', sys.argv[1], sys.argv[3]])\n"
            "open(sys.argv[2], 'w').close()\n"
            "time.sleep(60)\n"
        )
        cancel = threading.Event()

        def fire():
            for _ in range(200):
                if os.path.exists(started):
                    break
                time.sleep(0.05)
            cancel.set()

        threading.Thread(target=fire, daemon=True).start()
        with self.assertRaises(procs.ProcCancelled):
            procs.run([sys.executable, "-c", script, grandchild, started, late], cancel=cancel)
        time.sleep(4)
        self.assertFalse(os.path.exists(late))

    def test_a_descendant_holding_the_pipes_does_not_hang_the_cancel(self):
        # PR #55 round 1 F2: the grandchild inherits stdout / stderr and outlives a child
        # that is stopped (POSIX stops the child only); the cancel must still return. The
        # grandchild ends on its own after 40 s, so nothing is killed by pid here.
        script = (
            "import subprocess, sys, time\n"
            "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(40)'])\n"
            "time.sleep(60)\n"
        )
        cancel = threading.Event()
        threading.Timer(1.0, cancel.set).start()
        t0 = time.monotonic()
        with self.assertRaises(procs.ProcCancelled):
            procs.run([sys.executable, "-c", script], cancel=cancel)
        self.assertLess(time.monotonic() - t0, 25)

    def test_without_cancel_output_and_input_are_as_before(self):
        cancel = threading.Event()
        res = procs.run(
            [sys.executable, "-c", "import sys; print(sys.stdin.read().upper())"],
            input_text="hello",
            cancel=cancel,
        )
        self.assertEqual((res.returncode, res.stdout.strip()), (0, "HELLO"))

    def test_timeout_still_applies(self):
        with self.assertRaises(procs.ProcTimeout):
            procs.run(
                [sys.executable, "-c", "import time; time.sleep(60)"],
                timeout=1,
                cancel=threading.Event(),
            )

    def test_cancel_already_set_starts_nothing_long(self):
        cancel = threading.Event()
        cancel.set()
        t0 = time.monotonic()
        with self.assertRaises(procs.ProcCancelled):
            procs.run([sys.executable, "-c", "import time; time.sleep(60)"], cancel=cancel)
        self.assertLess(time.monotonic() - t0, 20)


if __name__ == "__main__":
    unittest.main()
