"""Subprocess helpers: UTF-8 everywhere, timeouts, detached spawn, pid checks.

Executable lookup honours REVALI_<NAME>_CMD (e.g. REVALI_GH_CMD="python gh_stub.py")
so tests and other CLIs can be substituted without touching PATH.
"""

import os
import shlex
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional


class ProcError(Exception):
    pass


class ProcTimeout(ProcError):
    pass


class ExeNotFound(ProcError):
    pass


class ProcCancelled(ProcError):
    """The `cancel` event of `run` was set while the command ran; the command was stopped."""


@dataclass
class Result:
    cmd: list
    returncode: int
    stdout: str
    stderr: str
    duration: float

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    @property
    def text(self) -> str:
        return (self.stdout or "") + (self.stderr or "")


def one_line(text: str, limit: int = 200) -> str:
    """A command's error for a one-line message: git's run over several lines, so the
    whitespace is folded and the text cut to `limit` characters."""
    return " ".join((text or "").split())[:limit]


def resolve(name: str) -> list:
    """Return the argv prefix for an executable, or raise ExeNotFound."""
    override = os.environ.get("REVALI_%s_CMD" % name.upper().replace("-", "_"))
    if override:
        if os.name == "nt":
            # shlex keeps the quotes in non-posix mode; strip them per token.
            return [
                tok[1:-1] if len(tok) > 1 and tok[0] == tok[-1] == '"' else tok
                for tok in shlex.split(override, posix=False)
            ]
        return shlex.split(override)
    path = shutil.which(name)
    if not path:
        raise ExeNotFound("executable not found on PATH: %s" % name)
    return [path]


def child_env(extra: Optional[dict] = None) -> dict:
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    if extra:
        env.update(extra)
    return env


def _no_window() -> dict:
    """Windows: a console program started by a process without a console (the detached
    pipeline) gets a fresh console window unless CREATE_NO_WINDOW is set; the output is
    captured anyway. Elsewhere there is nothing to pass."""
    if os.name == "nt":
        return {"creationflags": 0x08000000}  # CREATE_NO_WINDOW
    return {}


def run(
    cmd: list,
    cwd: Optional[str] = None,
    timeout: Optional[float] = None,
    env: Optional[dict] = None,
    input_text: Optional[str] = None,
    log: Optional[Callable[[str], None]] = None,
    cancel: Optional[threading.Event] = None,
) -> Result:
    """Run a command to completion. Never raises on non-zero exit; raises on timeout, and with
    `cancel`, ProcCancelled once another thread sets it (the command is stopped first)."""
    start = time.monotonic()
    if log:
        log("$ " + " ".join(shlex.quote(str(c)) for c in cmd))
    if cancel is not None:
        return _run_cancellable(cmd, cwd, timeout, env, input_text, log, cancel, start)
    try:
        proc = subprocess.run(
            [str(c) for c in cmd],
            cwd=cwd,
            env=child_env(env),
            input=input_text,
            stdin=None if input_text is not None else subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            **_no_window(),
        )
    except subprocess.TimeoutExpired as exc:
        raise ProcTimeout("timed out after %ss: %s" % (timeout, cmd[0])) from exc
    except FileNotFoundError as exc:
        raise ExeNotFound("cannot execute %s: %s" % (cmd[0], exc)) from exc
    res = Result(
        cmd=list(cmd),
        returncode=proc.returncode,
        stdout=proc.stdout or "",
        stderr=proc.stderr or "",
        duration=time.monotonic() - start,
    )
    if log:
        log("  -> exit %d in %.1fs" % (res.returncode, res.duration))
    return res


# Fixed margins of the cancel mechanism, not tunables (like the margins in runners.py): how
# often a cancellable command looks at its event, how long a stopped command gets to exit
# before it is killed outright, and how long revali then waits for its pipes to close.
CANCEL_POLL_S = 0.25
CANCEL_GRACE_S = 5.0
CANCEL_REAP_S = 5.0


