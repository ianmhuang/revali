"""Review tests for fix/host-bash-in-stubs: the bash the wsl / ssh stand-ins run scripts with.

The host is made up: `which` answers from a table and `isfile` from a list, so the Windows
cases run the same on Linux. Paths are compared the way Windows compares them (case and
slash direction do not matter).

AC-1: on Windows the bash is Git Bash, found from the git on PATH (`<Git>\\bin\\bash.exe`,
      else `<Git>\\usr\\bin\\bash.exe`), with a WindowsApps `bash.exe` on PATH or no bash on
      PATH at all.
AC-2: a bash on PATH is used on Windows only when it is not under a `WindowsApps`
      directory; with nothing usable the stubs print one line naming what was looked for
      and exit 1. Round 1 added two things to this: the same launcher in the Windows system
      directory (`%SystemRoot%\\System32`) is refused as well, and off Windows the line
      names only the PATH lookup that was made there.
AC-3: elsewhere the answer is the `bash` on PATH, as before.
AC-4: wsl_stub, ssh_stub and (through wsl_stub) wsl_flaky_stub run the bash the one helper
      found; no stub looks bash up on its own.
"""

import contextlib
import io
import ntpath
import os
import re
import shutil
import sys
import tempfile
import unittest
from unittest import mock

from tests.helpers import FAKE_BIN

sys.path.insert(0, FAKE_BIN)

import hostbash  # noqa: E402
import ssh_stub  # noqa: E402
import wsl_flaky_stub  # noqa: E402
import wsl_stub  # noqa: E402

# not the default install directory, and with a space in it
GIT = r"D:\Dev Tools\Git"
GIT_ON_PATH = GIT + r"\cmd\git.exe"
BIN_BASH = GIT + r"\bin\bash.exe"
USR_BIN_BASH = GIT + r"\usr\bin\bash.exe"
WINDOWSAPPS_BASH = r"C:\Users\reviewer\AppData\Local\Microsoft\WindowsApps\bash.exe"
OTHER_BASH = r"C:\msys64\usr\bin\bash.exe"
# the made-up host's Windows directory, handed to the lookup so this machine's does not matter
SYSTEM_ROOT = r"C:\Windows"
SYSTEM32_BASH = SYSTEM_ROOT + r"\System32\bash.exe"

# what the stubs read from the environment; cleared so a caller's settings do not leak in
KNOBS = (
    "REVALI_FAKE_LOG",
    "REVALI_FAKE_REMOTE",
    "REVALI_FAKE_SSH_DOWN",
    "REVALI_FAKE_SSH_BASH_FAILS",
    "REVALI_FAKE_WSL_PLAN",
    "REVALI_FAKE_WSL_CALLS",
)


def norm(path):
    return ntpath.normcase(ntpath.normpath(str(path)))


def lookup(platform, on_path, files=(), system_root=SYSTEM_ROOT):
    """find_bash on a made-up host. `on_path` maps a command to what PATH resolves it to,
    `files` are the files that exist, `system_root` is its Windows directory. `platform`
    None leaves it to sys.platform.
    Returns (the answer, the commands PATH was asked for)."""
    existing = {norm(f) for f in files}
    asked = []

    def which(cmd, *args, **kwargs):
        name = str(cmd).lower()
        if name.endswith(".exe"):
            name = name[:-4]
        asked.append(name)
        return on_path.get(name)

    def isfile(path):
        return norm(path) in existing

    kwargs = {"which": which, "isfile": isfile, "system_root": system_root}
    if platform is not None:
        kwargs["platform"] = platform
    return hostbash.find_bash(**kwargs), asked


class PathCase(unittest.TestCase):
    def assertSamePath(self, got, want):
        self.assertIsNotNone(got, "expected %s, found no bash" % want)
        self.assertEqual(norm(got), norm(want))


