"""Review stage: build the reviewer prompt, spawn `claude -p`, judge its output,
guard the working tree, check AC coverage, smoke-run the new tests, commit them.
"""

import json
import os
import string
import time
from dataclasses import dataclass, field
from typing import Collection, Dict, List, Optional, Sequence, Tuple

from revali import EXIT_ERROR, engines, gitops, models
from revali.engines import EngineRequest
from revali.engines.base import ModelUnavailable, run_advice
from revali.preflight import (
    Context,
    Stop,
    check_tree_unmoved,
    review_left_out,
    test_pattern_glob,
)
from revali.procs import ProcTimeout, resolve, run, run_shell
from revali.runners import (
    FILES_PLACEHOLDER,
    RunnerError,
    files_argument,
    get_runner,
    sandbox_key,
    steps_with_files,
)
from revali.state import (
    RunLog,
    State,
    now_iso,
    read_text,
    safe_branch,
    write_json_atomic,
    write_text,
)
from revali.timing import fmt_duration

APPROVE, CHANGES_REQUESTED, NEEDS_INFO = "APPROVE", "CHANGES_REQUESTED", "NEEDS_INFO"
MANIFEST_PATTERNS = [
    "requirements*.txt",
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "Pipfile",
    "Pipfile.lock",
    "poetry.lock",
    "package.json",
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "Cargo.toml",
    "Cargo.lock",
    "go.mod",
    "go.sum",
    "CMakeLists.txt",
    ".gitmodules",
    "conanfile.*",
    "vcpkg.json",
    "Gemfile",
    "Gemfile.lock",
]
SHELL_ALLOW = ["git diff", "git log", "git show"]  # read-only git, mapped by the engine
TRAILER = "Revali-Round"  # on every test commit revali makes; marks the reviewer's files as its own
LIST_FIELDS = (
    "questions",
    "findings",
    "previous_findings",
    "scope_mismatch",
    "dependencies_changed",
    "test_changes",
    "tests",
    "not_testable",
    "suggestions",
)


@dataclass
class ReviewerRun:
    data: dict
    raw: str
    model_requested: str
    model_actual: str
    fallback: bool
    cost: float
    denials: list
    duration_ms: int
    model_reason: str = ""


@dataclass
class RoundOutcome:
    round_no: int
    verdict: str
    reasons: List[str]
    data: dict
    test_files: List[str] = field(default_factory=list)
    commit_sha: str = ""
    review_md: str = ""
    review_path: str = ""
    model_actual: str = ""
    model_reason: str = ""  # why this reviewer model (auto, explicit, --reviewer-model)
    cost: float = 0.0
    fallback: bool = False
    bounces: int = 0
    gaps: List[str] = field(default_factory=list)


# ---- inputs -----------------------------------------------------------------


def assemble_checklist(ctx: Context) -> str:
    parts = []
    if ctx.builtin_checklist and os.path.isfile(ctx.builtin_checklist):
        parts.append("### Built-in\n\n" + read_text(ctx.builtin_checklist).strip())
    user_path = ctx.user_cfg.checklist if ctx.user_cfg else ""
    if user_path and os.path.isfile(user_path):
        parts.append("### User\n\n" + read_text(user_path).strip())
    proj = ctx.cfg.review.checklist
    if proj:
        path = proj if os.path.isabs(proj) else os.path.join(ctx.repo_root, proj)
        if os.path.isfile(path):
            parts.append(
                "### Project (overrides the layers above on conflict)\n\n" + read_text(path).strip()
            )
    return "\n\n".join(parts) if parts else "(no checklist configured)"


# after a listed test file the review's diff leaves out (`[review] exclude`); a rename names
# its new path (`%s`), since `git diff` on the old name alone shows a deletion
OMITTED_TEST_MARK = "(omitted from the diff above; `git diff` shows its change)"
RENAMED_TEST_MARK = (
    "(renamed to %s; omitted from the diff above; `git diff` on both names shows its change)"
)


def _omitted_mark(names: Tuple[str, ...]) -> str:
    return RENAMED_TEST_MARK % names[-1] if len(names) > 1 else OMITTED_TEST_MARK


def modified_tests_value(ctx: Context, exclude: List[str], drop: Collection[str]) -> str:
    """The prompt's list of the files under test_dir on the base that the diff modifies,
    deletes or renames (a rename under its old name), whatever their name: an earlier
    change's reviewer test is listed like any other. A file the branch adds is not on the
    base and not listed. A file the review's diff leaves out (`gitops.left_out`) stays
    listed, so its change still gets a `test_changes` entry, followed by OMITTED_TEST_MARK, or
    by RENAMED_TEST_MARK with its new path when it is a rename."""
    test_dir = ctx.cfg.project.test_dir
    out = [
        names[0] + (" " + _omitted_mark(names) if gitops.left_out(names, exclude, drop) else "")
        for status, names in gitops.name_status(ctx.base_ref, "HEAD", ctx.repo_root)
        if status in ("M", "D", "R", "T") and under_test_dir(names[0], test_dir)
    ]
    return ", ".join(out) or "(none)"


def manifests_touched(ctx: Context) -> List[str]:
    return [p for p in ctx.changed_files if gitops.matches_any(p, MANIFEST_PATTERNS)]


def _prior_findings_section(rdir: str, round_no: int) -> str:
    if round_no <= 1:
        return ""
    path = os.path.join(rdir, "review-%d.json" % (round_no - 1))
    if not os.path.isfile(path):
        return ""
    try:
        prev = json.loads(read_text(path))
    except ValueError:
        return ""
    data = prev.get("data", prev)
    lines = [
        "# Previous round (%d)" % (round_no - 1),
        "",
        "Verdict: %s. Summary: %s" % (data.get("verdict", "?"), data.get("summary", "")),
        "",
        "Findings from that round (mark each in `previous_findings` as resolved, unresolved,",
        "or accepted when the author's response convinced you; raise new findings only for",
        "what the fix introduced):",
        "",
    ]
    for f in data.get("findings", []):
        lines.append(
            "- %s [%s %s] %s:%s %s"
            % (
                f.get("id", "?"),
                f.get("severity", "?"),
                f.get("kind", "?"),
                f.get("file", "?"),
                f.get("line", 0),
                f.get("text", ""),
            )
        )
    if not data.get("findings"):
        lines.append("- (none)")
    if data.get("questions"):
        lines += ["", "Questions you asked:"] + ["- " + q for q in data["questions"]]
    return "\n".join(lines) + "\n"


def _response_section(rdir: str, round_no: int) -> str:
    if round_no <= 1:
        return ""
    path = os.path.join(rdir, "response-%d.md" % (round_no - 1))
    if not os.path.isfile(path):
        return ""
    return "# The author's response to round %d\n\n%s\n" % (round_no - 1, read_text(path).strip())


def _prior_tests_section(state: State) -> str:
    if not state.test_files:
        return ""
    return (
        "Test files you wrote in earlier rounds (update them; do not create duplicates):\n"
        + "\n".join("- " + p for p in state.test_files)
        + "\n"
    )


def _pending_tests_section(state: State) -> str:
    if not state.pending_test_files:
        return ""
    return (
        "Files you wrote in the previous round (NEEDS_INFO) that are not committed yet; they are\n"
        "in the working tree. Update or delete them; whatever remains is committed with this\n"
        "round's tests:\n" + "\n".join("- " + p for p in state.pending_test_files) + "\n"
    )


def tracked_test_files(ctx: Context) -> List[str]:
    """Files under test_dir that HEAD tracks, forward slashes."""
    res = gitops._git(["ls-files", "--", ctx.cfg.project.test_dir], ctx.repo_root)
    return [p.strip().replace("\\", "/") for p in res.stdout.splitlines() if p.strip()]


def existing_test_names(ctx: Context, state: State) -> List[str]:
    """Tracked files matching test_file_pattern that this pipeline did not write:
    names the reviewer must not use or modify."""
    pattern = test_pattern_glob(ctx)
    return sorted(
        p
        for p in tracked_test_files(ctx)
        if gitops.matches_any(p, [pattern]) and p not in state.test_files
    )


