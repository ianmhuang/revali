"""Subcommand implementations: run (detached or foreground), preflight, wait,
status, reset, clean, stop. Review / validate / merge stages plug in here in
later milestones; until then `run` stops after preflight with a clear message.
"""

import os
import shutil
import sys
import time
import traceback
from typing import Optional

from revali import EXIT_ACTION, EXIT_ERROR, EXIT_HUMAN, EXIT_OK, NAME, VERSION, gitops
from revali.config import ConfigError, history_path, load_user_config, paths_for
from revali.preflight import Stop, check_tree_unmoved, locate, preflight
from revali.procs import kill_tree, pid_alive, python_exe, spawn_detached
from revali.state import (
    LockHeld,
    RunLog,
    State,
    TreeLockHeld,
    acquire_lock,
    acquire_tree_lock,
    append_history,
    lock_owner_alive,
    read_lock,
    read_tree_lock,
    release_lock,
    release_tree_lock,
    review_dir,
    run_died,
    safe_branch,
    tree_lock_owner,
    tree_lock_path,
)


def _interrupted(state: State) -> bool:
    """A reviewer session was started and its round never finished: the flag is set before
    the session is spawned and cleared when the round records its result or discards its
    files, so it survives `revali stop`, Ctrl-C, a kill, and any later run that stops in
    preflight. Nothing else leaves files under test_dir."""
    return state.reviewer_running


STAGE_FOR_EXIT = {EXIT_ACTION: "needs_action", EXIT_HUMAN: "needs_human", EXIT_ERROR: "error"}


def _entry_script() -> str:
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(here, "revali.py")


def _locate_run(cwd: str, branch: str = "", allow_detached: bool = False) -> tuple:
    """(repo root, branch, review dir) for the checked-out (or given) branch. Stop (exit 1)
    outside a repository or, unless allow_detached, on a detached HEAD (branch "HEAD")."""
    root = gitops.repo_root(cwd)
    if not root:
        raise Stop(EXIT_ERROR, "not inside a git repository")
    if not branch:
        try:
            branch = gitops.current_branch(root)
        except gitops.GitError as exc:
            raise Stop(EXIT_ERROR, "not inside a git repository (%s)" % exc) from exc
        if branch == "HEAD" and not allow_detached:
            raise Stop(EXIT_ERROR, "detached HEAD; check out a branch first")
    return root, branch, review_dir(root, branch, paths_for(root).state_dir)


def _located(cwd: str, branch: str = "", allow_detached: bool = False) -> Optional[tuple]:
    """_locate_run for a subcommand: prints the ERROR line and returns None when it cannot."""
    try:
        return _locate_run(cwd, branch, allow_detached)
    except Stop as stop:
        print("ERROR: %s" % stop.message)
        return None


def _rdir_for(cwd: str, branch: str = "") -> Optional[str]:
    try:
        return _locate_run(cwd, branch)[2]
    except Stop:
        return None


def _tree_lock_path(root: str) -> str:
    return tree_lock_path(root, paths_for(root).state_dir)


def _tree_held_message(owner: dict) -> str:
    return (
        "ERROR: a revali run is already in progress in this working tree on branch %s (pid %d); "
        "use `revali wait --branch %s` or `revali stop`"
        % (owner.get("branch", "?"), int(owner.get("pid", 0)), owner.get("branch", "?"))
    )


def _print_identity(root: str, branch: str) -> None:
    """The first line of run / wait / status: which working tree and branch this is about,
    so sessions running revali in several checkouts can tell their output apart."""
    print("repo: %s  branch: %s" % (root, branch))


def _run_log_path(rdir: str) -> str:
    return os.path.join(rdir, paths_for(gitops.repo_root(os.getcwd())).logs_dir, "run.log")


def _print_stop(stop: Stop) -> None:
    label = {EXIT_ACTION: "ACTION NEEDED", EXIT_HUMAN: "NEEDS A HUMAN", EXIT_ERROR: "ERROR"}.get(
        stop.exit_code, "STOP"
    )
    print("%s: %s" % (label, stop.message))


