"""Subcommand implementations: run (detached or foreground), preflight, wait,
status, reset, clean, stop. Review / validate / merge stages plug in here in
later milestones; until then `run` stops after preflight with a clear message.
"""
import os
import shutil
import sys
import time
from typing import Optional

from revali import EXIT_ACTION, EXIT_ERROR, EXIT_HUMAN, EXIT_OK, NAME, VERSION
from revali import gitops
from revali.config import history_path, load_user_config, ConfigError
from revali.preflight import Stop, locate, preflight
from revali.procs import kill_tree, pid_alive, python_exe, spawn_detached
from revali.state import (LockHeld, RunLog, State, TERMINAL_STAGES, acquire_lock,
                          append_history, lock_owner_alive, read_lock, release_lock,
                          review_dir, safe_branch)

STAGE_FOR_EXIT = {EXIT_ACTION: "needs_action", EXIT_HUMAN: "needs_human", EXIT_ERROR: "error"}


def _entry_script() -> str:
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(here, "revali.py")


def _rdir_for(cwd: str, branch: str = "") -> Optional[str]:
    root = gitops.repo_root(cwd)
    if not root:
        return None
    try:
        branch = branch or gitops.current_branch(root)
    except gitops.GitError:
        return None
    return review_dir(root, branch)


def _print_stop(stop: Stop) -> None:
    label = {EXIT_ACTION: "ACTION NEEDED", EXIT_HUMAN: "NEEDS A HUMAN", EXIT_ERROR: "ERROR"}.get(
        stop.exit_code, "STOP")
    print("%s: %s" % (label, stop.message))


# ---- run --------------------------------------------------------------------

def cmd_run(args) -> int:
    if args.foreground:
        return _run_foreground(args)
    return _run_detached(args)


def _run_detached(args) -> int:
    cwd = os.getcwd()
    rdir = _rdir_for(cwd)
    if not rdir:
        print("ERROR: not inside a git repository")
        return EXIT_ERROR
    pid = lock_owner_alive(rdir)
    if pid:
        print("ERROR: a revali run is already in progress (pid %d); use `revali wait` or `revali stop`" % pid)
        return EXIT_ERROR
    os.makedirs(os.path.join(rdir, "logs"), exist_ok=True)
    log_path = os.path.join(rdir, "logs", "run.log")
    cmd = [python_exe(), _entry_script(), "run", "--foreground"]
    if args.base:
        cmd += ["--base", args.base]
    if args.dry_run:
        cmd.append("--dry-run")
    if args.verbose:
        cmd.append("--verbose")
    child = spawn_detached(cmd, cwd=cwd, log_path=log_path)
    # Reserve the lock for the child so a second `run` cannot slip in first.
    acquire_lock(rdir, pid=child)
    print("started revali run (pid %d); log: %s" % (child, log_path))
    print("next: `revali wait --timeout 9m` (repeat until it reports a result)")
    return EXIT_OK


def _run_foreground(args) -> int:
    cwd = os.getcwd()
    rdir = _rdir_for(cwd)
    if not rdir:
        print("ERROR: not inside a git repository")
        return EXIT_ERROR
    try:
        acquire_lock(rdir)
    except LockHeld as exc:
        print("ERROR: %s" % exc)
        return EXIT_ERROR
    log = RunLog(rdir, verbose=args.verbose)
    state = State.load(rdir) or State()
    code = EXIT_ERROR
    try:
        code = _pipeline(args, cwd, rdir, state, log)
    finally:
        release_lock(rdir)
    return code


def _pipeline(args, cwd: str, rdir: str, state: State, log: RunLog) -> int:
    try:
        ctx = preflight(cwd, base_override=args.base or "", dry_run=args.dry_run, log=log)
    except Stop as stop:
        _print_stop(stop)
        state.set_stage(rdir, STAGE_FOR_EXIT.get(stop.exit_code, "error"), stop.message, stop.exit_code)
        return stop.exit_code

    state.branch = ctx.branch
    state.base = ctx.base
    state.head_sha = ctx.head_sha
    state.base_sha = ctx.base_sha
    state.set_stage(rdir, "preflight", "preflight passed", None)

    # Later milestones: pr -> review -> validate -> ready_to_merge.
    stop = Stop(EXIT_ERROR, "review stage is not implemented in this version; preflight passed")
    _print_stop(stop)
    state.set_stage(rdir, "error", stop.message, stop.exit_code)
    return stop.exit_code


# ---- preflight --------------------------------------------------------------

def cmd_preflight(args) -> int:
    cwd = os.getcwd()
    rdir = _rdir_for(cwd)
    log = RunLog(rdir if rdir and os.path.isdir(rdir) else None, verbose=args.verbose)
    try:
        preflight(cwd, base_override=args.base or "", dry_run=True, log=log)
    except Stop as stop:
        _print_stop(stop)
        return stop.exit_code
    print("preflight OK")
    return EXIT_OK


# ---- wait / status / reset / clean / stop -----------------------------------

