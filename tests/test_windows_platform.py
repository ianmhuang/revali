"""`windows` as a validation platform (feature/windows-platform, M3.7.1): the config accepts
it with the runners that can run Windows, and preflight probes every listed platform."""

import os
import unittest
from types import SimpleNamespace
from unittest import mock

from revali import EXIT_ERROR, EXIT_OK, preflight
from revali.config import ConfigError, parse_project_config
from revali.preflight import Stop
from tests.helpers import ROOT, RepoCase, run_cli

LINUX = """
[validate.linux]
runner = "wsl"
new_test = "python3 -m unittest {files}"
"""

WINDOWS_LOCAL = """
[validate.windows]
runner = "local"
new_test = "python -m unittest {files}"
"""

WINDOWS_SSH = """
[validate.windows]
runner = "ssh"
host = "winbox"
new_test = "python -m unittest {files}"
"""

WINDOWS_WSL = """
[validate.windows]
runner = "wsl"
new_test = "python -m unittest {files}"
"""

WINDOWS_DEFAULT_RUNNER = """
[validate.windows]
new_test = "python -m unittest {files}"
"""


def config(platforms, *tables):
    return "[project]\nconfig_version = 1\nplatforms = %s\n%s" % (
        "[%s]" % ", ".join('"%s"' % p for p in platforms),
        "".join(tables),
    )


def problems(text, host_nt=None):
    """The ConfigError problems of `text`, [] when it loads. `host_nt` pretends the host is
    (True) or is not (False) Windows."""
    patches = []
    if host_nt is not None:
        patches.append(mock.patch("os.name", "nt" if host_nt else "posix"))
    for p in patches:
        p.start()
    try:
        parse_project_config(text)
    except ConfigError as exc:
        return list(exc.problems)
    finally:
        for p in reversed(patches):
            p.stop()
    return []


def load(text, host_nt=True):
    with mock.patch("os.name", "nt" if host_nt else "posix"):
        return parse_project_config(text)


class PlatformNames(unittest.TestCase):
    """AC-1"""

    def test_windows_alone_loads(self):
        cfg = load(config(["windows"], WINDOWS_LOCAL))
        self.assertEqual(cfg.project.platforms, ["windows"])
        self.assertEqual(cfg.validate.platforms["windows"].runner, "local")

    def test_both_orders_load(self):
        for order in (["linux", "windows"], ["windows", "linux"]):
            with self.subTest(order=order):
                cfg = load(config(order, LINUX, WINDOWS_LOCAL))
                self.assertEqual(cfg.project.platforms, order)

    def test_another_name_lists_both_supported_names(self):
        found = problems(config(["linux", "macos"], LINUX))
        line = [p for p in found if "macos" in p]
        self.assertEqual(len(line), 1, found)
        self.assertIn("not supported", line[0])
        self.assertIn("linux, windows", line[0])

    def test_windows_without_its_table_is_refused(self):
        found = problems(config(["linux", "windows"], LINUX), host_nt=True)
        self.assertIn("missing [validate.windows] section for listed platform", found)

    def test_a_name_listed_twice_is_refused(self):
        # round 1 F2: the note would contradict itself and the probe would run twice
        for order in (["linux", "linux"], ["linux", "windows", "linux"]):
            with self.subTest(order=order):
                found = problems(config(order, LINUX, WINDOWS_LOCAL), host_nt=True)
                self.assertEqual(found, ["project.platforms lists 'linux' more than once"])
        found = problems(
            config(["windows", "linux", "windows", "linux"], LINUX, WINDOWS_LOCAL), host_nt=True
        )
        self.assertEqual(
            found,
            [
                "project.platforms lists 'linux' more than once",
                "project.platforms lists 'windows' more than once",
            ],
        )


class RunnerRules(unittest.TestCase):
    """AC-2"""

    def test_windows_on_wsl_is_refused_by_name(self):
        for host_nt in (True, False):
            with self.subTest(host_nt=host_nt):
                found = problems(config(["windows"], WINDOWS_WSL), host_nt=host_nt)
                line = [p for p in found if "validate.windows" in p and "wsl" in p]
                self.assertEqual(len(line), 1, found)
                self.assertIn("Linux", line[0])

    def test_the_default_runner_is_wsl_so_a_bare_windows_table_is_refused(self):
        found = problems(config(["windows"], WINDOWS_DEFAULT_RUNNER), host_nt=True)
        self.assertTrue(any("validate.windows" in p and "wsl" in p for p in found), found)

    def test_windows_local_needs_a_windows_host(self):
        self.assertEqual(problems(config(["windows"], WINDOWS_LOCAL), host_nt=True), [])
        found = problems(config(["windows"], WINDOWS_LOCAL), host_nt=False)
        line = [p for p in found if "validate.windows" in p and "local" in p]
        self.assertEqual(len(line), 1, found)
        self.assertIn("Windows host", line[0])

    def test_windows_over_ssh_loads_on_any_host(self):
        for host_nt in (True, False):
            with self.subTest(host_nt=host_nt):
                self.assertEqual(problems(config(["windows"], WINDOWS_SSH), host_nt=host_nt), [])

    def test_linux_keeps_its_rules(self):
        linux_local = LINUX.replace('runner = "wsl"', 'runner = "local"')
        for host_nt in (True, False):
            with self.subTest(host_nt=host_nt):
                self.assertEqual(problems(config(["linux"], linux_local), host_nt=host_nt), [])
                self.assertEqual(problems(config(["linux"], LINUX), host_nt=host_nt), [])