def branch_test_commits(ctx: Context) -> Tuple[List[Tuple[str, List[str]]], Dict[str, str]]:
    """The reviewer's test commits between the base and HEAD, oldest first: the commits whose
    message carries the `Revali-Round` trailer (commit_tests writes it), each with the files
    under test_dir it added or modified that HEAD still tracks and that are still the
    reviewer's. This is how ownership survives a history rewrite, which gives those commits
    new SHAs; a rewrite that drops the trailer (the author folds the reviewer's commit into
    their own) drops the ownership with it.

    The newest commit in the range that added a path decides whose the path is, as git sees
    it: the author takes a file over by deleting it in one commit and re-creating the name in
    a later commit without the trailer. A commit that replaces the content in place is a
    modification to git, like any edit, and changes nothing. The taken-over paths are
    returned separately, as {path: sha of the commit that re-added it}, "" when no commit in
    the range added the path at all."""
    tracked = set(tracked_test_files(ctx))
    test_dir = ctx.cfg.project.test_dir
    found = gitops.trailer_commits(ctx.base_ref, "HEAD", TRAILER, ctx.repo_root)
    reviewer = {sha for sha, _ in found}
    added_by: Dict[str, str] = {}  # one git call per path, whichever side owns it
    out = []
    taken: Dict[str, str] = {}
    for sha, _ in found:
        paths = []
        for p in gitops.commit_paths(sha, ctx.repo_root):
            if not under_test_dir(p, test_dir) or p not in tracked:
                continue
            if p not in added_by:
                added_by[p] = gitops.last_add_commit(ctx.base_ref, "HEAD", p, ctx.repo_root)
            if added_by[p] in reviewer:
                paths.append(p)
            else:
                taken[p] = added_by[p]
        out.append((sha, sorted(paths)))
    return out, taken


def short_shas(shas: Sequence[str]) -> str:
    """`a1b2c3d4e5, f6a7b8c9d0`: the log's spelling of a list of commits."""
    return ", ".join(sha[:10] for sha in shas)


def recover_test_ownership(
    ctx: Context, state: State, log: Optional[RunLog]
) -> Tuple[List[str], List[str]]:
    """Add the branch's trailer commits and their surviving test files to the state (front of
    the lists, no duplicates), and drop the files the author took over by deleting and
    re-creating them. Runs on every run, so the state heals whatever forgot the files: a
    rewrite, `revali reset`, a state written before this rule existed. Idempotent; returns
    (commits, files) found on the branch and logs only what changed in the state, so a run
    where nothing changed stays quiet."""
    found, taken = branch_test_commits(ctx)
    commits = [sha for sha, _ in found]
    files = sorted({p for _, paths in found for p in paths})
    new_commits = [c for c in commits if c not in state.test_commits]
    new_files = [f for f in files if f not in state.test_files]
    dropped = [f for f in state.test_files if f in taken]
    emptied = [sha for sha, paths in found if not paths and sha in new_commits]
    state.test_commits = commits + [c for c in state.test_commits if c not in commits]
    state.test_files = files + [f for f in state.test_files if f not in files and f not in taken]
    if log and new_files:
        message = (
            "recovered the reviewer's %d test file(s) from %d earlier test commit(s) on the "
            "branch (%s trailer): %s; commits %s"
            % (
                len(new_files),
                len(commits),
                TRAILER,
                ", ".join(new_files),
                short_shas(commits),
            )
        )
        if emptied:
            message += "; none of the files of %d of them is still the reviewer's in HEAD: %s" % (
                len(emptied),
                short_shas(emptied),
            )
        log.stage("run", message)
    elif log and emptied:
        # nothing recovered (no file left, or the state kept its files but lost its commits):
        # the commits new to the state whose files are all gone still get a line, once
        log.stage(
            "run",
            "found %d earlier reviewer test commit(s) on the branch (%s trailer) but none of "
            "their test files is still the reviewer's in HEAD: %s"
            % (len(emptied), TRAILER, short_shas(emptied)),
        )
    re_added = [f for f in dropped if taken[f]]
    never_added = [f for f in dropped if not taken[f]]
    if log and re_added:
        log.stage(
            "run",
            "the reviewer's test file(s) were deleted and re-created by a commit without the "
            "%s trailer; they are the author's now, existing files the reviewer must not "
            "modify (to hand one back, delete it in a commit of its own and let the reviewer "
            "re-create it): %s"
            % (
                TRAILER,
                ", ".join("%s (re-added by %s)" % (f, short_shas([taken[f]])) for f in re_added),
            ),
        )
    if log and never_added:
        log.stage(
            "run",
            "the reviewer's test file(s) were not added by any commit between %s and HEAD, so "
            "they are existing files the reviewer must not modify: %s"
            % (ctx.base_ref, ", ".join(never_added)),
        )
    return commits, files


def _existing_tests_section(ctx: Context, state: State) -> str:
    names = existing_test_names(ctx, state)
    if not names:
        return ""
    return (
        "Test files that already exist in `%s/` and are not yours (do not modify, delete, or\n"
        "overwrite them; choose a topic that gives a different file name):\n"
        % ctx.cfg.project.test_dir
        + "\n".join("- " + p for p in names)
        + "\n"
    )


def _lint_section(ctx: Context) -> str:
    cmd = ctx.cfg.project.lint.strip()
    text = ""
    if cmd:
        text = (
            "The project's lint command `%s` must pass on your files: the script runs it over "
            "the tree before the smoke run and sends you back once when it fails.\n" % cmd
        )
    fmt = ctx.cfg.project.format.strip()  # it runs with or without a lint line
    if fmt:
        text += (
            "%s the script formats your test files with `%s`, so you need not "
            "format them yourself.\n" % ("Before lint" if cmd else "After your answer", fmt)
        )
    return text


RELATED_NAMES_MAX = 3  # names listed per document; the rest are counted


def _related_entry(r) -> str:
    parts = []
    if r.links:
        parts.append("links to " + ", ".join("`%s` (line %d)" % (path, no) for path, no in r.links))
    if r.names:
        shown = ", ".join("`%s` (line %d)" % (n, no) for n, no in r.names[:RELATED_NAMES_MAX])
        more = len(r.names) - RELATED_NAMES_MAX
        parts.append("mentions " + shown + (" and %d more" % more if more > 0 else ""))
    mark = " (changed in this diff)" if r.changed else ""
    return "- `%s`%s: %s" % (r.page, mark, "; ".join(parts))


def _os_error_text(exc: OSError, root: str) -> str:
    """`exc` for the prompt: its file relative to `root`, or its last part when outside, so
    the prompt and its saved copy do not carry the user's home directory."""
    name = exc.filename
    if not isinstance(name, str) or not exc.strerror:
        return str(exc)
    try:
        rel = os.path.relpath(name, root)
    except ValueError:
        rel = ".."
    if rel == ".." or rel.startswith(".." + os.sep):
        rel = os.path.basename(name)
    return "%s: %s" % (exc.strerror, rel.replace(os.sep, "/"))


def _lines_outside_tests(
    changed_lines: Dict[str, List[str]], test_dir: str
) -> Dict[str, List[str]]:
    """The changed lines of the files outside `test_dir`: a test's identifiers, fixture
    strings and AC references are not names a document should be listed for. The test files'
    paths still count, as links and as names (`docgraph.related` takes them from `paths`)."""
    return {f: v for f, v in changed_lines.items() if not under_test_dir(f, test_dir)}


