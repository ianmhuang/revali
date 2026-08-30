"""Validate stage: baseline before review, full run after approval, diagnoser on failure."""
import os
import string
from dataclasses import dataclass, field
from typing import List, Optional

from revali import EXIT_ERROR
from revali import claude
from revali.config import PlatformCfg
from revali.preflight import Context, Stop
from revali.runners import RunReport, RunnerError, get_runner, steps_for, tail
from revali.state import RunLog, State, now_iso, read_text, write_text

TOOL_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROMPT_PATH = os.path.join(TOOL_ROOT, "prompts", "diagnose.md")
SCHEMA_PATH = os.path.join(TOOL_ROOT, "schemas", "diagnose.schema.json")
PASS, FAIL = "PASS", "FAIL"
LOG_LINES = 200
DIAGNOSER_TOOLS = "Read,Grep,Glob"


@dataclass
class ValidationOutcome:
    number: int
    result: str
    report: Optional[RunReport] = None
    failed_step: str = ""
    diagnosis: Optional[dict] = None
    diagnosis_error: str = ""
    model_actual: str = ""
    fallback: bool = False
    cost: float = 0.0
    section_md: str = ""
    skipped_reason: str = ""


def platform(ctx: Context) -> PlatformCfg:
    return ctx.cfg.validate.platforms[ctx.cfg.project.platforms[0]]


def _runner(ctx: Context):
    try:
        return get_runner(platform(ctx))
    except RunnerError as exc:
        raise Stop(EXIT_ERROR, str(exc))


def baseline(ctx: Context, rdir: str, log: Optional[RunLog]) -> None:
    """Existing suite must pass on the branch before anyone reviews it."""
    if ctx.doc.kind == "docs":
        return
    plat = platform(ctx)
    steps = [s for s in steps_for(plat, ["setup", "build", "test"]) if s[1].strip()]
    if not any(name == "test" for name, _ in steps):
        if log:
            log.stage("preflight", "no existing test command; baseline skipped")
        return
    runner = _runner(ctx)
    if log:
        log.stage("preflight", "baseline: existing suite on %s" % runner.name)
    try:
        report = runner.run(ctx.repo_root, "HEAD", steps, {}, os.path.join(rdir, "logs"), "baseline",
                            log.detail if log else None)
    except RunnerError as exc:
        raise Stop(EXIT_ERROR, "sandbox failed during baseline: %s" % exc)
    failed = report.failed
    if failed is not None:
        raise Stop(EXIT_ERROR, "branch is broken before review: %s failed (exit %d%s); see %s\n%s"
                   % (failed.name, failed.returncode, ", timed out" if failed.timed_out else "",
                      failed.log_path, tail(failed.text, 20)))


def run_validation(ctx: Context, state: State, rdir: str, log: Optional[RunLog]) -> ValidationOutcome:
    number = len(state.validations) + 1
    outcome = ValidationOutcome(number=number, result=PASS)
    if ctx.doc.kind == "docs":
        outcome.skipped_reason = "kind docs: nothing to run"
    else:
        plat = platform(ctx)
        steps = [s for s in steps_for(plat, ["setup", "build", "test", "new_test"]) if s[1].strip()]
        runner = _runner(ctx)
        label = "validate-r%d" % max(1, len(state.rounds))
        if log:
            log.stage("validate", "run %d: %s on %s (%s)" % (number, ", ".join(n for n, _ in steps), runner.name, label))
        try:
            report = runner.run(ctx.repo_root, "HEAD", steps, {}, os.path.join(rdir, "logs"), label,
                                log.detail if log else None)
        except RunnerError as exc:
            raise Stop(EXIT_ERROR, "sandbox failed: %s" % exc)
        outcome.report = report
        failed = report.failed
        if failed is not None:
            if failed.name in ("setup", "build"):
                raise Stop(EXIT_ERROR, "sandbox %s step failed (exit %d); this is an environment problem, "
                                       "not a verdict; see %s\n%s"
                           % (failed.name, failed.returncode, failed.log_path, tail(failed.text, 20)))
            outcome.result = FAIL
            outcome.failed_step = failed.name
            if not ctx.dry_run:
                _diagnose(ctx, state, rdir, failed, outcome, log)
    outcome.section_md = render_section(ctx, outcome)
    path = os.path.join(rdir, "tests.md")
    existing = read_text(path) if os.path.isfile(path) else "# Tests\n"
    write_text(path, existing.rstrip("\n") + "\n\n" + outcome.section_md)
    state.validations.append({
        "number": number, "result": outcome.result, "failed_step": outcome.failed_step,
        "round": len(state.rounds), "head_sha": ctx.head_sha, "cause": (outcome.diagnosis or {}).get("cause", ""),
        "model": outcome.model_actual, "fallback": outcome.fallback, "cost_usd": outcome.cost, "at": now_iso(),
    })
    state.cost_usd += outcome.cost
    if outcome.model_actual and outcome.model_actual not in state.models_used:
        state.models_used.append(outcome.model_actual)
    state.last_verdict = outcome.result
    state.save(rdir)
    if log:
        log.stage("validate", "run %d: %s%s" % (number, outcome.result,
                                                 " (%s)" % outcome.failed_step if outcome.failed_step else ""))
    return outcome


