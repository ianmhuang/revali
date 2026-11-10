"""Review tests for fix/ssh-session-dirs, AC-4 and AC-6: how a results file that stops
early or holds a broken row is read.

AC-4: an incomplete results row means "the session was stopped while writing it"
      (`SessionEndedEarly`) only when it is the last row; an incomplete row with rows after
      it is a sandbox error saying the results file is damaged, naming the row and the file,
      and the ssh runner does not add its "may still be running" note to it.
AC-6: a session that ended early names `<label>-script.log` and quotes its first non-empty
      line, cut to 200 characters, whenever that file is not empty; the `max_procs` sentence
      comes only with one of the fork strings and `max_procs` > 0; an empty or missing file
      adds nothing.

The rows are read by `report_from_results`, the function both the wsl and the ssh runner
hand their logs directory to. The ssh case runs the real SshRunner against the repository's
stand-ins: a step writes a broken row ahead of its own, and the stand-in then drops the
connection (exit 255).
"""

import os
import shutil
import tempfile
import unittest

from revali.runners import RunnerError, SessionEndedEarly, SshRunner, report_from_results
from tests.helpers import git
from tests.test_ssh_runner import HAVE_BASH, SshCase, plat

LABEL = "validate-r1"
STEPS = [("setup", "make setup"), ("test", "make test")]
FORK_FAIL = "bash: fork: retry: Resource temporarily unavailable"


class RowsCase(unittest.TestCase):
    def setUp(self):
        self.logs = tempfile.mkdtemp(prefix="revali-earlyrows-")
        self.addCleanup(shutil.rmtree, self.logs, True)
        self.results = os.path.join(self.logs, LABEL + ".results")
        self.script_log = os.path.join(self.logs, LABEL + "-script.log")

    def put(self, path, text):
        with open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)

    def refused(self, rows, script_log=None, max_procs=0):
        """The error `report_from_results` raises for these rows and this script log."""
        self.put(self.results, rows)
        if script_log is None:
            if os.path.exists(self.script_log):
                os.remove(self.script_log)
        else:
            self.put(self.script_log, script_log)
        with self.assertRaises(RunnerError) as cm:
            report_from_results(self.logs, LABEL, list(STEPS), max_procs=max_procs)
        return cm.exception


class BrokenRows(RowsCase):
    """AC-4."""

    def test_only_a_broken_last_row_is_a_session_stopped_while_writing(self):
        # the same cut-off row, last and then with a row after it
        last = self.refused("setup\t0\t0\ntest\t")
        self.assertIsInstance(last, SessionEndedEarly)
        self.assertRegex(str(last), r"(?i)stopped while writing")
        followed = self.refused("setup\t0\ntest\t0\t0\n")
        self.assertNotIsInstance(followed, SessionEndedEarly)
        self.assertRegex(str(followed), r"(?i)damaged")
        self.assertNotRegex(str(followed), r"(?i)stopped while writing")

    def test_a_damaged_file_names_the_row_and_the_file(self):
        cases = (
            ("setup\t0\nsetup\t0\t0\ntest\t0\t0\n", 1),
            ("setup\t0\t0\ntest\tx\t0\nmore\t0\t0\n", 2),
            ("setup\t0\t0\ntest\t0\t7\ntest\t", 2),  # two broken rows: the first has one after it
        )
        for rows, number in cases:
            with self.subTest(rows=rows):
                exc = self.refused(rows)
                self.assertNotIsInstance(exc, SessionEndedEarly)
                text = str(exc)
                self.assertRegex(text, r"(?i)damaged")
                self.assertRegex(text, r"(?i)\brow %d\b" % number)
                self.assertIn(self.results, text)


