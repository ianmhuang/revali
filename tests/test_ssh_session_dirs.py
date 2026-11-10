"""fix/ssh-session-dirs (M3.4.10): every ssh sandbox session works in remote directories of
its own (AC-1, AC-2), a lost connection says what it left on the host (AC-3), only an
incomplete last results row reads as a session stopped while writing (AC-4), a wsl session
that fails before its script runs leaves no `<label>-extra` (AC-5), and a session that ended
early names a non-empty script log (AC-6)."""

import os
import re
import shutil
import tempfile
import unittest
from unittest import mock

from revali.config import PlatformCfg
from revali.runners import (
    RunnerError,
    SessionEndedEarly,
    SshRunner,
    WslRunner,
    report_from_results,
    session_id,
)
from tests.helpers import ROOT, git
from tests.test_ssh_runner import HAVE_BASH, SID, SshCase, plat

LABEL = "validate-r1"
SCOPE = "feature__mul"
KEY = "0123abcd"
BASE = ("sample", "%s-%s" % (SCOPE, KEY))  # <repo>/<branch>-<key> under the sandbox root


class SshSessionCase(SshCase):
    def session(self, steps, label=LABEL):
        logs = os.path.join(self.tmp, "logs-%d" % len(self.calls()))
        head = git(["rev-parse", "HEAD"], self.repo).strip()
        return logs, SshRunner(plat()).run(
            self.repo, head, steps, {}, logs, label, scope=SCOPE, key=KEY
        )

    def base_dir(self, *parts):
        return os.path.join(self.remote, ".revali", "sandbox", *BASE, *parts)

    def session_ids(self, since=0):
        """The session ids the scp uploads went to, in order."""
        ids = []
        for exe, argv in self.calls()[since:]:
            m = re.search(r"/%s-(%s)-in/$" % (LABEL, SID), argv[-1])
            if exe == "scp" and m:
                ids.append(m.group(1))
        return ids


class SessionIds(unittest.TestCase):
    """AC-1: the id itself."""

    def test_eight_hex_characters_new_every_time(self):
        ids = {session_id() for _ in range(50)}
        self.assertEqual(len(ids), 50)
        for sid in ids:
            self.assertRegex(sid, "^[0-9a-f]{8}$")


@unittest.skipUnless(HAVE_BASH, "needs bash")
class OwnDirectories(SshSessionCase):
    """AC-1, AC-2."""

    def test_every_remote_path_of_a_session_carries_its_id(self):
        logs, report = self.session([("setup", "true"), ("test", "true")])
        self.assertTrue(report.ok, [(s.name, s.returncode) for s in report.steps])
        (sid,) = self.session_ids()
        own = "%s-%s" % (LABEL, sid)
        calls = self.calls()
        self.assertEqual([c[0] for c in calls], ["ssh", "scp", "ssh", "scp", "ssh"])
        self.assertIn("%s/%s-in" % ("/".join(BASE), own), calls[0][1][-1])
        self.assertIn("%s/%s-logs" % ("/".join(BASE), own), calls[0][1][-1])
        self.assertIn("%s/%s-in/%s.sh" % ("/".join(BASE), own, LABEL), calls[2][1][-1])
        self.assertTrue(calls[3][1][-2].endswith("/%s-logs/." % own), calls[3][1])
        rm = calls[4][1][-1].partition("&&")[0]
        for name in (own + "-in", own + "-logs", own):
            self.assertIn("%s/%s" % ("/".join(BASE), name), rm)
        with open(os.path.join(logs, LABEL + ".sh"), encoding="utf-8") as fh:
            script = fh.read()
        self.assertIn('SB="$HOME/.revali/sandbox/%s/%s"' % ("/".join(BASE), own), script)
        self.assertIn('LABEL="%s"' % LABEL, script)
        # the host's logs directory keeps its names
        for name in (LABEL + ".results", LABEL + "-setup.log", LABEL + "-test.log"):
            self.assertTrue(os.path.isfile(os.path.join(logs, name)), name)
        self.assertEqual(self.remote_leftovers(), [])

    def test_two_sessions_of_one_checkout_branch_and_label_share_no_path(self):
        self.session([("test", "true")])
        self.session([("test", "true")])
        first, second = self.session_ids()
        self.assertNotEqual(first, second)

    def orphan(self, sid="deadbeef"):
        """What a script left running by a lost connection has on the host right now."""
        own = "%s-%s" % (LABEL, sid)
        files = {
            self.base_dir(own, "repo", "busy"): "x",
            self.base_dir(own + "-in", LABEL + ".sh"): "# the orphan's script\n",
            self.base_dir(own + "-logs", LABEL + ".results"): "setup\t0\t0\ntest\t0\t0\n",
        }
        for path, text in files.items():
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8", newline="") as fh:
                fh.write(text)
        return files

    def test_a_later_session_neither_reads_nor_deletes_an_orphan(self):
        files = self.orphan()
        _logs, report = self.session([("setup", "true"), ("test", "exit 3")])
        # the orphan's passing `test` row is not this session's result
        self.assertFalse(report.ok)
        self.assertEqual(report.failed.name, "test")
        self.assertEqual(report.failed.returncode, 3)
        for path, text in files.items():
            with open(path, encoding="utf-8", newline="") as fh:
                self.assertEqual(fh.read(), text, path)
        # the shared levels stay while the orphan is in them; only the orphan is left
        self.assertTrue(os.path.isdir(self.base_dir()))
        self.assertEqual(
            sorted(os.listdir(self.base_dir())),
            ["%s-deadbeef" % LABEL, "%s-deadbeef-in" % LABEL, "%s-deadbeef-logs" % LABEL],
        )

    def test_shared_levels_go_once_empty(self):
        self.session([("test", "true")])
        self.assertEqual(os.listdir(os.path.join(self.remote, ".revali", "sandbox")), [])