def _related_docs_section(
    ctx: Context, diff: str, exclude: List[str], drop: Collection[str] = ()
) -> str:
    """The documents of the link graph that concern this diff, and a map of the graph's
    documents. Empty when `[review] doc_roots` is empty; a graph that cannot be read is
    logged and said, never an error of the run."""
    from revali import docgraph

    roots = list(ctx.cfg.review.doc_roots)
    if not roots:
        return ""
    head = "## Documents related to this change\n\n"
    try:
        graph = docgraph.build(ctx.repo_root, roots)
        # `diff` and these paths leave out the same files (`gitops.left_out`)
        paths = gitops.diff_paths(ctx.base_ref, "HEAD", ctx.repo_root, exclude=exclude, drop=drop)
        changed = _lines_outside_tests(gitops.diff_changed_lines(diff), ctx.cfg.project.test_dir)
        found = docgraph.related(graph, paths, changed)
    except OSError as exc:
        if ctx.log:
            ctx.log.stage("review", "the documentation graph could not be read: %s" % exc)
        return head + "This round the related documents are unavailable: %s\n" % _os_error_text(
            exc, ctx.repo_root
        )
    if not graph.nodes:
        return (
            head
            + "There is no documentation graph: none of the documentation roots exists (%s).\n"
            % ", ".join(roots)
        )
    lines = [
        "Documents reachable by relative links from %s that link to a file this diff "
        "changes, or name in a code span something it adds or removes outside `%s/` (what "
        "it adds or removes there is not counted as a name; a link to a file there, or its "
        "path, still counts). They are not in the diff unless marked; read each one and "
        "compare it with the new behaviour." % (", ".join(roots), ctx.cfg.project.test_dir),
        "",
    ]
    if found:
        lines += [_related_entry(r) for r in found]
    else:
        lines.append("No document in the graph links to a changed file or names a changed name.")
    docs = [n for n in graph.nodes.values() if n.path.lower().endswith(".md")]
    cap = ctx.cfg.review.doc_map_max
    if cap > 0:
        lines += ["", "Map of the documentation (depth = links from the nearest root):", ""]
        lines += ["- `%s` (depth %d)" % (n.path, n.depth) for n in docs[:cap]]
        if len(docs) > cap:
            lines.append("- and %d more" % (len(docs) - cap))
    return head + "\n".join(lines) + "\n"


def _bounce_section(notes: str) -> str:
    if not notes:
        return ""
    return "# Corrections required before this review is accepted\n\n%s\n" % notes.strip()


def build_prompt(
    ctx: Context, state: State, rdir: str, round_no: int, bounce_notes: str = ""
) -> str:
    cfg = ctx.cfg
    exclude, drop = review_left_out(ctx)
    diff = gitops.diff_text(ctx.base_ref, "HEAD", ctx.repo_root, exclude=exclude, drop=drop)
    kind = ctx.doc.kind
    tests_required = (
        "required for kind %s" % kind
        if kind in ("feature", "fix")
        else "not required for kind %s: leave `tests` empty and list every AC in "
        "`not_testable` with reason 'kind %s'" % (kind, kind)
    )
    guide = ""
    if cfg.project.test_guide:
        gpath = os.path.join(ctx.repo_root, cfg.project.test_guide)
        if os.path.isfile(gpath):
            guide = "\nHow tests are added in this project (from %s):\n\n%s\n" % (
                cfg.project.test_guide,
                read_text(gpath).strip(),
            )
    values = {
        "branch": ctx.branch,
        "base": ctx.base,
        "kind": kind,
        "round": round_no,
        "round_note": " (a re-review after the author changed the branch)" if round_no > 1 else "",
        "change_md": ctx.doc.raw.strip(),
        "diff": diff.strip() or "(empty)",
        "test_file_pattern": cfg.project.test_file_pattern,
        "exclude": ", ".join(cfg.review.exclude) or "(nothing)",
        "excluded_files": ", ".join(ctx.excluded_files) or "(none)",
        "modified_tests": modified_tests_value(ctx, exclude, drop),
        "manifests": ", ".join(manifests_touched(ctx)) or "(none)",
        "related_docs_section": _related_docs_section(ctx, diff, exclude, drop),
        "prior_section": _prior_findings_section(rdir, round_no),
        "response_section": _response_section(rdir, round_no),
        "bounce_section": _bounce_section(bounce_notes),
        "lint_section": _lint_section(ctx),
        "test_dir": cfg.project.test_dir,
        "tests_required": tests_required,
        "test_guide_section": guide,
        "prior_tests_section": _prior_tests_section(state),
        "pending_tests_section": _pending_tests_section(state),
        "existing_tests_section": _existing_tests_section(ctx, state),
        "checklist": assemble_checklist(ctx),
    }
    template = string.Template(read_text(ctx.review_prompt))
    return template.safe_substitute(values)


# ---- reviewer session -------------------------------------------------------


def validate_shape(data) -> List[str]:
    problems = []
    if not isinstance(data, dict):
        return ["structured output is not an object"]
    if data.get("verdict") not in (APPROVE, CHANGES_REQUESTED, NEEDS_INFO):
        problems.append("verdict missing or invalid")
    if not isinstance(data.get("summary"), str) or not data.get("summary", "").strip():
        problems.append("summary missing")
    for key in LIST_FIELDS:
        if not isinstance(data.get(key), list):
            problems.append("%s must be a list" % key)
    for f in data.get("findings", []) if isinstance(data.get("findings"), list) else []:
        if (
            not isinstance(f, dict)
            or f.get("severity") not in ("high", "medium", "low")
            or f.get("kind") not in ("correctness", "convention", "security")
        ):
            problems.append("finding with invalid severity/kind: %r" % (f,))
    for t in data.get("tests", []) if isinstance(data.get("tests"), list) else []:
        if not isinstance(t, dict) or not t.get("path") or not isinstance(t.get("covers"), list):
            problems.append("test entry without path/covers: %r" % (t,))
    return problems


def planned_reviewer(ctx: Context) -> models.Resolved:
    """Which model the Reviewer would run on, and why (explicit or auto). `run
    --reviewer-model` (ctx.reviewer_model) takes the place of `[review] model` for this run,
    and the reason then says so and where it stands against the author's tier."""
    cfg = ctx.cfg.review
    engine_cfg = ctx.cfg.engine_for("review")
    author = ctx.doc.author_model if ctx.doc else ""
    chosen = models.resolve(
        models.REVIEWER,
        ctx.reviewer_model or cfg.model,
        cfg.fallback_model,
        author,
        engine_cfg.tiers,
        ctx.cfg.foreign_ladders(engine_cfg.name),
    )
    if ctx.reviewer_model:
        chosen.reason = "--reviewer-model, %s" % models.against_author(
            chosen.model, author, engine_cfg.tiers
        )
    return chosen


def spawn_reviewer(
    ctx: Context,
    prompt: str,
    rdir: str,
    round_no: int,
    attempt: int,
    log: Optional[RunLog],
    cancel=None,
    advice=run_advice,
) -> ReviewerRun:
    """One Reviewer session. A model that cannot serve (its usage limit, overloaded) stops it
    with `advice(stop)`: by default what a `revali run` can do next; tools/review_bench.py
    passes its own (PR #99 round 6 F1)."""
    cfg = ctx.cfg.review
    engine = engines.for_role(ctx.cfg, "review")
    chosen = planned_reviewer(ctx)
    raw_path = os.path.join(ctx.logs, "review-r%d-%d.raw.json" % (round_no, attempt))
    if log:
        log.stage(
            "review",
            "round %d attempt %d: reviewer %s%s via %s (budget $%.2f, timeout %d min)"
            % (
                round_no,
                attempt,
                chosen.model,
                " (%s)" % chosen.reason if chosen.reason else "",
                engine.name,
                cfg.budget_usd,
                cfg.timeout_min,
            ),
        )
    request = EngineRequest(
        role="reviewer",
        prompt=prompt,
        schema_text=read_text(ctx.review_schema),
        model=chosen.model,
        fallback_model=chosen.fallback,
        effort=cfg.effort,
        budget_usd=cfg.budget_usd,
        timeout_s=cfg.timeout_min * 60,
        cwd=ctx.repo_root,
        raw_path=raw_path,
        may_write=[ctx.cfg.project.test_dir],
        shell_allow=list(SHELL_ALLOW),
        cancel=cancel,
    )
    try:
        result = engine.run(request, log)
    except ModelUnavailable as stop:
        raise stop.with_advice(advice(stop)) from stop
    problems = validate_shape(result.data)
    if problems:
        raise Stop(
            EXIT_ERROR,
            "reviewer output does not match the schema: %s; raw saved to %s"
            % ("; ".join(problems[:5]), raw_path),
            cost=result.cost,
        )
    return ReviewerRun(
        data=result.data,
        raw=result.raw,
        model_requested=chosen.model,
        model_actual=result.model_actual,
        fallback=result.fallback,
        cost=result.cost,
        denials=result.denials,
        duration_ms=result.duration_ms,
        model_reason=chosen.reason,
    )