def parse_duration(text: str) -> float:
    text = str(text).strip().lower()
    if text.endswith("m"):
        return float(text[:-1]) * 60
    if text.endswith("h"):
        return float(text[:-1]) * 3600
    if text.endswith("s"):
        return float(text[:-1])
    return float(text)


def cmd_wait(args) -> int:
    rdir = _rdir_for(os.getcwd())
    if not rdir:
        print("ERROR: not inside a git repository")
        return EXIT_ERROR
    deadline = time.monotonic() + parse_duration(args.timeout)
    while True:
        pid = lock_owner_alive(rdir)
        state = State.load(rdir)
        if not pid:
            if state is None:
                print("no revali run recorded for this branch")
                return EXIT_ERROR
            if state.stage in TERMINAL_STAGES:
                print("%s: %s" % (state.stage, state.message))
                return state.last_exit if state.last_exit >= 0 else EXIT_ERROR
            lock = read_lock(rdir)
            if lock and not pid_alive(int(lock.get("pid", 0))):
                release_lock(rdir)
                print("error: the run (pid %s) died at stage '%s' without a result; see logs/run.log"
                      % (lock.get("pid"), state.stage))
                return EXIT_ERROR
            print("%s: %s" % (state.stage, state.message))
            return state.last_exit if state.last_exit >= 0 else EXIT_ERROR
        if time.monotonic() >= deadline:
            stage = state.stage if state else "starting"
            print("still running (pid %d), stage %s; call `revali wait` again" % (pid, stage))
            return EXIT_OK + 4  # 4: still running, distinct from the pipeline codes
        time.sleep(min(2.0, max(0.1, deadline - time.monotonic())))


def cmd_status(args) -> int:
    cwd = os.getcwd()
    root = gitops.repo_root(cwd)
    if not root:
        print("not inside a git repository")
        return EXIT_ERROR
    branch = args.branch or gitops.current_branch(root)
    rdir = review_dir(root, branch)
    state = State.load(rdir)
    pid = lock_owner_alive(rdir)
    print("%s %s" % (NAME, VERSION))
    print("branch: %s" % branch)
    if pid:
        print("running: yes (pid %d)" % pid)
    if state is None:
        print("state: none")
    else:
        print("stage: %s" % state.stage)
        if state.message:
            print("message: %s" % state.message)
        print("round: %d, fixes: %d, cost: $%.2f" % (state.round, state.fixes, state.cost_usd))
        if state.pr_url:
            print("pr: %s" % state.pr_url)
        print("updated: %s" % state.updated_at)
    # Leftover review dirs whose branch is gone.
    base = os.path.join(root, ".revali")
    if os.path.isdir(base):
        existing = set()
        res = gitops._git(["for-each-ref", "--format=%(refname:short)", "refs/heads/"], root)
        for line in res.stdout.splitlines():
            existing.add(safe_branch(line.strip()))
        stale = [d for d in os.listdir(base) if os.path.isdir(os.path.join(base, d)) and d not in existing]
        if stale:
            print("stale review dirs (branch no longer exists): %s  -> `revali clean <name>`" % ", ".join(stale))
    return EXIT_OK


def cmd_reset(args) -> int:
    rdir = _rdir_for(os.getcwd())
    if not rdir:
        print("not inside a git repository")
        return EXIT_ERROR
    if lock_owner_alive(rdir):
        print("ERROR: a run is in progress; `revali stop` first")
        return EXIT_ERROR
    path = State.path(rdir)
    if os.path.isfile(path):
        os.unlink(path)
        print("state removed: %s (review files kept)" % path)
    else:
        print("no state to remove")
    release_lock(rdir)
    return EXIT_OK


def cmd_clean(args) -> int:
    root = gitops.repo_root(os.getcwd())
    if not root:
        print("not inside a git repository")
        return EXIT_ERROR
    name = args.branch
    rdir = os.path.join(root, ".revali", safe_branch(name))
    if not os.path.isdir(rdir):
        rdir = os.path.join(root, ".revali", name)
    if not os.path.isdir(rdir):
        print("nothing to clean for '%s'" % name)
        return EXIT_ERROR
    if lock_owner_alive(rdir):
        print("ERROR: a run is in progress for that branch; `revali stop` first")
        return EXIT_ERROR
    shutil.rmtree(rdir)
    print("removed %s" % rdir)
    return EXIT_OK


def cmd_stop(args) -> int:
    rdir = _rdir_for(os.getcwd())
    if not rdir:
        print("not inside a git repository")
        return EXIT_ERROR
    pid = lock_owner_alive(rdir)
    if not pid:
        release_lock(rdir)
        print("no run in progress")
        return EXIT_OK
    kill_tree(pid)
    for _ in range(50):
        if not pid_alive(pid):
            break
        time.sleep(0.1)
    release_lock(rdir)
    state = State.load(rdir)
    if state is not None:
        state.set_stage(rdir, "stopped", "stopped by user at stage '%s'" % state.stage, EXIT_ERROR)
    print("stopped pid %d" % pid)
    return EXIT_OK


def cmd_version(args) -> int:
    print("%s %s" % (NAME, VERSION))
    return EXIT_OK
