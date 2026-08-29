"""Review stage: build the reviewer prompt, spawn `claude -p`, judge its output,
guard the working tree, check AC coverage, smoke-run the new tests, commit them.
"""
import json
import os
import string
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from revali import EXIT_ERROR
from revali import claude, gitops
from revali.claude import model_family, pick_actual_model  # noqa: F401  (re-exported)
from revali.preflight import Context, Stop
from revali.procs import resolve, run
from revali.runners import RunnerError, get_runner, steps_for
from revali.state import State, RunLog, now_iso, read_text, write_json_atomic, write_text

TOOL_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROMPT_PATH = os.path.join(TOOL_ROOT, "prompts", "review.md")
SCHEMA_PATH = os.path.join(TOOL_ROOT, "schemas", "review.schema.json")
BUILTIN_CHECKLIST = os.path.join(TOOL_ROOT, "checklists", "default.md")

APPROVE, CHANGES_REQUESTED, NEEDS_INFO = "APPROVE", "CHANGES_REQUESTED", "NEEDS_INFO"
MANIFEST_PATTERNS = [
    "requirements*.txt", "pyproject.toml", "setup.py", "setup.cfg", "Pipfile", "Pipfile.lock",
    "poetry.lock", "package.json", "package-lock.json", "yarn.lock", "pnpm-lock.yaml",
    "Cargo.toml", "Cargo.lock", "go.mod", "go.sum", "CMakeLists.txt", ".gitmodules",
    "conanfile.*", "vcpkg.json", "Gemfile", "Gemfile.lock",
]
ALLOWED_TOOLS = "Bash(git diff *) Bash(git log *) Bash(git show *)"
LIST_FIELDS = ("questions", "findings", "previous_findings", "scope_mismatch", "dependencies_changed",
               "test_changes", "tests", "not_testable", "suggestions")


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
    cost: float = 0.0
    fallback: bool = False
    bounces: int = 0
    gaps: List[str] = field(default_factory=list)


# ---- inputs -----------------------------------------------------------------

def assemble_checklist(ctx: Context) -> str:
    parts = []
    if os.path.isfile(BUILTIN_CHECKLIST):
        parts.append("### Built-in\n\n" + read_text(BUILTIN_CHECKLIST).strip())
    user_path = ctx.user_cfg.checklist if ctx.user_cfg else ""
    if user_path and os.path.isfile(user_path):
        parts.append("### User\n\n" + read_text(user_path).strip())
    proj = ctx.cfg.review.checklist
    if proj:
        path = proj if os.path.isabs(proj) else os.path.join(ctx.repo_root, proj)
        if os.path.isfile(path):
            parts.append("### Project (overrides the layers above on conflict)\n\n" + read_text(path).strip())
    return "\n\n".join(parts) if parts else "(no checklist configured)"


def test_pattern_glob(ctx: Context) -> str:
    return ctx.cfg.project.test_file_pattern.replace("{topic}", "*")


def modified_existing_tests(ctx: Context) -> List[str]:
    """Existing test files (present on base) that the diff modifies or deletes."""
    root = ctx.repo_root
    res = gitops._git(["diff", "--name-only", "--diff-filter=MD", "%s...HEAD" % ctx.base_ref, "--",
                       ctx.cfg.project.test_dir], root)
    pattern = test_pattern_glob(ctx)
    return [p.strip() for p in res.stdout.splitlines()
            if p.strip() and not gitops.matches_any(p.strip(), [pattern])]


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
    lines = ["# Previous round (%d)" % (round_no - 1), "",
             "Verdict: %s. Summary: %s" % (data.get("verdict", "?"), data.get("summary", "")), "",
             "Findings from that round (mark each in `previous_findings` as resolved, unresolved,",
             "or accepted when the author's response convinced you; raise new findings only for",
             "what the fix introduced):", ""]
    for f in data.get("findings", []):
        lines.append("- %s [%s %s] %s:%s %s" % (f.get("id", "?"), f.get("severity", "?"), f.get("kind", "?"),
                                                f.get("file", "?"), f.get("line", 0), f.get("text", "")))
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
    return ("Test files you wrote in earlier rounds (update them; do not create duplicates):\n"
            + "\n".join("- " + p for p in state.test_files) + "\n")


def _bounce_section(notes: str) -> str:
    if not notes:
        return ""
    return "# Corrections required before this review is accepted\n\n%s\n" % notes.strip()


