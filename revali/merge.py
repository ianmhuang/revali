"""`revali merge`: the one irreversible step, always started by a human."""

import json
import os
import shutil
import stat
import time
from typing import List, Optional

from revali import EXIT_ACTION, EXIT_ERROR, EXIT_OK, gitops
from revali.config import ConfigError, load_project_config
from revali.preflight import Stop
from revali.procs import resolve, run, run_retry
from revali.state import RunLog, State

POLL_ENV = "REVALI_POLL_SECONDS"


def _poll_seconds() -> float:
    try:
        return float(os.environ.get(POLL_ENV, "20"))
    except ValueError:
        return 20.0


def remove_tree(path: str) -> None:
    def _onexc(func, p, exc):
        try:
            os.chmod(p, stat.S_IWRITE)
            func(p)
        except OSError:
            pass

    if os.path.isdir(path):
        try:
            shutil.rmtree(path, onexc=_onexc)
        except TypeError:
            shutil.rmtree(path, onerror=lambda f, p, e: _onexc(f, p, e[1]))


def pr_checks(pr_number: int, cwd: str, log: Optional[RunLog]) -> List[dict]:
    res = run(
        resolve("gh") + ["pr", "checks", str(pr_number), "--json", "name,state,bucket"],
        cwd=cwd,
        log=log.detail if log else None,
        timeout=120,
    )
    if not res.ok:
        # gh exits 1 with "no checks reported" when there are none; treat as empty.
        if "no checks" in res.text.lower() or not res.stdout.strip():
            return []
        raise Stop(EXIT_ERROR, "gh pr checks failed: %s" % res.text.strip()[:300])
    try:
        items = json.loads(res.stdout or "[]")
    except ValueError:
        return []
    return items if isinstance(items, list) else []


def wait_for_checks(pr_number: int, cwd: str, timeout_min: int, log: Optional[RunLog]) -> None:
    deadline = time.monotonic() + timeout_min * 60
    while True:
        checks = pr_checks(pr_number, cwd, log)
        if not checks:
            if log:
                log.stage("merge", "no CI checks on the PR")
            return
        buckets = {}
        for c in checks:
            buckets.setdefault(str(c.get("bucket", "")).lower(), []).append(str(c.get("name", "?")))
        failed = buckets.get("fail", []) + buckets.get("cancel", [])
        if failed:
            raise Stop(EXIT_ACTION, "CI checks failed: %s" % ", ".join(failed))
        pending = buckets.get("pending", [])
        if not pending:
            if log:
                log.stage("merge", "CI checks green: %s" % ", ".join(sum(buckets.values(), [])))
            return
        if time.monotonic() >= deadline:
            raise Stop(
                EXIT_ERROR,
                "CI checks still pending after %d minutes: %s" % (timeout_min, ", ".join(pending)),
            )
        if log:
            log.stage("merge", "waiting for CI: %s" % ", ".join(pending))
        time.sleep(_poll_seconds())


def do_merge(cwd: str, rdir: str, state: State, log: RunLog) -> int:
    root = gitops.repo_root(cwd)
    try:
        cfg = load_project_config(root)
    except ConfigError as exc:
        raise Stop(EXIT_ERROR, "; ".join(exc.problems)) from exc
    if not gitops.gh_auth_ok(root, log.detail):
        raise Stop(EXIT_ERROR, "gh is not logged in")
    if not state.pr_number:
        raise Stop(EXIT_ERROR, "no PR number recorded; run the pipeline first")
    branch = state.branch
    base = state.base or cfg.project.base_branch
    if gitops.current_branch(root) != branch:
        raise Stop(
            EXIT_ERROR,
            "check out %s before merging (you are on %s)" % (branch, gitops.current_branch(root)),
        )
    if gitops.dirty_paths(root, (cfg.paths.state_dir + "/",)):
        raise Stop(EXIT_ERROR, "working tree is not clean")
    head = gitops.rev_parse("HEAD", root) or ""
    if state.head_sha and head != state.head_sha:
        raise Stop(
            EXIT_ACTION,
            "HEAD moved since validation (%s -> %s); run `revali run` again"
            % (state.head_sha[:10], head[:10]),
        )

    # In a linked worktree whose base branch is checked out elsewhere, gh's --delete-branch
    # would try to check out the base here and fail after the PR was merged; do the local
    # part by hand instead. Decided before the CI wait: a refusal must not cost that wait.
    elsewhere = gitops.worktree_holding(base, root)
    if elsewhere and not gitops.is_linked_worktree(root):
        # the primary tree cannot be detached and removed like a linked worktree
        # No alternative is offered: the branch is checked out here, so a new worktree cannot
        # take it, and this tree's .revali state would not follow it anyway.
        raise Stop(
            EXIT_ERROR,
            "%s is checked out in %s; remove or switch that worktree, then merge again "
            "(the layout that works alone is a linked worktree from the start: docs/workflow.md, "
            '"Several agents on one repository")' % (base, elsewhere),
        )

    if cfg.merge.wait_for_checks:
        wait_for_checks(state.pr_number, root, cfg.merge.checks_timeout_min, log)

    argv = ["pr", "merge", str(state.pr_number), "--%s" % cfg.merge.method]
    if elsewhere:
        log.stage(
            "merge",
            "gh %s (worktree mode: %s is checked out in %s)" % (" ".join(argv), base, elsewhere),
        )
    else:
        argv.append("--delete-branch")
        log.stage("merge", "gh " + " ".join(argv))
    state.pending_effect = "merge"
    state.save(rdir)
    res = run_retry(resolve("gh") + argv, retries=0, cwd=root, log=log.detail, timeout=300)
    if not res.ok:
        # gh can fail on its local follow-up after the PR itself merged; the PR is the truth
        if pr_merged(state.pr_number, root, log):
            log.stage(
                "merge",
                "gh reported an error but PR #%d is merged: %s"
                % (state.pr_number, res.text.strip()[:200]),
            )
        else:
            state.pending_effect = ""
            state.save(rdir)
            raise Stop(EXIT_ERROR, "gh pr merge failed: %s" % res.text.strip()[:400])
    state.pending_effect = ""
    state.set_stage(rdir, "merged", "merged PR #%d into %s" % (state.pr_number, base), EXIT_OK)

    if elsewhere:
        _worktree_follow_up(root, branch, base, elsewhere, log)
        return EXIT_OK
    # Local follow-up: gh usually switches to the base branch and deletes the local branch itself.
    if gitops.current_branch(root) != base:
        run(resolve("git") + ["checkout", "--quiet", base], cwd=root, log=log.detail)
    run(resolve("git") + ["pull", "--quiet", "--prune"], cwd=root, log=log.detail, timeout=300)
    removed, why = _delete_local_branch(root, branch, log, when=gitops.current_branch(root) == base)
    log.stage(
        "merge",
        "local: on %s, branch %s %s%s"
        % (gitops.current_branch(root), branch, "removed" if removed else "kept", why),
    )
    return EXIT_OK


