"""Stand-in for ssh. The host is a directory: $REVALI_FAKE_REMOTE plays the remote user's
home, so `$HOME/x` and home-relative paths land inside it. `bash <script>` runs with the
host's bash (Git Bash on Windows, /bin/bash elsewhere) and HOME set to that directory.
Every call is appended to $REVALI_FAKE_LOG. Use via REVALI_SSH_CMD="<python> <this file>".

Knobs: REVALI_FAKE_SSH_DOWN=1 refuses every connection (exit 255);
REVALI_FAKE_SSH_BASH_FAILS=1 makes `bash` fail before the script starts.
"""

import json
import os
import shlex
import shutil
import subprocess
import sys


def remote_root():
    """REVALI_FAKE_REMOTE, else a directory under the test's REVALI_HOME (private per test,
    so parallel workers never share it), else ~/fake-remote."""
    if os.environ.get("REVALI_FAKE_REMOTE"):
        return os.environ["REVALI_FAKE_REMOTE"]
    if os.environ.get("REVALI_HOME"):
        return os.path.join(os.environ["REVALI_HOME"], "fake-remote")
    return os.path.join(os.path.expanduser("~"), "fake-remote")


def local(path):
    root = remote_root()
    if path == "$HOME":
        return root
    if path.startswith("$HOME/"):
        return os.path.join(root, path[6:])
    if path.startswith("/"):
        return os.path.join(root, path.lstrip("/"))
    return os.path.join(root, path)


def record(argv):
    log = os.environ.get("REVALI_FAKE_LOG")
    if log:
        with open(log, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"exe": "ssh", "argv": argv}) + "\n")


def one(words):
    name = words[0]
    if name == "git":  # the preflight probe
        print("git version 2.43.0 (fake)")
        return 0
    if name == "command":  # command -v <tool>
        print("/usr/bin/%s" % words[-1])
        return 0
    if name == "true":
        return 0
    if name == "mkdir":
        for p in words[1:]:
            if not p.startswith("-"):
                os.makedirs(local(p), exist_ok=True)
        return 0
    if name == "rm":
        if os.environ.get("REVALI_FAKE_SSH_RM_FAILS"):
            print("rm: cannot remove: Permission denied", file=sys.stderr)
            return 1
        for p in words[1:]:
            if not p.startswith("-"):
                shutil.rmtree(local(p), ignore_errors=True)
        return 0
    if name == "rmdir":
        for p in [w for w in words[1:] if not w.startswith("-")]:  # in order, like rmdir
            try:
                os.rmdir(local(p))
            except OSError:
                pass
        return 0
    if name == "2>/dev/null":
        return 0
    if name == "bash":
        if os.environ.get("REVALI_FAKE_SSH_BASH_FAILS"):
            print("bash: cannot execute", file=sys.stderr)
            return 127
        bash = shutil.which("bash")
        if not bash:
            print("no bash on this host", file=sys.stderr)
            return 1
        env = dict(os.environ)
        env["HOME"] = remote_root()
        return subprocess.run([bash, local(words[1])], env=env).returncode
    print("ssh_stub: unhandled %s" % words, file=sys.stderr)
    return 2


def main(argv):
    record(argv)
    rest = list(argv)
    while rest and rest[0] == "-o":
        rest = rest[2:]
    if not rest:
        return 1
    host, cmd = rest[0], rest[1:]
    if os.environ.get("REVALI_FAKE_SSH_DOWN"):
        print("ssh: connect to host %s port 22: Connection refused" % host, file=sys.stderr)
        return 255
    # revali passes one command line; the remote shell would split and unquote it
    tokens = shlex.split(" ".join(cmd))
    # (separator, words): ";" always runs the next group, "&&" only after success
    groups, cur, sep = [], [], ";"
    for tok in tokens:
        if tok in (";", "&&"):
            groups.append((sep, cur))
            cur, sep = [], tok
        elif tok.endswith(";") and len(tok) > 1:
            cur.append(tok[:-1])
            groups.append((sep, cur))
            cur, sep = [], ";"
        else:
            cur.append(tok)
    groups.append((sep, cur))
    rc = 0
    for sep, words in groups:
        if not words:
            continue
        if sep == "&&" and rc != 0:
            continue
        rc = one(words)
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
