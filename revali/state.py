"""Per-branch state under <state_dir>/<branch>/: state.json, lock, logs, history append."""
import json
import os
import re
import tempfile
import time
from dataclasses import asdict, dataclass, field
from typing import List, Optional

from revali import STATE_VERSION, VERSION, PROMPT_VERSION
from revali.procs import pid_alive

STAGES = (
    "preflight", "pr", "review", "validate", "ready_to_merge", "merged",
    "needs_action", "needs_human", "error", "stopped",
)
TERMINAL_STAGES = ("ready_to_merge", "merged", "needs_action", "needs_human", "error", "stopped")


def safe_branch(branch: str) -> str:
    """feature/x -> feature__x; anything unsafe for a directory name -> _."""
    name = branch.replace("/", "__").replace("\\", "__")
    return re.sub(r"[^A-Za-z0-9._-]", "_", name)


def review_dir(repo_root: str, branch: str, state_dir: str) -> str:
    return os.path.join(repo_root, state_dir, safe_branch(branch))


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


@dataclass
class State:
    version: int = STATE_VERSION
    revali_version: str = VERSION
    prompt_version: str = PROMPT_VERSION
    repo: str = ""               # owner/name from gh
    branch: str = ""
    base: str = ""
    stage: str = "preflight"
    round: int = 0
    fixes: int = 0
    pr_number: int = 0
    pr_url: str = ""
    head_sha: str = ""
    base_sha: str = ""
    rounds: List[dict] = field(default_factory=list)   # per round: head_sha, base_sha, verdict, model
    validations: List[dict] = field(default_factory=list)  # per validation run: result, failed_step, cause
    test_commits: List[str] = field(default_factory=list)
    test_files: List[str] = field(default_factory=list)
    cost_usd: float = 0.0
    models_used: List[str] = field(default_factory=list)
    fallback: bool = False
    no_tests: bool = False
    pending_effect: str = ""     # write-ahead marker for commit/push/comment
    needs_info_used: bool = False
    force_push: bool = False     # set after a detected history rewrite; cleared by the next push
    last_verdict: str = ""       # APPROVE | CHANGES_REQUESTED | NEEDS_INFO | PASS | FAIL
    reviewer_running: bool = False  # a reviewer session may have left files in test_dir (STATE_VERSION 2)
    pending_test_files: List[str] = field(default_factory=list)  # left uncommitted by a NEEDS_INFO round;
    # the clean-tree check tolerates exactly these until the next round commits or removes them (STATE_VERSION 3)
    last_exit: int = -1
    message: str = ""
    started_at: str = ""
    updated_at: str = ""

    @classmethod
    def path(cls, rdir: str) -> str:
        return os.path.join(rdir, "state.json")

    @classmethod
    def load(cls, rdir: str) -> Optional["State"]:
        path = cls.path(rdir)
        if not os.path.isfile(path):
            return None
        with open(path, "r", encoding="utf-8", newline="") as fh:
            data = json.load(fh)
        st = cls()
        for key, value in data.items():
            if hasattr(st, key):
                setattr(st, key, value)
        return st

    def save(self, rdir: str) -> None:
        os.makedirs(rdir, exist_ok=True)
        if not self.started_at:
            self.started_at = now_iso()
        self.updated_at = now_iso()
        write_json_atomic(self.path(rdir), asdict(self))

    def set_stage(self, rdir: str, stage: str, message: str = "", exit_code: Optional[int] = None) -> None:
        assert stage in STAGES, stage
        self.stage = stage
        self.message = message
        if exit_code is not None:
            self.last_exit = exit_code
        self.save(rdir)


def write_retry_s(path: str) -> float:
    """[paths] write_retry_s for the repository that holds `path`: the layered config of the
    repository whose `.git` entry is found walking up from the file (so the user and project
    files can change it), the defaults.toml value for a file outside any repository. Imported
    lazily: config does not know state."""
    from revali.config import load_defaults, paths_for
    directory = os.path.dirname(os.path.abspath(path))
    while True:
        if os.path.exists(os.path.join(directory, ".git")):
            return float(paths_for(directory).write_retry_s)
        parent = os.path.dirname(directory)
        if parent == directory:
            return float(load_defaults()["paths"]["write_retry_s"])
        directory = parent