# ---- run --------------------------------------------------------------------


def cmd_run(args) -> int:
    if args.foreground or args.dry_run:  # a dry run spawns nothing, not even itself
        return _run_foreground(args)
    return _run_detached(args)


def _run_detached(args) -> int:
    cwd = os.getcwd()
    found = _located(cwd)
    if not found:
        return EXIT_ERROR
    root, branch, rdir = found
    _print_identity(root, branch)
    pid = lock_owner_alive(rdir)
    if pid:
        print(
            "ERROR: a revali run is already in progress (pid %d); "
            "use `revali wait` or `revali stop`" % pid
        )
        return EXIT_ERROR
    tpath = _tree_lock_path(root)
    owner = tree_lock_owner(tpath)
    if owner:
        print(_tree_held_message(owner))
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
    # Reserve both locks for the child so a second `run` cannot slip in first.
    acquire_lock(rdir, pid=child)
    acquire_tree_lock(tpath, branch, pid=child)
    print("started revali run (pid %d); log: %s" % (child, log_path))
    print("next: `revali wait --timeout 9m` (repeat until it reports a result)")
    return EXIT_OK


def _run_foreground(args) -> int:
    cwd = os.getcwd()
    found = _located(cwd)
    if not found:
        return EXIT_ERROR
    root, branch, rdir = found
    _print_identity(root, branch)
    try:
        acquire_lock(rdir)
    except LockHeld as exc:
        print("ERROR: %s" % exc)
        return EXIT_ERROR
    tpath = _tree_lock_path(root)
    try:
        acquire_tree_lock(tpath, branch)
    except TreeLockHeld as exc:
        release_lock(rdir)
        print(_tree_held_message({"pid": exc.pid, "branch": exc.branch}))
        return EXIT_ERROR
    log = RunLog(rdir, verbose=args.verbose, logs_dir=paths_for(root).logs_dir)
    state = State.load(rdir) or State()
    code = EXIT_ERROR
    try:
        code = _pipeline(args, cwd, rdir, state, log)
    finally:
        release_lock(rdir)
        release_tree_lock(tpath)
    return code


def _pipeline(args, cwd: str, rdir: str, state: State, log: RunLog) -> int:
    started = False  # the "no result yet" mark is on disk
    try:
        # No result yet: with the process gone, `wait` / `status` read -1 as "died" (see
        # state.run_died) instead of the previous run's exit code.
        state.last_exit = -1
        state.save(rdir)
        started = True
        return _stages(args, cwd, rdir, state, log)
    except ConfigError as exc:
        stop = Stop(EXIT_ERROR, "configuration: " + "; ".join(exc.problems))
        _print_stop(stop)
        return _record_stop(state, rdir, log, "error", stop, started)
    except Stop as stop:
        _print_stop(stop)
        return _record_stop(
            state, rdir, log, STAGE_FOR_EXIT.get(stop.exit_code, "error"), stop, started
        )
    except Exception as exc:  # a bug or an OS error: record it so the next run can continue
        tb = traceback.format_exc()
        print(tb, file=sys.stderr, end="")  # the detached child's stderr is run.log
        for line in tb.rstrip("\n").split("\n"):
            log.detail(line)
        # until _stages records its first stage, state.stage is still the previous run's
        where = "last recorded stage '%s'" % state.stage if started else "before its first stage"
        stop = Stop(
            EXIT_ERROR, "the run stopped with %s: %s (%s)" % (type(exc).__name__, exc, where)
        )
        _print_stop(stop)
        return _record_stop(state, rdir, log, "error", stop, started)


def _stage(
    state: State, rdir: str, log: RunLog, stage: str, message: str = "", exit_code=None
) -> None:
    """state.set_stage plus the run's timing: a running stage starts its clock, a terminal
    one closes the running clock."""
    log.timing.stage(stage)
    state.set_stage(rdir, stage, message, exit_code)


