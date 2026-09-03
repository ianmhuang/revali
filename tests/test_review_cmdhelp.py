"""Reviewer acceptance tests for fix/cli-descriptions: every `revali <cmd> -h` carries the
sentence `revali --help` lists for it (AC-1); every subcommand argument renders with a help
text (AC-2); one helper registers each subcommand with `help=` and `description=` from the
same sentence (AC-3); `_close_stopped` puts `started_at` and `updated_at` back after a failed
write (AC-4); parsing and dispatch are what they were (AC-5)."""
import argparse
import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

from revali import cli
from revali.state import State
from tests.helpers import rmtree_force

COMMANDS = ("run", "preflight", "wait", "status", "reset", "clean", "stop", "merge", "stats", "version")


def _squash(text):
    """argparse wraps long lines; compare on whitespace-normalised text."""
    return " ".join(text.split())


def _dash_h(argv):
    """Run `revali <argv> -h` through the real entry point; return the printed text."""
    buf = io.StringIO()
    code = None
    with redirect_stdout(buf):
        try:
            cli.main(list(argv) + ["-h"])
        except SystemExit as raised:
            code = raised.code
    assert code == 0, "-h did not exit 0: %r" % (code,)
    return buf.getvalue()


def _subparsers_action(parser):
    actions = [a for a in parser._actions if isinstance(a, argparse._SubParsersAction)]
    assert len(actions) == 1
    return actions[0]


class EverySubcommandHelpCarriesItsListingSentence(unittest.TestCase):
    def setUp(self):
        self.parser = cli.build_parser()
        self.sub = _subparsers_action(self.parser)
        # the sentence `revali --help` lists for each command
        self.listing = {a.dest: a.help for a in self.sub._choices_actions}
        self.top = _squash(_dash_h([]))

    def test_all_ten_commands_are_listed_with_a_sentence(self):
        self.assertEqual(set(self.listing), set(COMMANDS))
        for name in COMMANDS:
            with self.subTest(command=name):
                self.assertTrue(self.listing[name], name)
                self.assertIn(_squash(self.listing[name]), self.top)                          # AC-1: what --help lists

    def test_cmd_dash_h_prints_the_listed_sentence(self):
        for name in COMMANDS:
            with self.subTest(command=name):
                own = _squash(_dash_h([name]))
                self.assertIn(_squash(self.listing[name]), own)                               # AC-1: the same sentence

    def test_cmd_dash_h_is_more_than_the_usage_line_and_dash_h(self):
        """Before the fix a subcommand's help was the usage line, `options:` and `-h` only."""
        for name in COMMANDS:
            with self.subTest(command=name):
                text = _dash_h([name])
                body = text.split("\n\n", 1)[1] if "\n\n" in text else ""
                self.assertTrue(body.strip(), text)                                            # AC-1: a description block
                self.assertNotIn("usage:", body.split("\n\n", 1)[0])
                self.assertIn(_squash(self.listing[name]), _squash(body))


class EveryArgumentHasAHelpText(unittest.TestCase):
    EXPECTED = {
        "run": {"foreground", "dry_run", "base"},
        "preflight": {"base"},
        "wait": {"timeout"},
        "status": {"branch"},
        "reset": set(),
        "clean": {"branch"},
        "stop": set(),
        "merge": set(),
        "stats": set(),
        "version": set(),
    }

    def setUp(self):
        self.parser = cli.build_parser()
        self.sub = _subparsers_action(self.parser)

    def _arguments(self, name):
        return [a for a in self.sub.choices[name]._actions if not isinstance(a, argparse._HelpAction)]

    def test_the_argument_set_is_unchanged(self):
        for name in COMMANDS:
            with self.subTest(command=name):
                self.assertEqual({a.dest for a in self._arguments(name)}, self.EXPECTED[name])   # AC-5: no new flags

    def test_each_argument_has_help_and_it_renders(self):
        for name in COMMANDS:
            rendered = _squash(_dash_h([name]))
            for action in self._arguments(name):
                with self.subTest(command=name, argument=action.dest):
                    self.assertTrue(action.help and action.help.strip(), "%s %s" % (name, action.dest))   # AC-2
                    self.assertIn(_squash(action.help), rendered)                              # AC-2: shown on -h

    def test_no_dash_h_line_ends_on_a_bare_argument_name(self):
        """A bare argument renders as a line holding only the option or positional name."""
        for name in COMMANDS:
            text = _dash_h([name])
            for action in self._arguments(name):
                names = action.option_strings or [action.dest]
                for line in text.splitlines():
                    stripped = line.strip()
                    for opt in names:
                        with self.subTest(command=name, argument=opt):
                            self.assertNotEqual(stripped, opt, "%s -h shows a bare %s" % (name, opt))    # AC-2
                            self.assertFalse(stripped.startswith(opt + " ") and stripped.split() == [opt, opt.upper().lstrip("-")],
                                             "%s -h shows a bare %s" % (name, opt))

    def test_the_preflight_base_status_branch_and_clean_branch_texts(self):
        """The three arguments that had no help before the change."""
        self.assertTrue(_squash(self.sub.choices["preflight"]._option_string_actions["--base"].help))      # AC-2
        self.assertTrue(_squash(self.sub.choices["status"]._option_string_actions["--branch"].help))      # AC-2
        positional = [a for a in self.sub.choices["clean"]._actions if not a.option_strings]
        self.assertEqual([a.dest for a in positional], ["branch"])
        self.assertTrue(_squash(positional[0].help or ""))                                                # AC-2


