"""Stand-in for `claude -p`, driven by the scenario JSON's "claude" list.

Each invocation consumes the next entry:
  {"exit": 0, "structured_output": {...}, "model": "claude-fable-5", "cost": 0.5,
   "write_files": {"tests/test_review_x.py": "..."}, "delete_files": ["tests/test_review_y.py"],
   "raw_stdout": "<override>",
   "is_error": false}
The prompt (last argv element) and argv are appended to REVALI_FAKE_LOG.
Use via REVALI_CLAUDE_CMD="<python> <this file>".
"""
import json
import os
import sys


def scenario_path():
    return os.environ.get("REVALI_FAKE_SCENARIO", "")


def next_entry():
    path = scenario_path()
    data = {}
    if path and os.path.isfile(path):
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    entries = data.get("claude") or []
    idx_path = path + ".claude_idx"
    idx = 0
    if os.path.isfile(idx_path):
        with open(idx_path, "r", encoding="utf-8") as fh:
            idx = int(fh.read().strip() or 0)
    with open(idx_path, "w", encoding="utf-8") as fh:
        fh.write(str(idx + 1))
    if not entries:
        return {"exit": 0, "structured_output": {}, "model": "claude-fable-5", "cost": 0.1}
    return entries[min(idx, len(entries) - 1)]


def main(argv):
    entry = next_entry()
    prompt = sys.stdin.read() if not sys.stdin.isatty() else ""
    log = os.environ.get("REVALI_FAKE_LOG")
    if log:
        with open(log, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"exe": "claude", "argv": argv, "prompt": prompt, "cwd": os.getcwd()}) + "\n")
    for rel, content in (entry.get("write_files") or {}).items():
        path = os.path.join(os.getcwd(), rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(content)
    for rel in entry.get("delete_files") or []:   # a reviewer dropping one of its own earlier files
        path = os.path.join(os.getcwd(), rel)
        if os.path.isfile(path):
            os.remove(path)
    if "raw_stdout" in entry:
        sys.stdout.write(entry["raw_stdout"])
        return int(entry.get("exit", 0))
    model = entry.get("model", "claude-fable-5")
    cost = float(entry.get("cost", 0.1))
    out = {
        "type": "result",
        "subtype": "error" if entry.get("is_error") else "success",
        "is_error": bool(entry.get("is_error", False)),
        "num_turns": 3,
        "duration_ms": 1234,
        "total_cost_usd": cost,
        "structured_output": entry.get("structured_output", {}),
        "result": json.dumps(entry.get("structured_output", {})),
        "permission_denials": entry.get("permission_denials", []),
        "modelUsage": {
            "claude-haiku-4-5-20251001": {"costUSD": 0.001},
            model: {"costUSD": cost - 0.001},
        },
        "session_id": "fake-session",
    }
    sys.stdout.write(json.dumps(out))
    return int(entry.get("exit", 0))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
