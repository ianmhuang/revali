"""Per-branch state under <state_dir>/<branch>/: state.json, lock, logs, history append."""

import errno
import json
import os
import re
import tempfile
import time
import typing
from dataclasses import asdict, dataclass, field, fields
from typing import List, Optional

from revali import PROMPT_VERSION, STATE_VERSION, VERSION
from revali.procs import pid_alive
from revali.timing import Timing

STAGES = (
    "preflight",
    "pr",
    "review",
    "validate",
    "ready_to_merge",
    "merged",
    "needs_action",
    "needs_human",
    "error",
    "stopped",
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
    repo: str = ""  # owner/name from gh
    branch: str = ""
    base: str = ""
    stage: str = "preflight"
    round: int = 0
    fixes: int = 0
    pr_number: int = 0
    pr_url: str = ""
    head_sha: str = ""
    base_sha: str = ""
    rounds: List[dict] = field(
        default_factory=list
    )  # per round: head_sha, base_sha, verdict, model
    validations: List[dict] = field(
        default_factory=list
    )  # per validation run: result, failed_step, cause
    test_commits: List[str] = field(default_factory=list)
    test_files: List[str] = field(default_factory=list)
    tests_mode: str = ""  # [review] tests the rounds ran with: "commit" | "archive"; "" (a state
    # from before the key) counts as commit (STATE_VERSION 6)
    baseline_sha: str = ""  # the commit the baseline suite passed on (STATE_VERSION 4)
    issues: List[dict] = field(
        default_factory=list
    )  # GitHub issues opened for pre-existing bugs: number, url, validation, tests, cause,
    # summary, recommendation, withhold (STATE_VERSION 5); found: true when the issue was open
    # already, opened outside this branch, and was reused (STATE_VERSION 8; absent = opened)
    cost_usd: float = 0.0
    models_used: List[str] = field(default_factory=list)
    fallback: bool = False
    no_tests: bool = False
    pending_effect: str = ""  # write-ahead marker for commit/push/comment
    needs_info_used: bool = False
    force_push: bool = False  # set after a detected history rewrite; cleared by the next push
    last_verdict: str = ""  # APPROVE | CHANGES_REQUESTED | NEEDS_INFO | PASS | FAIL
    fix_due: bool = False  # the last CHANGES_REQUESTED or validation FAIL still owes the author
    # a fix cycle: set when a run stops on one, cleared when the next run counts it; the stage
    # cannot carry it, a stop before the count records `error` or `stopped` (STATE_VERSION 7)
    reviewer_running: bool = (
        False  # a reviewer session may have left files in test_dir (STATE_VERSION 2)
    )
    pending_test_files: List[str] = field(
        default_factory=list
    )  # left uncommitted by a NEEDS_INFO round;
    # the clean-tree check tolerates exactly these until the next round commits or removes them
    # (STATE_VERSION 3)
    placed_test_files: List[str] = field(default_factory=list)
    # archive mode: the copies the round placed in test_dir, recorded before they are written,
    # plus the reviewer's files a failed take could not move; the cleanup of a round that
    # stops early deletes exactly these (STATE_VERSION 6)
    last_exit: int = -1
    message: str = ""
    started_at: str = ""
    updated_at: str = ""

    @classmethod
    def path(cls, rdir: str) -> str:
        return os.path.join(rdir, "state.json")

    @classmethod
    def load(cls, rdir: str) -> Optional["State"]:
        """The state in `rdir`, None when there is no state file. A field that is null takes
        its default; keys that are not fields are ignored; anything else State cannot use
        (not JSON, not an object, a value of the wrong type) raises StateError."""
        path = cls.path(rdir)
        if not os.path.isfile(path):
            return None
        try:
            data = read_json_retry(path)
        except ValueError as exc:  # JSONDecodeError, or bytes that are not UTF-8
            raise StateError("%s is not valid JSON (%s)" % (path, exc)) from exc
        if not isinstance(data, dict):
            raise StateError(
                "%s does not hold a JSON object (found %s)" % (path, type(data).__name__)
            )
        hints = typing.get_type_hints(cls)
        st = cls()
        for f in fields(cls):
            value = data.get(f.name)
            if value is not None:
                setattr(st, f.name, _checked(path, f.name, hints[f.name], value))
        if "fix_due" not in data:
            # a state from before STATE_VERSION 7: the stage was the record
            st.fix_due = st.stage == "needs_action" and st.last_verdict in (
                "CHANGES_REQUESTED",
                "FAIL",
            )
        return st

    def save(self, rdir: str) -> None:
        # what is written is the current layout, whatever version the file was read as
        self.version = STATE_VERSION
        os.makedirs(rdir, exist_ok=True)
        if not self.started_at:
            self.started_at = now_iso()
        self.updated_at = now_iso()
        write_json_atomic(self.path(rdir), asdict(self))

    def set_stage(
        self, rdir: str, stage: str, message: str = "", exit_code: Optional[int] = None
    ) -> None:
        assert stage in STAGES, stage
        self.stage = stage
        self.message = message
        if exit_code is not None:
            self.last_exit = exit_code
        self.save(rdir)


class StateError(ValueError):
    """state.json holds something State cannot use: not JSON, not an object, or a field whose
    value has the wrong type. The message names the file and the field."""


def _fits(expected: type, value) -> bool:
    if isinstance(value, bool):  # a bool is an int to isinstance, not to a counter
        return expected is bool
    if expected is float:
        return isinstance(value, (int, float))
    return isinstance(value, expected)


def _checked(path: str, name: str, hint, value):
    """`value` for field `name` as State holds it, or StateError when its type is wrong."""
    if typing.get_origin(hint) is list:
        (item,) = typing.get_args(hint)
        want = "a list of %s" % item.__name__
        if not isinstance(value, list):
            raise StateError(
                "%s: field '%s' should be %s, found %s" % (path, name, want, type(value).__name__)
            )
        for i, element in enumerate(value):
            if not _fits(item, element):
                raise StateError(
                    "%s: field '%s' should be %s, found %s at index %d"
                    % (path, name, want, type(element).__name__, i)
                )
        return value
    if not _fits(hint, value):
        raise StateError(
            "%s: field '%s' should be %s, found %s"
            % (path, name, hint.__name__, type(value).__name__)
        )
    if hint is float:
        try:
            return float(value)
        except OverflowError:  # an int literal past a double's range (310+ digits)
            raise StateError(
                "%s: field '%s' should be float, found an int too large for one" % (path, name)
            ) from None
    return value


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


def _until_permitted(attempt, path: str, retry_s: Optional[float]):
    """`attempt()`, repeated while it raises PermissionError, for up to `retry_s` seconds
    (`[paths] write_retry_s` for the repository holding `path` when None); the last error
    propagates once the window is over. Other errors are not retried. The window, and the
    config lookup behind it, is resolved on the first refusal only: `wait` and `status` poll
    through `State.load`, and the common case is a first attempt that succeeds."""
    deadline = None
    pause = 0.02
    while True:
        try:
            return attempt()
        except PermissionError:
            if deadline is None:
                window = write_retry_s(path) if retry_s is None else retry_s
                deadline = time.monotonic() + window
            if time.monotonic() >= deadline:
                raise
        time.sleep(pause)
        pause = min(pause * 2, 0.2)


def read_json_retry(path: str, retry_s: Optional[float] = None):
    """`json.load` of `path`. On Windows, opening the file while another process is inside
    `write_json_atomic`'s rename fails with PermissionError, so the open is retried for the
    same window the writer uses against a reader."""

    def attempt():
        with open(path, "r", encoding="utf-8", newline="") as fh:
            return json.load(fh)

    return _until_permitted(attempt, path, retry_s)


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
        _until_permitted(lambda: os.replace(tmp, path), path, retry_s)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def run_died(state: "State") -> bool:
    """With no live process: the state file holds no result. A run writes `last_exit` -1 when
    it starts and every result (a terminal stage, a finished `run --dry-run`, which leaves
    stage `preflight`) writes an exit code >= 0, so -1 means a run began and never finished,
    whatever stage it last recorded (during preflight that is still the previous run's).
    A state file from before that reset counts as dead at any non-terminal stage but the
    dry run's."""
    if state.last_exit < 0:
        return True
    if state.stage in TERMINAL_STAGES:
        return False
    return state.stage != "preflight"


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
    """Deliberately not `read_json_retry`: a lock that cannot be read right now is treated
    as absent, and the caller's own write settles who holds it."""
    path = lock_path(rdir)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8", newline="") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def acquire_lock(rdir: str, pid: Optional[int] = None) -> None:
    """Take the branch lock for this process, or reserve it for `pid` (a detached child).
    A record already carrying the caller's pid, or `pid`, is the caller's own: the parent
    and the child write in either order."""
    os.makedirs(rdir, exist_ok=True)
    existing = read_lock(rdir)
    holder = int(existing.get("pid", 0)) if existing else 0
    if existing and pid_alive(holder) and holder not in (os.getpid(), pid or 0):
        raise LockHeld(holder, existing.get("since", "?"))
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


# ---- working-tree lock ------------------------------------------------------
# One checkout has one HEAD, so it runs one pipeline at a time whatever branch each session
# has checked out. The branch lock above keys the state; this one keys the tree.


class TreeLockHeld(Exception):
    def __init__(self, pid: int, branch: str, since: str):
        self.pid, self.branch, self.since = pid, branch, since
        super().__init__(
            "another revali run holds this working tree (branch %s, pid %d since %s)"
            % (branch, pid, since)
        )


def tree_lock_path(repo_root: str, state_dir: str) -> str:
    return os.path.join(repo_root, state_dir, "tree.lock")


def read_tree_lock(path: str) -> Optional[dict]:
    """Same rule as `read_lock`: no `read_json_retry`, an unreadable lock counts as absent."""
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8", newline="") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def tree_lock_owner(path: str) -> Optional[dict]:
    """The lock record ({pid, branch, since}) while a live process holds the working tree;
    a stale or unreadable file is removed and None returned."""
    existing = read_tree_lock(path)
    if existing and pid_alive(int(existing.get("pid", 0))):
        return existing
    release_tree_lock(path)
    return None


def acquire_tree_lock(path: str, branch: str, pid: Optional[int] = None) -> None:
    """Same contract as acquire_lock: a record carrying the caller's pid or `pid` is ours."""
    owner = tree_lock_owner(path)
    if owner and int(owner.get("pid", 0)) not in (os.getpid(), pid or 0):
        raise TreeLockHeld(
            int(owner["pid"]), str(owner.get("branch", "?")), str(owner.get("since", "?"))
        )
    os.makedirs(os.path.dirname(path), exist_ok=True)
    write_json_atomic(path, {"pid": pid or os.getpid(), "branch": branch, "since": now_iso()})


def release_tree_lock(path: str) -> None:
    if os.path.isfile(path):
        os.unlink(path)


# ---- logs -------------------------------------------------------------------


class RunLog:
    """Timestamped stage lines to stdout and <state_dir>/<branch>/<logs_dir>/revali.log."""

    def __init__(
        self,
        rdir: Optional[str] = None,
        verbose: bool = False,
        quiet: bool = False,
        logs_dir: Optional[str] = None,
    ):
        if rdir and not logs_dir:
            raise TypeError(
                "RunLog needs logs_dir (the configured [paths] logs_dir) when rdir is given"
            )
        self.path = os.path.join(rdir, logs_dir, "revali.log") if rdir else None
        self.verbose = verbose
        self.quiet = quiet
        self.timing = Timing()  # stage and sandbox wall times, reported at the end of the run
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


# what a non-blocking lock attempt raises when another process holds the lock: flock gives
# EWOULDBLOCK (BlockingIOError), msvcrt.locking EACCES (or EDEADLOCK); anything else is a lock
# that will never be had on this file
_LOCK_HELD_ERRNOS = {errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK, errno.EDEADLK}


def _try_os_lock(fd: int) -> Optional[bool]:
    """One non-blocking attempt at an exclusive OS lock on `fd` (byte 0 on Windows, the whole
    file elsewhere); the OS drops it when the process ends. True: locked; False: another
    process holds it; None: locking failed for another reason (no lock support, say)."""
    try:
        if os.name == "nt":
            import msvcrt

            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError as exc:
        if isinstance(exc, BlockingIOError) or exc.errno in _LOCK_HELD_ERRNOS:
            return False
        return None


def _os_unlock(fd: int) -> None:
    try:
        if os.name == "nt":
            import msvcrt

            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass  # closing the descriptor releases it as well


def history_retry_s() -> float:
    """The history lock's window: `[paths] write_retry_s` of the user layer
    (`~/.revali/config.toml`) over defaults.toml. The history file is a user-level file, so no
    repository's config applies to it; a user file that does not load gives the default."""
    from revali.config import ConfigError, load_defaults, load_user_config

    default = float(load_defaults()["paths"]["write_retry_s"])
    try:
        value = load_user_config().sections.get("paths", {}).get("write_retry_s")
    except (ConfigError, OSError):
        return default
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return default


def _lock_history(lock: str, retry_s: Optional[float]) -> "typing.Tuple[Optional[int], str]":
    """(descriptor of `lock` with the OS lock held, "") or (None, why the row goes unlocked):
    the lock file cannot be opened, locking is not supported there, or another process holds
    the lock past the window (`retry_s`, else `history_retry_s()`, looked up on the first
    refusal only)."""
    try:
        fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o644)
    except OSError as exc:
        return None, "the lock file %s cannot be opened (%s)" % (lock, exc)
    deadline = None
    pause = 0.01
    while True:
        got = _try_os_lock(fd)
        if got:
            return fd, ""
        if got is None:
            os.close(fd)
            return None, "the lock file %s cannot be locked here" % lock
        if deadline is None:
            try:
                window = history_retry_s() if retry_s is None else retry_s
            except Exception as exc:  # a broken defaults.toml: the row still goes in
                os.close(fd)
                return None, "no lock window (%s)" % exc
            deadline = time.monotonic() + window
        left = deadline - time.monotonic()
        if left <= 0:
            os.close(fd)
            return None, "another process held %s for longer than %ss" % (lock, window)
        time.sleep(min(pause, left))  # the last attempt lands on the deadline, not past it
        pause = min(pause * 2, 0.1)


def append_history(path: str, record: dict, retry_s: Optional[float] = None) -> str:
    """Append one row. Writers in other processes (other projects, other worktrees) are kept
    apart by an OS lock on `<path>.lock`, never on the history file, so readers are not
    blocked: without it Windows lost up to a quarter of the rows of 4 concurrent writers.
    A lock that cannot be had is skipped rather than the row dropped; the return value says
    why ("" when the row was written under the lock)."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    record = dict(record)
    record.setdefault("at", now_iso())
    record.setdefault("revali_version", VERSION)
    record.setdefault("prompt_version", PROMPT_VERSION)
    line = json.dumps(record, ensure_ascii=False) + "\n"
    fd, unlocked = _lock_history(path + ".lock", retry_s)
    try:
        with open(path, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(line)
    finally:
        if fd is not None:
            _os_unlock(fd)
            os.close(fd)
    return unlocked


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
