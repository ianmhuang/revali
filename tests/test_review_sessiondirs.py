"""Review tests for fix/ssh-session-dirs, AC-1 to AC-3: the remote directories of an ssh
sandbox session.

AC-1: the remote clone, inbox and logs directory of a session are `<base>/<label>-<id>`,
      `<base>/<label>-<id>-in` and `<base>/<label>-<id>-logs`, `<id>` being 8 hex characters
      new for each session; two sessions of one checkout, branch and label share no remote
      path; the file names in the host's logs directory keep `<label>`.
AC-2: a session's cleanup removes only its own directories, and the shared levels only when
      they are empty; a later session neither reads nor deletes what an orphan has there.
AC-3: when the ssh call that runs the script ends with exit 255 and the session was cut
      short, the message names the session's three directories on the host and, when they
      are left there, says to delete them once the script has ended.

The real SshRunner runs against the repository's stand-ins for ssh and scp; the fake host is
a directory. The orphan is made the way a real one comes about: a first session loses its
connection and its cleanup cannot reach the host, so its directories stay; the test then
puts into them what a script that is still running would have there (a clone it works in,
result rows it went on to write) and runs a second session of the same checkout, branch and
label.
"""

import os
import re
import unittest

from revali.runners import RunnerError, SshRunner
from tests.helpers import git
from tests.test_ssh_runner import HAVE_BASH, SshCase, plat

LABEL = "validate-r1"
SCOPE = "feature__mul"
KEY = "0123abcd"
BRANCH_LEVEL = "%s-%s" % (SCOPE, KEY)
BASE_REL = ".revali/sandbox/sample/%s" % BRANCH_LEVEL  # <base>, relative to the remote home
OWN = r"%s-[0-9a-f]{8}" % re.escape(LABEL)


class SessionCase(SshCase):
    def setUp(self):
        super().setUp()
        os.environ.pop("REVALI_FAKE_SSH_SCRIPT_DROP", None)
        self.head = git(["rev-parse", "HEAD"], self.repo).strip()
        self.logs = os.path.join(self.tmp, "session-logs")

    def session(self, steps):
        return SshRunner(plat()).run(
            self.repo, self.head, steps, {}, self.logs, LABEL, scope=SCOPE, key=KEY
        )

    def lost(self, steps):
        """A session whose connection goes away while the script runs: the message."""
        os.environ["REVALI_FAKE_SSH_SCRIPT_DROP"] = "0"
        try:
            with self.assertRaises(RunnerError) as cm:
                self.session(steps)
        finally:
            os.environ.pop("REVALI_FAKE_SSH_SCRIPT_DROP", None)
        return str(cm.exception)

    def base_dir(self, *parts):
        return os.path.join(self.remote, ".revali", "sandbox", "sample", BRANCH_LEVEL, *parts)

    def names_under_base(self, texts):
        """The first path component below <base> of every remote path in `texts`."""
        found = set()
        for text in texts:
            found.update(re.findall(r"%s/([A-Za-z0-9._-]+)" % re.escape(BRANCH_LEVEL), text))
        return found

    def call_texts(self, since=0):
        return [arg for _exe, argv in self.calls()[since:] for arg in argv]

    def own_name(self, since=0):
        """`<label>-<id>` of the one session in the calls from `since` on, read off the upload."""
        uploads = [argv[-1] for exe, argv in self.calls()[since:] if exe == "scp"][:1]
        self.assertEqual(len(uploads), 1, self.calls()[since:])
        match = re.fullmatch(r"box:%s/(%s)-in/" % (re.escape(BASE_REL), OWN), uploads[0])
        self.assertIsNotNone(match, uploads[0])
        return match.group(1)

    def assert_names_the_directories(self, text, own):
        for suffix in ("", "-in", "-logs"):
            path = "%s/%s%s" % (BASE_REL, own, suffix)
            # the whole name, not the start of a longer one
            self.assertRegex(text, re.escape(path) + r"(?![A-Za-z0-9_-])")
        self.assertIn("box", text)


