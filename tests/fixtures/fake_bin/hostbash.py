"""The bash the wsl and ssh stubs run scripts with.

On Windows that is Git Bash, the bash the stubs were written for. It is found from the git on
PATH, because a PowerShell PATH usually holds only `<Git>\\cmd` and, before any other bash,
`%LOCALAPPDATA%\\Microsoft\\WindowsApps\\bash.exe`: WSL's launcher, which cannot open the
Windows paths the stubs hand it. Elsewhere it is the `bash` on PATH.
"""

import ntpath
import os
import shutil
import sys

MISSING = (
    "no bash for the stub: looked for Git Bash next to the git on PATH "
    "(<Git>\\bin\\bash.exe, <Git>\\usr\\bin\\bash.exe) and for a bash on PATH outside "
    "WindowsApps (WSL's launcher there cannot run the stub's scripts)"
)

# <Git>\cmd\git.exe, <Git>\bin\git.exe and <Git>\mingw64\bin\git.exe sit two or three levels
# below the Git directory
_GIT_DEPTHS = (2, 3)
_BASH_UNDER_GIT = (("bin", "bash.exe"), ("usr", "bin", "bash.exe"))


def find_bash(platform=None, which=shutil.which, isfile=os.path.isfile):
    """The path of the bash to use, or None when there is none."""
    if (platform or sys.platform) != "win32":
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
    if bash and "windowsapps" not in ntpath.normcase(bash).split("\\"):
        return bash
    return None
