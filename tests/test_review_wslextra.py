"""Review tests for fix/ssh-session-dirs, AC-5: when `WslRunner.run` fails before the
sandbox script runs, `<label>-extra` (the session's extra files, staged in the logs
directory) is not left there.

The real WslRunner runs against a stand-in for wsl.exe written here: it answers `wslpath`
with a made-up path, fails the n-th `wslpath` call when asked, notes whether the extra
directory was there at that moment (so "not left" is not "never made"), and notes a `bash`
call instead of running the script. A script that cannot be written is a directory sitting
where `<label>.sh` goes. `clone_source` reports a repository it cannot resolve by returning
its arguments unchanged, so it has no failure to stage from outside.
"""

import os
import shutil
import sys
import tempfile
import unittest

from revali.config import PlatformCfg
from revali.runners import RunnerError, WslRunner
from tests.helpers import _quote

LABEL = "validate-r1"

WSL_STUB = r"""import os
import sys


def note(name, text):
    with open(os.environ[name], "w", encoding="utf-8", newline="") as fh:
        fh.write(text)


def main(argv):
    cmd = argv[3:] if argv[:1] == ["-d"] else argv
    if cmd[:1] != ["wslpath"]:
        note("WSLEXTRA_BASH", "ran")
        return 0
    count = 1
    if os.path.exists(os.environ["WSLEXTRA_COUNT"]):
        with open(os.environ["WSLEXTRA_COUNT"], "r", encoding="utf-8") as fh:
            count = int(fh.read()) + 1
    note("WSLEXTRA_COUNT", str(count))
    note("WSLEXTRA_SEEN", "yes" if os.path.isdir(os.environ["WSLEXTRA_DIR"]) else "no")
    if count == int(os.environ["WSLEXTRA_FAIL_AT"]):
        print("wslpath: %s: Invalid argument" % cmd[-1], file=sys.stderr)
        return 1
    print("/mnt/x/%d" % count)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
"""


class ExtraDirectory(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="revali-wslextra-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        env = dict(os.environ)
        self.addCleanup(os.environ.update, env)
        self.addCleanup(os.environ.clear)
        stub = os.path.join(self.tmp, "wsl_stand_in.py")
        with open(stub, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(WSL_STUB)
        os.environ["REVALI_WSL_CMD"] = "%s %s" % (_quote(sys.executable), _quote(stub))
        self.round = 0
        self.fresh(0)

    def fresh(self, fail_at):
        """A new logs directory and a stand-in that fails its `fail_at`-th wslpath (0: none)."""
        self.round += 1
        self.logs = os.path.join(self.tmp, "logs-%d" % self.round)
        self.extra = os.path.join(self.logs, LABEL + "-extra")
        for name in ("COUNT", "SEEN", "BASH"):
            os.environ["WSLEXTRA_" + name] = os.path.join(
                self.tmp, "%s-%d" % (name.lower(), self.round)
            )
        os.environ["WSLEXTRA_DIR"] = self.extra
        os.environ["WSLEXTRA_FAIL_AT"] = str(fail_at)

    def noted(self, name):
        path = os.environ["WSLEXTRA_" + name]
        if not os.path.exists(path):
            return ""
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read()

    def run_session(self):
        runner = WslRunner(
            PlatformCfg(
                name="linux",
                runner="wsl",
                distro="Ubuntu",
                command_timeout_min=1,
                sandbox_dir="~/.revali/sandbox",
            )
        )
        return runner.run(
            os.path.join(self.tmp, "no-such-repo"),
            "abc123",
            [("setup", "true"), ("test", "true")],
            {"tests/test_new.py": "x = 1\n"},
            self.logs,
            LABEL,
        )

    def test_a_failing_wslpath_leaves_no_extra_directory(self):
        # the clone source, the logs directory, the extra directory, then the script itself
        for failing in (1, 2, 3, 4):
            with self.subTest(wslpath_call=failing):
                self.fresh(failing)
                with self.assertRaises(RunnerError) as cm:
                    self.run_session()
                self.assertIn("wslpath", str(cm.exception))
                self.assertEqual(self.noted("COUNT"), str(failing))
                self.assertEqual(self.noted("SEEN"), "yes")  # it was there when the call failed
                self.assertEqual(self.noted("BASH"), "")  # the script never ran
                self.assertFalse(os.path.exists(self.extra), os.listdir(self.logs))

    def test_a_script_that_cannot_be_written_leaves_no_extra_directory(self):
        self.fresh(0)
        os.makedirs(os.path.join(self.logs, LABEL + ".sh"))  # a directory where the script goes
        with self.assertRaises((OSError, RunnerError)):
            self.run_session()
        self.assertEqual(self.noted("COUNT"), "3")  # every path was translated before the write
        self.assertEqual(self.noted("SEEN"), "yes")
        self.assertEqual(self.noted("BASH"), "")
        self.assertFalse(os.path.exists(self.extra), os.listdir(self.logs))


if __name__ == "__main__":
    unittest.main()