def _record_stop(
    state: State, rdir: str, log: RunLog, stage: str, stop: Stop, started: bool
) -> int:
    """Persist a run's outcome. When the state file itself cannot be written, say what `wait`
    will show instead of escaping with a traceback; the exit code stands either way."""
    try:
        _stage(state, rdir, log, stage, stop.message, stop.exit_code)
        _record_history(state, stop.exit_code, log)
    except OSError as exc:
        if started:
            shows = "`wait` and `status` will report the run as dead"
        else:
            shows = "`wait` and `status` still show the previous run's result"
        note = "the state file could not be updated either (%s); %s; see run.log" % (exc, shows)
        log.detail(note)
        print("ERROR: " + note)
    return stop.exit_code


def _record_history(state: State, exit_code: int, log: Optional[RunLog] = None) -> None:
    """The history row for this run. With `log`, the run's timing is closed, written to the
    log as one `run: timing` line, and stored in the row (`stage_s`, `sandbox_s`)."""
    timing = {}
    if log is not None:
        log.timing.close()
        log.stage("run", "timing " + log.timing.summary())
        timing = log.timing.as_dict()
    try:
        path = history_path(load_user_config())
    except ConfigError:
        path = history_path(None)
    try:
        append_history(
            path,
            {
                "repo": state.repo,
                "branch": state.branch,
                "base": state.base,
                "stage": state.stage,
                "exit": exit_code,
                "rounds": len(state.rounds),
                "fixes": state.fixes,
                "last_verdict": state.last_verdict,
                "cost_usd": round(state.cost_usd, 4),
                "models": state.models_used,
                "fallback": state.fallback,
                "pr": state.pr_number,
                **timing,
            },
        )
    except OSError:
        pass


def _rerun_bookkeeping(ctx, state: State, rdir: str, log: RunLog) -> None:
    """Count fix cycles, detect rewritten history, refuse a rerun with no change. On every run
    the reviewer's test commits on the branch (the `Revali-Round` trailer) and their files are
    read back into the state, so the reviewer may update its own files whatever SHAs they now
    sit under: after a rewrite, after `revali reset`, or with a state that forgot them."""
    from revali import review

    cfg = ctx.cfg.review
    rewritten = False
    if state.rounds:
        missing = [
            c for c in state.test_commits if c and not gitops.head_contains(c, ctx.repo_root)
        ]
        if missing:
            log.stage(
                "run",
                "the reviewer's test commits are no longer in HEAD (rebase or reset); "
                "the review starts over from round 1",
            )
            state.rounds, state.test_commits, state.test_files = [], [], []
            state.fixes, state.needs_info_used, state.last_verdict = 0, False, ""
            state.force_push = True  # the remote still has the dropped commits
            rewritten = True
        elif state.stage == "needs_action":
            if state.last_verdict in (review.CHANGES_REQUESTED, "FAIL"):
                if ctx.head_sha == state.head_sha:
                    raise Stop(
                        EXIT_ACTION,
                        "nothing changed since the last review (HEAD %s); "
                        "fix, commit, then run again" % ctx.head_sha[:10],
                    )
                state.fixes += 1
                log.stage("run", "fix cycle %d of %d" % (state.fixes, cfg.max_fixes))
    commits, _ = review.recover_test_ownership(ctx, state, log)
    if rewritten and not commits:
        log.stage(
            "run",
            "no commit between %s and HEAD carries a %s trailer; the reviewer's earlier "
            "test files, if any, now count as existing files it must not modify"
            % (ctx.base_ref, review.TRAILER),
        )
    if state.fixes > cfg.max_fixes:
        raise Stop(
            EXIT_HUMAN,
            "%d fix cycles used (limit %d); a human decides how to proceed. "
            "Latest review: %s"
            % (state.fixes, cfg.max_fixes, os.path.join(rdir, "review-%d.md" % len(state.rounds))),
        )