def discard_unfinished_tests(
    ctx: Context,
    log: Optional[RunLog],
    left_by: str = "the reviewer",
    stage: str = "review",
    only: Optional[Sequence[str]] = None,
) -> Tuple[List[str], List[str]]:
    """Delete untracked files (and files only added to the index, unstaged first) matching
    test_file_pattern under test_dir after a review round
    stopped before its tests were committed: they are half-written and would make the next
    run refuse a dirty tree. This and testarchive.remove_placed (the copies of the archived
    files a round placed there, in archive mode) are the only deletions inside test_dir
    revali ever performs. `left_by` names the culprit in the log line ("the reviewer", "the
    interrupted run"); `stage` is the log label, `run` when the cleanup happens before
    preflight's tree check.
    `only` restricts the sweep to those paths (the pending list, when no session was
    interrupted and the reviewer's files are known exactly); None sweeps the whole pattern.
    Returns (removed, stuck): the paths deleted, and those the OS refused to delete (an open
    file on Windows), which the caller keeps in the tolerated list."""
    pattern = test_pattern_glob(ctx)
    wanted = None if only is None else {p.replace("\\", "/") for p in only}
    removed = []
    stuck = []
    reasons = []
    for entry in gitops.dirty_paths(ctx.repo_root, (ctx.cfg.paths.state_dir + "/",)):
        code, path = entry.split(" ", 1)
        path = path.replace("\\", "/")
        if wanted is not None and path not in wanted:
            continue
        # "A" is a file in the index but not in HEAD: above all a draft the lint gate had
        # marked intent-to-add when the run was killed before its `finally` could unmark it.
        # It is never committed, so it is a draft like an untracked one.
        if (
            (code == "??" or code[0] == "A")
            and under_test_dir(path, ctx.cfg.project.test_dir)
            and gitops.matches_any(path, [pattern])
        ):
            try:
                if code[0] == "A":
                    gitops.git_ok(["reset", "-q", "--", path], ctx.repo_root)
                full = os.path.join(ctx.repo_root, path)
                if os.path.lexists(full):  # "AD": only the index entry was left
                    os.remove(full)
                removed.append(path)
            except (OSError, gitops.GitError) as exc:
                stuck.append(path)
                reasons.append("%s (%s)" % (path, exc))
    if removed and log:
        log.stage(
            stage,
            "removed %d unfinished test file(s) %s left behind: %s"
            % (len(removed), left_by, ", ".join(removed)),
        )
    if stuck and log:
        log.stage(
            stage,
            "could not remove %d unfinished test file(s): %s" % (len(stuck), ", ".join(reasons)),
        )
    return removed, stuck


# ---- checks after the reviewer --------------------------------------------


def drop_pending_tests(
    ctx: Context,
    state: State,
    log: Optional[RunLog],
    stage: str = "review",
    keep: Sequence[str] = (),
    tolerated_next_run: bool = True,
) -> None:
    """Forget a NEEDS_INFO round's pending files once a round that was meant to commit them
    stopped: the untracked ones are gone with discard_unfinished_tests, and a tracked one the
    reviewer had modified (its own file from an earlier round) goes back to HEAD, so the next
    run does not refuse a file the author was told to leave alone. `keep` lists the files
    the cleanup could not delete: they stay in (or join) the list, so the next run tolerates
    them and hands them to the reviewer instead of refusing the tree. A tracked pending file
    the reviewer deleted comes back the same way; HEAD must have it (an intent-to-add entry
    is in the index but not in HEAD)."""
    restored = []
    tracked = set(tracked_test_files(ctx))
    for path in state.pending_test_files:
        if path in tracked and _in_head(path, ctx.repo_root):
            _restore_from_head(path, ctx.repo_root)
            restored.append(path)
    if restored and log:
        log.stage(
            stage,
            "restored %d pending test file(s) of the reviewer's own from HEAD: %s"
            % (len(restored), ", ".join(restored)),
        )
    kept = sorted(set(p.replace("\\", "/") for p in keep))
    if kept and log and tolerated_next_run:
        log.stage(
            stage,
            "the next run will tolerate the %d file(s) it could not remove and list them "
            "for the reviewer to update or delete: %s" % (len(kept), ", ".join(kept)),
        )
    # in place: the run passed this very list to preflight as `tolerate` before the cleanup hook ran
    state.pending_test_files[:] = kept


def restore_own_tests(
    ctx: Context, state: State, log: Optional[RunLog], left_by: str, stage: str
) -> List[str]:
    """Restore from HEAD the reviewer's own test files from earlier rounds (`state.test_files`,
    committed, so HEAD has them) that a session modified or deleted before its round could
    commit. Only for a session that was in flight: the round started on a tree preflight found
    clean, so nothing but that session changed them. A pending NEEDS_INFO file is left to
    drop_pending_tests, which logs it on its own. Returns the restored paths."""
    restored = []
    for entry in gitops.dirty_paths(ctx.repo_root, (ctx.cfg.paths.state_dir + "/",)):
        code, path = entry.split(" ", 1)
        path = path.replace("\\", "/")
        if code[0] in "?A" or path not in state.test_files or path in state.pending_test_files:
            continue  # not in HEAD, not the reviewer's, or a pending file
        _restore_from_head(path, ctx.repo_root)
        restored.append(path)
    restored.sort()
    if restored and log:
        log.stage(
            stage,
            "restored %d test file(s) of the reviewer's own from earlier rounds that %s "
            "modified or deleted: %s" % (len(restored), left_by, ", ".join(restored)),
        )
    return restored


# Every State field discard_round_leftovers reads, directly or through the helpers it calls
# (testarchive.archive_mode / remove_placed, restore_own_tests, drop_pending_tests): the
# pending list, the interrupted-session flag, the tests mode, archive mode's placed copies,
# the reviewer's own files. `revali reset` cleans up from a partly unreadable state only when
# none of these was skipped, so a field the cleanup starts reading must be added here.
CLEANUP_FIELDS = (
    "pending_test_files",
    "reviewer_running",
    "tests_mode",
    "placed_test_files",
    "test_files",
)


def discard_round_leftovers(
    ctx: Context,
    state: State,
    log: Optional[RunLog],
    left_by: str,
    stage: str,
    only: Optional[Sequence[str]] = None,
    tolerated_next_run: bool = True,
) -> None:
    """Everything a round that did not reach its commit leaves behind, in one call: delete
    the untracked drafts, restore a modified tracked pending file, keep the undeletable ones
    tolerated. Shared by the stop path of a round, the cleanup before the next run, and
    `revali reset`. `only` limits the deletion to the paths named (see
    discard_unfinished_tests); an interrupted session's files are not known, so those callers
    leave it None. `tolerated_next_run` False skips the "next run will tolerate" line: `reset`
    drops the state that would carry the list and gives its own advice. In archive mode the
    copies of the archived files the round placed in test_dir go first (by the state's list,
    whatever their names), the archive itself is never touched.
    `only` None also means a session was in flight (the round's own stop path, the run after
    an interrupted round, `reset` with `reviewer_running`), so the reviewer's own files from
    earlier rounds that it changed go back to HEAD too; with a list, no session ran and such a
    change is the author's, left for preflight to refuse."""
    from revali import testarchive  # lazily: testarchive uses this module's helpers

    stuck: List[str] = []
    if testarchive.archive_mode(ctx, state):
        _, stuck = testarchive.remove_placed(ctx, state, log, stage)
    _, more = discard_unfinished_tests(ctx, log, left_by, stage=stage, only=only)
    stuck += more
    if only is None:
        restore_own_tests(ctx, state, log, left_by, stage)
    drop_pending_tests(
        ctx, state, log, stage=stage, keep=stuck, tolerated_next_run=tolerated_next_run
    )


def interruption_cleanup(state: State, rdir: str, log: Optional[RunLog]):
    """The `before_tree` hook for a run that follows an interrupted round: delete what the
    unfinished session left under test_dir, drop the pending files (untracked ones were among
    them; a modified tracked one goes back to HEAD), clear the flag. None when no round was
    interrupted."""
    if not state.reviewer_running:
        return None

    def hook(ctx: Context) -> None:
        discard_round_leftovers(ctx, state, log, "the interrupted run", stage="run")
        state.reviewer_running = False
        state.save(rdir)

    return hook