def _delete_local_branch(root: str, branch: str, log: RunLog, when: bool) -> tuple:
    """`git branch -D <branch>` when `when` holds and the branch still exists. (removed, why):
    `why` is empty on success or when there was nothing to delete, and names git's error
    otherwise, so the log line says why a branch is still there."""
    if not gitops.rev_parse(branch, root):
        return True, ""
    if not when:
        return False, ""
    res = run(resolve("git") + ["branch", "-D", branch], cwd=root, log=log.detail)
    if res.ok:
        return True, ""
    return False, " (git branch -D failed: %s)" % res.text.strip()[:200]


def pr_merged(pr_number: int, cwd: str, log: Optional[RunLog]) -> bool:
    res = run(
        resolve("gh") + ["pr", "view", str(pr_number), "--json", "state"],
        cwd=cwd,
        log=log.detail if log else None,
        timeout=120,
    )
    if not res.ok:
        return False
    try:
        return str(json.loads(res.stdout).get("state", "")).upper() == "MERGED"
    except ValueError:
        return False


def _worktree_follow_up(root: str, branch: str, base: str, elsewhere: str, log: RunLog) -> None:
    """After the PR merged from a linked worktree: delete the remote branch, detach this
    worktree at the merged base, drop the local branch, and say what is left to the user."""
    res = run(
        resolve("git") + ["push", "--quiet", "origin", "--delete", branch],
        cwd=root,
        log=log.detail,
        timeout=300,
    )
    if not res.ok:
        log.stage(
            "merge", "note: could not delete origin/%s: %s" % (branch, res.text.strip()[:200])
        )
    res = run(
        resolve("git") + ["fetch", "--quiet", "--prune", "origin", base],
        cwd=root,
        log=log.detail,
        timeout=300,
    )
    if not res.ok:
        log.stage("merge", "note: git fetch failed: %s" % res.text.strip()[:200])
    else:
        res = run(
            resolve("git") + ["checkout", "--quiet", "--detach", "FETCH_HEAD"],
            cwd=root,
            log=log.detail,
        )
        if not res.ok:
            log.stage("merge", "note: git checkout --detach failed: %s" % res.text.strip()[:200])
    removed, why = _delete_local_branch(
        root, branch, log, when=gitops.current_branch(root) == "HEAD"
    )
    now = gitops.current_branch(root)
    where = "detached at the merged %s" % base if now == "HEAD" else "still on %s" % now
    log.stage(
        "merge",
        "worktree: %s, local branch %s %s%s; remove this worktree with `git worktree remove %s` "
        "and run `git pull` in %s"
        % (where, branch, "removed" if removed else "kept", why, root, elsewhere),
    )


def merge_summary(state: State, base: str) -> str:
    return (
        "MERGED: PR #%d into %s\n  rounds: %d, fix cycles: %d, validations: %d, cost: $%.2f\n"
        "  tests landed: %s"
        % (
            state.pr_number,
            base,
            len(state.rounds),
            state.fixes,
            len(state.validations),
            state.cost_usd,
            ", ".join(state.test_files) or "none",
        )
    )