def _approved_round_awaiting_validation(ctx, state: State) -> int:
    """The round whose APPROVE was recorded but never validated (the previous run died in
    between), when HEAD is still where that round left it: the reviewer's test commit, or
    the reviewed HEAD when it committed none. 0 otherwise. Called before the state takes the
    new HEAD."""
    from revali.review import APPROVE

    if not state.rounds:
        return 0
    last = state.rounds[-1]
    round_no = len(state.rounds)
    if last.get("verdict") != APPROVE:
        return 0
    if any(v.get("round") == round_no for v in state.validations):
        return 0
    if ctx.head_sha != (last.get("test_commit") or last.get("head_sha")):
        return 0
    return round_no


def _model_label(chosen) -> str:
    return chosen.model + (" (%s)" % chosen.reason if chosen.reason else "")


def _stages(args, cwd: str, rdir: str, state: State, log: RunLog) -> int:
    from revali import pr as prstage
    from revali import review, validate

    if not state.repo:
        # so a run that stops in preflight still names its repo in history
        state.repo = gitops.remote_repo("origin", cwd)
    first_pass = not state.rounds and not args.dry_run
    baseline_hook = (lambda ctx: validate.baseline(ctx, rdir, log)) if first_pass else None
    cleanup_hook = review.interruption_cleanup(state, rdir, log) if not args.dry_run else None
    ctx = preflight(
        cwd,
        base_override=args.base or "",
        dry_run=args.dry_run,
        log=log,
        baseline=baseline_hook,
        before_tree=cleanup_hook,
        tolerate=state.pending_test_files,
    )
    _rerun_bookkeeping(ctx, state, rdir, log)
    resume_round = _approved_round_awaiting_validation(ctx, state)
    state.branch, state.base = ctx.branch, ctx.base
    # lowercased like gitops.remote_repo, so stats groups both sources under one row
    state.repo = ("%s/%s" % (ctx.repo.owner, ctx.repo.name)).lower() if ctx.repo else ""
    state.head_sha, state.base_sha = ctx.head_sha, ctx.base_sha
    _stage(state, rdir, log, "preflight", "preflight passed")

    if args.dry_run:
        msg = (
            "dry run: would push %s, open a draft PR against %s, run reviewer %s (round %d), "
            "then stop"
            % (
                ctx.branch,
                ctx.base,
                _model_label(review.planned_reviewer(ctx)),
                len(state.rounds) + 1,
            )
        )
        log.stage("run", msg)
        _stage(state, rdir, log, "preflight", msg, EXIT_OK)
        log.timing.close()
        log.stage("run", "timing " + log.timing.summary())
        print("DRY RUN OK: " + msg)
        return EXIT_OK

    _stage(state, rdir, log, "pr", "pushing and opening the PR")
    prstage.ensure_pr(ctx, state, rdir, log)
    state.head_sha = ctx.head_sha

    if resume_round:
        log.stage(
            "run",
            "round %d was approved at %s but the previous run stopped before validation; "
            "continuing there without a new review" % (resume_round, ctx.head_sha[:10]),
        )
        record = state.rounds[-1]
        return _validate_and_finish(
            ctx,
            state,
            rdir,
            log,
            resume_round,
            record.get("data", {}),
            os.path.join(rdir, "review-%d.md" % resume_round),
        )

    _stage(state, rdir, log, "review", "reviewer round %d" % (len(state.rounds) + 1))
    outcome = review.run_round(ctx, state, rdir, log)
    if outcome.commit_sha:
        check_tree_unmoved(ctx, tail="the test commit was not pushed")
        state.pending_effect = "push"
        state.save(rdir)
        res = gitops.push_branch(ctx.branch, ctx.repo_root, log.detail)
        state.pending_effect = ""
        if not res.ok:
            raise Stop(EXIT_ERROR, "git push of the test commit failed: %s" % res.text.strip())
        state.head_sha = outcome.commit_sha
    comment = outcome.review_md
    if prstage.is_public(ctx):
        comment = review.render_review_summary(
            outcome.data,
            outcome.verdict,
            outcome.round_no,
            outcome.model_actual,
            outcome.cost,
            [a[0] for a in ctx.doc.acs],
            ctx.cfg.paths.state_dir,
        )
    prstage.post_comment(ctx, state, rdir, "review-%d" % outcome.round_no, comment, log)

    counts = review.counts_label(outcome.data, outcome.review_path)
    others = review.non_blocking_note(outcome.data, outcome.round_no, outcome.review_path, rdir)
    if outcome.verdict == review.NEEDS_INFO:
        questions = "\n".join("  - " + q for q in outcome.data.get("questions", []))
        pending = ""
        if state.pending_test_files:
            pending = (
                "\nThe reviewer's draft test files stay uncommitted in %s until the next round; "
                "leave them alone and do not commit them:\n%s"
                % (
                    ctx.cfg.project.test_dir,
                    "\n".join("  - " + p for p in state.pending_test_files),
                )
            )
        _stage(
            state,
            rdir,
            log,
            "needs_action",
            "reviewer needs information (%s)" % counts,
            EXIT_ACTION,
        )
        prstage.update_body(ctx, state, rdir, log)
        _record_history(state, EXIT_ACTION, log)
        print(
            "ACTION NEEDED: the reviewer has questions (round %d). Answer them in %s, adjust "
            "change.md if the acceptance criteria were unclear, then run again.\n%s%s%s"
            % (
                outcome.round_no,
                os.path.join(rdir, "response-%d.md" % outcome.round_no),
                questions,
                pending,
                others,
            )
        )
        return EXIT_ACTION
    if outcome.verdict == review.CHANGES_REQUESTED:
        reasons = "\n".join("  - " + r for r in outcome.reasons)
        _stage(
            state,
            rdir,
            log,
            "needs_action",
            "changes requested in round %d (%s)" % (outcome.round_no, counts),
            EXIT_ACTION,
        )
        prstage.update_body(ctx, state, rdir, log)
        _record_history(state, EXIT_ACTION, log)
        print(
            "ACTION NEEDED: changes requested (round %d, fix cycle %d of %d). Full review: %s\n"
            "Fix what blocks, or answer each finding in %s (fixed / wontfix: <reason>), "
            "commit, run again.\n%s%s"
            % (
                outcome.round_no,
                state.fixes,
                ctx.cfg.review.max_fixes,
                outcome.review_path,
                os.path.join(rdir, "response-%d.md" % outcome.round_no),
                reasons,
                others,
            )
        )
        return EXIT_ACTION

    return _validate_and_finish(
        ctx, state, rdir, log, outcome.round_no, outcome.data, outcome.review_path
    )


