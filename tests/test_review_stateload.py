"""Acceptance tests for feature/state-load-normalize, the State.load / State.save half
(AC-1, AC-2, AC-5, AC-6): what a hand-edited state.json reads as and what a save writes.
Black box: a file is written under a temp directory and read back through State."""

import dataclasses
import json
import os
import tempfile
import unittest

from revali import STATE_VERSION
from revali.state import State, StateError
from tests.helpers import rmtree_force

FIELDS = [f.name for f in dataclasses.fields(State)]


class StateFileCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="revali review state ")
        self.addCleanup(rmtree_force, self.tmp)

    def put(self, data):
        """state.json as given: bytes and str verbatim, anything else dumped as JSON."""
        if isinstance(data, bytes):
            raw = data
        elif isinstance(data, str):
            raw = data.encode("utf-8")
        else:
            raw = json.dumps(data).encode("utf-8")
        with open(State.path(self.tmp), "wb") as fh:
            fh.write(raw)
        return raw

    def load(self, data):
        self.put(data)
        return State.load(self.tmp)

    def on_disk(self):
        with open(State.path(self.tmp), "r", encoding="utf-8", newline="") as fh:
            return json.load(fh)

    def refused(self, data):
        """The message of the StateError the file is refused with; the file is untouched."""
        raw = self.put(data)
        with self.assertRaises(StateError) as ctx:
            State.load(self.tmp)
        self.assertIsInstance(ctx.exception, ValueError)
        with open(State.path(self.tmp), "rb") as fh:
            self.assertEqual(fh.read(), raw)
        text = str(ctx.exception)
        self.assertIn(State.path(self.tmp), text)
        return text


class NullIsTheDefault(StateFileCase):
    """AC-1"""

    def test_each_field_alone(self):
        for name in FIELDS:
            with self.subTest(field=name):
                data = {"branch": "feature/x", "pr_url": "https://example.invalid/pull/3"}
                data[name] = None
                st = self.load(data)
                default = getattr(State(), name)
                self.assertEqual(getattr(st, name), default)
                self.assertIs(type(getattr(st, name)), type(default))
                if name != "branch":
                    self.assertEqual(st.branch, "feature/x")  # the rest of the file is read

    def test_every_field_at_once(self):
        st = self.load({name: None for name in FIELDS})
        self.assertEqual(dataclasses.asdict(st), dataclasses.asdict(State()))

    def test_a_null_list_is_a_new_empty_list_per_load(self):
        lists = [name for name in FIELDS if isinstance(getattr(State(), name), list)]
        self.assertIn("pending_test_files", lists)
        first = self.load({name: None for name in lists})
        second = self.load({name: None for name in lists})
        for name in lists:
            with self.subTest(field=name):
                self.assertEqual(getattr(first, name), [])
                getattr(first, name).append("x")
                self.assertEqual(getattr(second, name), [])
                self.assertEqual(getattr(State(), name), [])

    def test_null_counters_count_from_zero(self):
        st = self.load({"fixes": None, "round": None, "cost_usd": None, "fix_due": None})
        self.assertEqual(st.fixes + 1, 1)
        self.assertEqual(st.round, 0)
        self.assertEqual(st.cost_usd + 0.5, 0.5)
        self.assertIs(type(st.cost_usd), float)
        self.assertIs(st.fix_due, False)

    def test_an_empty_object_is_a_default_state(self):
        st = self.load({})
        self.assertEqual(dataclasses.asdict(st), dataclasses.asdict(State()))