def under_test_dir(path: str, test_dir: str) -> bool:
    """True when `path` lies under `test_dir`, whichever way either is spelled (trailing
    slash, backslashes). validate.py decides baseline reuse with the same check."""
    p = path.replace("\\", "/").rstrip("/")
    d = test_dir.replace("\\", "/").rstrip("/") + "/"
    return p.startswith(d)


def _in_head(path: str, root: str) -> bool:
    return gitops._git(["cat-file", "-e", "HEAD:" + path], root).ok


def _restore_from_head(path: str, root: str) -> None:
    """Index and working tree of `path` back to HEAD; a git failure ends the run (exit 1)."""
    try:
        gitops.git_ok(["checkout", "HEAD", "--", path], root)
    except gitops.GitError as exc:
        raise Stop(EXIT_ERROR, "could not restore %s from HEAD: %s" % (path, exc)) from exc


def _remove_new(path: str, code: str, root: str) -> None:
    """Delete a file that is not in HEAD; a staged addition is unstaged first.
    A git failure ends the run (exit 1), as in _restore_from_head."""
    if code[0] == "A":
        try:
            gitops.git_ok(["rm", "-q", "--cached", "--", path], root)
        except gitops.GitError as exc:
            raise Stop(EXIT_ERROR, "could not unstage %s: %s" % (path, exc)) from exc
    full = os.path.join(root, path)
    if os.path.isdir(full):
        import shutil

        shutil.rmtree(full, ignore_errors=True)
    elif os.path.exists(full):
        os.remove(full)


def guard_worktree(ctx: Context, log: Optional[RunLog]) -> List[str]:
    """Restore from HEAD anything the reviewer touched outside test_dir. Returns the offending
    paths."""
    root = ctx.repo_root
    offenders = []
    for entry in gitops.dirty_paths(root, (ctx.cfg.paths.state_dir + "/",)):
        code, path = entry.split(" ", 1)
        if under_test_dir(path, ctx.cfg.project.test_dir):
            continue
        offenders.append(path)
        if code == "??" or code[0] == "A":
            _remove_new(path, code, root)
        else:
            _restore_from_head(path, root)
    if offenders and log:
        log.stage(
            "review",
            "reviewer touched files outside %s; reverted: %s"
            % (ctx.cfg.project.test_dir, ", ".join(offenders)),
        )
    return offenders


def restore_protected_tests(ctx: Context, state: State, log: Optional[RunLog]) -> List[str]:
    """Restore from HEAD every tracked file under test_dir the reviewer modified or deleted
    that this pipeline did not write in an earlier round. Returns the restored paths."""
    root = ctx.repo_root
    restored = []
    for entry in gitops.dirty_paths(root, (ctx.cfg.paths.state_dir + "/",)):
        code, path = entry.split(" ", 1)
        path = path.replace("\\", "/")
        if code == "??" or code[0] == "A" or not under_test_dir(path, ctx.cfg.project.test_dir):
            continue  # new files are the reviewer's own
        if path in state.test_files:
            continue
        _restore_from_head(path, root)
        restored.append(path)
    restored.sort()
    if restored and log:
        log.stage(
            "review",
            "reviewer modified existing test file(s) it did not write; restored: %s"
            % ", ".join(restored),
        )
    return restored


def new_test_files(ctx: Context) -> List[str]:
    files = []
    for entry in gitops.dirty_paths(ctx.repo_root, (ctx.cfg.paths.state_dir + "/",)):
        _, path = entry.split(" ", 1)
        if under_test_dir(path, ctx.cfg.project.test_dir):
            files.append(path.replace("\\", "/"))
    return sorted(files)


def ac_gaps(data: dict, ac_ids: List[str]) -> List[str]:
    covered = set()
    for t in data.get("tests", []):
        covered.update(str(c) for c in t.get("covers", []))
    for nt in data.get("not_testable", []):
        if isinstance(nt, dict) and nt.get("reason", "").strip():
            covered.add(str(nt.get("ac")))
    return [ac for ac in ac_ids if ac not in covered]


def is_blocking(f: dict) -> bool:
    sev, kind = f.get("severity"), f.get("kind")
    return (kind in ("correctness", "security") and sev in ("high", "medium")) or (
        kind == "convention" and sev == "high"
    )


def format_finding(f: dict) -> str:
    return "%s [%s %s] %s:%s %s" % (
        f.get("id", "F?"),
        f.get("severity"),
        f.get("kind"),
        f.get("file", "?"),
        f.get("line", 0),
        f.get("text", ""),
    )


def non_blocking_findings(data: dict) -> List[dict]:
    return [f for f in data.get("findings", []) if not is_blocking(f)]


def finding_counts(data: dict) -> Tuple[int, int]:
    """(blocking, non-blocking) finding counts of one reviewer answer."""
    findings = data.get("findings", [])
    blocking = sum(1 for f in findings if is_blocking(f))
    return blocking, len(findings) - blocking


def counts_label(data: dict, review_path: str) -> str:
    """Suffix for the needs_action stage message: counts and where the full review is."""
    blocking, other = finding_counts(data)
    return "%d blocking, %d non-blocking finding(s); full review: %s" % (
        blocking,
        other,
        review_path,
    )


def non_blocking_note(data: dict, round_no: int, review_path: str, rdir: str) -> str:
    """Lines appended to an ACTION NEEDED message: the findings that did not block,
    which the author must still answer. Empty when there are none."""
    others = non_blocking_findings(data)
    if not others:
        return ""
    lines = [
        "Also %d non-blocking finding(s) in %s; fix or answer each in %s "
        "or they come back as unresolved:"
        % (len(others), review_path, os.path.join(rdir, "response-%d.md" % round_no))
    ]
    lines += ["  - " + format_finding(f) for f in others]
    return "\n" + "\n".join(lines)


def _check_reasons(data: dict, gaps: List[str]) -> List[str]:
    """What blocks a round whatever the reviewer's own verdict says."""
    reasons = []
    for f in data.get("findings", []):
        if is_blocking(f):
            reasons.append(format_finding(f))
    for tc in data.get("test_changes", []):
        if not tc.get("justified"):
            reasons.append(
                "existing test %s changed without justification: %s"
                % (tc.get("file", "?"), tc.get("reason", ""))
            )
    for dep in data.get("dependencies_changed", []):
        if not dep.get("justified"):
            reasons.append(
                "dependency change in %s not justified: %s"
                % (dep.get("file", "?"), dep.get("reason", ""))
            )
    if gaps:
        reasons.append(
            "acceptance criteria without a test or a not_testable reason: %s" % ", ".join(gaps)
        )
    return reasons


def reviewer_only(data: dict, gaps: List[str]) -> bool:
    """True when the round asks for changes only because the reviewer's verdict does: no
    blocking finding, unjustified test or dependency change or AC gap. Open questions need
    no check here: they come with the verdict NEEDS_INFO, which this rules out."""
    return data.get("verdict") == CHANGES_REQUESTED and not _check_reasons(data, gaps)


def compute_verdict(data: dict, gaps: List[str], needs_info_allowed: bool) -> Tuple[str, List[str]]:
    reasons = _check_reasons(data, gaps)
    questions = [q for q in data.get("questions", []) if str(q).strip()]
    if data.get("verdict") == NEEDS_INFO and questions:
        if needs_info_allowed and not reasons:
            return NEEDS_INFO, ["question: " + str(q) for q in questions]
        reasons.append(
            "reviewer still has unanswered questions: " + "; ".join(str(q) for q in questions)
        )
    if reasons:
        return CHANGES_REQUESTED, reasons
    if data.get("verdict") == CHANGES_REQUESTED:
        return CHANGES_REQUESTED, ["reviewer requested changes: " + data.get("summary", "")]
    return APPROVE, []