def _validate_and_finish(
    ctx, state: State, rdir: str, log: RunLog, round_no: int, data: dict, review_path: str
) -> int:
    """After an APPROVE (this run's, or one a previous run recorded and never validated)."""
    from revali import pr as prstage
    from revali import review, validate

    counts = review.counts_label(data, review_path)
    others = review.non_blocking_note(data, round_no, review_path, rdir)
    check_tree_unmoved(ctx, tail="validation was not started")
    _stage(state, rdir, log, "validate", "review approved in round %d; validating" % round_no)
    prstage.update_body(ctx, state, rdir, log)
    vout = validate.run_validation(ctx, state, rdir, log)
    section = vout.section_md
    if prstage.is_public(ctx):
        section = validate.render_section_summary(vout, ctx.cfg.paths.state_dir)
    prstage.post_comment(ctx, state, rdir, "validate-%d" % vout.number, section, log)

    if vout.result == validate.FAIL:
        summary = validate.summary_for_author(vout, rdir)
        _stage(
            state,
            rdir,
            log,
            "needs_action",
            "validation %d failed at %s (%s)" % (vout.number, vout.failed_step, counts),
            EXIT_ACTION,
        )
        prstage.update_body(ctx, state, rdir, log)
        _record_history(state, EXIT_ACTION, log)
        print(
            "ACTION NEEDED: %s\nFix (or correct the test if the diagnosis says the test is wrong, "
            "and say so in %s), commit, run again.%s"
            % (summary, os.path.join(rdir, "response-%d.md" % round_no), others)
        )
        return EXIT_ACTION

    _stage(state, rdir, log, "ready_to_merge", "validation %d passed" % vout.number, EXIT_OK)
    prstage.mark_ready(ctx, state, log)
    prstage.update_body(ctx, state, rdir, log)
    _record_history(state, EXIT_OK, log)
    flags = []
    if state.fallback:
        flags.append("a reviewer round ran on a fallback model")
    if not state.test_files and ctx.doc.kind in ("feature", "fix"):
        flags.append("no runnable tests were written")
    print(
        "READY TO MERGE: %s (PR %s)\n  review rounds: %d, fix cycles: %d, validation: %s%s\n"
        "  tests landing: %s\n  cost: $%.2f, models: %s\n  merge with: revali merge; "
        "the PR is no longer a draft%s"
        % (
            ctx.doc.title,
            state.pr_url or "#%d" % state.pr_number,
            len(state.rounds),
            state.fixes,
            vout.result,
            " (%s)" % vout.skipped_reason if vout.skipped_reason else "",
            ", ".join(state.test_files) or "none",
            state.cost_usd,
            ", ".join(state.models_used) or "-",
            "\n  note: " + "; ".join(flags) if flags else "",
        )
    )
    return EXIT_OK


