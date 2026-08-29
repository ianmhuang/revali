import os
import tempfile
import unittest

from tests.helpers import ROOT  # noqa: F401
from revali.state import (LockHeld, State, acquire_lock, append_history, lock_owner_alive,
                          read_history, release_lock, safe_branch, write_json_atomic)


class StateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="revali state ")

    def test_safe_branch(self):
        self.assertEqual(safe_branch("feature/mul"), "feature__mul")
        self.assertEqual(safe_branch("fix/a b:c"), "fix__a_b_c")
        self.assertEqual(safe_branch("main"), "main")

    def test_roundtrip(self):
        st = State(branch="feature/mul", base="main", stage="review", round=2, fixes=1, cost_usd=0.42)
        st.rounds.append({"head_sha": "abc", "verdict": "APPROVE"})
        st.save(self.tmp)
        loaded = State.load(self.tmp)
        self.assertEqual(loaded.branch, "feature/mul")
        self.assertEqual(loaded.round, 2)
        self.assertEqual(loaded.rounds[0]["verdict"], "APPROVE")
        self.assertTrue(loaded.started_at)
        self.assertTrue(loaded.updated_at)

    def test_load_missing(self):
        self.assertIsNone(State.load(self.tmp))

    def test_set_stage_persists_exit(self):
        st = State()
        st.set_stage(self.tmp, "needs_action", "fix it", 2)
        loaded = State.load(self.tmp)
        self.assertEqual(loaded.stage, "needs_action")
        self.assertEqual(loaded.last_exit, 2)
        self.assertEqual(loaded.message, "fix it")

    def test_unknown_stage_rejected(self):
        with self.assertRaises(AssertionError):
            State().set_stage(self.tmp, "flying")

    def test_atomic_write_lf(self):
        path = os.path.join(self.tmp, "x.json")
        write_json_atomic(path, {"a": "中文"})
        with open(path, "rb") as fh:
            raw = fh.read()
        self.assertNotIn(b"\r\n", raw)
        self.assertIn("中文".encode("utf-8"), raw)
        self.assertFalse([f for f in os.listdir(self.tmp) if f.startswith(".tmp-")])

    def test_lock_lifecycle(self):
        acquire_lock(self.tmp)
        self.assertEqual(lock_owner_alive(self.tmp), os.getpid())
        acquire_lock(self.tmp)  # re-entrant for the same pid
        release_lock(self.tmp)
        self.assertIsNone(lock_owner_alive(self.tmp))

    def test_stale_lock_ignored(self):
        write_json_atomic(os.path.join(self.tmp, "lock"), {"pid": 999999999, "since": "x"})
        self.assertIsNone(lock_owner_alive(self.tmp))
        acquire_lock(self.tmp)  # must not raise
        release_lock(self.tmp)

    def test_lock_held_by_live_pid(self):
        # Our own pid as "someone else": simulate by writing it then acquiring with another pid value.
        write_json_atomic(os.path.join(self.tmp, "lock"), {"pid": os.getpid(), "since": "x"})
        with self.assertRaises(LockHeld):
            _acquire_as_other(self.tmp)

    def test_history(self):
        path = os.path.join(self.tmp, "h", "history.jsonl")
        append_history(path, {"repo": "r", "result": "PASS"})
        append_history(path, {"repo": "r", "result": "FAIL"})
        rows = read_history(path)
        self.assertEqual([r["result"] for r in rows], ["PASS", "FAIL"])
        self.assertTrue(all("at" in r and "revali_version" in r for r in rows))
        self.assertEqual(read_history(os.path.join(self.tmp, "none.jsonl")), [])


def _acquire_as_other(rdir):
    """acquire_lock treats the current pid as re-entrant; fake a different caller."""
    import revali.state as st
    real = os.getpid
    try:
        os.getpid = lambda: real() + 1
        st.acquire_lock(rdir)
    finally:
        os.getpid = real


if __name__ == "__main__":
    unittest.main()
