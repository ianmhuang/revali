"""`State.load` on a hand-edited or damaged state.json (feature/state-load-normalize): a null
field takes its default, any other wrong type is a StateError that every command reports on
one line, `reset` still clears it, and `save` always stamps the current STATE_VERSION."""

import dataclasses
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from revali import EXIT_ERROR, EXIT_OK, STATE_VERSION
from revali.state import State, StateError, acquire_lock, lock_path
from tests.helpers import RepoCase, claude_entry, rmtree_force, run_cli

DEAD_PID = 999999999


def write_raw(rdir, data):
    """state.json exactly as given: a dict/list is dumped as JSON, a str is written verbatim."""
    os.makedirs(rdir, exist_ok=True)
    text = data if isinstance(data, str) else json.dumps(data)
    with open(State.path(rdir), "w", encoding="utf-8", newline="") as fh:
        fh.write(text)


class LoadNormalizes(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="revali state ")
        self.addCleanup(rmtree_force, self.tmp)

    def load(self, data):
        write_raw(self.tmp, data)
        return State.load(self.tmp)

    # --- AC-1: null is the default ------------------------------------------------------

    def test_every_null_field_takes_its_default(self):
        names = [f.name for f in dataclasses.fields(State)]
        st = self.load({name: None for name in names})
        expected = State()
        for name in names:
            self.assertEqual(getattr(st, name), getattr(expected, name), name)
            self.assertIs(type(getattr(st, name)), type(getattr(expected, name)), name)

    def test_null_lists_are_fresh_objects(self):
        a = self.load({"pending_test_files": None, "rounds": None})
        b = self.load({"pending_test_files": None, "rounds": None})
        a.pending_test_files.append("x.py")
        a.rounds.append({})
        self.assertEqual((b.pending_test_files, b.rounds), ([], []))

    def test_valid_values_are_kept(self):
        st = self.load(
            {
                "stage": "needs_action",
                "fixes": 2,
                "cost_usd": 1.25,
                "fix_due": True,
                "pending_test_files": ["tests/a.py"],
                "rounds": [{"verdict": "APPROVE"}],
            }
        )
        self.assertEqual(st.stage, "needs_action")
        self.assertEqual(st.fixes, 2)
        self.assertEqual(st.cost_usd, 1.25)
        self.assertTrue(st.fix_due)
        self.assertEqual(st.pending_test_files, ["tests/a.py"])
        self.assertEqual(st.rounds, [{"verdict": "APPROVE"}])

    # --- AC-2: wrong types --------------------------------------------------------------

    def assert_refused(self, data, field, expected, found):
        write_raw(self.tmp, data)
        with self.assertRaises(StateError) as ctx:
            State.load(self.tmp)
        text = str(ctx.exception)
        self.assertIn(State.path(self.tmp), text)
        self.assertIn(field, text)
        self.assertIn(expected, text)
        self.assertIn(found, text)

    def test_wrong_scalar_types(self):
        cases = [
            ("fixes", "1", "int", "str"),
            ("stage", 3, "str", "int"),
            ("fix_due", 1, "bool", "int"),
            ("cost_usd", "x", "float", "str"),
            ("pr_number", 1.5, "int", "float"),
        ]
        for field, value, expected, found in cases:
            with self.subTest(field=field):
                self.assert_refused({field: value}, "'%s'" % field, expected, found)

    def test_bool_is_not_an_int_or_a_float(self):
        self.assert_refused({"fixes": True}, "'fixes'", "int", "bool")
        self.assert_refused({"cost_usd": False}, "'cost_usd'", "float", "bool")

    def test_wrong_container_types(self):
        self.assert_refused({"pending_test_files": "a.py"}, "'pending_test_files'", "list", "str")
        self.assert_refused({"rounds": {}}, "'rounds'", "list", "dict")

    def test_wrong_list_elements(self):
        self.assert_refused(
            {"pending_test_files": ["a.py", None]}, "'pending_test_files'", "str", "NoneType"
        )
        self.assert_refused({"rounds": [{}, 1]}, "'rounds'", "dict", "int")
        self.assert_refused({"test_commits": [7]}, "'test_commits'", "str", "int")

    def test_top_level_not_an_object(self):
        write_raw(self.tmp, [1, 2])
        with self.assertRaises(StateError) as ctx:
            State.load(self.tmp)
        self.assertIn(State.path(self.tmp), str(ctx.exception))
        self.assertIn("object", str(ctx.exception))

    def test_invalid_json(self):
        write_raw(self.tmp, '{"stage": "review",')
        with self.assertRaises(StateError) as ctx:
            State.load(self.tmp)
        self.assertIn(State.path(self.tmp), str(ctx.exception))
        self.assertIn("JSON", str(ctx.exception))

    def test_state_error_is_a_value_error(self):
        self.assertTrue(issubclass(StateError, ValueError))

    def test_an_int_is_accepted_for_a_float(self):
        st = self.load({"cost_usd": 1})
        self.assertEqual(st.cost_usd, 1.0)
        self.assertIs(type(st.cost_usd), float)

    # --- AC-5: keys that are not fields ---------------------------------------------------

    def test_unknown_keys_and_method_names_are_ignored(self):
        st = self.load({"stage": "review", "no_such_key": 1, "save": 1, "path": "x", "load": 2})
        self.assertEqual(st.stage, "review")
        self.assertFalse(hasattr(st, "no_such_key"))
        self.assertTrue(callable(st.save))
        st.save(self.tmp)  # still the method
        self.assertEqual(State.load(self.tmp).stage, "review")
        self.assertTrue(State.path(self.tmp).endswith("state.json"))

    # --- AC-6: version --------------------------------------------------------------------

    def file_version(self):
        with open(State.path(self.tmp), "r", encoding="utf-8") as fh:
            return json.load(fh)["version"]

    def test_an_old_state_saved_back_carries_the_current_version(self):
        st = self.load({"version": 6, "stage": "needs_action", "last_verdict": "FAIL"})
        self.assertTrue(st.fix_due)  # still derived for a state from before version 7
        st.save(self.tmp)
        self.assertEqual(self.file_version(), STATE_VERSION)
        self.assertTrue(State.load(self.tmp).fix_due)

    def test_save_ignores_an_in_memory_version(self):
        State(version=3).save(self.tmp)
        self.assertEqual(self.file_version(), STATE_VERSION)