# ---- preflight --------------------------------------------------------------


def cmd_preflight(args) -> int:
    cwd = os.getcwd()
    rdir = _rdir_for(cwd)
    root = gitops.repo_root(cwd)
    log = RunLog(
        rdir if rdir and os.path.isdir(rdir) else None,
        verbose=args.verbose,
        logs_dir=paths_for(root).logs_dir if root else "",
    )
    state = (State.load(rdir) if rdir else None) or State()
    try:
        # the same view of the tree as `run`: a NEEDS_INFO round's files may be dirty
        preflight(
            cwd,
            base_override=args.base or "",
            dry_run=True,
            log=log,
            tolerate=state.pending_test_files,
        )
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
    found = _located(os.getcwd(), args.branch)
    if not found:
        return EXIT_ERROR
    root, branch, rdir = found
    _print_identity(root, branch)
    deadline = time.monotonic() + parse_duration(args.timeout)
    while True:
        pid = lock_owner_alive(rdir)
        state = State.load(rdir)
        if not pid:
            if state is None:
                print("no revali run recorded for this branch")
                return EXIT_ERROR
            if run_died(state):
                lock = read_lock(rdir)  # stale when present: nobody alive owns it
                release_lock(rdir)
                print(
                    "error: the run%s died at stage '%s' without a result; see %s"
                    % (
                        " (pid %s)" % lock.get("pid") if lock else "",
                        state.stage,
                        _run_log_path(rdir),
                    )
                )
                return EXIT_ERROR
            print("%s: %s" % (state.stage, state.message))
            return state.last_exit if state.last_exit >= 0 else EXIT_ERROR
        if time.monotonic() >= deadline:
            stage = state.stage if state else "starting"
            again = "revali wait --branch %s" % args.branch if args.branch else "revali wait"
            print("still running (pid %d), stage %s; call `%s` again" % (pid, stage, again))
            return EXIT_OK + 4  # 4: still running, distinct from the pipeline codes
        time.sleep(min(2.0, max(0.1, deadline - time.monotonic())))


