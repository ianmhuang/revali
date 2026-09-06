"""Validate stage: baseline before review, full run after approval, diagnoser on failure."""

import os
import string
import time
from dataclasses import dataclass
from typing import Optional

from revali import EXIT_ERROR, engines, gitops, models
from revali.config import PlatformCfg
from revali.engines import EngineRequest
from revali.issues import IssueRef, maybe_open
from revali.preflight import Context, Stop
from revali.review import TRAILER, under_test_dir
from revali.runners import (
    RunnerError,
    RunReport,
    get_runner,
    steps_for,
    steps_with_files,
    tail,
)
from revali.state import RunLog, State, now_iso, read_text, safe_branch, write_text
from revali.timing import fmt_duration

PASS, FAIL = "PASS", "FAIL"
LOG_LINES = 200
INTRODUCED_BY = ("branch", "base", "unknown")
SUITE_FAILED_REASON = "existing suite failed; base rerun applies to the reviewer's tests only"


@dataclass
class BaseRerun:
    """The reviewer's test files run once more on the base tip after they failed on the
    branch. Evidence for the diagnosis only: `reason` says why it did not run or why what
    ran is unusable (setup or build failed on base, a sandbox error, a timeout)."""

    sha: str = ""
    ran: bool = False  # a sandbox session was started
    report: Optional[RunReport] = None
    reason: str = ""

    @property
    def new_test(self):
        return self.report.step("new_test") if self.report else None

    @property
    def available(self) -> bool:
        step = self.new_test
        return step is not None and not step.timed_out and not self.reason

    def one_line(self) -> str:
        """`on base <sha>: new_test exit N`, or the reason there is no such result."""
        head = "on base %s: " % self.sha[:10]
        if self.available:
            return head + "new_test exit %d" % self.new_test.returncode
        return head + ("unavailable, " if self.ran else "not run, ") + self.reason

    def as_dict(self) -> dict:
        """The `base_rerun` entry of a validation record: `exit` is the base `new_test` exit
        code with an empty `reason` when that result is usable, else None with the reason."""
        return {
            "sha": self.sha,
            "ran": self.ran,
            "exit": self.new_test.returncode if self.available else None,
            "reason": "" if self.available else self.reason,
        }


@dataclass
class ValidationOutcome:
    number: int
    result: str
    report: Optional[RunReport] = None
    failed_step: str = ""
    diagnosis: Optional[dict] = None
    diagnosis_error: str = ""
    model_actual: str = ""
    model_reason: str = ""
    fallback: bool = False
    cost: float = 0.0
    section_md: str = ""
    skipped_reason: str = ""
    suite_note: str = ""  # why the existing suite was not rerun, when it was not
    base_rerun: Optional[BaseRerun] = None  # set on FAIL
    issue: Optional[IssueRef] = None  # the issue for a pre-existing bug, when one applies

    @property
    def introduced_by(self) -> str:
        return (self.diagnosis or {}).get("introduced_by", "")


def platform(ctx: Context) -> PlatformCfg:
    return ctx.cfg.validate.platforms[ctx.cfg.project.platforms[0]]


def _runner(ctx: Context):
    try:
        return get_runner(platform(ctx))
    except RunnerError as exc:
        raise Stop(EXIT_ERROR, str(exc)) from exc


def baseline(ctx: Context, state: State, rdir: str, log: Optional[RunLog]) -> None:
    """Existing suite must pass on the branch before anyone reviews it. On success the commit
    it passed on is recorded (`state.baseline_sha`) so validation can tell when the suite it
    would run is the one that already ran."""
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
    started = time.monotonic()
    try:
        report = runner.run(
            ctx.repo_root,
            "HEAD",
            steps,
            {},
            ctx.logs,
            "baseline",
            log.detail if log else None,
            scope=safe_branch(ctx.branch),
        )
    except RunnerError as exc:
        raise Stop(EXIT_ERROR, "sandbox failed during baseline: %s" % exc) from exc
    took = time.monotonic() - started
    if log:
        log.timing.sandbox("baseline", took)
    failed = report.failed
    if log:
        log.stage(
            "preflight",
            "baseline %s (%s)"
            % ("passed" if failed is None else "failed at " + failed.name, fmt_duration(took)),
        )
    if failed is not None:
        raise Stop(
            EXIT_ERROR,
            "branch is broken before review: %s failed (exit %d%s); see %s\n%s"
            % (
                failed.name,
                failed.returncode,
                ", timed out" if failed.timed_out else "",
                failed.log_path,
                tail(failed.text, 20),
            ),
        )
    state.baseline_sha = ctx.head_sha
    state.save(rdir)


