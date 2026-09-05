"""Stand-in for wsl.exe that runs the generated sandbox script with the host's bash
(Git Bash on Windows, /bin/bash elsewhere). `wslpath -a` converts Windows paths to
the bash-style form. Use via REVALI_WSL_CMD="<python> <this file>".
"""

import os
import re
import shutil
import subprocess
import sys


def bash_path(path):
    m = re.match(r"^([A-Za-z]):[\\/](.*)$", path)
    if m:
        return "/%s/%s" % (m.group(1).lower(), m.group(2).replace("\\", "/"))
    return path.replace("\\", "/")


def main(argv):
    # argv: -d <distro> -e <cmd...>
    if len(argv) >= 4 and argv[0] == "-d" and argv[2] == "-e":
        cmd = argv[3:]
    else:
        cmd = argv
    if not cmd:
        return 1
    if cmd[0] == "wslpath":
        print(bash_path(cmd[-1]))
        return 0
    if cmd[0] == "bash":
        bash = shutil.which("bash")
        if not bash:
            print("no bash on this host", file=sys.stderr)
            return 1
        env = dict(os.environ)
        # A private HOME per test when REVALI_HOME is set (RepoCase sets it per test), so
        # sandboxes under "$HOME/.revali/sandbox" never collide between parallel workers.
        if env.get("REVALI_HOME"):
            env["HOME"] = os.path.join(env["REVALI_HOME"], "wsl-home")
            os.makedirs(env["HOME"], exist_ok=True)
        else:
            env.setdefault("HOME", os.path.expanduser("~"))
        proc = subprocess.run([bash] + cmd[1:], env=env)
        return proc.returncode
    print("wsl_stub: unhandled %s" % cmd, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
