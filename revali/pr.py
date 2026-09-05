"""PR stage: .gitignore, push, draft PR, comments, body updates. All via gh."""

import os
import re
from typing import Optional

from revali import EXIT_ERROR, gitops
from revali.preflight import Context, Stop, check_tree_unmoved
from revali.procs import resolve, run, run_retry
from revali.secretscan import scan_text
from revali.state import RunLog, State, write_text


def _log(log: Optional[RunLog]):
    return log.detail if log else None


def ensure_pr(ctx: Context, state: State, rdir: str, log: Optional[RunLog]) -> None:
    root = ctx.repo_root
    entry = ctx.cfg.paths.state_dir + "/"
    check_tree_unmoved(ctx)
    if gitops.ensure_gitignore(root, entry):
        gitops.git_ok(["add", ".gitignore"], root)
        res = run(
            resolve("git") + ["commit", "--quiet", "-m", "chore: ignore %s" % entry],
            cwd=root,
            log=_log(log),
        )
        if not res.ok:
            raise Stop(EXIT_ERROR, "could not commit .gitignore: %s" % res.text.strip())
        ctx.head_sha = gitops.rev_parse("HEAD", root) or ctx.head_sha
        if log:
            log.stage("pr", "added %s to .gitignore" % entry)

    if ctx.dry_run:
        if log:
            log.stage("pr", "dry run: would push %s and open a draft PR" % ctx.branch)
        return

    check_tree_unmoved(ctx)
    state.pending_effect = "push"
    state.save(rdir)
    res = gitops.push_branch(ctx.branch, root, _log(log), force=state.force_push)
    if not res.ok:
        raise Stop(EXIT_ERROR, "git push failed: %s" % res.text.strip())
    if state.force_push and log:
        log.stage("pr", "force-pushed (with lease) after the history rewrite")
    state.force_push = False
    state.pending_effect = ""
    state.save(rdir)

    try:
        pr = gitops.gh_pr_open(ctx.branch, root, _log(log))
    except gitops.GhError as exc:
        raise Stop(EXIT_ERROR, str(exc)) from exc
    if pr is None:
        closed = [
            p
            for p in gitops.gh_pr_any(ctx.branch, root, _log(log))
            if str(p.get("state", "")).upper() != "OPEN"
        ]
        if closed:
            raise Stop(
                EXIT_ERROR,
                "PR #%s for this branch is %s; reopen it or start a new branch"
                % (closed[0].get("number"), str(closed[0].get("state", "")).lower()),
            )
        state.pending_effect = "pr-create"
        state.save(rdir)
        number, url = create_draft(ctx, rdir, log)
        state.pending_effect = ""
    else:
        number, url = int(pr.get("number", 0)), pr.get("url", "")
        if log:
            log.stage("pr", "reusing open PR #%d" % number)
    state.pr_number = number
    state.pr_url = url
    state.save(rdir)


def create_draft(ctx: Context, rdir: str, log: Optional[RunLog]):
    body_path = os.path.join(ctx.logs, "pr-body.md")
    write_text(body_path, pr_body(ctx, None))
    res = run_retry(
        resolve("gh")
        + [
            "pr",
            "create",
            "--draft",
            "--base",
            ctx.base,
            "--head",
            ctx.branch,
            "--title",
            ctx.doc.title,
            "--body-file",
            body_path,
        ],
        cwd=ctx.repo_root,
        log=_log(log),
        timeout=120,
    )
    if not res.ok:
        raise Stop(EXIT_ERROR, "gh pr create failed: %s" % res.text.strip())
    url = res.stdout.strip().splitlines()[-1] if res.stdout.strip() else ""
    m = re.search(r"/pull/(\d+)", url)
    number = int(m.group(1)) if m else 0
    if not number:
        pr = gitops.gh_pr_open(ctx.branch, ctx.repo_root, _log(log))
        if pr:
            number, url = int(pr.get("number", 0)), pr.get("url", url)
    if log:
        log.stage("pr", "opened draft PR #%d %s" % (number, url))
    return number, url


def is_public(ctx: Context) -> bool:
    """Anything but PRIVATE (public, internal, unknown) gets summaries on the PR."""
    return bool(ctx.repo) and ctx.repo.visibility != "PRIVATE"


def pr_body(ctx: Context, state: Optional[State]) -> str:
    """change.md minus the verbatim request when the repo is public, plus a status table."""
    body = ctx.doc.raw.strip()
    if is_public(ctx):
        body = re.sub(
            r"## Request\n.*?(?=\n## )",
            "## Request\n(withheld: public repository)\n",
            body,
            flags=re.S,
        )
    if state and state.rounds:
        rows = [
            "",
            "## revali status",
            "",
            "| round | verdict | model | cost |",
            "|---|---|---|---|",
        ]
        for r in state.rounds:
            rows.append(
                "| %d | %s | %s%s | $%.2f |"
                % (
                    r.get("round", 0),
                    r.get("verdict", "?"),
                    r.get("model", "?"),
                    " (fallback)" if r.get("fallback") else "",
                    r.get("cost_usd", 0.0),
                )
            )
        rows.append("")
        rows.append("stage: `%s`" % state.stage)
        body += "\n" + "\n".join(rows) + "\n"
    return body + "\n"


def update_body(ctx: Context, state: State, rdir: str, log: Optional[RunLog]) -> None:
    if ctx.dry_run or not state.pr_number:
        return
    path = os.path.join(ctx.logs, "pr-body.md")
    write_text(path, pr_body(ctx, state))
    res = run_retry(
        resolve("gh") + ["pr", "edit", str(state.pr_number), "--body-file", path],
        cwd=ctx.repo_root,
        log=_log(log),
        timeout=120,
    )
    if not res.ok and log:
        log.stage("pr", "warning: could not update the PR body: %s" % res.text.strip()[:200])


def mark_ready(ctx: Context, state: State, log: Optional[RunLog]) -> None:
    """Draft -> ready for review, once validation passed."""
    if ctx.dry_run or not state.pr_number:
        return
    res = run_retry(
        resolve("gh") + ["pr", "ready", str(state.pr_number)],
        cwd=ctx.repo_root,
        log=_log(log),
        timeout=120,
    )
    if log:
        log.stage(
            "pr",
            (
                "PR #%d marked ready" % state.pr_number
                if res.ok
                else "warning: gh pr ready failed: %s" % res.text.strip()[:200]
            ),
        )


def post_comment(
    ctx: Context, state: State, rdir: str, name: str, body: str, log: Optional[RunLog]
) -> bool:
    """Post a comment after a credential scan. Returns True when posted."""
    if ctx.dry_run or not state.pr_number:
        return False
    hits = scan_text(body, label=name)
    if hits:
        body = (
            "revali withheld this comment: it looked like it contained a credential (%s). "
            "The full text is in %s/ on the author's machine."
            % (", ".join(sorted({h.pattern for h in hits})), ctx.cfg.paths.state_dir)
        )
        if log:
            log.stage("pr", "comment %s withheld: possible credential" % name)
    path = os.path.join(ctx.logs, "comment-%s.md" % name)
    write_text(path, body)
    state.pending_effect = "comment:" + name
    state.save(rdir)
    res = run_retry(
        resolve("gh") + ["pr", "comment", str(state.pr_number), "--body-file", path],
        cwd=ctx.repo_root,
        log=_log(log),
        timeout=120,
    )
    state.pending_effect = ""
    state.save(rdir)
    if not res.ok:
        if log:
            log.stage(
                "pr", "warning: could not post comment %s: %s" % (name, res.text.strip()[:200])
            )
        return False
    return True