def build_prompt(ctx: Context, state: State, rdir: str, round_no: int, bounce_notes: str = "") -> str:
    cfg = ctx.cfg
    exclude = list(cfg.review.exclude) + [test_pattern_glob(ctx)]
    diff = gitops.diff_text(ctx.base_ref, "HEAD", ctx.repo_root, exclude=exclude)
    kind = ctx.doc.kind
    tests_required = "required for kind %s" % kind if kind in ("feature", "fix") else \
        "not required for kind %s: leave `tests` empty and list every AC in `not_testable` with reason 'kind %s'" % (kind, kind)
    guide = ""
    if cfg.project.test_guide:
        gpath = os.path.join(ctx.repo_root, cfg.project.test_guide)
        if os.path.isfile(gpath):
            guide = "\nHow tests are added in this project (from %s):\n\n%s\n" % (cfg.project.test_guide, read_text(gpath).strip())
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
        "modified_tests": ", ".join(modified_existing_tests(ctx)) or "(none)",
        "manifests": ", ".join(manifests_touched(ctx)) or "(none)",
        "prior_section": _prior_findings_section(rdir, round_no),
        "response_section": _response_section(rdir, round_no),
        "bounce_section": _bounce_section(bounce_notes),
        "test_dir": cfg.project.test_dir,
        "tests_required": tests_required,
        "test_guide_section": guide,
        "prior_tests_section": _prior_tests_section(state),
        "checklist": assemble_checklist(ctx),
    }
    template = string.Template(read_text(PROMPT_PATH))
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
        if not isinstance(f, dict) or f.get("severity") not in ("high", "medium", "low") \
                or f.get("kind") not in ("correctness", "convention", "security"):
            problems.append("finding with invalid severity/kind: %r" % (f,))
    for t in data.get("tests", []) if isinstance(data.get("tests"), list) else []:
        if not isinstance(t, dict) or not t.get("path") or not isinstance(t.get("covers"), list):
            problems.append("test entry without path/covers: %r" % (t,))
    return problems


def spawn_reviewer(ctx: Context, prompt: str, rdir: str, round_no: int, attempt: int,
                   log: Optional[RunLog]) -> ReviewerRun:
    cfg = ctx.cfg.review
    requested = (ctx.user_cfg.review_model if ctx.user_cfg and ctx.user_cfg.review_model else cfg.model)
    raw_path = os.path.join(rdir, "logs", "review-r%d-%d.raw.json" % (round_no, attempt))
    if log:
        log.stage("review", "round %d attempt %d: reviewer %s (budget $%.2f, timeout %d min)"
                  % (round_no, attempt, requested, cfg.budget_usd, cfg.timeout_min))
    result = claude.invoke(
        role="reviewer", model=requested, fallback_model=cfg.fallback_model, effort=cfg.effort,
        schema_text=read_text(SCHEMA_PATH), budget_usd=cfg.budget_usd,
        extra_args=["--permission-mode", "acceptEdits", "--allowedTools", ALLOWED_TOOLS],
        prompt=prompt, cwd=ctx.repo_root, timeout_s=cfg.timeout_min * 60, raw_path=raw_path, log=log)
    problems = validate_shape(result.data)
    if problems:
        raise Stop(EXIT_ERROR, "reviewer output does not match the schema: %s; raw saved to %s"
                   % ("; ".join(problems[:5]), raw_path))
    return ReviewerRun(
        data=result.data, raw=result.raw, model_requested=requested, model_actual=result.model_actual,
        fallback=result.fallback, cost=result.cost, denials=result.denials, duration_ms=result.duration_ms,
    )


# ---- checks after the reviewer --------------------------------------------

def _under_test_dir(path: str, test_dir: str) -> bool:
    p = path.replace("\\", "/").rstrip("/")
    d = test_dir.replace("\\", "/").rstrip("/") + "/"
    return p.startswith(d)


def guard_worktree(ctx: Context, log: Optional[RunLog]) -> List[str]:
    """Revert anything the reviewer touched outside test_dir. Returns the offending paths."""
    root = ctx.repo_root
    offenders = []
    for entry in gitops.dirty_paths(root):
        code, path = entry.split(" ", 1)
        if _under_test_dir(path, ctx.cfg.project.test_dir):
            continue
        offenders.append(path)
        if code == "??":
            full = os.path.join(root, path)
            if os.path.isdir(full):
                import shutil
                shutil.rmtree(full, ignore_errors=True)
            elif os.path.exists(full):
                os.remove(full)
        else:
            gitops._git(["checkout", "--", path], root)
    if offenders and log:
        log.stage("review", "reviewer touched files outside %s; reverted: %s"
                  % (ctx.cfg.project.test_dir, ", ".join(offenders)))
    return offenders