class WrongTypeIsRefused(StateFileCase):
    """AC-2"""

    def test_a_scalar_of_the_wrong_type(self):
        cases = [
            ("round", "2", "int", "str"),
            ("message", 5, "str", "int"),
            ("no_tests", "yes", "bool", "str"),
            ("needs_info_used", 0, "bool", "int"),
            ("cost_usd", "1.5", "float", "str"),
            ("last_exit", 2.5, "int", "float"),
            ("branch", ["x"], "str", None),  # how a container is named is not pinned here
            ("pr_number", {"n": 1}, "int", None),
            ("version", "7", "int", "str"),
        ]
        for name, value, expected, found in cases:
            with self.subTest(field=name, value=value):
                text = self.refused({"stage": "review", name: value})
                self.assertIn(name, text)
                self.assertIn(expected, text)
                if found is not None:
                    self.assertIn(found, text)

    def test_a_boolean_where_a_number_belongs(self):
        cases = [
            ("fixes", True),
            ("round", False),
            ("pr_number", True),
            ("last_exit", False),
            ("cost_usd", True),
        ]
        for name, value in cases:
            with self.subTest(field=name, value=value):
                text = self.refused({name: value})
                self.assertIn(name, text)
                self.assertIn("bool", text)

    def test_a_list_field_that_is_not_a_list(self):
        cases = [
            ("test_files", "tests/a.py"),
            ("rounds", {"verdict": "APPROVE"}),
            ("models_used", 3),
            ("issues", True),
            ("pending_test_files", 0),
        ]
        for name, value in cases:
            with self.subTest(field=name, value=value):
                text = self.refused({name: value})
                self.assertIn(name, text)
                self.assertIn("list", text)

    def test_a_list_element_of_the_wrong_type(self):
        cases = [
            ("test_files", ["tests/a.py", 3]),
            ("models_used", [None]),
            ("pending_test_files", ["tests/a.py", None, "tests/b.py"]),
            ("validations", [{"result": "PASS"}, "x"]),
            ("placed_test_files", [["tests/a.py"]]),
            ("issues", [None]),
            ("rounds", [{}, 1]),
            ("test_commits", [True]),
        ]
        for name, value in cases:
            with self.subTest(field=name, value=value):
                self.assertIn(name, self.refused({"stage": "review", name: value}))

    def test_a_top_level_value_that_is_not_an_object(self):
        for text in ("[]", '"review"', "7", "null", "true", '[{"stage": "review"}]'):
            with self.subTest(text=text):
                self.refused(text)

    def test_a_file_that_is_not_json(self):
        cases = [
            b"",
            b'{"stage": "review"',
            b"stage: review\n",
            b"{'stage': 'review'}",
            b"\xff\xfe\x00{",
        ]
        for raw in cases:
            with self.subTest(raw=raw):
                self.refused(raw)

    def test_an_int_is_accepted_for_a_float(self):
        for value in (0, 1, 3):
            with self.subTest(value=value):
                st = self.load({"cost_usd": value})
                self.assertEqual(st.cost_usd, float(value))
                self.assertIs(type(st.cost_usd), float)

    def test_an_int_too_large_for_a_float_is_refused(self):
        # round 1, F1: an int is accepted for a float field, but one no float can hold is a
        # StateError naming the field, not an OverflowError
        for digits in (400, 2000):
            with self.subTest(digits=digits):
                text = self.refused('{"stage": "review", "cost_usd": 1%s}' % ("0" * digits))
                self.assertIn("cost_usd", text)
                self.assertIn("float", text)

    def test_the_largest_ints_a_float_holds_are_accepted(self):
        for value in (2**53, 10**300):
            with self.subTest(value=value):
                st = self.load({"cost_usd": value})
                self.assertEqual(st.cost_usd, float(value))
                self.assertIs(type(st.cost_usd), float)

    def test_values_of_the_right_type_are_kept(self):
        data = {
            "stage": "validate",
            "round": 2,
            "fixes": 1,
            "pr_number": 12,
            "cost_usd": 2.75,
            "fallback": True,
            "fix_due": False,
            "rounds": [{"verdict": "CHANGES_REQUESTED"}, {"verdict": "APPROVE"}],
            "test_files": ["tests/test_review_a.py"],
            "pending_test_files": [],
            "last_exit": 0,
            "message": "validation 1 passed",
        }
        st = self.load(data)
        for name, value in data.items():
            with self.subTest(field=name):
                self.assertEqual(getattr(st, name), value)
                self.assertIs(type(getattr(st, name)), type(value))


class KeysThatAreNotFields(StateFileCase):
    """AC-5"""

    def test_method_names_do_not_replace_the_methods(self):
        st = self.load(
            {"stage": "review", "save": 1, "load": "x", "path": None, "set_stage": [1], "fixes": 2}
        )
        self.assertEqual((st.stage, st.fixes), ("review", 2))
        st.save(self.tmp)
        self.assertEqual(State.load(self.tmp).fixes, 2)
        st.set_stage(self.tmp, "validate", "moved on")
        again = State.load(self.tmp)
        self.assertEqual((again.stage, again.message), ("validate", "moved on"))
        self.assertEqual(os.path.basename(State.path(self.tmp)), "state.json")

    def test_unknown_keys_are_ignored_whatever_they_hold(self):
        st = self.load(
            {
                "stage": "review",
                "no_such_field": {"a": [None, 1]},
                "Stage": 3,
                "fixes ": "1",
                "__class__": 1,
                "__dict__": [],
                "": None,
            }
        )
        self.assertIsInstance(st, State)
        self.assertEqual((st.stage, st.fixes), ("review", 0))
        self.assertFalse(hasattr(st, "no_such_field"))
        self.assertFalse(hasattr(st, "Stage"))
        st.save(self.tmp)
        self.assertEqual(State.load(self.tmp).stage, "review")


class SaveWritesTheCurrentVersion(StateFileCase):
    """AC-6"""

    def test_a_version_6_state_that_owes_a_fix(self):
        st = self.load(
            {"version": 6, "stage": "needs_action", "last_verdict": "CHANGES_REQUESTED", "fixes": 1}
        )
        self.assertIs(st.fix_due, True)  # derived from the stage, as before this change
        st.save(self.tmp)
        written = self.on_disk()
        self.assertEqual(written["version"], STATE_VERSION)
        self.assertIs(written["fix_due"], True)
        self.assertEqual(written["fixes"], 1)
        again = State.load(self.tmp)
        self.assertEqual((again.version, again.fix_due), (STATE_VERSION, True))

    def test_a_version_6_state_that_owes_nothing(self):
        for stage, verdict in (("error", "FAIL"), ("needs_action", "NEEDS_INFO"), ("review", "")):
            with self.subTest(stage=stage, last_verdict=verdict):
                st = self.load({"version": 6, "stage": stage, "last_verdict": verdict})
                self.assertIs(st.fix_due, False)
                st.save(self.tmp)
                written = self.on_disk()
                self.assertEqual(written["version"], STATE_VERSION)
                self.assertIs(written["fix_due"], False)
                self.assertIs(State.load(self.tmp).fix_due, False)

    def test_set_stage_writes_it_too(self):
        st = self.load({"version": 6, "stage": "review"})
        st.set_stage(self.tmp, "validate")
        self.assertEqual(self.on_disk()["version"], STATE_VERSION)

    def test_whatever_version_the_file_held(self):
        for version in (1, 6, STATE_VERSION, STATE_VERSION + 5):
            with self.subTest(version=version):
                st = self.load({"version": version, "stage": "review"})
                st.save(self.tmp)
                self.assertEqual(self.on_disk()["version"], STATE_VERSION)

    def test_whatever_version_the_object_holds(self):
        st = State(stage="review")
        st.version = 2
        st.save(self.tmp)
        self.assertEqual(self.on_disk()["version"], STATE_VERSION)


if __name__ == "__main__":
    unittest.main()