@unittest.skipUnless(HAVE_BASH, "needs bash")
class BrokenRowsOverSsh(SshCase):
    """AC-4: the ssh runner does not call a damaged file a session that was cut short."""

    # the step runs in <clone>/repo, and the remote logs directory is <clone>-logs
    JUNK = 'printf \'junk\\n\' >> "$(dirname "$PWD")-logs/%s.results"' % LABEL

    def lost(self, steps, keep):
        os.environ["REVALI_FAKE_SSH_SCRIPT_DROP"] = str(keep)
        head = git(["rev-parse", "HEAD"], self.repo).strip()
        logs = os.path.join(self.tmp, "session-logs")
        with self.assertRaises(RunnerError) as cm:
            SshRunner(plat()).run(self.repo, head, steps, {}, logs, LABEL, scope="feature__mul")
        with open(os.path.join(logs, LABEL + ".results"), encoding="utf-8") as fh:
            return cm.exception, fh.read()

    def test_a_broken_row_with_rows_after_it_is_not_a_lost_session(self):
        exc, rows = self.lost([("setup", self.JUNK), ("test", "true")], keep=2)
        self.assertEqual(rows, "junk\nsetup\t0\t0\n")  # what came back from the host
        self.assertNotIsInstance(exc, SessionEndedEarly)
        self.assertRegex(str(exc), r"(?i)damaged")
        self.assertRegex(str(exc), r"(?i)\brow 1\b")
        self.assertNotRegex(str(exc), r"(?i)still (be )?running")

    def test_a_broken_last_row_is_still_a_lost_session(self):
        exc, rows = self.lost([("setup", "true"), ("test", self.JUNK)], keep=2)
        self.assertEqual(rows, "setup\t0\t0\njunk\n")
        self.assertRegex(str(exc), r"(?i)stopped while writing")
        self.assertRegex(str(exc), r"(?i)may still be running")
        # fix/ssh-session-dirs AC-3: and it names what it had on the host
        self.assertRegex(str(exc), r"feature__mul/%s-[0-9a-f]{8}-logs" % LABEL)


class ScriptLog(RowsCase):
    """AC-6."""

    EARLY = ("", "setup\t0\t0\n")  # no row at all; the rows stop after a passing step

    def test_any_text_is_named_and_quoted_and_an_empty_file_adds_nothing(self):
        for rows in self.EARLY:
            with self.subTest(rows=rows):
                plain = self.refused(rows)
                self.assertIsInstance(plain, SessionEndedEarly)
                self.assertNotIn(self.script_log, str(plain))
                self.assertEqual(str(self.refused(rows, "")), str(plain))
                exc = self.refused(rows, "cp: cannot stat 'x': No such file or directory\n", 300)
                self.assertIsInstance(exc, SessionEndedEarly)
                text = str(exc)
                self.assertIn(self.script_log, text)
                self.assertIn("cp: cannot stat 'x': No such file or directory", text)
                self.assertNotIn("max_procs", text)
                # what the message said before is still said
                self.assertTrue(text.startswith(str(plain)), text)

    def test_the_first_line_that_is_not_empty_is_the_one_quoted(self):
        text = str(self.refused("setup\t0\t0\n", "\n\nKilled by signal 9\nlater trouble\n"))
        self.assertIn(self.script_log, text)
        self.assertIn("Killed by signal 9", text)
        self.assertNotIn("later trouble", text)

    def test_the_quote_is_cut_to_200_characters(self):
        text = str(self.refused("setup\t0\t0\n", "q" * 199 + "Z" + "q" * 100 + "\n"))
        self.assertIn("q" * 199 + "Z", text)
        self.assertNotIn("q" * 199 + "Zq", text)  # the 201st character is not there
        short = str(self.refused("setup\t0\t0\n", "q" * 199 + "Z\n"))
        self.assertIn("q" * 199 + "Z", short)

    def test_max_procs_needs_a_fork_string_and_a_limit(self):
        for rows in self.EARLY:
            with self.subTest(rows=rows):
                both = str(self.refused(rows, FORK_FAIL + "\n", 300))
                self.assertIn(self.script_log, both)
                self.assertIn(FORK_FAIL, both)
                self.assertRegex(both, r"max_procs = 300\b")
                no_limit = str(self.refused(rows, FORK_FAIL + "\n", 0))
                self.assertIn(self.script_log, no_limit)
                self.assertIn(FORK_FAIL, no_limit)
                self.assertNotIn("max_procs", no_limit)
                no_string = str(self.refused(rows, "Killed\n", 300))
                self.assertIn(self.script_log, no_string)
                self.assertIn("Killed", no_string)
                self.assertNotIn("max_procs", no_string)

    def test_a_fork_string_on_a_later_line_still_names_max_procs(self):
        # the quote is the first line; the strings are looked for in the whole file
        text = str(self.refused("setup\t0\t0\n", "first trouble\n%s\n" % FORK_FAIL, 300))
        self.assertIn("first trouble", text)
        self.assertRegex(text, r"max_procs = 300\b")


if __name__ == "__main__":
    unittest.main()