def baseline_reusable(ctx: Context, state: State) -> str:
    """The baseline commit when the existing suite validation would run is the one the
    baseline already ran: `[validate] reuse_baseline` is on, the baseline passed on a commit
    HEAD still contains, every commit since carries the reviewer's `Revali-Round` trailer,
    and every path changed since lies under test_dir. Empty string otherwise."""
    sha = state.baseline_sha
    root = ctx.repo_root
    if not sha or not ctx.cfg.validate.reuse_baseline or not gitops.head_contains(sha, root):
        return ""
    if sha != ctx.head_sha:
        trailer = {c for c, _ in gitops.trailer_commits(sha, "HEAD", TRAILER, root)}
        if any(c not in trailer for c in gitops.rev_list(sha, "HEAD", root)):
            return ""
        test_dir = ctx.cfg.project.test_dir
        if any(not under_test_dir(p, test_dir) for p in gitops.changed_files(sha, "HEAD", root)):
            return ""
    return sha


def run_validation(
    ctx: Context, state: State, rdir: str, log: Optional[RunLog]
) -> ValidationOutcome:
    number = len(state.validations) + 1
    outcome = ValidationOutcome(number=number, result=PASS)
    took = None
    reused = baseline_reusable(ctx, state)
    if reused:
        outcome.suite_note = (
            "existing suite not rerun: unchanged since the baseline that passed on %s "
            "(only the reviewer's test commits since)" % reused[:10]
        )
    which = ["setup", "build", "new_test"] if reused else ["setup", "build", "test", "new_test"]
    steps = []
    if ctx.doc.kind != "docs":
        # new_test names every reviewer file on the branch when it asks for {files}
        steps = steps_with_files(
            platform(ctx), which, list(state.test_files), log.stage if log else None, "validate"
        )
    if ctx.doc.kind == "docs":
        outcome.skipped_reason = "kind docs: nothing to run"
    elif not any(name in ("test", "new_test") for name, _ in steps):
        # `test` is empty and new_test (required by preflight) was skipped: `{files}` had no
        # file to name
        if reused:
            why = outcome.suite_note + " and no new test file"
        else:
            why = "no test command and new_test names no test file"
        outcome.skipped_reason = "nothing to run: " + why
        if log:
            log.stage("validate", "run %d: %s" % (number, outcome.skipped_reason))
    else:
        runner = _runner(ctx)
        label = "validate-r%d" % max(1, len(state.rounds))
        if log:
            if outcome.suite_note:
                log.stage("validate", outcome.suite_note)
            log.stage(
                "validate",
                "run %d: %s on %s (%s)"
                % (number, ", ".join(n for n, _ in steps), runner.name, label),
            )
        started = time.monotonic()
        try:
            report = runner.run(
                ctx.repo_root,
                "HEAD",
                steps,
                {},
                ctx.logs,
                label,
                log.detail if log else None,
                scope=safe_branch(ctx.branch),
            )
        except RunnerError as exc:
            raise Stop(EXIT_ERROR, "sandbox failed: %s" % exc) from exc
        took = time.monotonic() - started
        if log:
            log.timing.sandbox(label, took)
        outcome.report = report
        failed = report.failed
        if failed is not None:
            if failed.name in ("setup", "build"):
                raise Stop(
                    EXIT_ERROR,
                    "sandbox %s step failed (exit %d); this is an environment problem, "
                    "not a verdict; see %s\n%s"
                    % (failed.name, failed.returncode, failed.log_path, tail(failed.text, 20)),
                )
            outcome.result = FAIL
            outcome.failed_step = failed.name
            outcome.base_rerun = rerun_on_base(ctx, state, failed, max(1, len(state.rounds)), log)
            if not ctx.dry_run:
                _diagnose(ctx, state, rdir, failed, outcome, log)
                outcome.issue = maybe_open(ctx, state, rdir, outcome, log)
    outcome.section_md = render_section(ctx, outcome)
    path = os.path.join(rdir, "tests.md")
    existing = read_text(path) if os.path.isfile(path) else "# Tests\n"
    write_text(path, existing.rstrip("\n") + "\n\n" + outcome.section_md)
    state.validations.append(
        {
            "number": number,
            "result": outcome.result,
            "failed_step": outcome.failed_step,
            "round": len(state.rounds),
            "head_sha": ctx.head_sha,
            "cause": (outcome.diagnosis or {}).get("cause", ""),
            "introduced_by": outcome.introduced_by,
            "base_rerun": outcome.base_rerun.as_dict() if outcome.base_rerun else None,
            "issue": outcome.issue.number if outcome.issue else 0,
            "model": outcome.model_actual,
            "fallback": outcome.fallback,
            "cost_usd": outcome.cost,
            "at": now_iso(),
        }
    )
    state.cost_usd += outcome.cost
    if outcome.model_actual and outcome.model_actual not in state.models_used:
        state.models_used.append(outcome.model_actual)
    state.last_verdict = outcome.result
    state.save(rdir)
    if log:
        log.stage(
            "validate",
            "run %d: %s%s%s"
            % (
                number,
                outcome.result,
                " (%s)" % outcome.failed_step if outcome.failed_step else "",
                " (%s)" % fmt_duration(took) if took is not None else "",
            ),
        )
    return outcome