def _diagnose(ctx: Context, state: State, rdir: str, failed, outcome: ValidationOutcome, log: Optional[RunLog]) -> None:
    cfg = ctx.cfg.validate
    model = cfg.model
    tests_md_path = os.path.join(rdir, "tests.md")
    values = {
        "branch": ctx.branch, "base": ctx.base, "kind": ctx.doc.kind,
        "failed_step": failed.name, "failed_cmd": failed.cmd, "failed_exit": failed.returncode,
        "timed_out_note": " (timed out)" if failed.timed_out else "",
        "change_md": ctx.doc.raw.strip(),
        "tests_md": read_text(tests_md_path).strip() if os.path.isfile(tests_md_path) else "(none)",
        "test_files": "\n".join("- " + p for p in state.test_files) or "(none)",
        "log_lines": LOG_LINES, "log_tail": tail(failed.text, LOG_LINES) or "(empty)",
    }
    prompt = string.Template(read_text(PROMPT_PATH)).safe_substitute(values)
    write_text(os.path.join(rdir, "logs", "prompt-diagnose-%d.md" % outcome.number), prompt)
    if log:
        log.stage("validate", "diagnoser %s (budget $%.2f)" % (model, cfg.budget_usd))
    try:
        result = claude.invoke(
            role="diagnoser", model=model, fallback_model=cfg.fallback_model, effort=cfg.effort,
            schema_text=read_text(SCHEMA_PATH), budget_usd=cfg.budget_usd,
            extra_args=["--tools", DIAGNOSER_TOOLS], prompt=prompt, cwd=ctx.repo_root,
            timeout_s=ctx.cfg.review.timeout_min * 60,
            raw_path=os.path.join(rdir, "logs", "diagnose-%d.raw.json" % outcome.number), log=log)
    except Stop as stop:
        outcome.diagnosis_error = stop.message
        if log:
            log.stage("validate", "diagnosis unavailable: %s" % stop.message)
        return
    data = result.data
    if not data.get("summary") or data.get("cause") not in ("code", "test", "env", "unknown"):
        outcome.diagnosis_error = "diagnoser output did not match the schema"
    else:
        outcome.diagnosis = data
    outcome.model_actual = result.model_actual
    outcome.fallback = result.fallback
    outcome.cost = result.cost
    write_text(os.path.join(rdir, "diagnose-%d.json" % outcome.number),
               __import__("json").dumps({"meta": {"model_requested": model, "model_actual": result.model_actual,
                                                  "fallback": result.fallback, "cost_usd": result.cost,
                                                  "at": now_iso()}, "data": data}, indent=2, ensure_ascii=False) + "\n")


def render_section(ctx: Context, o: ValidationOutcome) -> str:
    out = ["## Validation %d: %s" % (o.number, o.result), "", "at: %s" % now_iso()]
    if o.skipped_reason:
        out += ["", o.skipped_reason, ""]
        return "\n".join(out)
    if o.report:
        out += ["", "| step | exit | log |", "|---|---|---|"]
        for s in o.report.steps:
            out.append("| %s | %s%s | `%s` |" % (s.name, s.returncode, " (timed out)" if s.timed_out else "",
                                                os.path.basename(s.log_path or "")))
    if o.result == FAIL and o.report and o.report.failed:
        out += ["", "### Failure output (%s, last 40 lines)" % o.failed_step, "", "```",
                tail(o.report.failed.text, 40) or "(empty)", "```"]
    if o.diagnosis:
        d = o.diagnosis
        out += ["", "### Diagnosis (%s%s)" % (o.model_actual, ", fallback" if o.fallback else ""), "",
                "cause: **%s**" % d.get("cause"), "", d.get("summary", "").strip(), ""]
        for f in d.get("failures", []):
            out.append("- `%s`: %s. %s" % (f.get("test"), f.get("cause"), f.get("note", "")))
        out += ["", "recommendation: %s" % d.get("recommendation", "")]
    elif o.diagnosis_error:
        out += ["", "diagnosis unavailable: %s" % o.diagnosis_error]
    out.append("")
    return "\n".join(out)


def summary_for_author(o: ValidationOutcome, rdir: str) -> str:
    lines = ["validation %d FAILED at step %s" % (o.number, o.failed_step)]
    if o.diagnosis:
        lines.append("cause: %s. %s" % (o.diagnosis.get("cause"), o.diagnosis.get("summary", "")))
        lines.append("recommendation: %s" % o.diagnosis.get("recommendation", ""))
    elif o.diagnosis_error:
        lines.append("diagnosis unavailable: %s" % o.diagnosis_error)
    if o.report and o.report.failed:
        lines.append("log: %s" % o.report.failed.log_path)
    lines.append("details: %s" % os.path.join(rdir, "tests.md"))
    return "\n".join(lines)
