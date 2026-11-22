"""Subcommand implementations: run (detached or foreground), preflight, wait,
status, reset, clean, stop. Review / validate / merge stages plug in here in
later milestones; until then `run` stops after preflight with a clear message.
"""

import copy
import os
import shutil
import sys
import time
import traceback
from typing import Optional, Tuple

from revali import EXIT_ACTION, EXIT_ERROR, EXIT_HUMAN, EXIT_OK, NAME, VERSION, gitops
from revali.build import build
from revali.config import (
    ConfigError,
    archive_root,
    history_path,
    load_user_config,
    paths_for,
    release_label,
)
from revali.issues import opened_count
from revali.preflight import Stop, check_tree_unmoved, dirty_tree_problem, locate, preflight
from revali.procs import ProcError, kill_tree, pid_alive, python_exe, spawn_detached
from revali.state import (
    LockHeld,
    RunLog,
    State,
    StateError,
    TreeLockHeld,
    acquire_lock,
    acquire_tree_lock,
    append_history,
    clear_wsl_retries,
    cost_per_round,
    lock_owner_alive,
    read_lock,
    read_tree_lock,
    read_wsl_retries,
    release_lock,
    release_tree_lock,
    review_dir,
    run_died,
    safe_branch,
    tree_lock_owner,
    tree_lock_path,
    wsl_retries_path,
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


def state_error_line(exc: StateError) -> str:
    """How every command reports a state file it cannot use (cli.main, `stop`)."""
    return "ERROR: %s; fix the file or run `revali reset` to clear it" % exc


# ---- run --------------------------------------------------------------------


def cmd_run(args) -> int:
    model = getattr(args, "reviewer_model", None)
    if model is not None and not model.strip():
        print("ERROR: --reviewer-model needs a model name (e.g. --reviewer-model opus)")
        return EXIT_ERROR
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
    # this run's result replaces the previous one even when it is a refusal, so must its record;
    # a failure is not reported here: a spawned child clears again and logs it, and a refused
    # run prints only its refusal
    clear_wsl_retries(rdir, paths_for(root).logs_dir)
    problem, state = _dirty_before_detach(root, rdir)
    if problem:
        problem = _noted(problem, _merge_note(state, root) if state else "")
        print("ERROR: %s" % problem)
        _record_refusal(state or State(), rdir, problem)
        return EXIT_ERROR
    logs = os.path.join(rdir, paths_for(gitops.repo_root(cwd)).logs_dir)
    os.makedirs(logs, exist_ok=True)
    log_path = os.path.join(logs, "run.log")
    # before the spawn: a slow git here must not leave the child running while this process
    # has not yet said its pid (PR #62 F2)
    revali_build = build()
    cmd = [python_exe(), _entry_script(), "run", "--foreground"]
    if args.base:
        cmd += ["--base", args.base]
    if getattr(args, "reviewer_model", None):
        cmd += ["--reviewer-model", args.reviewer_model.strip()]
    if args.dry_run:
        cmd.append("--dry-run")
    if args.verbose:
        cmd.append("--verbose")
    child = spawn_detached(cmd, cwd=cwd, log_path=log_path)
    # Reserve both locks for the child so a second `run` cannot slip in first.
    acquire_lock(rdir, pid=child)
    acquire_tree_lock(tpath, branch, pid=child)
    print("started revali run (pid %d); log: %s; revali %s" % (child, log_path, revali_build))
    print("next: `revali wait --timeout 9m` (repeat until it reports a result)")
    return EXIT_OK


def _dirty_before_detach(root: str, rdir: str) -> Tuple[str, Optional[State]]:
    """Preflight's clean-tree refusal, asked before the child is spawned so a dirty tree is
    reported here instead of in run.log, with the state it was read from. The problem is ""
    when the tree is clean, when an interrupted round left files the child cleans up before
    its own check (`reviewer_running`), or when the state file or git status cannot be read
    (the child reports that). A state file State cannot use raises StateError here, before
    anything is spawned."""
    try:
        state = State.load(rdir)
    except OSError:
        return "", None
    if state is not None and state.reviewer_running:
        return "", state
    tolerate = state.pending_test_files if state is not None else []
    try:
        return dirty_tree_problem(root, paths_for(root).state_dir, tolerate), state
    except (gitops.GitError, ProcError):  # ProcError: git missing or timed out
        return "", state


def _merge_note(state: State, root: str) -> str:
    """The line a stop adds while the validation that made the branch ready still stands, with
    HEAD where that validation passed: a later run that stopped before its PR stage, or the
    ready run itself stopping after it recorded `ready_to_merge`. "" when it does not or HEAD
    cannot be read. A condition, not a promise: `revali merge` still checks the tree and CI."""
    if not state.validated_sha:
        return ""
    try:
        head = gitops.rev_parse("HEAD", root)
    except (gitops.GitError, ProcError):
        return ""
    if head != state.validated_sha:
        return ""
    return "validation %d still stands for HEAD %s; `revali merge` may still merge it" % (
        len(state.validations),
        head[:10],
    )


def _noted(message: str, note: str) -> str:
    """`message` with `note` as its last line, so a header and the list it introduces (the
    dirty paths, the lint output) stay together."""
    if not note:
        return message
    return message.rstrip("\n") + "\n" + note


def _stop_noted(stop: Stop, note: str) -> Stop:
    return Stop(stop.exit_code, _noted(stop.message, note), cost=stop.cost) if note else stop


def _record_refusal(state: State, rdir: str, problem: str) -> None:
    """Record a start refused before detaching the way the child records a preflight stop
    (stage `error`, the message, exit 1), so `wait` and `status` report the refusal and not
    the previous run's result. Nothing ran, so no history row and no run.log line."""
    try:
        state.set_stage(rdir, "error", problem, EXIT_ERROR)
    except OSError as exc:
        print(
            "ERROR: the state file could not be updated either (%s); `wait` and `status` "
            "still show the previous run's result" % exc
        )


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
    logs_dir = paths_for(root).logs_dir
    log = RunLog(rdir, verbose=args.verbose, logs_dir=logs_dir)
    try:
        state = State.load(rdir) or State()
    except StateError:  # reported by cli.main; this run never started
        release_lock(rdir)
        release_tree_lock(tpath)
        raise
    code = EXIT_ERROR
    try:
        code = _pipeline(args, cwd, rdir, state, log, logs_dir)
    finally:
        release_lock(rdir)
        release_tree_lock(tpath)
    return code


def _pipeline(args, cwd: str, rdir: str, state: State, log: RunLog, logs_dir: str) -> int:
    started = False  # the "no result yet" mark is on disk
    try:
        # inside the try: a log write that fails here is recorded like any other error, and
        # the caller's finally still releases the locks
        problem = clear_wsl_retries(rdir, logs_dir)
        if problem:
            log.detail(problem)
        # No result yet: with the process gone, `wait` / `status` read -1 as "died" (see
        # state.run_died) instead of the previous run's exit code.
        state.last_exit = -1
        state.save(rdir)
        started = True
        return _stages(args, cwd, rdir, state, log)
    except ConfigError as exc:
        stop = Stop(EXIT_ERROR, "configuration: " + "; ".join(exc.problems))
        stop = _stop_noted(stop, _merge_note(state, cwd))
        _print_stop(stop)
        return _record_stop(state, rdir, log, "error", stop, started)
    except Stop as stop:
        stop = _stop_noted(stop, _merge_note(state, cwd))
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
        stop = _stop_noted(stop, _merge_note(state, cwd))
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
    will show instead of escaping with a traceback; the exit code stands either way. The
    history row and the `run: timing` line are written after that, so the timing line ends
    the log whether or not the state file could be saved."""
    try:
        _stage(state, rdir, log, stage, stop.message, stop.exit_code)
    except OSError as exc:
        if started:
            shows = "`wait` and `status` will report the run as dead"
        else:
            shows = "`wait` and `status` still show the previous run's result"
        note = "the state file could not be updated either (%s); %s; see run.log" % (exc, shows)
        log.detail(note)
        print("ERROR: " + note)
    _record_history(state, stop.exit_code, log)
    return stop.exit_code


def _record_history(
    state: State, exit_code: int, log: Optional[RunLog] = None, extra: Optional[dict] = None
) -> None:
    """The history row for this run. With `log`, the run's timing is closed, written to the
    log as one `run: timing` line, and stored in the row (`stage_s`, `sandbox_s`). `extra`
    adds fields (`revali archive`: `merged_outside`, `stage_before`)."""
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
        unlocked = append_history(
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
                "issues": opened_count(state),  # not the open ones reused (found)
                "revali_build": build(),
                **timing,
                **(extra or {}),
            },
        )
    except OSError:
        return
    if unlocked and log is not None:
        log.detail("history row appended without its lock: %s" % unlocked)


def _rerun_bookkeeping(ctx, state: State, rdir: str, log: RunLog) -> None:
    """Count fix cycles, detect rewritten history, refuse a rerun with no change. On every run
    the reviewer's test commits on the branch (the `Revali-Round` trailer) and their files are
    read back into the state, so the reviewer may update its own files whatever SHAs they now
    sit under: after a rewrite, after `revali reset`, or with a state that forgot them."""
    from revali import review, testarchive

    cfg = ctx.cfg.review
    rewritten = False
    archive = cfg.tests == testarchive.ARCHIVE_MODE
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
            state.fix_due = False  # owed to a round that is gone
            state.tests_mode = ""  # the rounds that fixed the mode are gone with them
            state.baseline_sha = ""  # the tree it passed on is gone; validation runs the suite
            state.baseline_platforms = []
            state.force_push = True  # the remote still has the dropped commits
            rewritten = True
    if state.rounds:  # still: a restart above dropped them, mode and all
        recorded = state.tests_mode or "commit"
        if recorded != cfg.tests:
            raise Stop(
                EXIT_ACTION,
                '[review] tests is "%s" but the %d review round(s) of this branch ran with '
                '"%s"; the mode is fixed for the life of a branch: set it back to "%s", or '
                "start a new branch for the other mode"
                % (cfg.tests, len(state.rounds), recorded, recorded),
            )
        if archive and testarchive.history_rewritten(ctx, state):
            # no reviewer commit to lose: the review goes on, the remote needs a forced push
            log.stage(
                "run",
                "the branch's history was rewritten since round %d (%s is no longer in HEAD); "
                "archive mode: the review continues, the push will use --force-with-lease"
                % (len(state.rounds), state.head_sha[:10]),
            )
            state.force_push = True
        if state.fix_due:  # not the stage: a stop since then may have recorded `error`
            if ctx.head_sha == state.head_sha:
                last = len(state.rounds)
                asked = ""
                if state.last_verdict == review.CHANGES_REQUESTED:
                    asked = "; round %d requested changes: %s" % (
                        last,
                        os.path.join(rdir, "review-%d.md" % last),
                    )
                raise Stop(
                    EXIT_ACTION,
                    "nothing changed since the last review (HEAD %s)%s; "
                    "fix, commit, then run again" % (ctx.head_sha[:10], asked),
                )
            state.fixes += 1
            state.fix_due = False
            log.stage("run", "fix cycle %d of %d" % (state.fixes, cfg.max_fixes))
    if archive:
        testarchive.rebuild(ctx, state, rdir, log)
        commits = []
    else:
        commits, _ = review.recover_test_ownership(ctx, state, log)
        stale = testarchive.archived_files(rdir)
        if stale:  # left by archive-mode rounds a reset or a restart dropped; never used again
            where = os.path.relpath(testarchive.archive_dir(rdir), ctx.repo_root)
            log.stage(
                "run",
                "%d archived test file(s) under %s from earlier archive-mode rounds; commit "
                "mode does not use them, and they travel to [paths] archive_dir at merge "
                "unless you delete that directory: %s"
                % (len(stale), where.replace("\\", "/"), ", ".join(stale)),
            )
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
    from revali import review, validate

    if not state.repo:
        # so a run that stops in preflight still names its repo in history
        state.repo = gitops.remote_repo("origin", cwd)
    # the preflight clock starts here: preflight() below includes the baseline sandbox session
    # unless it runs in parallel, and the state's preflight stage is only recorded once it has
    # passed
    log.timing.stage("preflight")
    first_pass = not state.rounds and not args.dry_run
    jobs = []

    def baseline_hook(ctx):
        if not ctx.cfg.validate.parallel_baseline:
            validate.baseline(ctx, state, rdir, log)
        elif validate.baseline_plan(ctx) is not None:
            jobs.append(validate.BaselineJob.start(ctx, log))
        else:
            validate.run_baseline(ctx, ctx.head_sha, log)  # logs why there is none

    cleanup_hook = review.interruption_cleanup(state, rdir, log) if not args.dry_run else None
    ctx = preflight(
        cwd,
        base_override=args.base or "",
        dry_run=args.dry_run,
        log=log,
        baseline=baseline_hook if first_pass else None,
        before_tree=cleanup_hook,
        tolerate=state.pending_test_files,
    )
    ctx.reviewer_model = (getattr(args, "reviewer_model", None) or "").strip()
    job = jobs[0] if jobs else None
    if job is None:
        return _after_preflight(args, ctx, state, rdir, log, None)
    try:
        return _after_preflight(args, ctx, state, rdir, log, job)
    except Stop as stop:
        failure = job.finish()
        if failure is not None and not job.raised:
            # the baseline failed while something else ended the run: report the baseline, as
            # the run would have before parallel_baseline, and say what happened to the rest
            raise validate.with_note(failure, _baseline_note(stop)) from None
        raise
    finally:
        job.finish()  # never leave a sandbox session running behind the run
        try:
            job.record(state, rdir)  # a passed baseline is kept whatever ended the run
        except OSError as exc:
            # the state file is busy; the run's own outcome matters more than this sha, and
            # validation reruns the suite without it
            log.detail("the passed baseline could not be recorded: %s" % exc)


def _baseline_note(stop: Stop) -> str:
    from revali.engines.base import SessionCancelled

    if isinstance(stop, SessionCancelled):
        return "the reviewer session was stopped when the baseline failed; its cost is not known"
    return "the run also stopped for this: %s" % stop.message.split("\n")[0]


def _after_preflight(args, ctx, state: State, rdir: str, log: RunLog, baseline) -> int:
    from revali import pr as prstage
    from revali import review

    _rerun_bookkeeping(ctx, state, rdir, log)
    resume_round = _approved_round_awaiting_validation(ctx, state)
    state.branch = ctx.branch
    # lowercased like gitops.remote_repo, so stats groups both sources under one row
    state.repo = ("%s/%s" % (ctx.repo.owner, ctx.repo.name)).lower() if ctx.repo else ""
    if not (args.dry_run and state.validated_sha):
        # a dry run on a ready branch leaves what `revali merge` reads (a `--base` dry run
        # would otherwise point the merge's checkout and pull at another base)
        state.base = ctx.base
        state.head_sha, state.base_sha = ctx.head_sha, ctx.base_sha
    if not args.dry_run:
        # this run reviews again: an earlier validation no longer counts, from the same save
        # that records the run's base and head
        state.validated_sha = ""
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
        label = release_label(ctx.cfg, ctx.doc)
        if label:
            log.stage("run", "dry run: would give the PR the release label '%s'" % label)
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
        if ctx.reviewer_model:  # PR #99 round 1 follow-up: say the option did nothing
            log.stage(
                "run",
                "--reviewer-model %s is not used: this run has no review round"
                % ctx.reviewer_model,
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
    outcome = review.run_round(ctx, state, rdir, log, baseline=baseline)
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
            model_note=outcome.model_reason if ctx.reviewer_model else "",
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
        from revali import stophint

        reasons = "\n".join("  - " + r for r in outcome.reasons)
        state.fix_due = True  # owed since review.run_round saved the round; saved by _stage
        lows = []
        if review.reviewer_only(outcome.data, outcome.gaps):  # a wontfix cannot answer the rest
            lows = stophint.derived_lows(state, ctx.repo_root, log.detail)
        last = _last_cycle(ctx, state)
        message = "changes requested in round %d (%s)" % (outcome.round_no, counts)
        if lows:
            message += "; " + stophint.short(lows, ready=False, last_cycle=last)
            others += "\n" + stophint.hint_line(
                lows, outcome.round_no, rdir, ready=False, last_cycle=last
            )
        if last:
            message += "; " + LAST_CYCLE
        _stage(state, rdir, log, "needs_action", message, EXIT_ACTION)
        prstage.update_body(ctx, state, rdir, log)
        _record_history(state, EXIT_ACTION, log)
        print(
            "ACTION NEEDED: changes requested (round %d, fix cycle %d of %d). Full review: %s\n"
            "Fix what blocks, or answer each finding in %s (fixed / wontfix: <reason>)%s\n%s%s"
            % (
                outcome.round_no,
                state.fixes,
                ctx.cfg.review.max_fixes,
                outcome.review_path,
                os.path.join(rdir, "response-%d.md" % outcome.round_no),
                _then(ctx, state),
                reasons,
                others,
            )
        )
        return EXIT_ACTION

    return _validate_and_finish(
        ctx, state, rdir, log, outcome.round_no, outcome.data, outcome.review_path
    )


# the end of the stage message after a round that used the last fix cycle, hint or not
LAST_CYCLE = "last fix cycle: the next run stops for a human"


def _last_cycle(ctx, state: State) -> bool:
    """The round that owes a fix used the last fix cycle: the next run on a new commit counts
    one more and stops with exit 3 in `_rerun_bookkeeping` instead of reviewing."""
    return state.fixes >= ctx.cfg.review.max_fixes


def _then(ctx, state: State) -> str:
    """How an exit 2 message after a CHANGES_REQUESTED round or a failed validation ends."""
    if not _last_cycle(ctx, state):
        return ", commit, run again."
    return (
        "; fix cycles used up (%d of %d): the next run on a new commit stops for a human to "
        "decide." % (state.fixes, ctx.cfg.review.max_fixes)
    )


def _validate_and_finish(
    ctx, state: State, rdir: str, log: RunLog, round_no: int, data: dict, review_path: str
) -> int:
    """After an APPROVE (this run's, or one a previous run recorded and never validated)."""
    from revali import pr as prstage
    from revali import review, stophint, validate

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
        state.fix_due = True  # saved by _stage
        message = "validation %d failed at %s (%s)" % (
            vout.number,
            validate.failed_where(vout),
            counts,
        )
        if _last_cycle(ctx, state):
            message += "; " + LAST_CYCLE
        _stage(state, rdir, log, "needs_action", message, EXIT_ACTION)
        prstage.update_body(ctx, state, rdir, log)
        _record_history(state, EXIT_ACTION, log)
        print(
            "ACTION NEEDED: %s\nFix (or correct the test if the diagnosis says the test is wrong, "
            "and say so in %s)%s%s"
            % (summary, os.path.join(rdir, "response-%d.md" % round_no), _then(ctx, state), others)
        )
        return EXIT_ACTION

    lows = stophint.derived_lows(state, ctx.repo_root, log.detail)
    message = "validation %d passed" % vout.number
    if lows:
        message += "; " + stophint.short(lows, ready=True)
    state.validated_sha = state.head_sha  # saved by _stage
    _stage(state, rdir, log, "ready_to_merge", message, EXIT_OK)
    prstage.mark_ready(ctx, state, log)
    prstage.update_body(ctx, state, rdir, log)
    _record_history(state, EXIT_OK, log)
    flags = []
    if state.fallback:
        flags.append("a reviewer round ran on a fallback model")
    if not state.test_files and ctx.doc.kind in ("feature", "fix"):
        flags.append("no runnable tests were written")
    tail = ""
    listed = review.non_blocking_findings(data)
    if listed:
        tail += "\n  %d non-blocking finding(s) in %s:" % (len(listed), review_path)
        tail += "".join("\n    - " + review.format_finding(f) for f in listed)
    if lows:
        tail += "\n  " + stophint.hint_line(lows, round_no, rdir, ready=True)
    print(
        "READY TO MERGE: %s (PR %s)\n  review rounds: %d, fix cycles: %d, validation: %s%s\n"
        "  %s: %s\n  cost: $%.2f, models: %s\n  %s\n  merge with: revali merge; "
        "the PR is no longer a draft%s%s"
        % (
            ctx.doc.title,
            state.pr_url or "#%d" % state.pr_number,
            len(state.rounds),
            state.fixes,
            vout.result,
            " (%s)" % vout.skipped_reason if vout.skipped_reason else "",
            "tests archived" if state.tests_mode == "archive" else "tests landing",
            ", ".join(state.test_files) or "none",
            state.cost_usd,
            ", ".join(state.models_used) or "-",
            cost_per_round(state),
            "\n  note: " + "; ".join(flags) if flags else "",
            tail,
        )
    )
    return EXIT_OK


# ---- preflight --------------------------------------------------------------


def cmd_preflight(args) -> int:
    cwd = os.getcwd()
    try:
        _, branch, rdir = _locate_run(cwd, "")
    except Stop:  # preflight below reports a repository or branch it cannot use
        branch, rdir = "", None
    root = gitops.repo_root(cwd)
    log = RunLog(
        rdir if rdir and os.path.isdir(rdir) else None,
        verbose=args.verbose,
        logs_dir=paths_for(root).logs_dir if root else "",
    )
    try:
        state = (State.load(rdir) if rdir else None) or State()
    except StateError:
        # preflight names the repo and branch in its own first line, which a state error
        # never reaches; name them here so this error says which branch it is about
        _print_identity(root, branch)  # resolved above: no second git call to fail here
        raise
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
    logs_dir = paths_for(root).logs_dir
    deadline = time.monotonic() + parse_duration(args.timeout)
    while True:
        pid = lock_owner_alive(rdir)
        state = State.load(rdir)
        if not pid:
            if state is None:
                print("no revali run recorded for this branch")
                return EXIT_ERROR
            retries = read_wsl_retries(rdir, logs_dir)
            if retries:  # before the result: the result line stays the last one
                print(
                    "wsl retries in this run: %d; see %s"
                    % (len(retries), wsl_retries_path(rdir, logs_dir))
                )
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
            retries = read_wsl_retries(rdir, logs_dir)
            if retries:
                print("wsl retries in this run: %d; latest: %s" % (len(retries), retries[-1]))
            return EXIT_OK + 4  # 4: still running, distinct from the pipeline codes
        time.sleep(min(2.0, max(0.1, deadline - time.monotonic())))


def cmd_status(args) -> int:
    found = _located(os.getcwd(), args.branch)
    if not found:
        return EXIT_ERROR
    root, branch, rdir = found
    paths = paths_for(root)
    _print_identity(root, branch)  # first: a state error below then names its branch
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
        if not pid and run_died(state):
            print(
                "running: no; the last run stopped at stage '%s' without a result (see %s)"
                % (state.stage, _run_log_path(rdir))
            )
        if state.message:
            print("message: %s" % state.message)
        print(
            "round: %d, fixes: %d, cost: $%.2f" % (len(state.rounds), state.fixes, state.cost_usd)
        )
        if state.rounds:
            print(cost_per_round(state))
            # the mode the first round fixed; a state from before the key: commit
            print("tests: %s" % (state.tests_mode or "commit"))
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
                "stale review dirs (branch no longer exists): %s  -> `revali archive --branch "
                "<name>` records a PR merged or closed elsewhere, `revali clean <name>` deletes"
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
    try:
        state = State.load(rdir)
    except StateError as exc:
        state = _salvaged(rdir, exc)
    if state is not None and (state.pending_test_files or state.reviewer_running):
        _reset_test_dir(state, rdir)
    if os.path.isfile(path):
        os.unlink(path)
        print("state removed: %s (review files kept)" % path)
    else:
        print("no state to remove")
    release_lock(rdir)
    return EXIT_OK


def _salvaged(rdir: str, exc: StateError) -> Optional[State]:
    """For `reset` on a state file State.load refused: the well-formed fields, when the ones
    the cleanup reads (review.CLEANUP_FIELDS) are among them; None otherwise. Either way the
    printed line names every skipped field and says whether anything is cleaned up."""
    from revali import review

    not_cleaned = (
        "the reviewer's uncommitted test files it may list are not cleaned up, check "
        "`git status` before the next run"
    )
    try:
        state, skipped = State.salvage(rdir)
    except StateError as whole:  # not JSON, not an object: nothing to read fields from
        print("the state file could not be read (%s); %s" % (whole, not_cleaned))
        return None
    head = "the state file could not be read as a whole (%s); skipped field(s): %s" % (
        exc,
        ", ".join(skipped),
    )
    needed = [name for name in skipped if name in review.CLEANUP_FIELDS]
    if needed:
        print("%s; %s among them, so %s" % (head, ", ".join(needed), not_cleaned))
        return None
    if state.pending_test_files or state.reviewer_running:
        print("%s; the cleanup of the reviewer's uncommitted test files reads the others" % head)
    else:
        print("%s; it lists no uncommitted test files of the reviewer's" % head)
    return state


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
        try:
            state = State.load(rdir)
        except StateError as exc:
            print(state_error_line(exc))
            return EXIT_ERROR
        if state is not None and run_died(state):
            # The process is gone and nothing recorded a result: close the episode so `wait`
            # and `status` stop reporting a death. Only the outcome fields change; what the
            # next run needs (reviewer_running, pending files, rounds, head_sha) stays.
            died_at = state.stage
            if not _close_stopped(
                state,
                rdir,
                _noted(
                    "found dead at stage '%s' with no result recorded; marked stopped "
                    "by `revali stop`" % died_at,
                    _merge_note(state, root),
                ),
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
    print("stopped pid %d" % pid)
    try:
        state = State.load(rdir)
    except StateError as exc:
        print(state_error_line(exc))
        return EXIT_ERROR
    if state is not None and not _close_stopped(
        state,
        rdir,
        _noted("stopped by user at stage '%s'" % state.stage, _merge_note(state, root)),
    ):
        return EXIT_ERROR
    return EXIT_OK


def _close_stopped(state: State, rdir: str, message: str) -> bool:
    """Record a run closed by `stop`: stage `stopped`, exit 1, a history row so `stats` sees
    the episode. A state file that cannot be written (a reader holding it on Windows past the
    retry window) is reported on one line, not as a traceback, and `state` is put back the
    way it was from a snapshot of the whole object; the run then still reads as dead, which
    is what it is."""
    before = copy.copy(state)  # set_stage and save assign scalars only; a shallow copy holds
    try:
        state.set_stage(rdir, "stopped", message, EXIT_ERROR)
    except OSError as exc:
        # set_stage assigns the outcome, and save stamps the timestamps, before the write
        state.__dict__.update(before.__dict__)
        print(
            "ERROR: the state file could not be updated (%s); `wait` and `status` will report "
            "the run as dead; run `revali stop` again once the file is free" % exc
        )
        return False
    _record_history(state, EXIT_ERROR)
    return True


def _mergeable(state: State) -> bool:
    """`ready_to_merge`, or a validation that still stands after a later run stopped before
    its PR stage (`state.validated_sha`; do_merge checks that HEAD is still there). Never a
    PR already recorded as merged or closed."""
    if state.stage == "ready_to_merge":
        return True
    return bool(state.validated_sha) and state.stage not in ("merged", "closed")


def cmd_merge(args) -> int:
    from revali import merge

    cwd = os.getcwd()
    found = _located(cwd)
    if not found:
        return EXIT_ERROR
    root, branch, rdir = found
    _print_identity(root, branch)
    state = State.load(rdir)
    if state is None or not _mergeable(state):
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
            # the stage stays the stopped run's; the note keeps saying the validation stands
            state.message = _noted(stop.message, _merge_note(state, root))
            state.save(rdir)
        _record_history(state, stop.exit_code)
        return stop.exit_code
    finally:
        release_lock(rdir)
        release_tree_lock(tpath)
    _record_history(state, code)
    print(merge.merge_summary(state, state.base))
    # the root found before the merge: a checkout does not move it, and asking git again here
    # could fail now that the PR is merged and skip the archive
    print(
        "  "
        + _archive_branch_dir(
            root, review_dir(root, state.branch, paths_for(root).state_dir), state
        )
    )
    return code


def _archive_branch_dir(root: str, rdir: str, state: State) -> str:
    """Move `rdir` to `[paths] archive_dir` (delete it when the key is empty), as `merge` and
    `archive` do last. Returns the line for the output."""
    from revali import merge

    paths = paths_for(root)
    root_dir = archive_root(paths)
    if root_dir:
        return merge.archive_review_dir(
            rdir, root_dir, state.repo or os.path.basename(root), state.pr_number, state.branch
        )
    merge.remove_tree(rdir)
    return "removed %s/%s/" % (paths.state_dir, os.path.basename(rdir))


def cmd_archive(args) -> int:
    """Record a PR merged or closed outside `revali merge`: never merges, pushes or edits it."""
    from revali import merge

    found = _located(os.getcwd(), args.branch, allow_detached=True)
    if not found:
        return EXIT_ERROR
    root, branch, rdir = found
    if branch == "HEAD":
        print("ERROR: detached HEAD; name the branch: `revali archive --branch <branch>`")
        return EXIT_ERROR
    _print_identity(root, branch)
    state = State.load(rdir) if os.path.isdir(rdir) else None
    if state is None:
        print(
            "ERROR: no review state for %s (%s); nothing records a PR" % (branch, State.path(rdir))
        )
        return EXIT_ERROR
    state.branch = state.branch or branch
    # before the locks: a log that cannot be opened must not leave them behind
    log = RunLog(rdir, verbose=args.verbose, logs_dir=paths_for(root).logs_dir)
    tpath = _tree_lock_path(root)
    if lock_owner_alive(rdir) or tree_lock_owner(tpath):
        print("ERROR: a run is in progress; `revali stop` first")
        return EXIT_ERROR
    try:
        acquire_lock(rdir)
    except LockHeld as exc:
        print("ERROR: %s" % exc)
        return EXIT_ERROR
    try:
        acquire_tree_lock(tpath, state.branch)
    except TreeLockHeld as exc:
        release_lock(rdir)
        print(_tree_held_message({"pid": exc.pid, "branch": exc.branch}))
        return EXIT_ERROR
    local = ""
    try:
        before = merge.record_outside(root, rdir, state, log)
        if state.stage == "merged":
            local = merge.drop_merged_branch(root, state, log)
            log.stage("archive", local)
    except Stop as stop:
        _print_stop(stop)
        return stop.exit_code
    finally:
        release_lock(rdir)
        release_tree_lock(tpath)
    extra = {"stage_before": before}
    if state.merged_outside:
        extra["merged_outside"] = True
    _record_history(state, EXIT_OK, extra=extra)
    print(merge.archive_summary(state, before))
    if local:
        print("  " + local)
    line = _archive_branch_dir(root, rdir, state)
    if line.startswith("could not archive"):
        line += "; `revali archive` again finishes the move"
    print("  " + line)
    return EXIT_OK


def cmd_version(args) -> int:
    print("%s %s" % (NAME, VERSION))
    return EXIT_OK