class OneHelperRegistersEveryCommand(unittest.TestCase):
    def test_every_add_parser_call_sets_help_and_description_to_one_sentence(self):
        calls = []
        real = argparse._SubParsersAction.add_parser

        def spy(self, name, **kwargs):
            calls.append((name, kwargs))
            return real(self, name, **kwargs)

        with mock.patch.object(argparse._SubParsersAction, "add_parser", spy):
            cli.build_parser()
        self.assertEqual([name for name, _ in calls], list(COMMANDS))                          # AC-3: ten, in order
        for name, kwargs in calls:
            with self.subTest(command=name):
                self.assertIn("help", kwargs)
                self.assertIn("description", kwargs)                                           # AC-3: never help without description
                self.assertTrue(kwargs["help"])
                self.assertEqual(kwargs["help"], kwargs["description"])                        # AC-3: one sentence

    def test_the_helper_takes_name_sentence_and_handler(self):
        self.assertTrue(callable(getattr(cli, "_command", None)), "cli._command missing")       # AC-3: one helper
        parent = argparse.ArgumentParser(prog="x")
        sub = parent.add_subparsers(dest="command")
        handler = lambda args: 7  # noqa: E731
        p = cli._command(sub, "thing", "do the thing", handler)
        self.assertIsInstance(p, argparse.ArgumentParser)
        self.assertEqual(p.description, "do the thing")                                        # AC-3: description=
        self.assertEqual(sub._choices_actions[-1].help, "do the thing")                        # AC-3: help=
        self.assertIs(parent.parse_args(["thing"]).func, handler)                              # AC-3: handler set

    def test_each_subparser_description_equals_its_listing_sentence(self):
        parser = cli.build_parser()
        sub = _subparsers_action(parser)
        listing = {a.dest: a.help for a in sub._choices_actions}
        for name in COMMANDS:
            with self.subTest(command=name):
                self.assertEqual(sub.choices[name].description, listing[name])                 # AC-3 / AC-1