def format_files(ctx: Context, test_files: List[str], log: Optional[RunLog]) -> None:
    """Run `[project] format` on `test_files`, the files under `test_dir` that differ from
    HEAD after the attempt (in archive mode the earlier rounds' placed-back files too), so a
    file that only needs formatting costs no bounce (the reviewer has no shell to run a
    formatter). A deleted file is a difference from HEAD too; it is left out, so the formatter
    only gets paths that exist. Its exit code is logged, not judged: lint runs next and
    decides."""
    cmd = ctx.cfg.project.format.strip()
    test_files = [p for p in test_files if os.path.isfile(os.path.join(ctx.repo_root, p))]
    if not cmd or not test_files:
        return
    cmd = cmd.replace(FILES_PLACEHOLDER, files_argument(test_files))
    try:
        res = run_shell(
            cmd,
            cwd=ctx.repo_root,
            timeout=ctx.cfg.review.timeout_min * 60,
            log=log.detail if log else None,
        )
    except ProcTimeout as exc:
        if log:
            log.stage(
                "review",
                "format of %d test file(s) with `%s`: %s" % (len(test_files), cmd, exc),
            )
        return
    if log:
        log.stage(
            "review",
            "format of %d test file(s) with `%s`: exit %d" % (len(test_files), cmd, res.returncode),
        )


def _intent_to_add(ctx: Context, test_files: List[str]) -> List[str]:
    """`git add -N` the untracked ones among `test_files`; returns the paths it added."""
    wanted = set(test_files)
    untracked = []
    for entry in gitops.dirty_paths(ctx.repo_root, (ctx.cfg.paths.state_dir + "/",)):
        code, path = entry.split(" ", 1)
        if code == "??" and path.replace("\\", "/") in wanted:
            untracked.append(path)
    if untracked:
        try:
            gitops.git_ok(["add", "-N", "--"] + untracked, ctx.repo_root)
        except gitops.GitError as exc:
            raise Stop(
                EXIT_ERROR, "could not mark the reviewer's files for lint: %s" % exc
            ) from exc
    return untracked


def _undo_intent_to_add(ctx: Context, paths: List[str]) -> None:
    """Untracked again, as the rest of the round (cleanup, commit) expects them."""
    if not paths:
        return
    try:
        gitops.git_ok(["reset", "-q", "--"] + paths, ctx.repo_root)
    except gitops.GitError as exc:
        raise Stop(
            EXIT_ERROR, "could not unmark the reviewer's files after lint: %s" % exc
        ) from exc


def lint_check(ctx: Context, test_files: List[str], log: Optional[RunLog]) -> Optional[str]:
    """Run `[project] lint` over the working tree with the reviewer's files in it. None =
    green or nothing to check; a string = red (bounce to the reviewer). Preflight ran the
    same line on the tree before the reviewer wrote anything, so red here is its files."""
    cmd = ctx.cfg.project.lint.strip()
    if not cmd or not test_files:
        return None
    # a lint command that walks `git ls-files` would skip the new files; intent-to-add lists
    # them there without staging content, and is undone whatever lint did
    added = _intent_to_add(ctx, test_files)
    try:
        res = run_shell(
            cmd,
            cwd=ctx.repo_root,
            timeout=ctx.cfg.review.timeout_min * 60,
            log=log.detail if log else None,
        )
    except ProcTimeout as exc:
        raise Stop(EXIT_ERROR, "lint timed out on the reviewer's files: %s" % exc) from exc
    finally:
        _undo_intent_to_add(ctx, added)
    # a command the shell cannot find is a non-zero exit like any other red lint result;
    # preflight ran the same line first and normally stops there
    if log:
        # a path the reviewer deleted is still listed; count the listed files that exist
        present = [p for p in test_files if os.path.isfile(os.path.join(ctx.repo_root, p))]
        log.stage(
            "review",
            "lint of %d test file(s) with `%s`: exit %d" % (len(present), cmd, res.returncode),
        )
    if res.ok:
        return None
    tail = "\n".join(res.text.strip().splitlines()[-30:])
    return "the project's lint command `%s` fails (exit %d) on the tree with your files:\n%s" % (
        cmd,
        res.returncode,
        tail,
    )


def smoke_run(
    ctx: Context,
    test_files: List[str],
    rdir: str,
    round_no: int,
    attempt: int,
    log: Optional[RunLog],
) -> Optional[str]:
    """Run the new tests once in the sandbox. None = they ran (pass or fail);
    a string = they could not run (bounce to the reviewer)."""
    plat = ctx.cfg.validate.platforms[ctx.cfg.project.platforms[0]]
    try:
        runner = get_runner(plat, notice=log.wsl_retry if log else None)
    except RunnerError as exc:
        raise Stop(EXIT_ERROR, str(exc)) from exc
    extra = {
        rel: read_text(os.path.join(ctx.repo_root, rel))
        for rel in test_files
        if os.path.isfile(os.path.join(ctx.repo_root, rel))
    }
    label = "smoke-r%d-%d" % (round_no, attempt)
    if log:
        log.stage("review", "smoke run of %d test file(s) on %s" % (len(extra), runner.name))
    # new_test names the reviewer's files that differ from HEAD when it asks for {files}
    steps = steps_with_files(
        plat, ["setup", "build", "new_test"], sorted(extra), log.stage if log else None, "review"
    )
    started = time.monotonic()
    try:
        report = runner.run(
            ctx.repo_root,
            "HEAD",
            steps,
            extra,
            ctx.logs,
            label,
            log.detail if log else None,
            scope=safe_branch(ctx.branch),
            key=sandbox_key(ctx.repo_root, ctx.branch),
        )
    except RunnerError as exc:
        raise Stop(EXIT_ERROR, "sandbox failed: %s" % exc) from exc
    took = time.monotonic() - started
    if log:
        log.timing.sandbox(label, took)
        log.stage("review", "smoke run finished (%s)" % fmt_duration(took))
    failed = report.failed
    if failed is None:
        return None
    if failed.name != "new_test":
        raise Stop(
            EXIT_ERROR,
            "sandbox %s step failed (exit %d); see %s"
            % (failed.name, failed.returncode, failed.log_path),
        )
    if failed.returncode == 1 and not failed.timed_out:
        return None  # tests ran and some failed: that is validation's business
    tail = "\n".join(failed.text.strip().splitlines()[-30:])
    return "the new tests could not run (exit %d%s) with `%s`:\n%s" % (
        failed.returncode,
        ", timed out" if failed.timed_out else "",
        failed.cmd,
        tail,
    )


def commit_tests(ctx: Context, files: List[str], round_no: int, log: Optional[RunLog]) -> str:
    root = ctx.repo_root
    gitops.git_ok(["add", "--"] + files, root)
    message = (
        "test: review tests (round %d)\n\n"
        "Written by the revali reviewer from the acceptance criteria.\n\n"
        "Co-Authored-By: Claude <noreply@anthropic.com>\n%s: %d\n" % (round_no, TRAILER, round_no)
    )
    res = run(
        resolve("git") + ["commit", "--quiet", "-F", "-"],
        cwd=root,
        input_text=message,
        log=log.detail if log else None,
    )
    if not res.ok:
        raise Stop(EXIT_ERROR, "could not commit the reviewer's tests: %s" % res.text.strip())
    sha = gitops.rev_parse("HEAD", root) or ""
    if log:
        log.stage("review", "committed %d test file(s) as %s" % (len(files), sha[:10]))
    return sha


# ---- rendering --------------------------------------------------------------


def _header(meta: dict) -> str:
    lines = ["<!-- generated by revali -->"]
    for key in (
        "tool",
        "round",
        "model_requested",
        "model_reason",
        "model_actual",
        "fallback",
        "prompt_version",
        "cost_usd",
        "duration_s",
        "at",
    ):
        if key in meta:
            lines.append("%s: %s" % (key, meta[key]))
    return "\n".join(lines) + "\n"