def rerun_on_base(
    ctx: Context, state: State, failed, round_no: int, log: Optional[RunLog]
) -> BaseRerun:
    """After the reviewer's tests failed on the branch, run the same files (taken from the
    working tree, so they exist on base too) with `setup`, `build`, `new_test` on the base
    tip preflight resolved, under the label `base-r<round>`. Evidence for the diagnosis:
    whatever goes wrong here is a reason on the result, never a Stop."""
    out = BaseRerun(sha=ctx.base_sha)
    if failed.name != "new_test":
        out.reason = SUITE_FAILED_REASON
    elif not ctx.cfg.validate.rerun_on_base:
        out.reason = "[validate] rerun_on_base is false"
    extra = {}
    if not out.reason:
        try:
            extra = {
                rel: read_text(os.path.join(ctx.repo_root, rel))
                for rel in state.test_files
                if os.path.isfile(os.path.join(ctx.repo_root, rel))
            }
        except (OSError, UnicodeDecodeError) as exc:
            out.reason = "could not read a reviewer test file: %s" % exc
        if not extra and not out.reason:
            out.reason = "no reviewer test file to rerun"
    if out.reason:
        if log:
            log.stage("validate", "base rerun not run: %s" % out.reason)
        return out
    runner = _runner(ctx)
    label = "base-r%d" % round_no
    files = sorted(extra)
    steps = steps_with_files(
        platform(ctx), ["setup", "build", "new_test"], files, log.stage if log else None, "validate"
    )
    if log:
        log.stage(
            "validate",
            "base rerun: %d reviewer test file(s) on %s at %s (%s)"
            % (len(files), ctx.base, out.sha[:10], label),
        )
    out.ran = True
    started = time.monotonic()
    try:
        out.report = runner.run(
            ctx.repo_root,
            out.sha,
            steps,
            extra,
            ctx.logs,
            label,
            log.detail if log else None,
            scope=safe_branch(ctx.branch),
        )
    except RunnerError as exc:
        out.reason = "sandbox error: %s" % exc
    took = time.monotonic() - started
    if log:
        log.timing.sandbox(label, took)
    if out.report is not None:
        step = out.report.failed
        if step is not None and step.name != "new_test":
            out.reason = "%s failed on base (exit %d%s)" % (
                step.name,
                step.returncode,
                ", timed out" if step.timed_out else "",
            )
        elif step is not None and step.timed_out:
            out.reason = "new_test timed out on base (exit %d)" % step.returncode
        elif out.new_test is None:
            out.reason = "new_test did not run on base"
    if log:
        log.stage(
            "validate",
            "base rerun: %s (%s)"
            % (
                (
                    "new_test exit %d" % out.new_test.returncode
                    if out.available
                    else "unavailable, " + out.reason
                ),
                fmt_duration(took),
            ),
        )
    return out


def _base_rerun_for_prompt(rerun: BaseRerun) -> str:
    """The `$base_rerun` value of the diagnosis prompt: what happened on base, phrased so
    it is true whether the rerun ran, ran without a usable result, or did not run."""
    if not rerun.available:
        return (
            "revali has no `new_test` result from the base branch (%s). "
            "Answer `introduced_by: unknown`." % rerun.reason
        )
    step = rerun.new_test
    return (
        "revali copied the reviewer's test files onto a sandbox clone of that commit (the "
        "branch's changes absent) and ran `new_test` there; `new_test` exited %d on base. "
        "Its output (last %d lines):\n\n```\n%s\n```"
        % (step.returncode, LOG_LINES, tail(step.text, LOG_LINES) or "(empty)")
    )


