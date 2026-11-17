"""Acceptance tests for fix/state-file-tolerance, the reading half (AC-1, AC-2): a state.json
that starts with a UTF-8 byte order mark is read by `run`, `wait`, `status`, `merge`, `reset`
and `stop` like the same file without it, and the next save drops the mark; `revali status`
says which repository and branch it is about before it reports a state it cannot use.
Black box through the CLI on the fixture repository."""

import json
import os
from unittest import mock

from revali import EXIT_ACTION, EXIT_ERROR, EXIT_OK
from revali.state import State
from tests.helpers import RepoCase, approve_response, claude_entry, run_cli

BOM = b"\xef\xbb\xbf"
PENDING = "tests/test_review_mul.py"
DEAD_PID = 999999999


class StateBytesCase(RepoCase):
    def setUp(self):
        super().setUp()
        os.environ["REVALI_POLL_SECONDS"] = "0.01"

    def raw(self):
        with open(State.path(self.rdir()), "rb") as fh:
            return fh.read()

    def put_raw(self, data):
        os.makedirs(self.rdir(), exist_ok=True)
        with open(State.path(self.rdir()), "wb") as fh:
            fh.write(data)

    def on_disk(self):
        """The file as strict UTF-8 JSON; a leading byte order mark does not parse here."""
        return json.loads(self.raw().decode("utf-8"))

    def mark(self, **changes):
        """Save the state file again the way an editor that adds the mark does."""
        raw = self.raw()
        self.assertFalse(raw.startswith(BOM), "revali itself wrote a byte order mark")
        data = json.loads(raw.decode("utf-8"))
        data.update(changes)
        self.put_raw(BOM + json.dumps(data, indent=2).encode("utf-8") + b"\n")
        self.assertTrue(self.raw().startswith(BOM))

    def ready(self):
        self.claude(claude_entry())
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)

    def needs_info_round(self):
        data = approve_response(verdict="NEEDS_INFO", questions=["Which integers?"], tests=[])
        self.claude(claude_entry(data))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ACTION, out)
        self.assertEqual(self.on_disk()["pending_test_files"], [PENDING])
        self.assertTrue(self.exists(PENDING))


class EveryCommandReadsIt(StateBytesCase):
    """AC-1"""

    def test_status_and_wait_report_the_recorded_result(self):
        self.ready()
        self.mark()
        code, out = run_cli(["status"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("stage: ready_to_merge", out)
        self.assertNotIn("ERROR:", out)
        code, out = run_cli(["wait", "--timeout", "1s"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("ready_to_merge", out)
        self.assertNotIn("ERROR:", out)

    def test_a_dry_run_reads_it_and_its_save_drops_the_mark(self):
        self.ready()
        before = self.on_disk()
        self.mark()
        code, out = run_cli(["run", "--dry-run"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertFalse(self.raw().startswith(BOM))
        after = self.on_disk()
        # read, not started over: what the ready run recorded is still in the file
        self.assertEqual(len(after["rounds"]), 1)
        self.assertEqual(after["pr_number"], before["pr_number"])

    def test_a_detached_run_starts(self):
        self.ready()
        self.mark()
        with mock.patch("revali.pipeline.spawn_detached", return_value=DEAD_PID) as spawn:
            code, out = run_cli(["run"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertNotIn("ERROR:", out)
        spawn.assert_called_once()

    def test_merge_reads_it(self):
        self.ready()
        self.mark()
        code, out = run_cli(["merge"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("MERGED", out)

    def test_reset_reads_it_and_cleans_up_the_pending_file(self):
        self.needs_info_round()
        self.mark()
        code, out = run_cli(["reset"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertNotIn("not cleaned up", out)
        self.assertFalse(self.exists(PENDING))
        self.assertFalse(os.path.exists(State.path(self.rdir())))

    def test_stop_closes_a_dead_run_and_its_save_drops_the_mark(self):
        self.ready()
        self.mark(stage="validate", last_exit=-1)  # a run that died without a result
        code, out = run_cli(["stop"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertNotIn("ERROR:", out)
        self.assertFalse(self.raw().startswith(BOM))
        self.assertEqual(self.on_disk()["stage"], "stopped")
        code, out = run_cli(["status"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("stage: stopped", out)


class StatusNamesTheBranchFirst(StateBytesCase):
    """AC-2"""

    def test_the_identity_line_comes_before_the_state_error(self):
        cases = {
            '{"stage": "review", "fixes": "1"}': "fixes",
            '{"stage": "review",': "",
            "[1, 2]": "",
        }
        for text, field in cases.items():
            with self.subTest(state=text):
                self.put_raw(text.encode("utf-8"))
                code, out = run_cli(["status"])
                self.assertEqual(code, EXIT_ERROR, out)
                self.assertNotIn("Traceback", out)
                lines = out.splitlines()
                self.assertTrue(lines, out)
                self.assertTrue(lines[0].startswith("repo: "), out)
                self.assertIn("branch: feature/mul", lines[0])
                errors = [n for n, line in enumerate(lines) if line.startswith("ERROR: ")]
                self.assertEqual(len(errors), 1, out)
                self.assertGreater(errors[0], 0, out)
                self.assertIn("state.json", lines[errors[0]])
                self.assertIn(field, lines[errors[0]])
                self.assertEqual(self.raw(), text.encode("utf-8"))  # reported, not repaired