class GitBashOnWindows(PathCase):
    """AC-1"""

    def test_git_bash_wins_when_the_windowsapps_launcher_is_the_bash_on_path(self):
        got, _ = lookup(
            "win32",
            {"git": GIT_ON_PATH, "bash": WINDOWSAPPS_BASH},
            [BIN_BASH, USR_BIN_BASH],
        )
        self.assertSamePath(got, BIN_BASH)

    def test_git_bash_is_found_with_no_bash_on_path(self):
        got, _ = lookup("win32", {"git": GIT_ON_PATH}, [BIN_BASH, USR_BIN_BASH])
        self.assertSamePath(got, BIN_BASH)

    def test_usr_bin_bash_when_bin_has_none(self):
        got, _ = lookup("win32", {"git": GIT_ON_PATH, "bash": WINDOWSAPPS_BASH}, [USR_BIN_BASH])
        self.assertSamePath(got, USR_BIN_BASH)

    def test_git_bash_wins_over_another_bash_on_path(self):
        got, _ = lookup("win32", {"git": GIT_ON_PATH, "bash": OTHER_BASH}, [BIN_BASH])
        self.assertSamePath(got, BIN_BASH)

    def test_git_resolved_with_an_upper_case_extension(self):
        # shutil.which on Windows appends the extension as PATHEXT spells it
        got, _ = lookup("win32", {"git": GIT + r"\cmd\git.EXE"}, [BIN_BASH])
        self.assertSamePath(got, BIN_BASH)

    def test_sys_platform_decides_when_no_platform_is_given(self):
        with mock.patch.object(sys, "platform", "win32"):
            got, _ = lookup(None, {"git": GIT_ON_PATH, "bash": WINDOWSAPPS_BASH}, [BIN_BASH])
        self.assertSamePath(got, BIN_BASH)


class PathBashOnWindows(PathCase):
    """AC-2, the lookup"""

    def test_a_bash_outside_windowsapps_is_used_without_git(self):
        got, _ = lookup("win32", {"bash": OTHER_BASH})
        self.assertSamePath(got, OTHER_BASH)

    def test_a_bash_outside_windowsapps_is_used_when_git_has_no_bash_beside_it(self):
        got, _ = lookup("win32", {"git": GIT_ON_PATH, "bash": OTHER_BASH})
        self.assertSamePath(got, OTHER_BASH)

    def test_the_windowsapps_launcher_is_never_the_answer(self):
        cases = {
            "alone": {"bash": WINDOWSAPPS_BASH},
            "git without a bash beside it": {"git": GIT_ON_PATH, "bash": WINDOWSAPPS_BASH},
            "lower case": {"bash": WINDOWSAPPS_BASH.lower()},
            "upper case": {"bash": WINDOWSAPPS_BASH.upper()},
            "forward slashes": {"bash": WINDOWSAPPS_BASH.replace("\\", "/")},
            "the packaged apps directory": {
                "bash": r"C:\Program Files\WindowsApps\Vendor.Shell_1.0_x64\bash.exe"
            },
        }
        for label, on_path in cases.items():
            with self.subTest(case=label):
                got, _ = lookup("win32", on_path)
                self.assertIsNone(got)

    def test_a_directory_that_only_contains_the_word_is_not_windowsapps(self):
        bash = r"C:\Tools\WindowsAppsBackup\bin\bash.exe"
        got, _ = lookup("win32", {"bash": bash})
        self.assertSamePath(got, bash)

    def test_nothing_at_all(self):
        got, _ = lookup("win32", {})
        self.assertIsNone(got)


