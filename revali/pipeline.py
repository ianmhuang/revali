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
from revali.config import history_path, load_user_config, paths_for, ConfigError
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
    return review_dir(root, branch, paths_for(root).state_dir)


def _print_stop(stop: Stop) -> None:
    label = {EXIT_ACTION: "ACTION NEEDED", EXIT_HUMAN: "NEEDS A HUMAN", EXIT_ERROR: "ERROR"}.get(
        stop.exit_code, "STOP")
    print("%s: %s" % (label, stop.message))


# ---- run --------------------------------------------------------------------

def cmd_run(args) -> int:
    if args.foreground or args.dry_run:  # a dry run spawns nothing, not even itself
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
    logs = os.path.join(rdir, paths_for(gitops.repo_root(cwd)).logs_dir)
    os.makedirs(logs, exist_ok=True)
    log_path = os.path.join(logs, "run.log")
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
    log = RunLog(rdir, verbose=args.verbose, logs_dir=paths_for(gitops.repo_root(cwd)).logs_dir)
    state = State.load(rdir) or State()
    code = EXIT_ERROR
    try:
        code = _pipeline(args, cwd, rdir, state, log)
    finally:
        release_lock(rdir)
    return code


def _pipeline(args, cwd: str, rdir: str, state: State, log: RunLog) -> int:
    try:
        return _stages(args, cwd, rdir, state, log)
    except ConfigError as exc:
        stop = Stop(EXIT_ERROR, "configuration: " + "; ".join(exc.problems))
        _print_stop(stop)
        state.set_stage(rdir, "error", stop.message, EXIT_ERROR)
        _record_history(state, EXIT_ERROR)
        return EXIT_ERROR
    except Stop as stop:
        _print_stop(stop)
        state.set_stage(rdir, STAGE_FOR_EXIT.get(stop.exit_code, "error"), stop.message, stop.exit_code)
        _record_history(state, stop.exit_code)
        return stop.exit_code


def _record_history(state: State, exit_code: int) -> None:
    try:
        path = history_path(load_user_config())
    except ConfigError:
        path = history_path(None)
    try:
        append_history(path, {
            "repo": state.repo, "branch": state.branch, "base": state.base, "stage": state.stage, "exit": exit_code,
            "rounds": len(state.rounds), "fixes": state.fixes, "last_verdict": state.last_verdict,
            "cost_usd": round(state.cost_usd, 4), "models": state.models_used, "fallback": state.fallback,
            "pr": state.pr_number,
        })
    except OSError:
        pass


def _rerun_bookkeeping(ctx, state: State, rdir: str, log: RunLog) -> None:
    """Count fix cycles, detect rewritten history, refuse a rerun with no change."""
    from revali.review import CHANGES_REQUESTED
    cfg = ctx.cfg.review
    if state.rounds:
        missing = [c for c in state.test_commits if c and not gitops.head_contains(c, ctx.repo_root)]
        if missing:
            log.stage("run", "the reviewer's test commits are no longer in HEAD (rebase or reset); "
                             "the review starts over from round 1")
            state.rounds, state.test_commits, state.test_files = [], [], []
            state.fixes, state.needs_info_used, state.last_verdict = 0, False, ""
            state.force_push = True  # the remote still has the dropped commits
        elif state.stage == "needs_action":
            if state.last_verdict in (CHANGES_REQUESTED, "FAIL"):
                if ctx.head_sha == state.head_sha:
                    raise Stop(EXIT_ACTION, "nothing changed since the last review (HEAD %s); "
                                            "fix, commit, then run again" % ctx.head_sha[:10])
                state.fixes += 1
                log.stage("run", "fix cycle %d of %d" % (state.fixes, cfg.max_fixes))
    if state.fixes > cfg.max_fixes:
        raise Stop(EXIT_HUMAN, "%d fix cycles used (limit %d); a human decides how to proceed. "
                               "Latest review: %s" % (state.fixes, cfg.max_fixes,
                                                       os.path.join(rdir, "review-%d.md" % len(state.rounds))))


def _model_label(chosen) -> str:
    return chosen.model + (" (%s)" % chosen.reason if chosen.reason else "")


