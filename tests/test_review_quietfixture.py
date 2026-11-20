"""macos-ci AC-1: building the test fixture starts no git maintenance and no git gc.

git starts `git maintenance run --auto` after a commit, and the receiving end of a push
starts it (or `git gc --auto`) in the remote; either may go on writing into the build while
it is copied. The build is made here under a global git configuration that asks for both,
with git's own trace (`GIT_TRACE2_EVENT`) on, which every git process of the build writes
to, the ones git starts itself included. The trace must name the commands the build runs
and neither `maintenance` nor `gc`.
"""

import json
import os
import re
import subprocess
import tempfile
import unittest
from unittest import mock

from tests.fixtures import make_sample_repo
from tests.helpers import rmtree_force

# Asks for what the build must not start. Nothing detaches, so on a builder that does start
# it the process is over before the test removes its directory.
ASKING = (
    "[maintenance]\n\tauto = true\n\tautoDetach = false\n"
    "[gc]\n\tauto = 1\n\tautoDetach = false\n"
    "[receive]\n\tautogc = true\n"
)
BACKGROUND = ("maintenance", "gc")


def git_version():
    out = subprocess.run(
        ["git", "version"], capture_output=True, text=True, encoding="utf-8", check=True
    ).stdout
    m = re.search(r"(\d+)\.(\d+)", out)
    return (int(m.group(1)), int(m.group(2))) if m else (0, 0)


def events(path):
    out = []
    if not os.path.isfile(path):
        return out
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                item = json.loads(line)
            except ValueError:
                continue
            if isinstance(item, dict):
                out.append(item)
    return out


@unittest.skipIf(git_version() < (2, 32), "needs maintenance.auto and GIT_CONFIG_GLOBAL")
class NoBackgroundGit(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="revali quiet "))
        self.addCleanup(rmtree_force, self.tmp)
        self.config = os.path.join(self.tmp, "gitconfig")
        with open(self.config, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(ASKING)

    def traced_build(self, name, with_remote, with_branch):
        trace = os.path.join(self.tmp, name + ".trace")
        env = {"GIT_CONFIG_GLOBAL": self.config, "GIT_TRACE2_EVENT": trace}
        with mock.patch.dict(os.environ, env):
            info = make_sample_repo.build(
                os.path.join(self.tmp, name), with_remote, with_branch, "local"
            )
        return info, events(trace)

    def commands(self, trace):
        return {str(e.get("name")) for e in trace if e.get("event") == "cmd_name"}

    def started(self, trace):
        """The command lines git itself started, as its `child_start` events give them."""
        out = []
        for e in trace:
            if e.get("event") == "child_start" and isinstance(e.get("argv"), list):
                out.append([str(a) for a in e["argv"]])
        return out

    def assert_no_background(self, trace):
        names = self.commands(trace)
        for name in BACKGROUND:
            self.assertNotIn(name, names, "the build ran `git %s`" % name)
        for argv in self.started(trace):
            self.assertFalse([a for a in argv if a in BACKGROUND], argv)

    def test_a_build_with_a_remote_and_a_branch(self):
        info, trace = self.traced_build("full", True, True)
        names = self.commands(trace)
        # the trace is real: it has the build's own commands and the push's receiving end
        for name in ("init", "commit", "push", "receive-pack"):
            self.assertIn(name, names)
        self.assert_no_background(trace)
        # and the build is what it was: both commits, and main in the remote
        log = subprocess.run(
            ["git", "log", "--format=%s", "feature/mul"],
            cwd=info["repo"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
        ).stdout.split("\n")
        self.assertEqual(log[:2], ["Add mul", "Initial sample project"])
        refs = subprocess.run(
            ["git", "--git-dir", info["remote"], "for-each-ref", "--format=%(refname)"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
        ).stdout.split()
        self.assertEqual(refs, ["refs/heads/main"])

    def test_a_build_without_a_remote_or_a_branch(self):
        info, trace = self.traced_build("bare bones", False, False)
        self.assertIsNone(info["remote"])
        self.assertIn("commit", self.commands(trace))
        self.assert_no_background(trace)

    def test_the_configuration_here_does_ask_for_it(self):
        # the control: a commit made the plain way under the same configuration starts
        # maintenance, so its absence above is the builder's doing
        repo = os.path.join(self.tmp, "control")
        os.makedirs(repo)
        trace = os.path.join(self.tmp, "control.trace")
        env = dict(os.environ)
        env.update({"GIT_CONFIG_GLOBAL": self.config, "GIT_TRACE2_EVENT": trace})
        for args in (
            ["init", "-q", "-b", "main"],
            ["-c", "user.email=t@example.invalid", "-c", "user.name=t"]
            + ["-c", "commit.gpgsign=false", "commit", "-q", "--allow-empty", "-m", "one"],
        ):
            subprocess.run(["git"] + args, cwd=repo, env=env, check=True, capture_output=True)
        names = self.commands(events(trace))
        self.assertIn("commit", names)
        self.assertTrue(names & set(BACKGROUND), names)


if __name__ == "__main__":
    unittest.main()