def _run_cancellable(cmd, cwd, timeout, env, input_text, log, cancel, start) -> Result:
    """`run` for a command another thread may stop. The child is not put in a session of its
    own, so `revali stop` (which kills the run's process group on POSIX) still reaches it. For
    the same reason a cancel ends the child itself on POSIX, and the whole tree on Windows
    (taskkill /T)."""
    if cancel.is_set():
        raise ProcCancelled("cancelled before it started: %s" % cmd[0])
    try:
        proc = subprocess.Popen(
            [str(c) for c in cmd],
            cwd=cwd,
            env=child_env(env),
            stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            **_no_window(),
        )
    except FileNotFoundError as exc:
        raise ExeNotFound("cannot execute %s: %s" % (cmd[0], exc)) from exc
    deadline = None if timeout is None else start + timeout
    pending_input = input_text
    while True:
        wait = CANCEL_POLL_S
        if deadline is not None:
            wait = max(0.0, min(wait, deadline - time.monotonic()))
        try:
            # the input goes with the first call; a retried communicate() keeps feeding it
            stdout, stderr = proc.communicate(input=pending_input, timeout=wait)
            break
        except subprocess.TimeoutExpired:
            pending_input = None
        if cancel.is_set():
            _stop(proc)
            if log:
                log("  -> cancelled after %.1fs" % (time.monotonic() - start))
            raise ProcCancelled("cancelled: %s" % cmd[0])
        if deadline is not None and time.monotonic() >= deadline:
            _stop(proc)
            raise ProcTimeout("timed out after %ss: %s" % (timeout, cmd[0]))
    res = Result(
        cmd=list(cmd),
        returncode=proc.returncode,
        stdout=stdout or "",
        stderr=stderr or "",
        duration=time.monotonic() - start,
    )
    if log:
        log("  -> exit %d in %.1fs" % (res.returncode, res.duration))
    return res


def _stop(proc: subprocess.Popen) -> None:
    """End a child started by _run_cancellable and reap it. On POSIX that takes bounded time
    even when a descendant that outlives the child holds its stdout / stderr open: the pipes
    are given up rather than read to their end. On Windows the bound comes from the tree kill;
    a process outside that tree holding the pipes keeps only the daemon reader threads busy."""
    if os.name == "nt":
        kill_tree(proc.pid)
    else:
        proc.terminate()
    try:
        proc.communicate(timeout=CANCEL_GRACE_S)
        return
    except subprocess.TimeoutExpired:
        pass
    proc.kill()
    try:
        proc.communicate(timeout=CANCEL_REAP_S)
        return
    except subprocess.TimeoutExpired:
        pass
    if os.name != "nt":
        # POSIX reads the pipes on this thread, so closing them ends the reading. On Windows
        # communicate() reads them on daemon threads that hold each pipe's lock while they
        # block; a close from here would wait for that lock, so the pipes are left to those
        # threads, which end with the last process holding them (PR #55 round 2 F8).
        for pipe in (proc.stdin, proc.stdout, proc.stderr):
            if pipe is not None:
                try:
                    pipe.close()
                except OSError:
                    pass
    try:
        proc.wait(timeout=CANCEL_REAP_S)
    except subprocess.TimeoutExpired:
        pass


RETRY_WAIT_S = 3.0  # the pause before a retry of a command that failed


def run_retry(cmd: list, retries: int = 1, wait: float = RETRY_WAIT_S, **kw) -> Result:
    """Retry a command once (by default) on non-zero exit; for transient gh/api failures."""
    res = run(cmd, **kw)
    attempt = 0
    while not res.ok and attempt < retries:
        attempt += 1
        time.sleep(wait)
        res = run(cmd, **kw)
    return res


def spawn_detached(cmd: list, cwd: str, log_path: str, env: Optional[dict] = None) -> int:
    """Start a process that survives the parent; stdout/stderr appended to log_path."""
    log_file = open(log_path, "ab")
    kwargs = dict(
        cwd=cwd,
        env=child_env(env),
        stdin=subprocess.DEVNULL,
        stdout=log_file,
        stderr=subprocess.STDOUT,
    )
    if os.name == "nt":
        flags = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        kwargs["creationflags"] = flags
    else:
        kwargs["start_new_session"] = True
    proc = subprocess.Popen([str(c) for c in cmd], **kwargs)
    log_file.close()
    _DETACHED.append(proc)  # keep the handle so Python does not warn about a live child
    return proc.pid


_DETACHED = []


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def kill_tree(pid: int) -> None:
    """Terminate a process and its children (the spawned claude/wsl sessions)."""
    if not pid_alive(pid):
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, **_no_window()
        )
        return
    import signal

    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass


def python_exe() -> str:
    return sys.executable


def run_shell(
    cmd: str,
    cwd: Optional[str] = None,
    timeout: Optional[float] = None,
    env: Optional[dict] = None,
    log: Optional[Callable[[str], None]] = None,
) -> Result:
    """Run a configured command string through the platform shell (lint/build/test lines)."""
    start = time.monotonic()
    if log:
        log("$ " + cmd)
    try:
        proc = subprocess.run(
            cmd,
            shell=True,
            cwd=cwd,
            env=child_env(env),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            **_no_window(),
        )
    except subprocess.TimeoutExpired as exc:
        raise ProcTimeout("timed out after %ss: %s" % (timeout, cmd)) from exc
    res = Result(
        cmd=[cmd],
        returncode=proc.returncode,
        stdout=proc.stdout or "",
        stderr=proc.stderr or "",
        duration=time.monotonic() - start,
    )
    if log:
        log("  -> exit %d in %.1fs" % (res.returncode, res.duration))
    return res
