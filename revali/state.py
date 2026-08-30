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


def write_json_atomic(path: str, data) -> None:
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


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
                 logs_dir: str = ""):
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