def cmd_status(args) -> int:
    found = _located(os.getcwd(), args.branch)
    if not found:
        return EXIT_ERROR
    root, branch, rdir = found
    paths = paths_for(root)
    state = State.load(rdir)
    pid = lock_owner_alive(rdir)
    _print_identity(root, branch)
    print("%s %s" % (NAME, VERSION))
    print("branch: %s" % branch)
    if pid:
        print("running: yes (pid %d)" % pid)
    if state is None:
        print("state: none")
    else:
        print("stage: %s" % state.stage)
        if not pid and run_died(state):
            print(
                "running: no; the last run stopped at stage '%s' without a result (see %s)"
                % (state.stage, _run_log_path(rdir))
            )
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
        stale = [
            d
            for d in os.listdir(base)
            if os.path.isdir(os.path.join(base, d)) and d not in existing
        ]
        if stale:
            print(
                "stale review dirs (branch no longer exists): %s  -> `revali clean <name>`"
                % ", ".join(stale)
            )
    return EXIT_OK


def cmd_reset(args) -> int:
    found = _located(os.getcwd())
    if not found:
        return EXIT_ERROR
    root, branch, rdir = found
    _print_identity(root, branch)
    if lock_owner_alive(rdir):
        print("ERROR: a run is in progress; `revali stop` first")
        return EXIT_ERROR
    path = State.path(rdir)
    state = State.load(rdir)
    if state is not None and (state.pending_test_files or state.reviewer_running):
        _reset_test_dir(state, rdir)
    if os.path.isfile(path):
        os.unlink(path)
        print("state removed: %s (review files kept)" % path)
    else:
        print("no state to remove")
    release_lock(rdir)
    return EXIT_OK


def _reset_test_dir(state: State, rdir: str) -> None:
    """The reviewer's uncommitted test files would outlive the state as a dirty tree the next
    run refuses, so `reset` disposes of them the way the run after an interrupted round does:
    untracked drafts deleted, a modified tracked file of the reviewer's own restored from HEAD.
    Without a usable project (config, change.md) the paths are printed for the author instead."""
    from revali import review

    cwd = os.getcwd()
    pending = list(state.pending_test_files)

    def by_hand(reason: str, paths) -> None:
        listed = list(paths) or [
            "(the interrupted session's untracked files under test_dir "
            "matching test_file_pattern)"
        ]
        print(
            "could not clean up the reviewer's uncommitted test files (%s); delete them by hand "
            "before the next run:\n  %s" % (reason, "\n  ".join(listed))
        )

    try:
        ctx = locate(cwd)
        log = RunLog(rdir, logs_dir=paths_for(gitops.repo_root(cwd)).logs_dir)
        # an interrupted session's files are not known, so that case sweeps the whole pattern;
        # otherwise only the pending list is the reviewer's, an author's own draft stays
        only = None if state.reviewer_running else pending
        review.discard_round_leftovers(
            ctx, state, log, "the reviewer", stage="reset", only=only, tolerated_next_run=False
        )
    except (Stop, gitops.GitError) as exc:
        message = exc.message if isinstance(exc, Stop) else str(exc)
        by_hand(message.splitlines()[0], pending)
        return
    if state.pending_test_files:
        print("delete by hand before the next run: %s" % ", ".join(state.pending_test_files))