class System32LauncherOnWindows(PathCase):
    """AC-2 as extended by round 1 F1: the inbox WSL component keeps the same launcher in the
    Windows system directory, which PATH lists before WindowsApps."""

    def test_the_system32_launcher_is_never_the_answer(self):
        cases = {
            "alone": ({"bash": SYSTEM32_BASH}, SYSTEM_ROOT),
            "git without a bash beside it": (
                {"git": GIT_ON_PATH, "bash": SYSTEM32_BASH},
                SYSTEM_ROOT,
            ),
            # PATH usually spells the directory C:\WINDOWS\system32
            "as PATH spells it": ({"bash": r"C:\WINDOWS\system32\bash.exe"}, SYSTEM_ROOT),
            "forward slashes": ({"bash": SYSTEM32_BASH.replace("\\", "/")}, SYSTEM_ROOT),
            "the system root in another case": ({"bash": SYSTEM32_BASH}, r"c:\WINDOWS"),
            "the 32-bit system directory": (
                {"bash": SYSTEM_ROOT + r"\SysWOW64\bash.exe"},
                SYSTEM_ROOT,
            ),
            "a Windows directory elsewhere": ({"bash": r"E:\WinNT\System32\bash.exe"}, r"E:\WinNT"),
        }
        for label, (on_path, system_root) in cases.items():
            with self.subTest(case=label):
                got, _ = lookup("win32", on_path, system_root=system_root)
                self.assertIsNone(got)

    def test_git_bash_wins_over_the_system32_launcher(self):
        got, _ = lookup("win32", {"git": GIT_ON_PATH, "bash": SYSTEM32_BASH}, [BIN_BASH])
        self.assertSamePath(got, BIN_BASH)

    def test_a_bash_outside_the_system_directory_itself_is_still_used(self):
        cases = {
            "a directory below it": SYSTEM_ROOT + r"\System32\tools\bash.exe",
            "a directory of the same name elsewhere": r"D:\Tools\System32\bash.exe",
        }
        for label, bash in cases.items():
            with self.subTest(case=label):
                got, _ = lookup("win32", {"bash": bash})
                self.assertSamePath(got, bash)


class Elsewhere(unittest.TestCase):
    """AC-3"""

    def test_the_bash_on_path_and_nothing_else(self):
        for platform in ("linux", "darwin"):
            with self.subTest(platform=platform):
                got, asked = lookup(
                    platform,
                    {"git": "/opt/Git/cmd/git", "bash": "/usr/local/bin/bash"},
                    ["/opt/Git/bin/bash.exe", "/opt/Git/usr/bin/bash.exe"],
                )
                self.assertEqual(got, "/usr/local/bin/bash")
                self.assertNotIn("git", asked)

    def test_the_answer_is_not_filtered(self):
        bash = "/mnt/c/Users/reviewer/AppData/Local/Microsoft/WindowsApps/bash"
        got, _ = lookup("linux", {"bash": bash})
        self.assertEqual(got, bash)

    def test_none_when_path_has_no_bash(self):
        got, _ = lookup("linux", {"git": "/usr/bin/git"}, ["/usr/bin/bash.exe"])
        self.assertIsNone(got)

    def test_sys_platform_decides_when_no_platform_is_given(self):
        with mock.patch.object(sys, "platform", "linux"):
            got, asked = lookup(None, {"git": GIT_ON_PATH, "bash": "/bin/bash"}, [BIN_BASH])
        self.assertEqual(got, "/bin/bash")
        self.assertNotIn("git", asked)


class OnThisHost(unittest.TestCase):
    """AC-1 to AC-3 with nothing faked: whatever this machine has."""

    def test_the_real_lookup(self):
        got = hostbash.find_bash()
        if sys.platform != "win32":
            self.assertEqual(got, shutil.which("bash"))
            return
        if got is not None:
            self.assertNotIn("windowsapps", norm(got).split("\\"))
            root = os.environ.get("SystemRoot")
            if root:
                self.assertNotEqual(norm(ntpath.dirname(got)), norm(ntpath.join(root, "System32")))
            self.assertTrue(os.path.isfile(got), got)