class BrokenStateCase(RepoCase):
    BROKEN = {"stage": "review", "fixes": "1"}

    def broken(self):
        write_raw(self.rdir(), self.BROKEN)

    def tree_lock(self):
        return os.path.join(self.repo, ".revali", "tree.lock")

    def assert_one_line_error(self, code, out):
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertIn("ERROR: ", out)
        self.assertIn("'fixes'", out)
        self.assertIn(State.path(self.rdir()), out)
        self.assertIn("revali reset", out)
        self.assertNotIn("Traceback", out)


class NullPendingFilesRun(RepoCase):
    """AC-1: the case that used to raise TypeError in the child's preflight."""

    def test_foreground_run_gets_past_preflight(self):
        write_raw(self.rdir(), {"pending_test_files": None, "placed_test_files": None})
        self.claude(claude_entry())
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertEqual(State.load(self.rdir()).stage, "ready_to_merge")

    def test_detached_run_starts(self):
        write_raw(self.rdir(), {"pending_test_files": None})
        with mock.patch("revali.pipeline.spawn_detached", return_value=DEAD_PID) as spawn:
            code, out = run_cli(["run"])
        self.assertEqual(code, EXIT_OK, out)
        spawn.assert_called_once()


class CommandsReportABrokenState(BrokenStateCase):
    """AC-3"""

    def test_wait(self):
        self.broken()
        self.assert_one_line_error(*run_cli(["wait", "--timeout", "1s"]))

    def test_status(self):
        self.broken()
        self.assert_one_line_error(*run_cli(["status"]))

    def test_merge(self):
        self.broken()
        self.assert_one_line_error(*run_cli(["merge"]))

    def test_detached_run_refuses_before_spawning(self):
        self.broken()
        with mock.patch("revali.pipeline.spawn_detached", return_value=DEAD_PID) as spawn:
            code, out = run_cli(["run"])
        self.assert_one_line_error(code, out)
        spawn.assert_not_called()
        self.assertFalse(os.path.isfile(lock_path(self.rdir())))
        self.assertFalse(os.path.isfile(self.tree_lock()))

    def test_foreground_run_releases_its_locks(self):
        self.broken()
        code, out = run_cli(["run", "--foreground"])
        self.assert_one_line_error(code, out)
        self.assertFalse(os.path.isfile(lock_path(self.rdir())))
        self.assertFalse(os.path.isfile(self.tree_lock()))

    def test_invalid_json_reads_the_same_way(self):
        write_raw(self.rdir(), "{not json")
        code, out = run_cli(["status"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertIn("ERROR: ", out)
        self.assertIn("revali reset", out)


class ResetAndStopOnABrokenState(BrokenStateCase):
    """AC-4"""

    def test_reset_removes_it(self):
        self.broken()
        code, out = run_cli(["reset"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertFalse(os.path.isfile(State.path(self.rdir())))
        self.assertIn("state removed", out)
        self.assertIn("could not be read", out)
        self.assertIn("'fixes'", out)
        self.assertNotIn("Traceback", out)
        self.claude(claude_entry())
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)

    def test_reset_removes_invalid_json(self):
        write_raw(self.rdir(), "{not json")
        code, out = run_cli(["reset"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertFalse(os.path.isfile(State.path(self.rdir())))

    def test_stop_kills_a_live_run_and_reports_the_state(self):
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(120)"], start_new_session=True
        )  # own group: kill_tree uses killpg on POSIX
        self.addCleanup(lambda: child.poll() is None and child.kill())
        self.broken()
        acquire_lock(self.rdir(), pid=child.pid)
        code, out = run_cli(["stop"])
        self.assert_one_line_error(code, out)
        self.assertIn("stopped pid %d" % child.pid, out)
        self.assertIsNotNone(child.poll())  # killed
        self.assertFalse(os.path.exists(lock_path(self.rdir())))
        self.assertFalse(os.path.isfile(self.tree_lock()))

    def test_stop_without_a_live_run(self):
        self.broken()
        code, out = run_cli(["stop"])
        self.assert_one_line_error(code, out)


if __name__ == "__main__":
    unittest.main()