@unittest.skipUnless(HAVE_BASH, "needs bash")
class LostConnection(SshSessionCase):
    """AC-3."""

    def lost(self):
        os.environ["REVALI_FAKE_SSH_SCRIPT_DROP"] = "0"
        with self.assertRaises(RunnerError) as cm:
            self.session([("setup", "true"), ("test", "true")])
        (sid,) = self.session_ids()
        return str(cm.exception), sid

    def dirs(self, sid):
        own = "$HOME/.revali/sandbox/%s/%s-%s" % ("/".join(BASE), LABEL, sid)
        return [own, own + "-in", own + "-logs"]

    def test_names_the_directories_it_removed(self):
        text, sid = self.lost()
        self.assertIn("may still be running on box", text)
        self.assertIn("revali removed this session's directories there", text)
        for d in self.dirs(sid):
            self.assertIn(d, text)
        self.assertIn("A later session uses directories of its own", text)

    def test_names_the_directories_left_when_the_cleanup_failed(self):
        os.environ["REVALI_FAKE_SSH_RM_FAILS"] = "1"
        text, sid = self.lost()
        self.assertIn("are left there; delete them once the script has ended", text)
        for d in self.dirs(sid):
            self.assertIn(d, text)

    def test_a_damaged_file_is_not_a_lost_session(self):
        # AC-4 on the ssh runner: a broken row with rows after it, then the connection drops
        junk = 'printf "junk\\n" >> "$(ls -d "$HOME"/.revali/sandbox/%s/%s-*-logs)/%s.results"' % (
            "/".join(BASE),
            LABEL,
            LABEL,
        )
        os.environ["REVALI_FAKE_SSH_SCRIPT_DROP"] = "2"
        with self.assertRaises(RunnerError) as cm:
            self.session([("setup", junk), ("test", "true")])
        text = str(cm.exception)
        self.assertNotIsInstance(cm.exception, SessionEndedEarly)
        self.assertIn("the sandbox results file is damaged: row 1", text)
        self.assertNotIn("may still be running", text)


class DamagedResults(unittest.TestCase):
    """AC-4."""

    STEPS = [("setup", "s"), ("test", "t")]

    def setUp(self):
        self.logs = tempfile.mkdtemp(prefix="revali-rows-")
        self.addCleanup(shutil.rmtree, self.logs, True)

    def read(self, rows):
        path = os.path.join(self.logs, "v.results")
        with open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write(rows)
        return path

    def test_an_incomplete_row_with_rows_after_it(self):
        for rows, n in (("setup\t0\nsetup\t0\t0\n", 1), ("setup\t0\t0\ntest\tx\t0\nmore\n", 2)):
            with self.subTest(rows=rows):
                path = self.read(rows)
                with self.assertRaises(RunnerError) as cm:
                    report_from_results(self.logs, "v", list(self.STEPS))
                self.assertNotIsInstance(cm.exception, SessionEndedEarly)
                text = str(cm.exception)
                self.assertIn("damaged: row %d is not a complete result" % n, text)
                self.assertIn(path, text)

    def test_an_incomplete_last_row_is_still_a_session_stopped_while_writing(self):
        for rows in ("setup\t0\t0\ntest\t", "setup\t0"):
            with self.subTest(rows=rows):
                self.read(rows)
                with self.assertRaises(SessionEndedEarly) as cm:
                    report_from_results(self.logs, "v", list(self.STEPS))
                self.assertIn("stopped while writing it", str(cm.exception))