def write_json_atomic(path: str, data, retry_s: Optional[float] = None) -> None:
    """Write to a temp file in the same directory, then rename over `path`. On Windows the
    rename fails with PermissionError while another process (`wait`, `status`) has the file
    open for reading, so it is retried for up to `retry_s` seconds; other errors are not."""
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        deadline = time.monotonic() + (write_retry_s(path) if retry_s is None else retry_s)
        pause = 0.02
        while True:
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                if time.monotonic() >= deadline:
                    raise
            time.sleep(pause)
            pause = min(pause * 2, 0.2)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def run_died(state: "State") -> bool:
    """With no live process: the recorded stage has no result. A finished `run --dry-run`
    leaves stage `preflight` with exit 0 and is the one non-terminal stage that is a result;
    a run resets `last_exit` to -1 when it starts, so a kill during preflight is not mistaken
    for it."""
    if state.stage in TERMINAL_STAGES:
        return False
    return not (state.stage == "preflight" and state.last_exit >= 0)


def write_text(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def read_text(path: str) -> str:
    with open(path, "r", encoding="utf-8", newline="") as fh:
        return fh.read()


# ---- lock -------------------------------------------------------------------

class LockHeld(Exception):
    def __init__(self, pid: int, since: str):
        self.pid = pid
        self.since = since
        super().__init__("another revali run holds the lock (pid %d since %s)" % (pid, since))


def lock_path(rdir: str) -> str:
    return os.path.join(rdir, "lock")


def read_lock(rdir: str) -> Optional[dict]:
    path = lock_path(rdir)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8", newline="") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def acquire_lock(rdir: str, pid: Optional[int] = None) -> None:
    os.makedirs(rdir, exist_ok=True)
    existing = read_lock(rdir)
    if existing and pid_alive(int(existing.get("pid", 0))) and int(existing.get("pid", 0)) != os.getpid():
        raise LockHeld(int(existing["pid"]), existing.get("since", "?"))
    write_json_atomic(lock_path(rdir), {"pid": pid or os.getpid(), "since": now_iso()})


def release_lock(rdir: str) -> None:
    path = lock_path(rdir)
    if os.path.isfile(path):
        os.unlink(path)


def lock_owner_alive(rdir: str) -> Optional[int]:
    existing = read_lock(rdir)
    if existing and pid_alive(int(existing.get("pid", 0))):
        return int(existing["pid"])
    return None


# ---- logs -------------------------------------------------------------------

class RunLog:
    """Timestamped stage lines to stdout and <state_dir>/<branch>/<logs_dir>/revali.log."""

    def __init__(self, rdir: Optional[str] = None, verbose: bool = False, quiet: bool = False,
                 logs_dir: Optional[str] = None):
        if rdir and not logs_dir:
            raise TypeError("RunLog needs logs_dir (the configured [paths] logs_dir) when rdir is given")
        self.path = os.path.join(rdir, logs_dir, "revali.log") if rdir else None
        self.verbose = verbose
        self.quiet = quiet
        if self.path:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)

    def _write(self, line: str) -> None:
        if self.path:
            with open(self.path, "a", encoding="utf-8", newline="\n") as fh:
                fh.write(line + "\n")

    def stage(self, stage: str, msg: str) -> None:
        line = "[%s] %s: %s" % (time.strftime("%H:%M:%S"), stage, msg)
        if not self.quiet:
            print(line, flush=True)
        self._write(line)

    def detail(self, msg: str) -> None:
        line = "[%s]   %s" % (time.strftime("%H:%M:%S"), msg)
        if self.verbose and not self.quiet:
            print(line, flush=True)
        self._write(line)


# ---- history ----------------------------------------------------------------

def append_history(path: str, record: dict) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    record = dict(record)
    record.setdefault("at", now_iso())
    record.setdefault("revali_version", VERSION)
    record.setdefault("prompt_version", PROMPT_VERSION)
    with open(path, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def read_history(path: str) -> List[dict]:
    if not os.path.isfile(path):
        return []
    out = []
    with open(path, "r", encoding="utf-8", newline="") as fh:
        for line in fh:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
    return out
