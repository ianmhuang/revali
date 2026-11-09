"""The bash the wsl / ssh stubs run scripts with (fix/host-bash-in-stubs): Git Bash on Windows,
found from the git on PATH, never the WindowsApps WSL launcher; `bash` on PATH elsewhere."""

import contextlib
import io
import os
import sys
import unittest
from unittest import mock

from tests.helpers import FAKE_BIN

sys.path.insert(0, FAKE_BIN)

import hostbash  # noqa: E402
import ssh_stub  # noqa: E402
import wsl_stub  # noqa: E402

GIT = r"C:\Program Files\Git"
WINDOWSAPPS_BASH = r"C:\Users\someone\AppData\Local\Microsoft\WindowsApps\bash.exe"


def finder(on_path, files=()):
    """`which` and `isfile` fakes: `on_path` maps a command to its path, `files` exist."""
    existing = {f.lower() for f in files}
    return (lambda cmd: on_path.get(cmd)), (lambda path: path.lower() in existing)


def find(platform, on_path, files=(), system_root=r"C:\Windows"):
    which, isfile = finder(on_path, files)
    return hostbash.find_bash(
        platform=platform, which=which, isfile=isfile, system_root=system_root
    )


class OnWindows(unittest.TestCase):
    """AC-1, AC-2"""

    def test_git_bash_wins_over_the_windowsapps_launcher(self):
        got = find(
            "win32",
            {"git": GIT + r"\cmd\git.exe", "bash": WINDOWSAPPS_BASH},
            [GIT + r"\bin\bash.exe", GIT + r"\usr\bin\bash.exe"],
        )
        self.assertEqual(got, GIT + r"\bin\bash.exe")

    def test_usr_bin_when_bin_has_none(self):
        got = find("win32", {"git": GIT + r"\cmd\git.exe"}, [GIT + r"\usr\bin\bash.exe"])
        self.assertEqual(got, GIT + r"\usr\bin\bash.exe")

    def test_found_with_no_bash_on_path_at_all(self):
        got = find("win32", {"git": GIT + r"\cmd\git.exe"}, [GIT + r"\bin\bash.exe"])
        self.assertEqual(got, GIT + r"\bin\bash.exe")

    def test_git_from_other_git_directories(self):
        for git in (GIT + r"\bin\git.exe", GIT + r"\mingw64\bin\git.exe"):
            with self.subTest(git=git):
                got = find("win32", {"git": git}, [GIT + r"\bin\bash.exe"])
                self.assertEqual(got, GIT + r"\bin\bash.exe")

    def test_a_bash_on_path_outside_windowsapps(self):
        msys = r"C:\msys64\usr\bin\bash.exe"
        self.assertEqual(find("win32", {"bash": msys}), msys)

    def test_the_windowsapps_launcher_alone_is_not_used(self):
        self.assertIsNone(find("win32", {"bash": WINDOWSAPPS_BASH}))
        # also when git is there but has no bash beside it
        self.assertIsNone(find("win32", {"git": GIT + r"\cmd\git.exe", "bash": WINDOWSAPPS_BASH}))

    def test_windowsapps_is_matched_in_any_case(self):
        self.assertIsNone(find("win32", {"bash": WINDOWSAPPS_BASH.lower()}))
        self.assertIsNone(find("win32", {"bash": WINDOWSAPPS_BASH.upper().replace("\\", "/")}))

    def test_the_system32_launcher_is_not_used(self):
        # PR #54 round 1 F1: the inbox WSL component puts the same launcher in System32,
        # ahead of WindowsApps on PATH
        for d in ("System32", "system32", "SysWOW64", "Sysnative"):
            with self.subTest(dir=d):
                bash = r"C:\Windows\%s\bash.exe" % d
                self.assertIsNone(find("win32", {"bash": bash}))
                self.assertIsNone(find("win32", {"bash": bash.replace("\\", "/").upper()}))

    def test_the_system_directory_follows_system_root(self):
        self.assertIsNone(
            find("win32", {"bash": r"D:\Win\System32\bash.exe"}, system_root=r"D:\Win")
        )
        # a bash elsewhere under the Windows directory is not the launcher
        other = r"C:\Windows\System32\tools\bash.exe"
        self.assertEqual(find("win32", {"bash": other}), other)

    def test_git_bash_wins_over_the_system32_launcher(self):
        got = find(
            "win32",
            {"git": GIT + r"\cmd\git.exe", "bash": r"C:\Windows\System32\bash.exe"},
            [GIT + r"\bin\bash.exe"],
        )
        self.assertEqual(got, GIT + r"\bin\bash.exe")

    def test_nothing_found(self):
        self.assertIsNone(find("win32", {}))

    def test_the_message_names_what_was_looked_for(self):
        text = hostbash.missing_message("win32")
        self.assertNotIn("\n", text)
        for part in (r"bin\bash.exe", r"usr\bin\bash.exe", "WindowsApps", "System32", "git"):
            self.assertIn(part, text)