def _diagnose(
    ctx: Context, state: State, rdir: str, failed, outcome: ValidationOutcome, log: Optional[RunLog]
) -> None:
    cfg = ctx.cfg.validate
    engine = engines.for_role(ctx.cfg, "validate")
    chosen = models.resolve(
        models.DIAGNOSER,
        cfg.model,
        cfg.fallback_model,
        ctx.doc.author_model,
        engine.tiers,
        ctx.cfg.foreign_ladders(engine.name),
    )
    model = chosen.model
    outcome.model_reason = chosen.reason
    tests_md_path = os.path.join(rdir, "tests.md")
    values = {
        "branch": ctx.branch,
        "base": ctx.base,
        "kind": ctx.doc.kind,
        "failed_step": failed.name,
        "failed_cmd": failed.cmd,
        "failed_exit": failed.returncode,
        "timed_out_note": " (timed out)" if failed.timed_out else "",
        "change_md": ctx.doc.raw.strip(),
        "tests_md": read_text(tests_md_path).strip() if os.path.isfile(tests_md_path) else "(none)",
        "test_files": "\n".join("- " + p for p in state.test_files) or "(none)",
        "log_lines": LOG_LINES,
        "log_tail": tail(failed.text, LOG_LINES) or "(empty)",
        "base_sha": ctx.base_sha,
        "base_rerun": _base_rerun_for_prompt(outcome.base_rerun),
    }
    prompt = string.Template(read_text(ctx.diagnose_prompt)).safe_substitute(values)
    write_text(os.path.join(ctx.logs, "prompt-diagnose-%d.md" % outcome.number), prompt)
    if log:
        log.stage(
            "validate",
            "diagnoser %s%s via %s (budget $%.2f)"
            % (
                model,
                " (%s)" % chosen.reason if chosen.reason else "",
                engine.name,
                cfg.budget_usd,
            ),
        )
    try:
        result = engine.run(
            EngineRequest(
                role="diagnoser",
                prompt=prompt,
                schema_text=read_text(ctx.diagnose_schema),
                model=model,
                fallback_model=chosen.fallback,
                effort=cfg.effort,
                budget_usd=cfg.budget_usd,
                timeout_s=ctx.cfg.review.timeout_min * 60,
                cwd=ctx.repo_root,
                raw_path=os.path.join(ctx.logs, "diagnose-%d.raw.json" % outcome.number),
                read_only=True,
            ),
            log,
        )
    except Stop as stop:
        outcome.diagnosis_error = stop.message
        if log:
            log.stage("validate", "diagnosis unavailable: %s" % stop.message)
        return
    data = result.data
    if (
        not data.get("summary")
        or data.get("cause") not in ("code", "test", "env", "unknown")
        or data.get("introduced_by") not in INTRODUCED_BY
    ):
        outcome.diagnosis_error = "diagnoser output did not match the schema"
    else:
        outcome.diagnosis = data
    outcome.model_actual = result.model_actual
    outcome.fallback = result.fallback
    outcome.cost = result.cost
    write_text(
        os.path.join(rdir, "diagnose-%d.json" % outcome.number),
        __import__("json").dumps(
            {
                "meta": {
                    "model_requested": model,
                    "model_actual": result.model_actual,
                    "model_reason": chosen.reason or "explicit",
                    "fallback": result.fallback,
                    "cost_usd": result.cost,
                    "at": now_iso(),
                },
                "data": data,
            },
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
    )


def _base_rerun_lines(r: BaseRerun) -> list:
    """The `### Base rerun` block: the step table and the new_test output when it ran, else
    one line saying why not."""
    if r.report is None:
        return [("unavailable: " if r.ran else "not run: ") + r.reason]
    out = ["| step | exit | log |", "|---|---|---|"]
    for s in r.report.steps:
        out.append(
            "| %s | %s%s | `%s` |"
            % (
                s.name,
                s.returncode,
                " (timed out)" if s.timed_out else "",
                os.path.basename(s.log_path or ""),
            )
        )
    if not r.available:
        out += ["", "unavailable: " + r.reason]
        return out
    out += [
        "",
        "new_test output on base (last 40 lines):",
        "",
        "```",
        tail(r.new_test.text, 40) or "(empty)",
        "```",
    ]
    return out


def render_section(ctx: Context, o: ValidationOutcome) -> str:
    out = ["## Validation %d: %s" % (o.number, o.result), "", "at: %s" % now_iso()]
    if o.skipped_reason:
        out += ["", o.skipped_reason, ""]
        return "\n".join(out)
    if o.suite_note:
        out += ["", o.suite_note]
    if o.report:
        out += ["", "| step | exit | log |", "|---|---|---|"]
        for s in o.report.steps:
            out.append(
                "| %s | %s%s | `%s` |"
                % (
                    s.name,
                    s.returncode,
                    " (timed out)" if s.timed_out else "",
                    os.path.basename(s.log_path or ""),
                )
            )
    if o.result == FAIL and o.report and o.report.failed:
        out += [
            "",
            "### Failure output (%s, last 40 lines)" % o.failed_step,
            "",
            "```",
            tail(o.report.failed.text, 40) or "(empty)",
            "```",
        ]
    if o.result == FAIL and o.base_rerun:
        out += ["", "### Base rerun (%s)" % o.base_rerun.sha[:10], ""]
        out += _base_rerun_lines(o.base_rerun)
    if o.diagnosis:
        d = o.diagnosis
        out += [
            "",
            "### Diagnosis (%s%s)" % (o.model_actual, ", fallback" if o.fallback else ""),
            "",
            "cause: **%s**" % d.get("cause"),
            "introduced by: **%s**" % d.get("introduced_by"),
            "",
            d.get("summary", "").strip(),
            "",
        ]
        for f in d.get("failures", []):
            out.append(
                "- `%s`: %s, introduced by %s. %s"
                % (f.get("test"), f.get("cause"), f.get("introduced_by"), f.get("note", ""))
            )
        out += ["", "recommendation: %s" % d.get("recommendation", "")]
    elif o.diagnosis_error:
        out += ["", "diagnosis unavailable: %s" % o.diagnosis_error]
    if o.issue:
        out += ["", "pre-existing bug, " + o.issue.line()]
    out.append("")
    return "\n".join(out)


def render_section_summary(o: ValidationOutcome, state_dir: str) -> str:
    """The PR comment for a non-private repository: result, exit codes, diagnosis cause only."""
    out = [
        "<!-- generated by revali; summary only (non-private repository) -->",
        "",
        "## Validation %d: %s" % (o.number, o.result),
        "",
    ]
    if o.skipped_reason:
        out += [o.skipped_reason, ""]
        return "\n".join(out)
    if o.suite_note:
        out += [o.suite_note, ""]
    if o.report:
        out += ["| step | exit |", "|---|---|"]
        for s in o.report.steps:
            out.append(
                "| %s | %s%s |" % (s.name, s.returncode, " (timed out)" if s.timed_out else "")
            )
        out.append("")
    if o.result == FAIL:
        out.append("failed at step `%s`" % o.failed_step)
        if o.base_rerun:
            out.append(o.base_rerun.one_line())
        if o.diagnosis:
            out.append(
                "diagnosis (%s): cause **%s**%s, %d failure(s) examined"
                % (
                    o.model_actual or "?",
                    o.diagnosis.get("cause"),
                    ", introduced by **base**" if o.introduced_by == "base" else "",
                    len(o.diagnosis.get("failures", [])),
                )
            )
        elif o.diagnosis_error:
            out.append("diagnosis unavailable")
        if o.issue:
            out.append("pre-existing bug, " + o.issue.line())
        out.append("")
    out += ["Full text: `%s/<branch>/tests.md` on the author's machine." % state_dir, ""]
    return "\n".join(out)


def summary_for_author(o: ValidationOutcome, rdir: str) -> str:
    lines = ["validation %d FAILED at step %s" % (o.number, o.failed_step)]
    if o.diagnosis:
        lines.append("cause: %s. %s" % (o.diagnosis.get("cause"), o.diagnosis.get("summary", "")))
        lines.append("introduced by: %s" % o.introduced_by)
        lines.append("recommendation: %s" % o.diagnosis.get("recommendation", ""))
    elif o.diagnosis_error:
        lines.append("diagnosis unavailable: %s" % o.diagnosis_error)
    if o.issue:
        lines.append("pre-existing bug, " + o.issue.line())
    if o.report and o.report.failed:
        lines.append("log: %s" % o.report.failed.log_path)
    lines.append("details: %s" % os.path.join(rdir, "tests.md"))
    return "\n".join(lines)