def new_test_files(ctx: Context) -> List[str]:
    files = []
    for entry in gitops.dirty_paths(ctx.repo_root):
        _, path = entry.split(" ", 1)
        if _under_test_dir(path, ctx.cfg.project.test_dir):
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


def compute_verdict(data: dict, gaps: List[str], needs_info_allowed: bool) -> Tuple[str, List[str]]:
    reasons = []
    for f in data.get("findings", []):
        sev, kind = f.get("severity"), f.get("kind")
        blocking = (kind in ("correctness", "security") and sev in ("high", "medium")) or \
                   (kind == "convention" and sev == "high")
        if blocking:
            reasons.append("%s [%s %s] %s:%s %s" % (f.get("id", "F?"), sev, kind, f.get("file", "?"),
                                                    f.get("line", 0), f.get("text", "")))
    for tc in data.get("test_changes", []):
        if not tc.get("justified"):
            reasons.append("existing test %s changed without justification: %s"
                           % (tc.get("file", "?"), tc.get("reason", "")))
    for dep in data.get("dependencies_changed", []):
        if not dep.get("justified"):
            reasons.append("dependency change in %s not justified: %s" % (dep.get("file", "?"), dep.get("reason", "")))
    if gaps:
        reasons.append("acceptance criteria without a test or a not_testable reason: %s" % ", ".join(gaps))
    questions = [q for q in data.get("questions", []) if str(q).strip()]
    if data.get("verdict") == NEEDS_INFO and questions:
        if needs_info_allowed and not reasons:
            return NEEDS_INFO, ["question: " + str(q) for q in questions]
        reasons.append("reviewer still has unanswered questions: " + "; ".join(str(q) for q in questions))
    if reasons:
        return CHANGES_REQUESTED, reasons
    if data.get("verdict") == CHANGES_REQUESTED:
        return CHANGES_REQUESTED, ["reviewer requested changes: " + data.get("summary", "")]
    return APPROVE, []


def smoke_run(ctx: Context, test_files: List[str], rdir: str, round_no: int, attempt: int,
              log: Optional[RunLog]) -> Optional[str]:
    """Run the new tests once in the sandbox. None = they ran (pass or fail);
    a string = they could not run (bounce to the reviewer)."""
    plat = ctx.cfg.validate.platforms[ctx.cfg.project.platforms[0]]
    try:
        runner = get_runner(plat)
    except RunnerError as exc:
        raise Stop(EXIT_ERROR, str(exc))
    extra = {rel: read_text(os.path.join(ctx.repo_root, rel)) for rel in test_files
             if os.path.isfile(os.path.join(ctx.repo_root, rel))}
    label = "smoke-r%d-%d" % (round_no, attempt)
    if log:
        log.stage("review", "smoke run of %d new test file(s) on %s" % (len(extra), runner.name))
    try:
        report = runner.run(ctx.repo_root, "HEAD", steps_for(plat, ["setup", "build", "new_test"]), extra,
                            os.path.join(rdir, "logs"), label, log.detail if log else None)
    except RunnerError as exc:
        raise Stop(EXIT_ERROR, "sandbox failed: %s" % exc)
    failed = report.failed
    if failed is None:
        return None
    if failed.name != "new_test":
        raise Stop(EXIT_ERROR, "sandbox %s step failed (exit %d); see %s" % (failed.name, failed.returncode, failed.log_path))
    if failed.returncode == 1 and not failed.timed_out:
        return None  # tests ran and some failed: that is validation's business
    tail = "\n".join(failed.text.strip().splitlines()[-30:])
    return ("the new tests could not run (exit %d%s) with `%s`:\n%s"
            % (failed.returncode, ", timed out" if failed.timed_out else "", failed.cmd, tail))


def commit_tests(ctx: Context, files: List[str], round_no: int, log: Optional[RunLog]) -> str:
    root = ctx.repo_root
    gitops.git_ok(["add", "--"] + files, root)
    message = ("test: review tests (round %d)\n\nWritten by the revali reviewer from the acceptance criteria.\n\n"
               "Co-Authored-By: Claude <noreply@anthropic.com>\nRevali-Round: %d\n" % (round_no, round_no))
    res = run(resolve("git") + ["commit", "--quiet", "-F", "-"], cwd=root, input_text=message,
              log=log.detail if log else None)
    if not res.ok:
        raise Stop(EXIT_ERROR, "could not commit the reviewer's tests: %s" % res.text.strip())
    sha = gitops.rev_parse("HEAD", root) or ""
    if log:
        log.stage("review", "committed %d test file(s) as %s" % (len(files), sha[:10]))
    return sha