@unittest.skipUnless(HAVE_BASH, "needs bash")
class OwnDirectories(SessionCase):
    def test_every_remote_path_of_a_session_carries_its_id(self):  # AC-1, AC-2
        report = self.session([("setup", "true"), ("test", "true")])
        self.assertTrue(report.ok, [(s.name, s.returncode) for s in report.steps])
        own = self.own_name()
        calls = self.calls()
        self.assertEqual([c[0] for c in calls], ["ssh", "scp", "ssh", "scp", "ssh"])
        mine = {own, own + "-in", own + "-logs"}
        # nothing the session creates, uploads, runs, fetches or removes sits anywhere else
        self.assertEqual(self.names_under_base(self.call_texts()), mine)
        with open(os.path.join(self.logs, LABEL + ".sh"), encoding="utf-8") as fh:
            script = fh.read()
        self.assertEqual(self.names_under_base([script]), mine)
        self.assertIn('SB="$HOME/%s/%s"' % (BASE_REL, own), script)
        self.assertIn('LOGS="$HOME/%s/%s-logs"' % (BASE_REL, own), script)
        self.assertIn("%s/%s-in" % (BASE_REL, own), calls[0][1][-1])
        self.assertIn("%s/%s-logs" % (BASE_REL, own), calls[0][1][-1])
        self.assertIn("%s/%s-in/%s.sh" % (BASE_REL, own, LABEL), calls[2][1][-1])
        self.assertEqual(calls[3][1][-2], "box:%s/%s-logs/." % (BASE_REL, own))
        # the names in the host's logs directory do not change
        names = os.listdir(self.logs)
        for name in (
            LABEL + ".results",
            LABEL + ".sh",
            LABEL + "-clone.log",
            LABEL + "-setup.log",
            LABEL + "-test.log",
        ):
            self.assertIn(name, names)
        sid = own[len(LABEL) + 1 :]
        self.assertEqual([n for n in names if sid in n], [])
        # its cleanup took its directories and the shared levels, which were empty
        self.assertEqual(os.listdir(os.path.join(self.remote, ".revali", "sandbox")), [])

    def test_two_sessions_of_one_checkout_branch_and_label_share_no_path(self):  # AC-1
        self.session([("test", "true")])
        first = self.names_under_base(self.call_texts())
        mark = len(self.calls())
        self.session([("test", "true")])
        second = self.names_under_base(self.call_texts(mark))
        for names in (first, second):
            self.assertEqual(len(names), 3, names)
            for name in names:
                self.assertRegex(name, r"^%s(-in|-logs)?$" % OWN)
        self.assertEqual(first & second, set())
        # the id is not a function of the checkout, the branch and the label
        self.assertNotEqual(self.own_name(), self.own_name(mark))


@unittest.skipUnless(HAVE_BASH, "needs bash")
class Orphan(SessionCase):
    def leave_an_orphan(self):
        """A first session whose connection went away and whose cleanup could not reach the
        host. Returns (its message, its `<label>...` name, {path: content} of what a script
        still running has in its directories)."""
        os.environ["REVALI_FAKE_SSH_RM_FAILS"] = "1"
        try:
            text = self.lost([("setup", "true"), ("test", "true")])
        finally:
            os.environ.pop("REVALI_FAKE_SSH_RM_FAILS", None)
        inboxes = [n for n in os.listdir(self.base_dir()) if n.endswith("-in")]
        self.assertEqual(len(inboxes), 1, os.listdir(self.base_dir()))
        name = inboxes[0][: -len("-in")]
        self.assertTrue(os.path.isdir(self.base_dir(name + "-logs")), os.listdir(self.base_dir()))
        # the script is still at work: its clone is there, and it goes on writing its rows
        added = {
            self.base_dir(name, "repo", "busy"): "x",
            self.base_dir(name + "-logs", LABEL + ".results"): "setup\t0\t0\ntest\t0\t0\n",
        }
        for path, content in added.items():
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8", newline="") as fh:
                fh.write(content)
        files = {}
        for root, _dirs, found in os.walk(self.base_dir()):
            for f in found:
                path = os.path.join(root, f)
                with open(path, "rb") as fh:
                    files[path] = fh.read()
        return text, name, files

    def test_a_later_session_neither_reads_nor_deletes_it(self):  # AC-1, AC-2
        _text, name, files = self.leave_an_orphan()
        mark = len(self.calls())
        report = self.session([("setup", "true"), ("test", "exit 3")])
        # the orphan's rows say `test` passed; this session's own `test` failed
        self.assertFalse(report.ok)
        self.assertEqual(report.failed.name, "test")
        self.assertEqual(report.failed.returncode, 3)
        self.assertNotEqual(self.own_name(mark), name)
        for path, content in files.items():
            self.assertTrue(os.path.isfile(path), path)
            with open(path, "rb") as fh:
                self.assertEqual(fh.read(), content, path)
        # the session removed its own directories; the shared levels stay, they are not empty
        self.assertEqual(
            sorted(os.listdir(self.base_dir())), sorted([name, name + "-in", name + "-logs"])
        )

    def test_the_lost_session_says_what_it_left_on_the_host(self):  # AC-3
        text, name, _files = self.leave_an_orphan()
        self.assertRegex(name, "^%s$" % OWN)
        self.assertEqual(name, self.own_name())
        self.assertRegex(text, r"(?i)may still be running")
        self.assert_names_the_directories(text, name)
        self.assertRegex(text, r"(?i)\bdelete\b[^.]{0,60}\bonce\b[^.]{0,60}\bscript\b")

    def test_a_lost_session_names_its_directories_when_it_could_remove_them(self):  # AC-3
        text = self.lost([("setup", "true"), ("test", "true")])
        self.assertRegex(text, r"(?i)may still be running")
        self.assert_names_the_directories(text, self.own_name())
        self.assertEqual(self.remote_leftovers(), [])


if __name__ == "__main__":
    unittest.main()
