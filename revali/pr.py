"""PR stage: .gitignore, push, draft PR, comments, body updates. All via gh."""

import os
import re
import urllib.parse
from typing import List, Optional

from revali import EXIT_ERROR, changedoc, gitops
from revali.config import release_label
from revali.preflight import Context, Stop, check_tree_unmoved
from revali.procs import ProcError, one_line, resolve, run, run_retry
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
        current: Optional[List[str]] = []
    else:
        number, url = int(pr.get("number", 0)), pr.get("url", "")
        if log:
            log.stage("pr", "reusing open PR #%d" % number)
        problem = sync_title(root, number, pr.get("title"), ctx.doc.title, log, "pr")
        if problem and log:
            log.stage("pr", "warning: could not update the PR title: %s" % problem)
        current = label_names(pr)
    state.pr_number = number
    state.pr_url = url
    state.save(rdir)
    if number:
        sync_labels(root, number, current, ctx.cfg, ctx.doc, log, "pr")


def label_names(pr: dict) -> Optional[List[str]]:
    """The label names of a PR as gh's JSON gives them (`labels: [{name}]`); None when the
    answer has no usable `labels` list."""
    labels = pr.get("labels") if isinstance(pr, dict) else None
    if not isinstance(labels, list):
        return None
    return [str(lb.get("name", "")) for lb in labels if isinstance(lb, dict) and lb.get("name")]


LABEL_DESCRIPTION = "Release notes group (set by revali)"


def sync_labels(
    root: str,
    number: int,
    current: Optional[List[str]],
    cfg,
    doc,
    log: Optional[RunLog],
    stage: str,
) -> None:
    """Make PR `number`'s release labels match change.md (`release_label`): add the wanted
    one, remove every other label of `[pr] release_labels`, leave the rest alone. Names
    compare in any case, as GitHub's do. `current` None (unknown) is taken as no labels, so
    nothing is removed. A label the repository lacks is created first. Never raises: each
    failure is one warning line, and the caller's stage goes on."""
    managed = list(cfg.pr.release_labels)
    if not managed:
        return
    wanted = release_label(cfg, doc)
    have = list(current or [])
    lowered = {n.lower() for n in have}
    managed_lower = {n.lower() for n in managed}
    add = wanted if wanted and wanted.lower() not in lowered else ""
    remove = [n for n in have if n.lower() in managed_lower and n.lower() != wanted.lower()]
    if not add and not remove:
        return
    if add and not _ensure_label(root, add, log, stage):
        add = ""
        if not remove:
            return
    args = ["pr", "edit", str(number)]
    if add:
        args += ["--add-label", add]
    if remove:
        args += ["--remove-label", ",".join(remove)]
    try:
        res = run_retry(resolve("gh") + args, cwd=root, log=_log(log), timeout=120)
        problem = "" if res.ok else (res.text.strip()[:200] or "gh exited %d" % res.returncode)
    except ProcError as exc:
        problem = str(exc)
    if not log:
        return
    if problem:
        log.stage(
            stage, "warning: could not update the release label of PR #%d: %s" % (number, problem)
        )
        return
    change = (["+" + add] if add else []) + ["-" + n for n in remove]
    log.stage(stage, "PR #%d release label: %s" % (number, " ".join(change)))


def _ensure_label(root: str, name: str, log: Optional[RunLog], stage: str) -> bool:
    """True when label `name` may be applied: the repository has it (GitHub looks a label up
    in any case), it was created now, or the lookup failed for another reason than "not
    found" (then applying it is still tried). False, with a warning, when creating it
    failed. One exact lookup, so a repository with many labels is no different."""
    path = "repos/{owner}/{repo}/labels/%s" % urllib.parse.quote(name, safe="")
    try:
        # no retry: "not found" is the usual answer the first time, not a transient failure
        res = run(resolve("gh") + ["api", path], cwd=root, log=_log(log), timeout=120)
        found = res.ok
        problem = "" if res.ok or "HTTP 404" in res.text else (res.text or "gh api failed")
    except ProcError as exc:
        found, problem = False, str(exc)
    if found:
        return True
    if problem:
        if log:
            log.stage(
                stage,
                "warning: could not check the label '%s': %s" % (name, one_line(problem)),
            )
        return True
    try:
        res = run_retry(
            resolve("gh") + ["label", "create", name, "--description", LABEL_DESCRIPTION],
            cwd=root,
            log=_log(log),
            timeout=120,
        )
        problem = "" if res.ok else (res.text.strip()[:200] or "gh exited %d" % res.returncode)
    except ProcError as exc:
        problem = str(exc)
    if problem:
        if log:
            log.stage(stage, "warning: could not create the label '%s': %s" % (name, problem))
        return False
    if log:
        log.stage(stage, "created the label '%s'" % name)
    return True


def sync_title(
    root: str,
    number: int,
    current: Optional[str],
    wanted: str,
    log: Optional[RunLog],
    stage: str,
) -> str:
    """Give PR `number` the title `wanted` (change.md's) when gh reported a different one. A
    squash merge takes the PR title as the commit subject, so a stale one would land on the
    base branch. Returns gh's message when the edit failed, "" otherwise (also when there was
    nothing to do: equal titles, or no title reported)."""
    wanted = (wanted or "").strip()
    if current is None or not wanted or current.strip() == wanted:
        return ""
    try:
        res = run_retry(
            resolve("gh") + ["pr", "edit", str(number), "--title", wanted],
            cwd=root,
            log=_log(log),
            timeout=120,
        )
    except ProcError as exc:  # a hung or missing gh is a failed edit like any other
        return str(exc)
    if not res.ok:
        return res.text.strip()[:200] or "gh exited %d" % res.returncode
    if log:
        log.stage(stage, "PR title updated: '%s' -> '%s'" % (current.strip(), wanted))
    return ""


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


def strip_request(text: str) -> str:
    """change.md without its Request section: the heading line and everything up to the next
    heading (or the end). A heading is what changedoc.parse takes for one (`## request`,
    `##  Request`, a CRLF line), so whatever validated as the Request is what goes; the words
    inside another section stay. Line endings are kept as they are."""
    kept = []
    skipping = False
    for line in text.splitlines(keepends=True):
        key = changedoc.section_key(line)
        if key:
            skipping = key == "request"
        if not skipping:
            kept.append(line)
    return "".join(kept)


def pr_body(ctx: Context, state: Optional[State]) -> str:
    """change.md minus the request (the user's verbatim words stay local, on every repository),
    plus a status table once there are rounds."""
    body = strip_request(ctx.doc.raw).strip()
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
