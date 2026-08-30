"""Preflight: everything that must be true before a branch is pushed for review.

Cheap, local checks first; gh calls after; lint last. Onboarding problems
(config, change.md) are collected and reported together so the author fixes
them in one pass instead of one per run.
"""
import os
from dataclasses import dataclass, field
from typing import List, Optional

from revali import EXIT_ACTION, EXIT_ERROR, V1_KINDS
from revali import changedoc, gitops
from revali.config import (Config, ConfigError, UserConfig, load_project_config,
                           load_user_config, paths_for, tool_file)
from revali.procs import ExeNotFound, ProcTimeout, run_shell
from revali.secretscan import format_hits, scan_diff
from revali.state import RunLog, review_dir

DISABLE_ENV = "REVALI_DISABLE"


class Stop(Exception):
    """Abort the pipeline with an exit code and a message for the author."""

    def __init__(self, exit_code: int, message: str):
        self.exit_code = exit_code
        self.message = message
        super().__init__(message)


@dataclass
class Context:
    cwd: str
    repo_root: str = ""
    branch: str = ""
    base: str = ""
    base_ref: str = ""          # ref actually compared against (origin/main or main)
    rdir: str = ""
    cfg: Optional[Config] = None
    user_cfg: Optional[UserConfig] = None
    doc: Optional[changedoc.ChangeDoc] = None
    repo: Optional[gitops.RepoInfo] = None
    login: str = ""
    head_sha: str = ""
    base_sha: str = ""
    diff_lines: int = 0
    changed_files: List[str] = field(default_factory=list)
    excluded_files: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    dry_run: bool = False
    log: Optional[RunLog] = None
    logs: str = ""                 # <state_dir>/<branch>/<logs_dir>
    review_prompt: str = ""        # resolved files (project override or the built-in)
    review_schema: str = ""
    builtin_checklist: str = ""
    diagnose_prompt: str = ""
    diagnose_schema: str = ""

    def say(self, msg: str) -> None:
        if self.log:
            self.log.stage("preflight", msg)

    def detail(self, msg: str) -> None:
        if self.log:
            self.log.detail(msg)


def check_engines(cfg: Config) -> List[str]:
    """Both roles must name an engine revali implements, before anything is pushed."""
    from revali import engines  # local: engines imports Stop from this module
    problems: List[str] = []
    for role in ("review", "validate"):
        try:
            engines.for_role(cfg, role)
        except ConfigError as exc:
            problems.extend("%s.engine: %s" % (role, p) for p in exc.problems)
    return problems


def _tail(text: str, lines: int = 30) -> str:
    parts = text.strip().splitlines()
    return "\n".join(parts[-lines:])


def locate(cwd: str, base_override: str = "", log: Optional[RunLog] = None) -> Context:
    """Steps that need no network: repo, config, branch, review dir. Raises Stop."""
    ctx = Context(cwd=cwd, log=log)
    if os.environ.get(DISABLE_ENV, "").strip() not in ("", "0", "false"):
        raise Stop(EXIT_ERROR, "%s is set; revali is switched off on this machine" % DISABLE_ENV)

    root = gitops.repo_root(cwd)
    if not root:
        raise Stop(EXIT_ERROR, "not inside a git repository: %s" % cwd)
    ctx.repo_root = root

    problems: List[str] = []
    try:
        ctx.user_cfg = load_user_config()
    except ConfigError as exc:
        problems.extend(exc.problems)
    try:
        ctx.cfg = load_project_config(root, ctx.user_cfg or UserConfig())
    except ConfigError as exc:
        problems.extend(exc.problems)

    ctx.branch = gitops.current_branch(root)
    if ctx.branch == "HEAD":
        problems.append("detached HEAD; check out the feature branch first")
    ctx.base = base_override or (ctx.cfg.project.base_branch if ctx.cfg else "")
    paths = ctx.cfg.paths if ctx.cfg else paths_for(root)
    ctx.rdir = review_dir(root, ctx.branch, paths.state_dir)
    ctx.logs = os.path.join(ctx.rdir, paths.logs_dir)
    if ctx.cfg:
        ctx.review_prompt = tool_file(ctx.cfg.review.prompt, root, "prompts", "review.md")
        ctx.review_schema = tool_file(ctx.cfg.review.schema, root, "schemas", "review.schema.json")
        ctx.builtin_checklist = tool_file(ctx.cfg.review.checklist_builtin, root, "checklists", "default.md")
        ctx.diagnose_prompt = tool_file(ctx.cfg.validate.prompt, root, "prompts", "diagnose.md")
        ctx.diagnose_schema = tool_file(ctx.cfg.validate.schema, root, "schemas", "diagnose.schema.json")
        problems.extend(check_engines(ctx.cfg))

    doc_path = os.path.join(ctx.rdir, changedoc.FILENAME)
    if not os.path.isfile(doc_path):
        problems.append("%s not found (copy templates/change.md, fill it in)"
                        % os.path.relpath(doc_path, root))
    else:
        ctx.doc = changedoc.load(doc_path)
        problems.extend(changedoc.validate(ctx.doc, V1_KINDS))

    if problems:
        raise Stop(EXIT_ERROR, "before revali can run:\n  - " + "\n  - ".join(problems))
    for warning in ctx.cfg.warnings:
        ctx.notes.append(warning)
    return ctx


def check_tree(ctx: Context) -> None:
    dirty = gitops.dirty_paths(ctx.repo_root, (ctx.cfg.paths.state_dir + "/",))
    if dirty:
        raise Stop(EXIT_ERROR, "working tree is not clean; commit or stash first:\n  "
                   + "\n  ".join(dirty[:20]))