class Stubs(unittest.TestCase):
    """AC-2 (the exit), AC-4"""

    WSL_ARGV = ["-d", "Ubuntu", "-e", "bash", "run.sh"]
    SSH_ARGV = ["-o", "BatchMode=yes", "host", "bash", "run.sh"]

    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="review-hostbash-")
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        env = mock.patch.dict(os.environ, {"REVALI_HOME": self.home})
        env.start()
        self.addCleanup(env.stop)
        for name in KNOBS:
            os.environ.pop(name, None)

    def entries(self):
        return (
            ("wsl_stub", wsl_stub.main, self.WSL_ARGV),
            ("wsl_flaky_stub", wsl_flaky_stub.main, self.WSL_ARGV),
            ("ssh_stub", ssh_stub.main, self.SSH_ARGV),
        )

    def call(self, entry, argv, found, platform=None):
        """A stub's entry point with the helper answering `found` and no process started.
        Returns (exit code, stderr, the command lines it wanted to start)."""
        err = io.StringIO()
        with contextlib.ExitStack() as stack:
            stack.enter_context(contextlib.redirect_stderr(err))
            stack.enter_context(mock.patch.object(hostbash, "find_bash", return_value=found))
            run = stack.enter_context(
                mock.patch("subprocess.run", return_value=mock.Mock(returncode=0))
            )
            if platform:
                stack.enter_context(mock.patch.object(sys, "platform", platform))
            code = entry(list(argv))
        return code, err.getvalue(), [list(c.args[0]) for c in run.call_args_list]

    def test_each_stub_runs_the_bash_the_helper_found(self):
        found = os.path.join(self.home, "the-bash-the-helper-found")
        for name, entry, argv in self.entries():
            with self.subTest(stub=name):
                code, _, started = self.call(entry, argv, found)
                self.assertEqual(code, 0)
                self.assertEqual(len(started), 1, started)
                self.assertEqual(started[0][0], found)

    def test_without_a_bash_each_stub_says_so_in_one_line_and_exits_1(self):
        for name, entry, argv in self.entries():
            with self.subTest(stub=name):
                code, err, started = self.call(entry, argv, None, platform="win32")
                self.assertEqual(code, 1)
                self.assertEqual(started, [])
                lines = [line for line in err.splitlines() if line.strip()]
                self.assertEqual(len(lines), 1, err)
                said = lines[0].lower()
                for looked_for in ("bash", "git", "windowsapps"):
                    self.assertIn(looked_for, said)

    def test_off_windows_the_line_names_only_the_path_lookup(self):
        # round 1 F2: git and WindowsApps are not looked at there, so the line names neither
        for platform in ("linux", "darwin"):
            for name, entry, argv in self.entries():
                with self.subTest(platform=platform, stub=name):
                    code, err, started = self.call(entry, argv, None, platform=platform)
                    self.assertEqual(code, 1)
                    self.assertEqual(started, [])
                    lines = [line for line in err.splitlines() if line.strip()]
                    self.assertEqual(len(lines), 1, err)
                    said = lines[0].lower()
                    for looked_for in ("bash", "path"):
                        self.assertIn(looked_for, said)
                    for not_looked_for in ("git", "windowsapps", "system32"):
                        self.assertNotIn(not_looked_for, said)

    def test_no_stub_looks_bash_up_on_its_own(self):
        helper = os.path.basename(hostbash.__file__)
        own_lookup = re.compile(r"""which\(\s*["']bash(\.exe)?["']""")
        stubs = sorted(n for n in os.listdir(FAKE_BIN) if n.endswith(".py") and n != helper)
        for expected in ("wsl_stub.py", "ssh_stub.py", "wsl_flaky_stub.py"):
            self.assertIn(expected, stubs)
        for name in stubs:
            with self.subTest(stub=name):
                with open(os.path.join(FAKE_BIN, name), "r", encoding="utf-8") as fh:
                    self.assertIsNone(own_lookup.search(fh.read()))


if __name__ == "__main__":
    unittest.main()