def cmd_clean(args) -> int:
    root = gitops.repo_root(os.getcwd())
    if not root:
        print("ERROR: not inside a git repository")
        return EXIT_ERROR
    name = args.branch
    _print_identity(root, name)
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
    # a detached HEAD is fine here: the run to stop is found through tree.lock
    found = _located(os.getcwd(), allow_detached=True)
    if not found:
        return EXIT_ERROR
    root, branch, rdir = found
    tpath = _tree_lock_path(root)
    pid = lock_owner_alive(rdir)
    if not pid:
        # The working tree's live run, whichever branch it names (this one too, when its own
        # lock is missing): its branch's state is the one to close. A stale record is only
        # followed on a detached HEAD, where it is the one thing that still names the branch
        # of a run that died; on a branch, a stale record for another branch is ignored and
        # removed as before. Read it before tree_lock_owner removes it.
        record = read_tree_lock(tpath) if branch == "HEAD" else None
        owner = tree_lock_owner(tpath)  # a stale file goes here
        named = owner or record
        if named and named.get("branch"):
            branch = str(named["branch"])
            rdir = review_dir(root, branch, paths_for(root).state_dir)
        if owner:
            pid = int(owner["pid"])
    _print_identity(root, branch)
    if not pid:
        release_lock(rdir)  # stale when present: nobody alive owns it
        release_tree_lock(tpath)
        state = State.load(rdir)
        if state is not None and run_died(state):
            # The process is gone and nothing recorded a result: close the episode so `wait`
            # and `status` stop reporting a death. Only the outcome fields change; what the
            # next run needs (reviewer_running, pending files, rounds, head_sha) stays.
            died_at = state.stage
            if not _close_stopped(
                state,
                rdir,
                "found dead at stage '%s' with no result recorded; marked stopped "
                "by `revali stop`" % died_at,
            ):
                return EXIT_ERROR
            print(
                "no live process; the run found dead at stage '%s' is now recorded as stopped"
                % died_at
            )
            return EXIT_OK
        print("no run in progress")
        return EXIT_OK
    kill_tree(pid)
    for _ in range(50):
        if not pid_alive(pid):
            break
        time.sleep(0.1)
    release_lock(rdir)
    release_tree_lock(tpath)
    state = State.load(rdir)
    print("stopped pid %d" % pid)
    if state is not None and not _close_stopped(
        state, rdir, "stopped by user at stage '%s'" % state.stage
    ):
        return EXIT_ERROR
    return EXIT_OK


def _close_stopped(state: State, rdir: str, message: str) -> bool:
    """Record a run closed by `stop`: stage `stopped`, exit 1, a history row so `stats` sees
    the episode. A state file that cannot be written (a reader holding it on Windows past the
    retry window) is reported on one line, not as a traceback, and `state` is put back the
    way it was; the run then still reads as dead, which is what it is."""
    before = (state.stage, state.message, state.last_exit, state.started_at, state.updated_at)
    try:
        state.set_stage(rdir, "stopped", message, EXIT_ERROR)
    except OSError as exc:
        # set_stage assigns the outcome, and save stamps the timestamps, before the write
        state.stage, state.message, state.last_exit, state.started_at, state.updated_at = before
        print(
            "ERROR: the state file could not be updated (%s); `wait` and `status` will report "
            "the run as dead; run `revali stop` again once the file is free" % exc
        )
        return False
    _record_history(state, EXIT_ERROR)
    return True


def cmd_merge(args) -> int:
    from revali import merge

    cwd = os.getcwd()
    found = _located(cwd)
    if not found:
        return EXIT_ERROR
    root, branch, rdir = found
    _print_identity(root, branch)
    state = State.load(rdir)
    if state is None or state.stage != "ready_to_merge":
        print(
            "ERROR: this branch is not ready to merge (stage: %s); run `revali run` first"
            % (state.stage if state else "none")
        )
        return EXIT_ERROR
    tpath = _tree_lock_path(root)
    if lock_owner_alive(rdir) or tree_lock_owner(tpath):
        print("ERROR: a run is in progress")
        return EXIT_ERROR
    try:
        acquire_lock(rdir)  # a `run` may have taken it since the check above
    except LockHeld as exc:
        print("ERROR: %s" % exc)
        return EXIT_ERROR
    try:
        acquire_tree_lock(tpath, branch)  # the checkout and pull below must not race a `run`
    except TreeLockHeld as exc:
        release_lock(rdir)
        print(_tree_held_message({"pid": exc.pid, "branch": exc.branch}))
        return EXIT_ERROR
    log = RunLog(rdir, verbose=args.verbose, logs_dir=paths_for(root).logs_dir)
    try:
        code = merge.do_merge(cwd, rdir, state, log)
    except Stop as stop:
        _print_stop(stop)
        if stop.exit_code != EXIT_ACTION:
            state.message = stop.message
            state.save(rdir)
        _record_history(state, stop.exit_code)
        return stop.exit_code
    finally:
        release_lock(rdir)
        release_tree_lock(tpath)
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
