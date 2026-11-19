"""Stand-in for scp, paired with ssh_stub: `host:<path>` maps to $REVALI_FAKE_REMOTE/<path>.
Local names are relative to the working directory, as revali passes them. Records every
call to $REVALI_FAKE_LOG. Use via REVALI_SCP_CMD="<python> <this file>".
"""

import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fakelog  # noqa: E402
from ssh_stub import knob  # noqa: E402  (one reading of the knobs for both stubs)


def remote_root():
    return os.environ.get("REVALI_FAKE_REMOTE") or os.path.join(
        os.path.expanduser("~"), "fake-remote"
    )


def is_remote(spec):
    head = spec.split(":", 1)[0]
    return ":" in spec and len(head) > 1 and "/" not in head and "\\" not in head


def local(spec):
    path = spec.split(":", 1)[1]
    if path.startswith("/"):
        path = path.lstrip("/")
    return os.path.join(remote_root(), path)


def copy_into(src, dst_dir):
    """Copy a file or a directory tree into dst_dir (scp -r semantics)."""
    os.makedirs(dst_dir, exist_ok=True)
    if os.path.isdir(src):
        shutil.copytree(
            src, os.path.join(dst_dir, os.path.basename(src.rstrip("/"))), dirs_exist_ok=True
        )
    else:
        shutil.copy2(src, os.path.join(dst_dir, os.path.basename(src)))


def main(argv):
    log = os.environ.get("REVALI_FAKE_LOG")
    if log:
        fakelog.record(log, {"exe": "scp", "argv": argv, "cwd": os.getcwd()})
    rest = []
    skip = False
    for tok in argv:
        if skip:
            skip = False
            continue
        if tok == "-o":
            skip = True
            continue
        if tok in ("-q", "-r"):
            continue
        rest.append(tok)
    if len(rest) < 2:
        return 1
    srcs, dst = rest[:-1], rest[-1]
    hosts = {s.split(":", 1)[0] for s in srcs + [dst] if is_remote(s)}
    # `1` refuses every call, one naming no remote host included, as before host lists
    if any(knob("REVALI_FAKE_SSH_DOWN", h) for h in hosts or {""}):
        print("ssh: connect to host: Connection refused", file=sys.stderr)
        return 255
    if is_remote(dst):
        target = local(dst)
        for src in srcs:
            if not os.path.exists(src):
                print("scp: %s: No such file or directory" % src, file=sys.stderr)
                return 1
            copy_into(src, target)
        return 0
    for src in srcs:
        if not is_remote(src):
            print("scp_stub: local-to-local copy not supported", file=sys.stderr)
            return 2
        path = local(src)
        if path.endswith("/."):
            shutil.copytree(path[:-2], dst, dirs_exist_ok=True)
        elif os.path.isdir(path):
            copy_into(path, dst)
        elif os.path.isfile(path):
            copy_into(path, dst)
        else:
            print("scp: %s: No such file or directory" % src, file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