def render_review_md(
    data: dict, verdict: str, reasons: List[str], meta: dict, ac_ids: List[str]
) -> str:
    out = [
        _header(meta),
        "",
        "# Review round %s: %s" % (meta.get("round", "?"), verdict),
        "",
        data.get("summary", "").strip(),
        "",
    ]
    if verdict != data.get("verdict"):
        out.append(
            "_Reviewer said %s; the verdict above follows the severity rules._\n"
            % data.get("verdict")
        )
    if reasons:
        out += ["## Blocking", ""] + ["- " + r for r in reasons] + [""]
    if data.get("questions"):
        out += (
            ["## Questions for the author", ""] + ["- " + str(q) for q in data["questions"]] + [""]
        )
    out += ["## Findings", ""]
    if data.get("findings"):
        for f in data["findings"]:
            out.append(
                "- **%s** [%s %s] `%s:%s` %s"
                % (
                    f.get("id", "?"),
                    f.get("severity"),
                    f.get("kind"),
                    f.get("file", "?"),
                    f.get("line", 0),
                    f.get("text", ""),
                )
            )
            if f.get("suggestion"):
                out.append("  - suggestion: %s" % f["suggestion"])
    else:
        out.append("- none")
    out.append("")
    if data.get("previous_findings"):
        out += ["## Previous findings", ""]
        out += [
            "- %s: %s %s" % (p.get("id"), p.get("status"), p.get("note", ""))
            for p in data["previous_findings"]
        ]
        out.append("")
    if data.get("scope_mismatch"):
        out += ["## Scope mismatch", ""] + ["- " + str(s) for s in data["scope_mismatch"]] + [""]
    if data.get("test_changes"):
        out += ["## Changes to existing tests", ""]
        out += [
            "- `%s`: %s (%s)"
            % (
                t.get("file"),
                "justified" if t.get("justified") else "NOT justified",
                t.get("reason", ""),
            )
            for t in data["test_changes"]
        ]
        out.append("")
    if data.get("dependencies_changed"):
        out += ["## Dependency changes", ""]
        out += [
            "- `%s`: %s (%s)"
            % (
                d.get("file"),
                "justified" if d.get("justified") else "NOT justified",
                d.get("reason", ""),
            )
            for d in data["dependencies_changed"]
        ]
        out.append("")
    out += ["## Tests written", ""]
    if data.get("tests"):
        for t in data["tests"]:
            out.append(
                "- `%s` covers %s: %s"
                % (t.get("path"), ", ".join(t.get("covers", [])) or "-", t.get("purpose", ""))
            )
    else:
        out.append("- none")
    out.append("")
    out += ["## AC coverage", ""]
    covered = {}
    for t in data.get("tests", []):
        for ac in t.get("covers", []):
            covered.setdefault(ac, []).append(t.get("path"))
    nt = {n.get("ac"): n.get("reason") for n in data.get("not_testable", []) if isinstance(n, dict)}
    for ac in ac_ids:
        if ac in covered:
            out.append("- %s: %s" % (ac, ", ".join("`%s`" % p for p in covered[ac])))
        elif ac in nt:
            out.append("- %s: not testable (%s)" % (ac, nt[ac]))
        else:
            out.append("- %s: **uncovered**" % ac)
    out.append("")
    if data.get("suggestions"):
        out += (
            ["## Follow-up suggestions (not required)", ""]
            + ["- " + str(s) for s in data["suggestions"]]
            + [""]
        )
    return "\n".join(out)


def render_review_summary(
    data: dict,
    verdict: str,
    round_no: int,
    model: str,
    cost: float,
    ac_ids: List[str],
    state_dir: str,
    model_note: str = "",
) -> str:
    """The PR comment for a non-private repository: verdict, finding locations, coverage.
    No finding text, suggestions, questions, or scope notes; those stay in review-<n>.md.
    `model_note` follows the model when the user picked it (`run --reviewer-model`)."""
    out = [
        "<!-- generated by revali; summary only (non-private repository) -->",
        "",
        "# Review round %d: %s" % (round_no, verdict),
        "",
        "model: %s%s, cost: $%.2f"
        % (model or "?", " (%s)" % model_note if model_note else "", cost),
        "",
    ]
    if data.get("questions"):
        out += ["%d question(s) for the author" % len(data["questions"]), ""]
    out += ["## Findings", ""]
    if data.get("findings"):
        for f in data["findings"]:
            out.append(
                "- %s [%s %s] `%s:%s`"
                % (
                    f.get("id", "?"),
                    f.get("severity"),
                    f.get("kind"),
                    f.get("file", "?"),
                    f.get("line", 0),
                )
            )
    else:
        out.append("- none")
    out.append("")
    if data.get("previous_findings"):
        out += ["## Previous findings", ""]
        out += ["- %s: %s" % (p.get("id"), p.get("status")) for p in data["previous_findings"]]
        out.append("")
    counts = []
    if data.get("scope_mismatch"):
        counts.append("%d scope note(s)" % len(data["scope_mismatch"]))
    if data.get("test_changes"):
        bad = [t for t in data["test_changes"] if not t.get("justified")]
        counts.append(
            "%d existing test file(s) changed, %d not justified"
            % (len(data["test_changes"]), len(bad))
        )
    if data.get("dependencies_changed"):
        counts.append("%d dependency change(s)" % len(data["dependencies_changed"]))
    if counts:
        out += ["; ".join(counts), ""]
    out += ["## Tests written", ""]
    if data.get("tests"):
        out += [
            "- `%s` covers %s" % (t.get("path"), ", ".join(t.get("covers", [])) or "-")
            for t in data["tests"]
        ]
    else:
        out.append("- none")
    out.append("")
    covered = set()
    for t in data.get("tests", []):
        covered.update(t.get("covers", []))
    nt = {n.get("ac") for n in data.get("not_testable", []) if isinstance(n, dict)}
    out += ["## AC coverage", ""]
    for ac in ac_ids:
        out.append(
            "- %s: %s"
            % (ac, "covered" if ac in covered else "not testable" if ac in nt else "**uncovered**")
        )
    out += [
        "",
        "Full text: `%s/<branch>/review-%d.md` on the author's machine." % (state_dir, round_no),
        "",
    ]
    return "\n".join(out)


def render_tests_md(ctx: Context, state: State, rounds: List[dict]) -> str:
    """tests.md (file 3): purpose / covers / expected per test, AC table, validation
    sections appended later."""
    out = [
        "<!-- generated by revali; validation results are appended below -->",
        "",
        "# Tests for %s" % ctx.doc.title,
        "",
        "Branch `%s`, kind `%s`." % (ctx.branch, ctx.doc.kind),
        "",
    ]
    latest = rounds[-1]["data"] if rounds else {}
    out += ["## Test files", ""]
    if latest.get("tests"):
        for t in latest["tests"]:
            out += [
                "### `%s`" % t.get("path"),
                "",
                "- purpose: %s" % t.get("purpose", ""),
                "- covers: %s" % (", ".join(t.get("covers", [])) or "-"),
                "- expected: %s" % t.get("expected", ""),
                "",
            ]
    else:
        out += ["- none", ""]
    out += ["## Acceptance criteria", ""]
    covered = {}
    for t in latest.get("tests", []):
        for ac in t.get("covers", []):
            covered.setdefault(ac, []).append(t.get("path"))
    nt = {
        n.get("ac"): n.get("reason") for n in latest.get("not_testable", []) if isinstance(n, dict)
    }
    for ac_id, text in ctx.doc.acs:
        if ac_id in covered:
            status = ", ".join("`%s`" % p for p in covered[ac_id])
        elif ac_id in nt:
            status = "not testable: %s" % nt[ac_id]
        else:
            status = "**uncovered**"
        out.append("- %s: %s -> %s" % (ac_id, text, status))
    out.append("")
    return "\n".join(out)


# ---- the round --------------------------------------------------------------


def run_round(
    ctx: Context, state: State, rdir: str, log: Optional[RunLog], baseline=None
) -> RoundOutcome:
    """One review round. Whatever stops it before the tests are committed leaves no
    half-written test files behind. `baseline` is the validate.BaselineJob still running
    alongside round 1, if any: a failure stops the reviewer, and the reviewer's answer is
    acted on only once the baseline has passed."""
    try:
        return _run_round(ctx, state, rdir, log, baseline)
    except Stop:
        discard_round_leftovers(ctx, state, log, "the reviewer", stage="review")
        state.reviewer_running = False
        state.save(rdir)
        raise


def _spent(state: State, rdir: str, cost: float) -> None:
    if cost:
        state.cost_usd += cost
        state.save(rdir)


