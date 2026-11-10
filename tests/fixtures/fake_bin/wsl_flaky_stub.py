"""Stand-in for a wsl.exe whose service fails some calls, as Wsl/Service/WSAETIMEDOUT did on
2026-11-09. Everything that does not fail is handed to wsl_stub.py.

REVALI_FAKE_WSL_PLAN names a JSON file:
    {"probe": n, "wslpath": n, "script": n, "script_after": n,
     "message": "...", "exit": -1}
`probe` (`bash -c ...`), `wslpath` and `script` (`bash <file>`) fail that many calls before
the stub lets them through; `script_after` runs the script first and then fails the same way
(the sandbox script ran, wsl.exe still reported an error). Each failure prints `message` the
way wsl.exe prints its own errors, UTF-16-LE, and exits with `exit` (-1 is 4294967295 on
Windows, 255 elsewhere). The counters are written back after every call.
REVALI_FAKE_WSL_CALLS names a file that gets one line per call: the kind.
"keep_rows": {"<label>": n} runs that label's script, then keeps only its first n result rows
and fails like `message` / `exit`: a session that died part way (wsl --shutdown, a VM crash).
"""

import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import wsl_stub  # noqa: E402

DEFAULT_MESSAGE = (
    "The operation timed out because a response was not received from the virtual machine "
    "or container.\r\nError code: Wsl/Service/WSAETIMEDOUT\r\n"
)


def kind_of(cmd):
    if cmd and cmd[0] == "wslpath":
        return "wslpath"
    if len(cmd) >= 2 and cmd[0] == "bash" and cmd[1] == "-c":
        return "probe"
    if cmd and cmd[0] == "bash":
        return "script"
    return "other"


def fail(plan):
    sys.stdout.flush()
    sys.stdout.buffer.write(plan.get("message", DEFAULT_MESSAGE).encode("utf-16-le"))
    sys.stdout.buffer.flush()
    return int(plan.get("exit", -1))


def take(plan, key):
    if int(plan.get(key, 0)) > 0:
        plan[key] = int(plan[key]) - 1
        return True
    return False


def main(argv):
    cmd = argv[3:] if len(argv) >= 4 and argv[0] == "-d" and argv[2] == "-e" else argv
    kind = kind_of(cmd)
    calls = os.environ.get("REVALI_FAKE_WSL_CALLS")
    if calls:
        with open(calls, "a", encoding="utf-8", newline="") as fh:
            fh.write(kind + "\n")
    plan_path = os.environ.get("REVALI_FAKE_WSL_PLAN")
    plan = {}
    if plan_path and os.path.isfile(plan_path):
        with open(plan_path, "r", encoding="utf-8") as fh:
            plan = json.load(fh)
    failing = take(plan, kind)
    after = kind == "script" and not failing and take(plan, "script_after")
    if plan_path:
        with open(plan_path, "w", encoding="utf-8", newline="") as fh:
            json.dump(plan, fh)
    if failing:
        return fail(plan)
    if kind == "probe":
        return 0  # the real probe asks for git, timeout and bash; the host has them or not
    code = wsl_stub.main(argv)
    if after:
        return fail(plan)
    keep = (plan.get("keep_rows") or {}).get(script_label(cmd)) if kind == "script" else None
    if keep is not None:
        # the session "died" after `keep` steps: later rows are lost, wsl.exe reports an error
        truncate_results(cmd, int(keep))
        return fail(plan)
    return code


def host_path(path):
    """The stub's wslpath turns C:\\x into /c/x; undo that on Windows."""
    m = re.match(r"^/([a-zA-Z])/(.*)$", path)
    if os.name == "nt" and m:
        return "%s:/%s" % (m.group(1).upper(), m.group(2))
    return path


def script_label(cmd):
    """`bash <logs>/<label>.sh` -> label."""
    if len(cmd) < 2:
        return ""
    return os.path.splitext(os.path.basename(cmd[1]))[0]


def truncate_results(cmd, keep):
    script = host_path(cmd[1])
    results = os.path.join(os.path.dirname(script), script_label(cmd) + ".results")
    with open(results, "r", encoding="utf-8", newline="") as fh:
        rows = [ln for ln in fh if ln.strip()]
    with open(results, "w", encoding="utf-8", newline="") as fh:
        fh.writelines(rows[:keep])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