def _stages(args, cwd: str, rdir: str, state: State, log: RunLog) -> int:
    from revali import pr as prstage
    from revali import review
    from revali import validate

    if not state.repo:
        # so a run that stops in preflight still names its repo in history
        state.repo = gitops.remote_repo("origin", cwd)
    first_pass = not state.rounds and not args.dry_run
    baseline_hook = (lambda ctx: validate.baseline(ctx, rdir, log)) if first_pass else None
    ctx = preflight(cwd, base_override=args.base or "", dry_run=args.dry_run, log=log, baseline=baseline_hook)
    _rerun_bookkeeping(ctx, state, rdir, log)
    state.branch, state.base = ctx.branch, ctx.base
    # lowercased like gitops.remote_repo, so stats groups both sources under one row
    state.repo = ("%s/%s" % (ctx.repo.owner, ctx.repo.name)).lower() if ctx.repo else ""
    state.head_sha, state.base_sha = ctx.head_sha, ctx.base_sha
    state.set_stage(rdir, "preflight", "preflight passed")

    if args.dry_run:
        msg = ("dry run: would push %s, open a draft PR against %s, run reviewer %s (round %d), "
               "then stop" % (ctx.branch, ctx.base, _model_label(review.planned_reviewer(ctx)),
                              len(state.rounds) + 1))
        log.stage("run", msg)
        state.set_stage(rdir, "preflight", msg, EXIT_OK)
        print("DRY RUN OK: " + msg)
        return EXIT_OK

    state.set_stage(rdir, "pr", "pushing and opening the PR")
    prstage.ensure_pr(ctx, state, rdir, log)
    state.head_sha = ctx.head_sha

    state.set_stage(rdir, "review", "reviewer round %d" % (len(state.rounds) + 1))
    outcome = review.run_round(ctx, state, rdir, log)
    if outcome.commit_sha:
        state.pending_effect = "push"
        state.save(rdir)
        res = gitops.push_branch(ctx.branch, ctx.repo_root, log.detail)
        state.pending_effect = ""
        if not res.ok:
            raise Stop(EXIT_ERROR, "git push of the test commit failed: %s" % res.text.strip())
        state.head_sha = outcome.commit_sha
    comment = outcome.review_md
    if prstage.is_public(ctx):
        comment = review.render_review_summary(outcome.data, outcome.verdict, outcome.round_no,
                                               outcome.model_actual, outcome.cost, [a[0] for a in ctx.doc.acs],
                                               ctx.cfg.paths.state_dir)
    prstage.post_comment(ctx, state, rdir, "review-%d" % outcome.round_no, comment, log)

    if outcome.verdict == review.NEEDS_INFO:
        questions = "\n".join("  - " + q for q in outcome.data.get("questions", []))
        state.set_stage(rdir, "needs_action", "reviewer needs information", EXIT_ACTION)
        prstage.update_body(ctx, state, rdir, log)
        _record_history(state, EXIT_ACTION)
        print("ACTION NEEDED: the reviewer has questions (round %d). Answer them in %s, adjust "
              "change.md if the acceptance criteria were unclear, then run again.\n%s"
              % (outcome.round_no, os.path.join(rdir, "response-%d.md" % outcome.round_no), questions))
        return EXIT_ACTION
    if outcome.verdict == review.CHANGES_REQUESTED:
        reasons = "\n".join("  - " + r for r in outcome.reasons)
        state.set_stage(rdir, "needs_action", "changes requested in round %d" % outcome.round_no, EXIT_ACTION)
        prstage.update_body(ctx, state, rdir, log)
        _record_history(state, EXIT_ACTION)
        print("ACTION NEEDED: changes requested (round %d, fix cycle %d of %d). Full review: %s\n"
              "Fix what blocks, or answer each finding in %s (fixed / wontfix: <reason>), commit, run again.\n%s"
              % (outcome.round_no, state.fixes, ctx.cfg.review.max_fixes, outcome.review_path,
                 os.path.join(rdir, "response-%d.md" % outcome.round_no), reasons))
        return EXIT_ACTION

    state.set_stage(rdir, "validate", "review approved in round %d; validating" % outcome.round_no)
    prstage.update_body(ctx, state, rdir, log)
    vout = validate.run_validation(ctx, state, rdir, log)
    section = vout.section_md
    if prstage.is_public(ctx):
        section = validate.render_section_summary(vout, ctx.cfg.paths.state_dir)
    prstage.post_comment(ctx, state, rdir, "validate-%d" % vout.number, section, log)

    if vout.result == validate.FAIL:
        summary = validate.summary_for_author(vout, rdir)
        state.set_stage(rdir, "needs_action", "validation %d failed at %s" % (vout.number, vout.failed_step), EXIT_ACTION)
        prstage.update_body(ctx, state, rdir, log)
        _record_history(state, EXIT_ACTION)
        print("ACTION NEEDED: %s\nFix (or correct the test if the diagnosis says the test is wrong, and say so "
              "in %s), commit, run again." % (summary, os.path.join(rdir, "response-%d.md" % outcome.round_no)))
        return EXIT_ACTION

    state.set_stage(rdir, "ready_to_merge", "validation %d passed" % vout.number, EXIT_OK)
    prstage.mark_ready(ctx, state, log)
    prstage.update_body(ctx, state, rdir, log)
    _record_history(state, EXIT_OK)
    flags = []
    if state.fallback:
        flags.append("a reviewer round ran on a fallback model")
    if not state.test_files and ctx.doc.kind in ("feature", "fix"):
        flags.append("no runnable tests were written")
    print("READY TO MERGE: %s (PR %s)\n  review rounds: %d, fix cycles: %d, validation: %s%s\n"
          "  tests landing: %s\n  cost: $%.2f, models: %s\n  merge with: revali merge; "
          "the PR is no longer a draft%s"
          % (ctx.doc.title, state.pr_url or "#%d" % state.pr_number, len(state.rounds), state.fixes,
             vout.result, " (%s)" % vout.skipped_reason if vout.skipped_reason else "",
             ", ".join(state.test_files) or "none", state.cost_usd, ", ".join(state.models_used) or "-",
             "\n  note: " + "; ".join(flags) if flags else ""))
    return EXIT_OK