# ---- rendering --------------------------------------------------------------

def _header(meta: dict) -> str:
    lines = ["<!-- generated by revali -->"]
    for key in ("tool", "round", "model_requested", "model_actual", "fallback", "prompt_version",
                "cost_usd", "duration_s", "at"):
        if key in meta:
            lines.append("%s: %s" % (key, meta[key]))
    return "\n".join(lines) + "\n"


def render_review_md(data: dict, verdict: str, reasons: List[str], meta: dict, ac_ids: List[str]) -> str:
    out = [_header(meta), "", "# Review round %s: %s" % (meta.get("round", "?"), verdict), "",
           data.get("summary", "").strip(), ""]
    if verdict != data.get("verdict"):
        out.append("_Reviewer said %s; the verdict above follows the severity rules._\n" % data.get("verdict"))
    if reasons:
        out += ["## Blocking", ""] + ["- " + r for r in reasons] + [""]
    if data.get("questions"):
        out += ["## Questions for the author", ""] + ["- " + str(q) for q in data["questions"]] + [""]
    out += ["## Findings", ""]
    if data.get("findings"):
        for f in data["findings"]:
            out.append("- **%s** [%s %s] `%s:%s` %s" % (f.get("id", "?"), f.get("severity"), f.get("kind"),
                                                       f.get("file", "?"), f.get("line", 0), f.get("text", "")))
            if f.get("suggestion"):
                out.append("  - suggestion: %s" % f["suggestion"])
    else:
        out.append("- none")
    out.append("")
    if data.get("previous_findings"):
        out += ["## Previous findings", ""]
        out += ["- %s: %s %s" % (p.get("id"), p.get("status"), p.get("note", "")) for p in data["previous_findings"]]
        out.append("")
    if data.get("scope_mismatch"):
        out += ["## Scope mismatch", ""] + ["- " + str(s) for s in data["scope_mismatch"]] + [""]
    if data.get("test_changes"):
        out += ["## Changes to existing tests", ""]
        out += ["- `%s`: %s (%s)" % (t.get("file"), "justified" if t.get("justified") else "NOT justified", t.get("reason", ""))
                for t in data["test_changes"]]
        out.append("")
    if data.get("dependencies_changed"):
        out += ["## Dependency changes", ""]
        out += ["- `%s`: %s (%s)" % (d.get("file"), "justified" if d.get("justified") else "NOT justified", d.get("reason", ""))
                for d in data["dependencies_changed"]]
        out.append("")
    out += ["## Tests written", ""]
    if data.get("tests"):
        for t in data["tests"]:
            out.append("- `%s` covers %s: %s" % (t.get("path"), ", ".join(t.get("covers", [])) or "-", t.get("purpose", "")))
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
        out += ["## Follow-up suggestions (not required)", ""] + ["- " + str(s) for s in data["suggestions"]] + [""]
    return "\n".join(out)


def render_tests_md(ctx: Context, state: State, rounds: List[dict]) -> str:
    """tests.md (file 3): purpose / covers / expected per test, AC table, validation sections appended later."""
    out = ["<!-- generated by revali; validation results are appended below -->", "",
           "# Tests for %s" % ctx.doc.title, "", "Branch `%s`, kind `%s`." % (ctx.branch, ctx.doc.kind), ""]
    latest = rounds[-1]["data"] if rounds else {}
    out += ["## Test files", ""]
    if latest.get("tests"):
        for t in latest["tests"]:
            out += ["### `%s`" % t.get("path"), "",
                    "- purpose: %s" % t.get("purpose", ""),
                    "- covers: %s" % (", ".join(t.get("covers", [])) or "-"),
                    "- expected: %s" % t.get("expected", ""), ""]
    else:
        out += ["- none", ""]
    out += ["## Acceptance criteria", ""]
    covered = {}
    for t in latest.get("tests", []):
        for ac in t.get("covers", []):
            covered.setdefault(ac, []).append(t.get("path"))
    nt = {n.get("ac"): n.get("reason") for n in latest.get("not_testable", []) if isinstance(n, dict)}
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

