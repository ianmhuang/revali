"""Subprocess helpers: UTF-8 everywhere, timeouts, detached spawn, pid checks.

Executable lookup honours REVALI_<NAME>_CMD (e.g. REVALI_GH_CMD="python gh_stub.py")
so tests and other CLIs can be substituted without touching PATH.
"""
import os
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Callable, Optional


class ProcError(Exception):
    pass


class ProcTimeout(ProcError):
    pass


class ExeNotFound(ProcError):
    pass


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


def resolve(name: str) -> list:
    """Return the argv prefix for an executable, or raise ExeNotFound."""
    override = os.environ.get("REVALI_%s_CMD" % name.upper().replace("-", "_"))
    if override:
        if os.name == "nt":
            # shlex keeps the quotes in non-posix mode; strip them per token.
            return [tok[1:-1] if len(tok) > 1 and tok[0] == tok[-1] == '"' else tok
                    for tok in shlex.split(override, posix=False)]
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


def run(cmd: list, cwd: Optional[str] = None, timeout: Optional[float] = None,
        env: Optional[dict] = None, input_text: Optional[str] = None,
        log: Optional[Callable[[str], None]] = None) -> Result:
    """Run a command to completion. Never raises on non-zero exit; raises on timeout."""
    start = time.monotonic()
    if log:
        log("$ " + " ".join(shlex.quote(str(c)) for c in cmd))
    try:
        proc = subprocess.run(
            [str(c) for c in cmd], cwd=cwd, env=child_env(env), input=input_text,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise ProcTimeout("timed out after %ss: %s" % (timeout, cmd[0])) from exc
    except FileNotFoundError as exc:
        raise ExeNotFound("cannot execute %s: %s" % (cmd[0], exc)) from exc
    res = Result(cmd=list(cmd), returncode=proc.returncode, stdout=proc.stdout or "",
                 stderr=proc.stderr or "", duration=time.monotonic() - start)
    if log:
        log("  -> exit %d in %.1fs" % (res.returncode, res.duration))
    return res


def run_retry(cmd: list, retries: int = 1, wait: float = 3.0, **kw) -> Result:
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
    kwargs = dict(cwd=cwd, env=child_env(env), stdin=subprocess.DEVNULL,
                  stdout=log_file, stderr=subprocess.STDOUT)
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
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
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


def run_shell(cmd: str, cwd: Optional[str] = None, timeout: Optional[float] = None,
              env: Optional[dict] = None, log: Optional[Callable[[str], None]] = None) -> Result:
    """Run a configured command string through the platform shell (lint/build/test lines)."""
    start = time.monotonic()
    if log:
        log("$ " + cmd)
    try:
        proc = subprocess.run(
            cmd, shell=True, cwd=cwd, env=child_env(env), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise ProcTimeout("timed out after %ss: %s" % (timeout, cmd)) from exc
    res = Result(cmd=[cmd], returncode=proc.returncode, stdout=proc.stdout or "",
                 stderr=proc.stderr or "", duration=time.monotonic() - start)
    if log:
        log("  -> exit %d in %.1fs" % (res.returncode, res.duration))
    return res
