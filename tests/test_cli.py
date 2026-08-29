import os
import time
import unittest

from tests.helpers import RepoCase, run_cli
from revali import EXIT_ERROR, EXIT_OK, VERSION
from revali.state import State, review_dir, lock_owner_alive


class CliTests(RepoCase):
    def rdir(self):
        return review_dir(self.repo, "feature/mul")

    def test_version(self):
        code, out = run_cli(["version"])
        self.assertEqual(code, EXIT_OK)
        self.assertIn(VERSION, out)

    def test_run_foreground_stops_after_preflight(self):
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR)
        self.assertIn("not implemented", out)
        state = State.load(self.rdir())
        self.assertEqual(state.stage, "error")
        self.assertEqual(state.branch, "feature/mul")
        self.assertTrue(state.head_sha)
        self.assertIsNone(lock_owner_alive(self.rdir()))
        self.assertTrue(os.path.isfile(os.path.join(self.rdir(), "logs", "revali.log")))

    def test_run_foreground_records_stop(self):
        self.write("src/calc.py", "# dirty\n")
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR)
        self.assertEqual(State.load(self.rdir()).stage, "error")
        self.assertIn("not clean", State.load(self.rdir()).message)

    def test_status_without_state(self):
        code, out = run_cli(["status"])
        self.assertEqual(code, EXIT_OK)
        self.assertIn("state: none", out)

    def test_status_after_run_and_stale_dirs(self):
        run_cli(["run", "--foreground"])
        os.makedirs(os.path.join(self.repo, ".revali", "gone__branch"))
        code, out = run_cli(["status"])
        self.assertIn("stage: error", out)
        self.assertIn("gone__branch", out)

    def test_reset_and_clean(self):
        run_cli(["run", "--foreground"])
        code, out = run_cli(["reset"])
        self.assertEqual(code, EXIT_OK)
        self.assertIsNone(State.load(self.rdir()))
        self.assertTrue(os.path.isfile(self.change_md()))
        code, out = run_cli(["clean", "feature/mul"])
        self.assertEqual(code, EXIT_OK)
        self.assertFalse(os.path.isdir(self.rdir()))
        code, out = run_cli(["clean", "feature/mul"])
        self.assertEqual(code, EXIT_ERROR)

    def test_wait_without_run(self):
        code, out = run_cli(["wait", "--timeout", "1s"])
        self.assertEqual(code, EXIT_ERROR)
        self.assertIn("no revali run", out)

    def test_detached_run_then_wait(self):
        code, out = run_cli(["run"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("started revali run", out)
        code, out = run_cli(["wait", "--timeout", "90s"])
        self.assertEqual(code, EXIT_ERROR, out)   # v1 milestone 1: stops after preflight
        self.assertIn("not implemented", out)
        self.assertIsNone(lock_owner_alive(self.rdir()))
        self.assertTrue(os.path.isfile(os.path.join(self.rdir(), "logs", "run.log")))

    def test_second_run_refused_while_running(self):
        code, out = run_cli(["run"])
        self.assertEqual(code, EXIT_OK, out)
        code2, out2 = run_cli(["run"])
        # Either the child already finished (fast machine) or the lock refuses us.
        if lock_owner_alive(self.rdir()):
            self.assertEqual(code2, EXIT_ERROR)
            self.assertIn("already in progress", out2)
        run_cli(["wait", "--timeout", "90s"])

    def test_stop_without_run(self):
        code, out = run_cli(["stop"])
        self.assertEqual(code, EXIT_OK)
        self.assertIn("no run in progress", out)

    def test_parse_duration(self):
        from revali.pipeline import parse_duration
        self.assertEqual(parse_duration("9m"), 540)
        self.assertEqual(parse_duration("30s"), 30)
        self.assertEqual(parse_duration("1h"), 3600)
        self.assertEqual(parse_duration("15"), 15)


if __name__ == "__main__":
    unittest.main()