def check_github(ctx: Context) -> None:
    if not gitops.gh_available():
        raise Stop(EXIT_ERROR, "gh (GitHub CLI) not found on PATH")
    if not gitops.gh_auth_ok(ctx.repo_root, ctx.detail):
        raise Stop(EXIT_ERROR, "gh is not logged in; run: gh auth login")
    try:
        ctx.login = gitops.gh_login(ctx.repo_root, ctx.detail)
        ctx.repo = gitops.gh_repo_info(ctx.repo_root, ctx.detail)
    except gitops.GhError as exc:
        raise Stop(EXIT_ERROR, "GitHub lookup failed: %s" % exc)
    if not ctx.repo.owner or ctx.repo.owner.lower() != ctx.login.lower():
        raise Stop(EXIT_ERROR, "repo owner is '%s' but you are '%s'; revali only runs on your own repos"
                   % (ctx.repo.owner, ctx.login))
    if ctx.repo.visibility != "PRIVATE":
        raise Stop(EXIT_ERROR, "repo visibility is %s; this version only runs on private repos"
                   % (ctx.repo.visibility or "unknown"))
    if not ctx.base:
        ctx.base = ctx.repo.default_branch
        if not ctx.base:
            raise Stop(EXIT_ERROR, "cannot determine the base branch; set project.base_branch")
    if ctx.branch == ctx.base:
        raise Stop(EXIT_ERROR, "you are on '%s'; revali reviews a feature branch, not the base" % ctx.base)


def check_base(ctx: Context) -> None:
    root = ctx.repo_root
    ref = ctx.base
    if gitops.has_remote("origin", root):
        if gitops.fetch("origin", ctx.base, root, ctx.detail):
            ref = "origin/%s" % ctx.base
        else:
            ctx.notes.append("could not fetch origin/%s; comparing against local %s" % (ctx.base, ctx.base))
    if gitops.rev_parse(ref, root) is None:
        raise Stop(EXIT_ERROR, "base branch '%s' does not exist" % ref)
    ctx.base_ref = ref
    if not gitops.is_ancestor(ref, "HEAD", root):
        raise Stop(EXIT_ACTION, "branch is behind %s; rebase onto it and run again (the review restarts)" % ref)
    ctx.head_sha = gitops.rev_parse("HEAD", root) or ""
    ctx.base_sha = gitops.rev_parse(ref, root) or ""
    if gitops.commits_between(ref, "HEAD", root) == 0:
        raise Stop(EXIT_ERROR, "no commits on this branch beyond %s; nothing to review" % ref)


def check_diff_size(ctx: Context) -> None:
    cfg = ctx.cfg
    exclude = list(cfg.review.exclude)
    exclude.append(cfg.project.test_file_pattern.replace("{topic}", "*"))
    total = 0
    files = []
    for added, deleted, path in gitops.diff_numstat(ctx.base_ref, "HEAD", ctx.repo_root):
        if gitops.matches_any(path, exclude):
            ctx.excluded_files.append(path)
            continue
        total += added + deleted
        files.append(path)
    ctx.diff_lines = total
    ctx.changed_files = files
    if not files and not ctx.excluded_files:
        raise Stop(EXIT_ERROR, "the diff against %s is empty" % ctx.base_ref)
    if total > cfg.review.max_diff_lines:
        raise Stop(EXIT_ACTION, "diff is %d lines (limit %d); split the change into smaller PRs"
                   % (total, cfg.review.max_diff_lines))


def check_secrets(ctx: Context) -> None:
    text = gitops.diff_text(ctx.base_ref, "HEAD", ctx.repo_root)
    hits = scan_diff(text)
    if hits:
        raise Stop(EXIT_ERROR, format_hits(hits))


def check_lint(ctx: Context) -> None:
    cmd = ctx.cfg.project.lint.strip()
    if not cmd:
        return
    ctx.say("lint: %s" % cmd)
    if ctx.dry_run:
        return
    try:
        res = run_shell(cmd, cwd=ctx.repo_root, timeout=ctx.cfg.review.timeout_min * 60, log=ctx.detail)
    except ExeNotFound as exc:
        raise Stop(EXIT_ERROR, "lint command could not start: %s" % exc)
    except ProcTimeout as exc:
        raise Stop(EXIT_ERROR, "lint timed out: %s" % exc)
    if not res.ok:
        raise Stop(EXIT_ACTION, "lint failed (exit %d); fix and run again:\n%s" % (res.returncode, _tail(res.text)))


def preflight(cwd: str, base_override: str = "", dry_run: bool = False,
              log: Optional[RunLog] = None, baseline=None) -> Context:
    """Run every check. Returns a populated Context or raises Stop.

    `baseline` is an optional callable(ctx) supplied by the validate stage (it
    runs the existing suite in the sandbox); None skips it.
    """
    ctx = locate(cwd, base_override, log)
    ctx.dry_run = dry_run
    ctx.say("repo %s, branch %s" % (ctx.repo_root, ctx.branch))
    check_tree(ctx)
    check_github(ctx)
    ctx.say("GitHub: %s/%s (%s), base %s" % (ctx.repo.owner, ctx.repo.name, ctx.repo.visibility.lower(), ctx.base))
    check_base(ctx)
    check_diff_size(ctx)
    ctx.say("diff: %d lines in %d files%s" % (
        ctx.diff_lines, len(ctx.changed_files),
        " (+%d excluded)" % len(ctx.excluded_files) if ctx.excluded_files else ""))
    check_secrets(ctx)
    check_lint(ctx)
    if baseline is not None:
        baseline(ctx)
    for note in ctx.notes:
        ctx.say("note: " + note)
    ctx.say("ok: %s (%s, %d AC)" % (ctx.doc.title, ctx.doc.kind, len(ctx.doc.acs)))
    return ctx