class WslExtraDir(unittest.TestCase):
    """AC-5: a wsl session that fails before its script runs leaves no `<label>-extra`."""

    def setUp(self):
        self.logs = tempfile.mkdtemp(prefix="revali-extra-")
        self.addCleanup(shutil.rmtree, self.logs, True)
        self.runner = WslRunner(PlatformCfg(runner="wsl", distro="Ubuntu", command_timeout_min=1))

    def extra(self):
        return os.path.join(self.logs, LABEL + "-extra")

    def run_session(self):
        with mock.patch("revali.runners.clone_source", return_value=("D:/x/repo", "abc")):
            self.runner.run(
                "D:/x/repo", "abc", [("test", "true")], {"t/x.py": "x\n"}, self.logs, LABEL
            )

    def test_any_failing_wslpath(self):
        for failing in range(1, 5):  # source, logs, extra, then the script
            with self.subTest(call=failing):
                seen = []

                def wslpath(_self, path, log=None, failing=failing, seen=seen):
                    seen.append(path)
                    if len(seen) == failing:
                        # the extra dir exists while the session is being set up
                        assert os.path.isdir(self.extra())
                        raise RunnerError("wslpath failed for %s" % path)
                    return "/mnt/x/%d" % len(seen)

                with mock.patch.object(WslRunner, "wslpath", wslpath):
                    with self.assertRaises(RunnerError):
                        self.run_session()
                self.assertEqual(len(seen), failing)
                self.assertFalse(os.path.exists(self.extra()))

    def failing_write(self, suffix):
        """write_text that fails for the one file whose name ends with `suffix`."""
        from revali.state import write_text

        def write(path, text):
            if path.endswith(suffix):
                raise OSError("disk full")
            return write_text(path, text)

        return mock.patch("revali.runners.write_text", side_effect=write)

    def test_writing_the_script_fails(self):
        with (
            mock.patch.object(WslRunner, "wslpath", lambda _s, p, log=None: "/mnt/x"),
            self.failing_write(LABEL + ".sh"),
        ):
            with self.assertRaises(OSError):
                self.run_session()
        self.assertFalse(os.path.exists(self.extra()))

    def test_staging_the_inputs_fails(self):
        # found while writing the test above: an extra file or step command that cannot be
        # written left the directory behind as well (wsl and ssh both stage through it)
        for suffix in ("x.py", "-test.cmd"):
            with self.subTest(suffix=suffix):
                with self.failing_write(suffix):
                    with self.assertRaises(OSError):
                        self.run_session()
                self.assertFalse(os.path.exists(self.extra()))

    def test_clone_source_fails(self):
        with mock.patch("revali.runners.clone_source", side_effect=RunnerError("no clone")):
            with self.assertRaises(RunnerError):
                self.runner.run(
                    "D:/x/repo", "abc", [("test", "true")], {"t/x.py": "x\n"}, self.logs, LABEL
                )
        self.assertFalse(os.path.exists(self.extra()))


class ScriptLogNote(unittest.TestCase):
    """AC-6."""

    STEPS = [("setup", "s"), ("test", "t")]

    def setUp(self):
        self.logs = tempfile.mkdtemp(prefix="revali-note-")
        self.addCleanup(shutil.rmtree, self.logs, True)

    def ended_early(self, script_log, max_procs=4096, rows="setup\t0\t0\n"):
        with open(os.path.join(self.logs, "v.results"), "w", encoding="utf-8") as fh:
            fh.write(rows)
        path = os.path.join(self.logs, "v-script.log")
        if script_log is not None:
            with open(path, "w", encoding="utf-8", newline="") as fh:
                fh.write(script_log)
        elif os.path.exists(path):
            os.remove(path)
        with self.assertRaises(SessionEndedEarly) as cm:
            report_from_results(self.logs, "v", list(self.STEPS), max_procs=max_procs)
        return str(cm.exception), path

    def test_any_text_is_named_with_its_first_line(self):
        text, path = self.ended_early("\n  \ncp: cannot stat 'x': No such file\nsecond\n")
        self.assertIn(path, text)
        self.assertIn("begin `cp: cannot stat 'x': No such file`", text)
        self.assertNotIn("second", text)
        self.assertNotIn("max_procs", text)

    def test_the_quote_is_cut_to_200_characters(self):
        text, _ = self.ended_early("y" * 300 + "\n")
        self.assertIn("`%s`" % ("y" * 200), text)
        self.assertNotIn("y" * 201, text)

    def test_empty_or_missing_adds_nothing(self):
        plain, path = self.ended_early(None)
        self.assertNotIn(path, plain)
        for blank in ("", "\n \n"):
            with self.subTest(blank=blank):
                self.assertEqual(self.ended_early(blank)[0], plain)

    def test_a_fork_string_with_a_limit_adds_max_procs(self):
        line = "bash: fork: retry: Resource temporarily unavailable"
        text, path = self.ended_early(line + "\n", max_procs=300)
        self.assertIn(path, text)
        self.assertIn("`%s`" % line, text)
        self.assertIn("max_procs = 300", text)
        text, _ = self.ended_early(line + "\n", max_procs=0)
        self.assertIn("`%s`" % line, text)
        self.assertNotIn("max_procs", text)

    def test_also_when_no_row_came_back(self):
        text, path = self.ended_early("Killed\n", rows="")
        self.assertIn("before `setup` finished", text)
        self.assertIn(path, text)
        self.assertIn("`Killed`", text)


class Docs(unittest.TestCase):
    """AC-7."""

    def read(self, name):
        with open(os.path.join(ROOT, "docs", name), encoding="utf-8") as fh:
            return " ".join(fh.read().split())

    def test_sandbox_md(self):
        text = self.read("sandbox.md")
        self.assertIn("<label>-<id>", text)
        self.assertIn("delete them once the script has ended", text)
        self.assertIn("first non-empty line", text)

    def test_files_md(self):
        self.assertIn("<label>-<id>", self.read("files.md"))


if __name__ == "__main__":
    unittest.main()