# ---- preflight --------------------------------------------------------------

def cmd_preflight(args) -> int:
    cwd = os.getcwd()
    rdir = _rdir_for(cwd)
    root = gitops.repo_root(cwd)
    log = RunLog(rdir if rdir and os.path.isdir(rdir) else None, verbose=args.verbose,
                 logs_dir=paths_for(root).logs_dir if root else "")
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
                print("error: the run (pid %s) died at stage '%s' without a result; see %s"
                      % (lock.get("pid"), state.stage,
                         os.path.join(rdir, paths_for(gitops.repo_root(os.getcwd())).logs_dir, "run.log")))
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
    paths = paths_for(root)
    rdir = review_dir(root, branch, paths.state_dir)
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
    base = os.path.join(root, paths.state_dir)
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
    state_dir = paths_for(root).state_dir
    rdir = os.path.join(root, state_dir, safe_branch(name))
    if not os.path.isdir(rdir):
        rdir = os.path.join(root, state_dir, name)
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


def cmd_merge(args) -> int:
    from revali import merge
    cwd = os.getcwd()
    rdir = _rdir_for(cwd)
    if not rdir:
        print("ERROR: not inside a git repository")
        return EXIT_ERROR
    state = State.load(rdir)
    if state is None or state.stage != "ready_to_merge":
        print("ERROR: this branch is not ready to merge (stage: %s); run `revali run` first"
              % (state.stage if state else "none"))
        return EXIT_ERROR
    if lock_owner_alive(rdir):
        print("ERROR: a run is in progress")
        return EXIT_ERROR
    acquire_lock(rdir)
    log = RunLog(rdir, verbose=args.verbose, logs_dir=paths_for(gitops.repo_root(cwd)).logs_dir)
    try:
        code = merge.do_merge(cwd, rdir, state, log)
    except Stop as stop:
        _print_stop(stop)
        if stop.exit_code != EXIT_ACTION:
            state.message = stop.message
            state.save(rdir)
        release_lock(rdir)
        _record_history(state, stop.exit_code)
        return stop.exit_code
    release_lock(rdir)
    _record_history(state, code)
    print(merge.merge_summary(state, state.base))
    root = gitops.repo_root(cwd)
    if root:
        state_dir = paths_for(root).state_dir
        merge.remove_tree(review_dir(root, state.branch, state_dir))
        print("  removed %s/%s/" % (state_dir, safe_branch(state.branch)))
    return code


def cmd_version(args) -> int:
    print("%s %s" % (NAME, VERSION))
    return EXIT_OK
