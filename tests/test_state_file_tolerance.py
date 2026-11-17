"""fix/state-file-tolerance (M3.14): a state file with a UTF-8 BOM reads like one without,
`status` names the branch before a state error, and `reset` cleans up from the fields of a
broken state that it can read."""

import json
import os

from revali import EXIT_ERROR, EXIT_OK
from revali.state import State
from tests.helpers import claude_entry, run_cli
from tests.test_reset_pending import PENDING, PendingCase

BOM = b"\xef\xbb\xbf"


class StateFileCase(PendingCase):
    def raw(self):
        with open(State.path(self.rdir()), "rb") as fh:
            return fh.read()

    def put_raw(self, data: bytes):
        with open(State.path(self.rdir()), "wb") as fh:
            fh.write(data)

    def with_bom(self):
        raw = self.raw()
        self.assertFalse(raw.startswith(BOM))
        self.put_raw(BOM + raw)

    def edit(self, **fields):
        data = json.loads(self.raw().decode("utf-8"))
        data.update(fields)
        self.put_raw(json.dumps(data).encode("utf-8"))


class Bom(StateFileCase):
    """AC-1"""

    def ready(self):
        self.claude(claude_entry())
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)

    def test_status_wait_and_stop_read_it(self):
        self.ready()
        self.with_bom()
        code, out = run_cli(["status"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("stage: ready_to_merge", out)
        code, out = run_cli(["wait", "--timeout", "1s"])
        self.assertEqual(code, EXIT_OK, out)
        code, out = run_cli(["stop"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("no run in progress", out)
        self.assertNotIn("ERROR", out)

    def test_a_run_reads_it_and_saves_it_without(self):
        self.ready()
        self.with_bom()
        code, out = run_cli(["run", "--dry-run"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertFalse(self.raw().startswith(BOM))
        self.assertEqual(State.load(self.rdir()).validated_sha, State.load(self.rdir()).head_sha)

    def test_merge_reads_it(self):
        self.ready()
        os.environ["REVALI_POLL_SECONDS"] = "0.01"
        self.with_bom()
        code, out = run_cli(["merge"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("MERGED: PR #7 into main", out)

    def test_reset_reads_it_and_cleans_up(self):
        self.needs_info_round()
        self.with_bom()
        code, out = run_cli(["reset"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertNotIn("could not be read", out)
        self.assertFalse(self.exists(PENDING))
        self.assertFalse(os.path.exists(State.path(self.rdir())))


class StatusNamesTheBranch(StateFileCase):
    """AC-2"""

    def test_identity_comes_before_the_error(self):
        self.needs_info_round()
        self.edit(fixes="1")
        code, out = run_cli(["status"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertIn("branch: feature/mul", out)
        self.assertIn("ERROR: ", out)
        self.assertLess(out.index("branch: feature/mul"), out.index("ERROR: "))
        self.assertIn("'fixes'", out)


class ResetSalvages(StateFileCase):
    """AC-3, AC-4"""

    def test_a_bad_field_elsewhere_still_cleans_up_the_pending_files(self):
        self.needs_info_round()
        self.edit(fixes="1", pr_number=True)
        code, out = run_cli(["reset"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("skipped field(s): fixes, pr_number", out)
        self.assertIn(PENDING, out)
        self.assertFalse(self.exists(PENDING))
        self.assertEqual(self.status_of_tests(), "")
        self.assertIn("state removed", out)

    def test_an_interrupted_round_is_swept_too(self):
        self.needs_info_round()
        self.edit(fixes="1", reviewer_running=True, pending_test_files=[])
        code, out = run_cli(["reset"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("skipped field(s): fixes", out)
        self.assertFalse(self.exists(PENDING))  # matches test_file_pattern, untracked

    def test_a_bad_field_the_cleanup_needs_leaves_the_files(self):
        self.needs_info_round()
        self.edit(pending_test_files=[PENDING, None])
        code, out = run_cli(["reset"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("pending_test_files are not usable", out)
        self.assertIn("not cleaned up", out)
        self.assertTrue(self.exists(PENDING))
        self.assertFalse(os.path.exists(State.path(self.rdir())))

    def test_a_file_that_is_not_json_leaves_the_files(self):
        self.needs_info_round()
        self.put_raw(b'{"pending_test_files": ["%s"],' % PENDING.encode())
        code, out = run_cli(["reset"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("could not be read", out)
        self.assertIn("not cleaned up", out)
        self.assertTrue(self.exists(PENDING))
        self.assertFalse(os.path.exists(State.path(self.rdir())))


class OthersStillRefuse(StateFileCase):
    """AC-5"""

    def test_run_wait_and_merge_refuse_a_bad_field(self):
        self.needs_info_round()
        self.edit(fixes="1")
        for argv in (["run", "--foreground"], ["wait", "--timeout", "1s"], ["merge"]):
            with self.subTest(argv=argv):
                code, out = run_cli(argv)
                self.assertEqual(code, EXIT_ERROR, out)
                self.assertIn("ERROR: ", out)
                self.assertIn("'fixes'", out)
        self.assertTrue(os.path.isfile(State.path(self.rdir())))

    def test_the_page_describes_both(self):
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(here, "docs", "files.md"), encoding="utf-8") as fh:
            text = " ".join(fh.read().split())
        self.assertIn("byte order mark", text)
        self.assertIn("reads the fields that are well formed", text)