class Elsewhere(unittest.TestCase):
    """AC-3"""

    def test_bash_on_path(self):
        for platform in ("linux", "darwin"):
            with self.subTest(platform=platform):
                self.assertEqual(find(platform, {"bash": "/bin/bash"}), "/bin/bash")

    def test_git_is_not_consulted(self):
        which = mock.Mock(side_effect=lambda cmd: {"bash": "/usr/bin/bash"}.get(cmd))
        got = hostbash.find_bash(platform="linux", which=which, isfile=lambda p: True)
        self.assertEqual(got, "/usr/bin/bash")
        self.assertEqual([c.args[0] for c in which.call_args_list], ["bash"])

    def test_none_when_absent(self):
        self.assertIsNone(find("linux", {}))

    def test_the_message_names_only_the_path_lookup(self):
        # PR #54 round 1 F2: no Git Bash or WindowsApps outside Windows
        for platform in ("linux", "darwin"):
            with self.subTest(platform=platform):
                text = hostbash.missing_message(platform)
                self.assertIn("no bash on PATH", text)
                self.assertNotIn("Git", text)
                self.assertNotIn("WindowsApps", text)


class StubsUseTheHelper(unittest.TestCase):
    """AC-2, AC-4"""

    def source(self, name):
        with open(os.path.join(FAKE_BIN, name), "r", encoding="utf-8") as fh:
            return fh.read()

    def test_no_stub_looks_bash_up_on_its_own(self):
        for name in sorted(os.listdir(FAKE_BIN)):
            if name.endswith(".py") and name != "hostbash.py":
                with self.subTest(stub=name):
                    text = self.source(name)
                    self.assertNotIn('which("bash")', text)
                    self.assertNotIn("which('bash')", text)

    def test_wsl_and_ssh_stubs_call_it(self):
        for name in ("wsl_stub.py", "ssh_stub.py"):
            with self.subTest(stub=name):
                self.assertIn("hostbash.find_bash()", self.source(name))

    def run_quiet(self, fn, *args):
        err = io.StringIO()
        with (
            contextlib.redirect_stderr(err),
            mock.patch.object(hostbash, "find_bash", return_value=None),
            mock.patch("subprocess.run") as run,
        ):
            code = fn(*args)
        run.assert_not_called()
        return code, err.getvalue()

    def test_wsl_stub_without_bash_exits_1_with_the_message(self):
        code, err = self.run_quiet(wsl_stub.main, ["-d", "Ubuntu", "-e", "bash", "x.sh"])
        self.assertEqual(code, 1)
        self.assertEqual(err.strip(), hostbash.missing_message())

    def test_ssh_stub_without_bash_exits_1_with_the_message(self):
        code, err = self.run_quiet(ssh_stub.one, ["bash", "x.sh"])
        self.assertEqual(code, 1)
        self.assertEqual(err.strip(), hostbash.missing_message())


if __name__ == "__main__":
    unittest.main()
