"""`revali merge`: the one irreversible step, always started by a human."""
import json
import os
import shutil
import stat
import time
from typing import List, Optional

from revali import EXIT_ACTION, EXIT_ERROR, EXIT_OK
from revali import gitops
from revali.config import ConfigError, load_project_config
from revali.preflight import Stop
from revali.procs import resolve, run, run_retry
from revali.state import RunLog, State, now_iso

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
    res = run(resolve("gh") + ["pr", "checks", str(pr_number), "--json", "name,state,bucket"],
              cwd=cwd, log=log.detail if log else None, timeout=120)
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
            raise Stop(EXIT_ERROR, "CI checks still pending after %d minutes: %s" % (timeout_min, ", ".join(pending)))
        if log:
            log.stage("merge", "waiting for CI: %s" % ", ".join(pending))
        time.sleep(_poll_seconds())


def do_merge(cwd: str, rdir: str, state: State, log: RunLog) -> int:
    root = gitops.repo_root(cwd)
    try:
        cfg = load_project_config(root)
    except ConfigError as exc:
        raise Stop(EXIT_ERROR, "; ".join(exc.problems))
    if not gitops.gh_auth_ok(root, log.detail):
        raise Stop(EXIT_ERROR, "gh is not logged in")
    if not state.pr_number:
        raise Stop(EXIT_ERROR, "no PR number recorded; run the pipeline first")
    branch = state.branch
    base = state.base or cfg.project.base_branch
    if gitops.current_branch(root) != branch:
        raise Stop(EXIT_ERROR, "check out %s before merging (you are on %s)" % (branch, gitops.current_branch(root)))
    if gitops.dirty_paths(root, (cfg.paths.state_dir + "/",)):
        raise Stop(EXIT_ERROR, "working tree is not clean")
    head = gitops.rev_parse("HEAD", root) or ""
    if state.head_sha and head != state.head_sha:
        raise Stop(EXIT_ACTION, "HEAD moved since validation (%s -> %s); run `revali run` again"
                   % (state.head_sha[:10], head[:10]))

    if cfg.merge.wait_for_checks:
        wait_for_checks(state.pr_number, root, cfg.merge.checks_timeout_min, log)

    log.stage("merge", "gh pr merge #%d --%s --delete-branch" % (state.pr_number, cfg.merge.method))
    state.pending_effect = "merge"
    state.save(rdir)
    res = run_retry(resolve("gh") + ["pr", "merge", str(state.pr_number), "--%s" % cfg.merge.method, "--delete-branch"],
                    retries=0, cwd=root, log=log.detail, timeout=300)
    if not res.ok:
        state.pending_effect = ""
        state.save(rdir)
        raise Stop(EXIT_ERROR, "gh pr merge failed: %s" % res.text.strip()[:400])
    state.pending_effect = ""
    state.set_stage(rdir, "merged", "merged PR #%d into %s" % (state.pr_number, base), EXIT_OK)

    # Local follow-up: gh usually switches to the base branch and deletes the local branch itself.
    if gitops.current_branch(root) != base:
        run(resolve("git") + ["checkout", "--quiet", base], cwd=root, log=log.detail)
    run(resolve("git") + ["pull", "--quiet", "--prune"], cwd=root, log=log.detail, timeout=300)
    if gitops.rev_parse(branch, root) and gitops.current_branch(root) == base:
        run(resolve("git") + ["branch", "-D", branch], cwd=root, log=log.detail)
    log.stage("merge", "local: on %s, branch %s removed" % (gitops.current_branch(root), branch))
    return EXIT_OK


def merge_summary(state: State, base: str) -> str:
    return ("MERGED: PR #%d into %s\n  rounds: %d, fix cycles: %d, validations: %d, cost: $%.2f\n"
            "  tests landed: %s" % (state.pr_number, base, len(state.rounds), state.fixes,
                                    len(state.validations), state.cost_usd,
                                    ", ".join(state.test_files) or "none"))
