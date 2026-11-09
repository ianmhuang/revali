"""The bash the wsl and ssh stubs run scripts with.

On Windows that is Git Bash, the bash the stubs were written for. It is found from the git on
PATH, because a PowerShell PATH usually holds only `<Git>\\cmd` and, before any other bash,
WSL's launcher (`%LOCALAPPDATA%\\Microsoft\\WindowsApps\\bash.exe`, or the inbox one in
`%SystemRoot%\\System32`), which cannot open the Windows paths the stubs hand it. Elsewhere
it is the `bash` on PATH.
"""

import ntpath
import os
import shutil
import sys

MISSING_WINDOWS = (
    "no bash for the stub: looked for Git Bash next to the git on PATH "
    "(<Git>\\bin\\bash.exe, <Git>\\usr\\bin\\bash.exe) and for a bash on PATH that is not "
    "WSL's launcher (WindowsApps, %SystemRoot%\\System32), which cannot run the stub's scripts"
)
MISSING_ELSEWHERE = "no bash for the stub: no bash on PATH"

# <Git>\cmd\git.exe, <Git>\bin\git.exe and <Git>\mingw64\bin\git.exe sit two or three levels
# below the Git directory
_GIT_DEPTHS = (2, 3)
_BASH_UNDER_GIT = (("bin", "bash.exe"), ("usr", "bin", "bash.exe"))
# where Windows keeps its own executables, WSL's bash.exe among them, for 64- and 32-bit callers
_SYSTEM_DIRS = ("System32", "SysWOW64", "Sysnative")


def _windows(platform) -> bool:
    return (platform or sys.platform) == "win32"


def missing_message(platform=None) -> str:
    """The one line a stub prints when find_bash found nothing, naming the lookup it made."""
    return MISSING_WINDOWS if _windows(platform) else MISSING_ELSEWHERE


def is_wsl_launcher(path: str, system_root: str) -> bool:
    """A bash.exe under a WindowsApps directory or in the Windows system directory."""
    norm = ntpath.normcase(path)
    if "windowsapps" in norm.split("\\"):
        return True
    system = {ntpath.normcase(ntpath.join(system_root, d)) for d in _SYSTEM_DIRS}
    return ntpath.dirname(norm) in system


def find_bash(platform=None, which=shutil.which, isfile=os.path.isfile, system_root=None):
    """The path of the bash to use, or None when there is none."""
    if not _windows(platform):
        return which("bash")
    git = which("git")
    if git:
        for depth in _GIT_DEPTHS:
            root = git
            for _ in range(depth):
                root = ntpath.dirname(root)
            for parts in _BASH_UNDER_GIT:
                candidate = ntpath.join(root, *parts)
                if isfile(candidate):
                    return candidate
    bash = which("bash")
    if system_root is None:
        system_root = os.environ.get("SystemRoot", "C:\\Windows")
    if bash and not is_wsl_launcher(bash, system_root):
        return bash
    return None