class CloseStoppedRestoresTheTimestamps(unittest.TestCase):
    STARTED = "2026-01-01T00:00:00+0000"
    UPDATED = "2026-01-01T00:05:00+0000"
    NOW = "2030-12-31T23:59:59+0000"   # what a save would stamp; must not survive a failed write

    def setUp(self):
        self.rdir = tempfile.mkdtemp(prefix="revali-cmdhelp-")
        self.addCleanup(rmtree_force, self.rdir)

    def _close(self, state, message="stopped by user at stage 'review'"):
        from revali.pipeline import _close_stopped
        buf = io.StringIO()
        with mock.patch("revali.state.now_iso", return_value=self.NOW):
            with mock.patch("revali.state.write_json_atomic", side_effect=PermissionError("state.json is in use")):
                with redirect_stdout(buf):
                    ok = _close_stopped(state, self.rdir, message)
        return ok, buf.getvalue()

    def test_started_at_and_updated_at_are_put_back_after_a_failed_write(self):
        state = State(branch="b", base="main", stage="review", message="reviewer round 1", last_exit=-1,
                      started_at=self.STARTED, updated_at=self.UPDATED)
        ok, out = self._close(state)
        self.assertFalse(ok)
        self.assertIn("ERROR:", out)
        self.assertEqual((state.stage, state.message, state.last_exit), ("review", "reviewer round 1", -1))
        self.assertEqual(state.started_at, self.STARTED)                                       # AC-4
        self.assertEqual(state.updated_at, self.UPDATED)                                       # AC-4
        self.assertNotIn(self.NOW, (state.started_at, state.updated_at))

    def test_an_empty_started_at_is_not_stamped_by_a_failed_write(self):
        state = State(branch="b", base="main", stage="validate", last_exit=-1)
        ok, _ = self._close(state)
        self.assertFalse(ok)
        self.assertEqual(state.started_at, "")                                                 # AC-4: was empty, stays empty
        self.assertEqual(state.updated_at, "")                                                 # AC-4
        self.assertEqual(state.stage, "validate")

    def test_nothing_landed_on_disk_either(self):
        state = State(branch="b", base="main", stage="review", last_exit=-1,
                      started_at=self.STARTED, updated_at=self.UPDATED)
        self._close(state)
        self.assertFalse(os.path.exists(State.path(self.rdir)))

    def test_a_successful_write_still_stamps_updated_at(self):
        """The restore must not leak into the success path."""
        from revali.pipeline import _close_stopped
        state = State(branch="b", base="main", stage="review", last_exit=-1,
                      started_at=self.STARTED, updated_at=self.UPDATED)
        with mock.patch("revali.state.now_iso", return_value=self.NOW):
            with mock.patch("revali.pipeline._record_history"):
                ok = _close_stopped(state, self.rdir, "x")
        self.assertTrue(ok)
        self.assertEqual(state.stage, "stopped")
        self.assertEqual(state.started_at, self.STARTED)
        self.assertEqual(state.updated_at, self.NOW)
        self.assertEqual(State.load(self.rdir).updated_at, self.NOW)


class ParsingAndDispatchAreUnchanged(unittest.TestCase):
    def setUp(self):
        self.parser = cli.build_parser()

    def test_each_command_dispatches_to_its_handler(self):
        from revali import pipeline
        expected = {"run": pipeline.cmd_run, "preflight": pipeline.cmd_preflight, "wait": pipeline.cmd_wait,
                    "status": pipeline.cmd_status, "reset": pipeline.cmd_reset, "clean": pipeline.cmd_clean,
                    "stop": pipeline.cmd_stop, "merge": pipeline.cmd_merge, "stats": cli._cmd_stats,
                    "version": pipeline.cmd_version}
        self.assertEqual(set(expected), set(COMMANDS))
        for name, func in expected.items():
            with self.subTest(command=name):
                argv = [name, "feature__x"] if name == "clean" else [name]
                args = self.parser.parse_args(argv)
                self.assertIs(args.func, func)                                                 # AC-5
                self.assertEqual(args.command, name)

    def test_flags_and_defaults_parse_as_before(self):
        args = self.parser.parse_args(["--verbose", "run", "--foreground", "--dry-run", "--base", "dev"])
        self.assertEqual((args.verbose, args.foreground, args.dry_run, args.base), (True, True, True, "dev"))   # AC-5
        args = self.parser.parse_args(["run"])
        self.assertEqual((args.verbose, args.foreground, args.dry_run, args.base), (False, False, False, ""))
        self.assertEqual(self.parser.parse_args(["preflight"]).base, "")
        self.assertEqual(self.parser.parse_args(["preflight", "--base", "dev"]).base, "dev")
        self.assertEqual(self.parser.parse_args(["wait"]).timeout, "9m")
        self.assertEqual(self.parser.parse_args(["wait", "--timeout", "30s"]).timeout, "30s")
        self.assertEqual(self.parser.parse_args(["status"]).branch, "")
        self.assertEqual(self.parser.parse_args(["status", "--branch", "b"]).branch, "b")
        self.assertEqual(self.parser.parse_args(["clean", "feature__x"]).branch, "feature__x")

    def test_a_missing_command_and_a_missing_clean_branch_still_exit_2(self):
        for argv in ([], ["clean"], ["nope"]):
            with self.subTest(argv=argv):
                with redirect_stdout(io.StringIO()):
                    with mock.patch("sys.stderr", new_callable=io.StringIO):
                        with self.assertRaises(SystemExit) as raised:
                            self.parser.parse_args(argv)
                self.assertEqual(raised.exception.code, 2)                                     # AC-5: argparse's usage error


if __name__ == "__main__":
    unittest.main()
