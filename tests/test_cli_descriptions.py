"""Every `revali <cmd> -h` describes its command and arguments; `_close_stopped` restores the
timestamps as well as the outcome fields when the write fails."""

import argparse
import inspect
import tempfile
import unittest
from unittest import mock

from revali import cli
from revali.state import State

COMMANDS = (
    "run",
    "preflight",
    "wait",
    "status",
    "reset",
    "clean",
    "stop",
    "merge",
    "stats",
    "version",
)


def _subparsers(parser):
    return [a for a in parser._actions if isinstance(a, argparse._SubParsersAction)][0]


class SubcommandHelp(unittest.TestCase):
    def setUp(self):
        self.parser = cli.build_parser()
        self.sub = _subparsers(self.parser)

    def test_the_ten_commands_are_registered(self):
        self.assertEqual(set(self.sub.choices), set(COMMANDS))

    def test_each_dash_h_prints_the_listing_sentence(self):  # AC-1
        listing = {a.dest: a.help for a in self.sub._choices_actions}
        for name in COMMANDS:
            with self.subTest(command=name):
                sentence = listing[name]
                self.assertTrue(sentence, "no listing sentence for %s" % name)
                own = " ".join(self.sub.choices[name].format_help().split())  # argparse wraps lines
                self.assertIn(" ".join(sentence.split()), own)

    def test_every_argument_has_a_help_text(self):  # AC-2
        for name in COMMANDS:
            for action in self.sub.choices[name]._actions:
                if isinstance(action, argparse._HelpAction):
                    continue
                with self.subTest(command=name, argument=action.dest):
                    self.assertTrue(action.help, "%s: %s has no help" % (name, action.dest))
        for action in self.parser._actions:
            if isinstance(action, (argparse._HelpAction, argparse._SubParsersAction)):
                continue
            self.assertTrue(action.help, "top level: %s has no help" % action.dest)

    def test_one_helper_registers_every_command(self):  # AC-3
        # `add_parser` is called only from `_command`, which hands the same sentence to both
        # `help=` and `description=`: observed through the calls, not by counting source text
        # (a formatter may split a call across lines).
        source = inspect.getsource(cli)
        self.assertEqual(source.count("add_parser("), 1)
        calls = []
        real = argparse._SubParsersAction.add_parser

        def spy(self_, name, **kwargs):
            calls.append((name, kwargs))
            return real(self_, name, **kwargs)

        with mock.patch.object(argparse._SubParsersAction, "add_parser", spy):
            cli.build_parser()
        self.assertEqual(sorted(name for name, _ in calls), sorted(COMMANDS))
        for name, kwargs in calls:
            with self.subTest(command=name):
                self.assertTrue(kwargs.get("help"), "%s: empty sentence" % name)
                self.assertEqual(kwargs.get("description"), kwargs["help"])

    def test_dispatch_is_unchanged(self):  # AC-5
        from revali import pipeline

        expected = {
            "run": pipeline.cmd_run,
            "preflight": pipeline.cmd_preflight,
            "wait": pipeline.cmd_wait,
            "status": pipeline.cmd_status,
            "reset": pipeline.cmd_reset,
            "clean": pipeline.cmd_clean,
            "stop": pipeline.cmd_stop,
            "merge": pipeline.cmd_merge,
            "stats": cli._cmd_stats,
            "version": pipeline.cmd_version,
        }
        for name, func in expected.items():
            argv = [name, "x"] if name == "clean" else [name]
            self.assertIs(self.parser.parse_args(argv).func, func, name)
        args = self.parser.parse_args(
            ["--verbose", "run", "--foreground", "--dry-run", "--base", "dev"]
        )
        self.assertEqual(
            (args.verbose, args.foreground, args.dry_run, args.base), (True, True, True, "dev")
        )
        self.assertEqual(self.parser.parse_args(["wait"]).timeout, "9m")
        self.assertEqual(self.parser.parse_args(["status", "--branch", "b"]).branch, "b")
        self.assertEqual(self.parser.parse_args(["clean", "feature__x"]).branch, "feature__x")


class CloseStoppedRestoresTimestamps(unittest.TestCase):  # AC-4
    def test_started_and_updated_at_survive_a_failed_write(self):
        from revali.pipeline import _close_stopped

        rdir = tempfile.mkdtemp(prefix="revali close ")
        from tests.helpers import rmtree_force

        self.addCleanup(rmtree_force, rdir)
        state = State(
            branch="b",
            base="main",
            stage="review",
            message="reviewer round 1",
            last_exit=-1,
            started_at="2026-01-01T00:00:00+0000",
            updated_at="2026-01-01T00:05:00+0000",
        )
        with mock.patch("revali.state.write_json_atomic", side_effect=PermissionError("busy")):
            with mock.patch("sys.stdout"):
                ok = _close_stopped(state, rdir, "stopped by user at stage 'review'")
        self.assertFalse(ok)
        self.assertEqual(
            (state.stage, state.message, state.last_exit), ("review", "reviewer round 1", -1)
        )
        self.assertEqual(state.started_at, "2026-01-01T00:00:00+0000")
        self.assertEqual(state.updated_at, "2026-01-01T00:05:00+0000")

    def test_an_empty_started_at_stays_empty(self):
        from revali.pipeline import _close_stopped

        rdir = tempfile.mkdtemp(prefix="revali close ")
        from tests.helpers import rmtree_force

        self.addCleanup(rmtree_force, rdir)
        state = State(branch="b", base="main", stage="review", last_exit=-1)
        with mock.patch("revali.state.write_json_atomic", side_effect=PermissionError("busy")):
            with mock.patch("sys.stdout"):
                _close_stopped(state, rdir, "x")
        self.assertEqual((state.started_at, state.updated_at), ("", ""))


if __name__ == "__main__":
    unittest.main()