def _run_round(
    ctx: Context, state: State, rdir: str, log: Optional[RunLog], baseline=None
) -> RoundOutcome:
    from revali import testarchive

    round_no = len(state.rounds) + 1
    needs_info_allowed = not state.needs_info_used
    bounce_notes = ""
    bounces = 0
    attempt = 0
    total_cost = 0.0
    archive = testarchive.archive_mode(ctx, state)
    if archive:
        # the copies placed below are leftovers from here on, should the round not take them back
        state.reviewer_running = True
        state.save(rdir)
        testarchive.place_back(ctx, state, rdir, log)
    while True:
        attempt += 1
        prompt = build_prompt(ctx, state, rdir, round_no, bounce_notes)
        write_text(os.path.join(ctx.logs, "prompt-r%d-%d.md" % (round_no, attempt)), prompt)
        check_tree_unmoved(ctx)
        state.reviewer_running = (
            True  # cleared when this round ends; a later run cleans up otherwise
        )
        state.save(rdir)
        if baseline is not None:
            baseline.raise_if_failed()  # failed already: no reviewer is paid for this branch
        try:
            rr = spawn_reviewer(
                ctx,
                prompt,
                rdir,
                round_no,
                attempt,
                log,
                cancel=baseline.failed if baseline is not None else None,
            )
        except Stop as stop:
            _spent(state, rdir, stop.cost)
            raise
        # counted now: whatever stops this round from here on, the session was paid for
        _spent(state, rdir, rr.cost)
        total_cost += rr.cost
        if baseline is not None:
            baseline.settle(
                state,
                rdir,
                log,
                note="the reviewer's answer was discarded ($%.2f counted in the run's cost)"
                % rr.cost,
            )
        if rr.denials and log:
            log.stage(
                "review", "note: %d tool call(s) were denied by the allowlist" % len(rr.denials)
            )
        offenders = guard_worktree(ctx, log)
        if offenders:
            raise Stop(
                EXIT_ERROR,
                "the reviewer modified files outside %s (reverted): %s"
                % (ctx.cfg.project.test_dir, ", ".join(offenders)),
            )
        restored = restore_protected_tests(ctx, state, log)
        files = new_test_files(ctx)
        gaps = ac_gaps(rr.data, ctx.doc.ac_ids)
        # format and lint first (cheap, local, also for a NEEDS_INFO round whose files stay
        # uncommitted and would otherwise fail the next run's preflight), then the smoke run
        format_files(ctx, files, log)
        lint_problem = lint_check(ctx, files, log)
        smoke_problem = None
        if files and ctx.doc.kind in ("feature", "fix") and rr.data.get("verdict") != NEEDS_INFO:
            smoke_problem = smoke_run(ctx, files, rdir, round_no, attempt, log)
        problems = []
        if restored:
            problems.append(
                "You modified or deleted existing test file(s) that are not yours; they were "
                "restored: %s. Do not touch them. Write your tests into new files under a "
                "different topic; names already taken: %s."
                % (", ".join(restored), ", ".join(existing_test_names(ctx, state)) or "(none)")
            )
        if gaps and rr.data.get("verdict") != NEEDS_INFO:
            problems.append(
                "These acceptance criteria are neither covered by a test nor listed in "
                "`not_testable` with a reason: %s. Cover them or explain." % ", ".join(gaps)
            )
        if lint_problem:
            problems.append("Fix the test files so they pass lint: " + lint_problem)
        if smoke_problem:
            problems.append("Fix the test files so they run: " + smoke_problem)
        if problems and bounces == 0:
            bounces = 1
            bounce_notes = "\n\n".join(problems)
            if log:
                log.stage(
                    "review",
                    "sending the reviewer back once: %s"
                    % "; ".join(p.split("\n")[0][:80] for p in problems),
                )
            continue
        break

    if restored:
        raise Stop(
            EXIT_ERROR,
            "the reviewer modified existing test file(s) it did not write on the last "
            "attempt of round %d; restored, no tests committed: %s"
            % (round_no, ", ".join(restored)),
        )
    if lint_problem:
        raise Stop(
            EXIT_ERROR, "the reviewer's tests still fail lint after a retry; " + lint_problem
        )
    if smoke_problem:
        raise Stop(
            EXIT_ERROR, "the reviewer's tests still cannot run after a retry; " + smoke_problem
        )
    asked = rr.data.get("verdict") == NEEDS_INFO
    verdict, reasons = compute_verdict(rr.data, [] if asked else gaps, needs_info_allowed)

    reviewed_head = (
        ctx.head_sha
    )  # what the reviewer saw; ctx.head_sha moves on with the run's commits
    commit_sha = ""
    if archive:
        # every verdict, NEEDS_INFO included: the archive is where the next round finds them
        testarchive.take(ctx, state, rdir, files, log)
    elif files and verdict != NEEDS_INFO:
        check_tree_unmoved(ctx)
        state.pending_effect = "commit-tests"
        state.save(rdir)
        commit_sha = commit_tests(ctx, files, round_no, log)
        ctx.head_sha = commit_sha  # the run's own commit: what the later checks expect
        state.pending_effect = ""
        state.test_commits.append(commit_sha)
        for f in files:
            if f not in state.test_files:
                state.test_files.append(f)
    # NEEDS_INFO keeps its files uncommitted for the next round to update; the state remembers
    # them so preflight tolerates exactly those. Any other verdict has committed or lost them.
    state.pending_test_files = list(files) if verdict == NEEDS_INFO and not archive else []
    state.tests_mode = testarchive.ARCHIVE_MODE if archive else "commit"

    meta = {
        "tool": "revali",
        "round": round_no,
        "model_requested": rr.model_requested,
        "model_reason": rr.model_reason or "explicit",
        "model_actual": rr.model_actual,
        "fallback": rr.fallback,
        "prompt_version": state.prompt_version,
        "cost_usd": "%.4f" % total_cost,
        "duration_s": rr.duration_ms // 1000,
        "at": now_iso(),
    }
    review_md = render_review_md(rr.data, verdict, reasons, meta, ctx.doc.ac_ids)
    review_path = os.path.join(rdir, "review-%d.md" % round_no)
    write_text(review_path, review_md)
    write_json_atomic(
        os.path.join(rdir, "review-%d.json" % round_no),
        {
            "meta": meta,
            "verdict": verdict,
            "reasons": reasons,
            "data": rr.data,
            "test_files": files,
            "head_sha": reviewed_head,
            "commit": commit_sha,
            "bounces": bounces,
        },
    )
    record = {
        "round": round_no,
        "head_sha": reviewed_head,
        "base_sha": ctx.base_sha,
        "verdict": verdict,
        "reviewer_verdict": rr.data.get("verdict"),
        "model": rr.model_actual,
        "fallback": rr.fallback,
        "cost_usd": total_cost,
        "test_commit": commit_sha,
        "data": rr.data,
        "at": now_iso(),
    }
    state.rounds.append(record)  # its cost is in state.cost_usd already, attempt by attempt
    if rr.model_actual and rr.model_actual not in state.models_used:
        state.models_used.append(rr.model_actual)
    state.fallback = state.fallback or rr.fallback
    state.last_verdict = verdict
    if verdict == NEEDS_INFO:
        state.needs_info_used = True
    write_text(os.path.join(rdir, "tests.md"), render_tests_md(ctx, state, state.rounds))
    state.reviewer_running = False
    if verdict == CHANGES_REQUESTED:
        # owed from the moment the verdict is saved: a run that stops before its exit-2
        # summary (the test commit's push, the PR comment) must not let the next run review
        # an unchanged branch again
        state.fix_due = True
    if commit_sha:
        state.head_sha = commit_sha  # the run's own commit, never taken for the author's fix
    state.save(rdir)
    if log:
        log.stage(
            "review",
            "round %d verdict %s (reviewer said %s; model %s%s; $%.2f)"
            % (
                round_no,
                verdict,
                rr.data.get("verdict"),
                rr.model_actual,
                ", FALLBACK" if rr.fallback else "",
                total_cost,
            ),
        )
    return RoundOutcome(
        round_no=round_no,
        verdict=verdict,
        reasons=reasons,
        data=rr.data,
        test_files=files,
        commit_sha=commit_sha,
        review_md=review_md,
        review_path=review_path,
        model_actual=rr.model_actual,
        model_reason=rr.model_reason,
        cost=total_cost,
        fallback=rr.fallback,
        bounces=bounces,
        gaps=gaps,
    )