def run_round(ctx: Context, state: State, rdir: str, log: Optional[RunLog]) -> RoundOutcome:
    round_no = len(state.rounds) + 1
    needs_info_allowed = not state.needs_info_used
    bounce_notes = ""
    bounces = 0
    attempt = 0
    total_cost = 0.0
    while True:
        attempt += 1
        prompt = build_prompt(ctx, state, rdir, round_no, bounce_notes)
        write_text(os.path.join(rdir, "logs", "prompt-r%d-%d.md" % (round_no, attempt)), prompt)
        rr = spawn_reviewer(ctx, prompt, rdir, round_no, attempt, log)
        total_cost += rr.cost
        if rr.denials and log:
            log.stage("review", "note: %d tool call(s) were denied by the allowlist" % len(rr.denials))
        offenders = guard_worktree(ctx, log)
        if offenders:
            raise Stop(EXIT_ERROR, "the reviewer modified files outside %s (reverted): %s"
                       % (ctx.cfg.project.test_dir, ", ".join(offenders)))
        files = new_test_files(ctx)
        gaps = ac_gaps(rr.data, ctx.doc.ac_ids)
        smoke_problem = None
        if files and ctx.doc.kind in ("feature", "fix") and rr.data.get("verdict") != NEEDS_INFO:
            smoke_problem = smoke_run(ctx, files, rdir, round_no, attempt, log)
        problems = []
        if gaps and rr.data.get("verdict") != NEEDS_INFO:
            problems.append("These acceptance criteria are neither covered by a test nor listed in "
                            "`not_testable` with a reason: %s. Cover them or explain." % ", ".join(gaps))
        if smoke_problem:
            problems.append("Fix the test files so they run: " + smoke_problem)
        if problems and bounces == 0:
            bounces = 1
            bounce_notes = "\n\n".join(problems)
            if log:
                log.stage("review", "sending the reviewer back once: %s" % "; ".join(p.split("\n")[0][:80] for p in problems))
            continue
        break

    if smoke_problem:
        raise Stop(EXIT_ERROR, "the reviewer's tests still cannot run after a retry; " + smoke_problem)
    asked = rr.data.get("verdict") == NEEDS_INFO
    verdict, reasons = compute_verdict(rr.data, [] if asked else gaps, needs_info_allowed)

    commit_sha = ""
    if files and verdict != NEEDS_INFO:
        state.pending_effect = "commit-tests"
        state.save(rdir)
        commit_sha = commit_tests(ctx, files, round_no, log)
        state.pending_effect = ""
        state.test_commits.append(commit_sha)
        for f in files:
            if f not in state.test_files:
                state.test_files.append(f)
    elif files:
        # NEEDS_INFO: keep the files uncommitted; the next round updates them.
        pass

    meta = {"tool": "revali", "round": round_no, "model_requested": rr.model_requested,
            "model_actual": rr.model_actual, "fallback": rr.fallback, "prompt_version": state.prompt_version,
            "cost_usd": "%.4f" % total_cost, "duration_s": rr.duration_ms // 1000, "at": now_iso()}
    review_md = render_review_md(rr.data, verdict, reasons, meta, ctx.doc.ac_ids)
    review_path = os.path.join(rdir, "review-%d.md" % round_no)
    write_text(review_path, review_md)
    write_json_atomic(os.path.join(rdir, "review-%d.json" % round_no),
                      {"meta": meta, "verdict": verdict, "reasons": reasons, "data": rr.data,
                       "test_files": files, "commit": commit_sha, "bounces": bounces})
    record = {"round": round_no, "head_sha": ctx.head_sha, "base_sha": ctx.base_sha, "verdict": verdict,
              "reviewer_verdict": rr.data.get("verdict"), "model": rr.model_actual, "fallback": rr.fallback,
              "cost_usd": total_cost, "test_commit": commit_sha, "data": rr.data, "at": now_iso()}
    state.rounds.append(record)
    state.cost_usd += total_cost
    if rr.model_actual and rr.model_actual not in state.models_used:
        state.models_used.append(rr.model_actual)
    state.fallback = state.fallback or rr.fallback
    state.last_verdict = verdict
    if verdict == NEEDS_INFO:
        state.needs_info_used = True
    write_text(os.path.join(rdir, "tests.md"), render_tests_md(ctx, state, state.rounds))
    state.save(rdir)
    if log:
        log.stage("review", "round %d verdict %s (reviewer said %s; model %s%s; $%.2f)"
                  % (round_no, verdict, rr.data.get("verdict"), rr.model_actual,
                     ", FALLBACK" if rr.fallback else "", total_cost))
    return RoundOutcome(round_no=round_no, verdict=verdict, reasons=reasons, data=rr.data, test_files=files,
                        commit_sha=commit_sha, review_md=review_md, review_path=review_path,
                        model_actual=rr.model_actual, cost=total_cost, fallback=rr.fallback,
                        bounces=bounces, gaps=gaps)