class Probes(unittest.TestCase):
    """AC-3"""

    def ctx(self, text):
        return SimpleNamespace(dry_run=False, cfg=load(text), detail=None)

    def run_check(self, ctx, answers):
        seen = []

        def probe(plat, log=None):
            seen.append(plat.name)
            return answers.get(plat.name, "")

        with mock.patch("revali.runners.probe_runner", probe):
            try:
                preflight.check_runner(ctx)
            except Stop as stop:
                return seen, stop
        return seen, None

    def test_every_platform_is_probed_in_list_order(self):
        for order in (["linux", "windows"], ["windows", "linux"]):
            with self.subTest(order=order):
                seen, stop = self.run_check(self.ctx(config(order, LINUX, WINDOWS_LOCAL)), {})
                self.assertEqual(seen, order)
                self.assertIsNone(stop)

    def test_the_first_failing_probe_stops_and_names_its_platform(self):
        ctx = self.ctx(config(["linux", "windows"], LINUX, WINDOWS_LOCAL))
        seen, stop = self.run_check(ctx, {"windows": "it cannot start"})
        self.assertEqual(seen, ["linux", "windows"])
        self.assertEqual(stop.exit_code, EXIT_ERROR)
        self.assertEqual(stop.message, "validate.windows: it cannot start")
        seen, stop = self.run_check(ctx, {"linux": "no distro", "windows": "unused"})
        self.assertEqual(seen, ["linux"])
        self.assertEqual(stop.message, "validate.linux: no distro")

    def test_one_platform_keeps_todays_message(self):
        seen, stop = self.run_check(self.ctx(config(["linux"], LINUX)), {"linux": "no distro"})
        self.assertEqual(seen, ["linux"])
        self.assertEqual(stop.message, "no distro")

    def test_a_dry_run_probes_nothing(self):
        ctx = self.ctx(config(["linux", "windows"], LINUX, WINDOWS_LOCAL))
        ctx.dry_run = True
        seen, stop = self.run_check(ctx, {"linux": "no distro"})
        self.assertEqual(seen, [])
        self.assertIsNone(stop)


class OnlyTheFirstIsValidated(unittest.TestCase):
    """AC-4"""

    def notes(self, cfg):
        return [w for w in cfg.warnings if "validated" in w]

    def test_more_than_one_platform_gets_a_note_naming_the_others(self):
        cfg = load(config(["windows", "linux"], LINUX, WINDOWS_LOCAL))
        notes = self.notes(cfg)
        self.assertEqual(len(notes), 1, cfg.warnings)
        self.assertIn("'windows'", notes[0])
        self.assertIn("'linux'", notes[0])
        self.assertIn("not validated", notes[0])
        self.assertLess(notes[0].index("'windows'"), notes[0].index("'linux'"))
        # round 1 F3: printed on a dry run too, which probes nothing, so it claims no probe
        self.assertNotIn("probed", notes[0])

    def test_one_platform_gets_no_note(self):
        self.assertEqual(self.notes(load(config(["linux"], LINUX))), [])
        self.assertEqual(self.notes(load(config(["windows"], WINDOWS_LOCAL))), [])


class NoteInPreflightOutput(RepoCase):
    """AC-4 end to end: the note is a `note:` line of the run's output."""

    def test_a_dry_run_prints_the_note(self):
        # ssh loads on any host, so the case runs on Linux and Windows alike
        self.write(
            "revali.toml",
            self.read("revali.toml").replace(
                'platforms = ["linux"]', 'platforms = ["linux", "windows"]'
            )
            + WINDOWS_SSH,
        )
        self.commit_all("list windows")
        code, out = run_cli(["run", "--dry-run"])
        self.assertEqual(code, EXIT_OK, out)
        notes = [ln for ln in out.splitlines() if "note:" in ln and "not validated" in ln]
        self.assertEqual(len(notes), 1, out)
        self.assertIn("'windows'", notes[0])


class Docs(unittest.TestCase):
    """AC-5"""

    def read(self, *parts):
        with open(os.path.join(ROOT, *parts), "r", encoding="utf-8", newline="") as fh:
            return fh.read()

    def test_configuration_page_names_the_platforms_and_rules(self):
        text = self.read("docs", "configuration.md")
        self.assertIn("`windows`", text)
        self.assertIn("`linux`", text)
        self.assertIn('runner = "wsl"', text)
        self.assertIn("first platform", text)

    def test_template_comment_names_windows(self):
        line = [
            ln for ln in self.read("templates", "revali.toml").splitlines() if "platforms =" in ln
        ]
        self.assertEqual(len(line), 1)
        self.assertIn("windows", line[0])
        self.assertNotIn("linux only", line[0])


if __name__ == "__main__":
    unittest.main()
