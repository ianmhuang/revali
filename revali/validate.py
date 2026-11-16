"""Validate stage: baseline before review, full run after approval, diagnoser on failure."""

import os
import string
import threading
import time
from dataclasses import dataclass, field, replace
from typing import List, Optional

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
    sandbox_key,
    steps_for,
    steps_with_files,
    tail,
)
from revali.state import RunLog, State, now_iso, read_text, safe_branch, write_text
from revali.testarchive import archive_mode, read_archived, sandbox_files
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
    # the platform this outcome is for, set only when several are listed; it names the
    # sandbox labels and the diagnosis files
    platform: str = ""
    took: Optional[float] = None  # seconds in the sandbox, when a session ran
    # the run as a whole: one outcome per listed platform, in list order; the whole run's own
    # fields are those of the first platform that failed (or the first platform)
    platforms: List["ValidationOutcome"] = field(default_factory=list, compare=False)

    @property
    def introduced_by(self) -> str:
        return (self.diagnosis or {}).get("introduced_by", "")

    @property
    def tag(self) -> str:
        """`<number>`, or `<number>-<platform>`: the file names of this outcome's diagnosis."""
        return "%d-%s" % (self.number, self.platform) if self.platform else "%d" % self.number

    @property
    def failed_at(self) -> str:
        """The failed step, with ` on <platform>` when several platforms are listed."""
        if self.platform and self.failed_step:
            return "%s on %s" % (self.failed_step, self.platform)
        return self.failed_step

    @property
    def failing(self) -> List["ValidationOutcome"]:
        return [p for p in self.platforms if p.result == FAIL]


def failed_where(o: ValidationOutcome) -> str:
    """Where the run failed: the step, or `<step> on <platform>` for every failing platform."""
    return ", ".join(p.failed_at for p in o.failing) or o.failed_at


def several(ctx: Context) -> bool:
    """More than one platform listed: every label and message names its platform."""
    return len(ctx.cfg.project.platforms) > 1


def platform(ctx: Context, name: str = "") -> PlatformCfg:
    """The `[validate.<name>]` table; the first listed platform's without a name."""
    return ctx.cfg.validate.platforms[name or ctx.cfg.project.platforms[0]]


def suffixed(label: str, name: str) -> str:
    """`label`, or `label-<name>` when a platform name is given (several are listed)."""
    return "%s-%s" % (label, name) if name else label


def _on(name: str) -> str:
    return " on %s" % name if name else ""


def _runner(ctx: Context, name: str = ""):
    try:
        return get_runner(platform(ctx, name))
    except RunnerError as exc:
        raise Stop(EXIT_ERROR, str(exc)) from exc


def baseline(ctx: Context, state: State, rdir: str, log: Optional[RunLog]) -> None:
    """Existing suite must pass on the branch before anyone reviews it. On success the commit
    it passed on is recorded (`state.baseline_sha`) so validation can tell when the suite it
    would run is the one that already ran."""
    sha = run_baseline(ctx, ctx.head_sha, log)
    if sha:
        state.baseline_sha = sha
        state.save(rdir)


def baseline_steps(ctx: Context, name: str = ""):
    """One platform's baseline steps (the first platform's without a name), or None when it
    has no baseline to run (a docs change, or no existing test command)."""
    if ctx.doc.kind == "docs":
        return None
    steps = [s for s in steps_for(platform(ctx, name), ["setup", "build", "test"]) if s[1].strip()]
    return steps if any(n == "test" for n, _ in steps) else None


def baseline_plan(ctx: Context):
    """(platform, steps) for every listed platform, in list order, steps None where that
    platform has no baseline; None when no platform has one."""
    plan = [(name, baseline_steps(ctx, name)) for name in ctx.cfg.project.platforms]
    return plan if any(steps is not None for _, steps in plan) else None


def run_baseline(ctx: Context, ref: str, log: Optional[RunLog]) -> str:
    """Run the existing suite on commit `ref` in the sandbox of every listed platform, in list
    order. Returns `ref` when every one that ran passed, "" when there was nothing to run;
    raises Stop at the first that failed (the later ones do not run). Touches no state, so it
    may run on a thread of its own (BaselineJob)."""
    plan = baseline_plan(ctx)
    if plan is None:
        if log and ctx.doc.kind != "docs":
            log.stage("preflight", "no existing test command; baseline skipped")
        return ""
    for name, steps in plan:
        tag = name if several(ctx) else ""
        if steps is None:
            if log:
                log.stage("preflight", "no existing test command on %s; baseline skipped" % name)
            continue
        _baseline_on(ctx, ref, tag, steps, log)
    return ref


def _baseline_on(ctx: Context, ref: str, tag: str, steps, log: Optional[RunLog]) -> None:
    """One platform's baseline; `tag` is its name when several are listed, else ""."""
    runner = _runner(ctx, tag)
    label = suffixed("baseline", tag)
    on = _on(tag)
    if log:
        log.stage("preflight", "baseline%s: existing suite on %s" % (on, runner.name))
    started = time.monotonic()
    try:
        report = runner.run(
            ctx.repo_root,
            ref,
            steps,
            {},
            ctx.logs,
            label,
            log.detail if log else None,
            scope=safe_branch(ctx.branch),
            key=sandbox_key(ctx.repo_root, ctx.branch),
        )
    except RunnerError as exc:
        raise Stop(EXIT_ERROR, "sandbox failed during baseline%s: %s" % (on, exc)) from exc
    took = time.monotonic() - started
    if log:
        log.timing.sandbox(label, took)
    failed = report.failed
    if log:
        log.stage(
            "preflight",
            "baseline%s %s (%s)"
            % (on, "passed" if failed is None else "failed at " + failed.name, fmt_duration(took)),
        )
    if failed is not None:
        raise Stop(
            EXIT_ERROR,
            "branch is broken before review%s: %s failed (exit %d%s); see %s\n%s"
            % (
                on,
                failed.name,
                failed.returncode,
                ", timed out" if failed.timed_out else "",
                failed.log_path,
                tail(failed.text, 20),
            ),
        )


class BaselineJob:
    """The baseline on a thread of its own, started at the end of preflight so it runs while
    the PR stage and the round 1 reviewer do ([validate] parallel_baseline). The thread only
    runs the sandbox session and logs; the main thread records the result (`settle`).

    `failed` is set the moment the baseline fails: the reviewer session's cancel event."""

    THREAD_NAME = "revali-baseline"

    def __init__(self, ctx: Context, log: Optional[RunLog]):
        self.ref = ctx.head_sha  # what preflight saw; later commits of the run do not count
        self.failed = threading.Event()
        self.sha = ""
        self.stop: Optional[Stop] = None
        self.raised = False  # its failure has been raised into the run already
        self._settled = False
        self._thread = threading.Thread(
            target=self._run, args=(ctx, log), name=self.THREAD_NAME, daemon=True
        )

    @classmethod
    def start(cls, ctx: Context, log: Optional[RunLog]) -> "BaselineJob":
        job = cls(ctx, log)
        if log:
            log.stage(
                "preflight", "baseline started; it runs alongside the PR stage and the review"
            )
        job._thread.start()
        return job

    def _run(self, ctx: Context, log: Optional[RunLog]) -> None:
        try:
            self.sha = run_baseline(ctx, self.ref, log)
        except Stop as stop:
            self.stop = stop
            self.failed.set()
        except Exception as exc:  # a bug in the thread must still end the run, not vanish
            self.stop = Stop(EXIT_ERROR, "the baseline crashed: %s: %s" % (type(exc).__name__, exc))
            self.failed.set()

    def finish(self) -> Optional[Stop]:
        """Wait for the baseline to end; its Stop when it failed, else None."""
        self._thread.join()
        return self.stop

    def raise_if_failed(self) -> None:
        """Without waiting: raise the baseline's Stop when it has already failed."""
        if self.failed.is_set():
            self.raised = True
            raise self.stop

    def settle(self, state: State, rdir: str, log: Optional[RunLog], note: str = "") -> None:
        """Wait for the baseline (logging that the run is waiting), then raise its Stop, with
        `note` on a line of its own, or record the commit it passed on, as `baseline` does."""
        if self._thread.is_alive() and log:
            log.stage("review", "waiting for the baseline to finish")
        stop = self.finish()
        if stop is not None:
            self.raised = True
            raise with_note(stop, note)
        self.record(state, rdir)

    def record(self, state: State, rdir: str) -> None:
        """Once the baseline has ended: store the commit it passed on, as `baseline` does, so a
        run that stops later for another reason still has it. A no-op otherwise."""
        if self._thread.is_alive() or self._settled:
            return
        self._settled = True
        if self.sha:
            state.baseline_sha = self.sha
            state.save(rdir)


def with_note(stop: Stop, note: str) -> Stop:
    """`stop` with `note` after its first line (the line run.log and `wait` show first)."""
    if not note:
        return stop
    first, _, rest = stop.message.partition("\n")
    message = first + "\n" + note + ("\n" + rest if rest else "")
    return Stop(stop.exit_code, message, cost=stop.cost)


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
    """Run the suite and the reviewer's tests on every listed platform, in list order, going on
    after a platform FAILs; then the base rerun, the diagnosis and the issue for each platform
    that FAILed. A `setup` or `build` failure on any platform stops at once (exit 1), before
    any diagnosis is spent."""
    number = len(state.validations) + 1
    reused = baseline_reusable(ctx, state)
    suite_note = ""
    if reused:
        suite_note = (
            "existing suite not rerun: unchanged since the baseline that passed on %s "
            "(only the reviewer's test commits since)" % reused[:10]
        )
    which = ["setup", "build", "new_test"] if reused else ["setup", "build", "test", "new_test"]
    names = list(ctx.cfg.project.platforms)
    tags = names if several(ctx) else [""]
    if suite_note and log and several(ctx) and ctx.doc.kind != "docs":
        log.stage("validate", suite_note)
    parts = [_validate_on(ctx, state, rdir, log, number, tag, which, suite_note) for tag in tags]
    round_no = max(1, len(state.rounds))
    for part in parts:
        if part.result != FAIL:
            continue
        failed = part.report.failed
        part.base_rerun = rerun_on_base(ctx, state, failed, round_no, log, part.platform)
        if not ctx.dry_run:
            _diagnose(ctx, state, rdir, failed, part, log)
            part.issue = maybe_open(ctx, state, rdir, part, log)
    outcome = _whole(parts)
    outcome.section_md = render_section(ctx, outcome)
    path = os.path.join(rdir, "tests.md")
    existing = read_text(path) if os.path.isfile(path) else "# Tests\n"
    write_text(path, existing.rstrip("\n") + "\n\n" + outcome.section_md)
    state.validations.append(
        {
            "number": number,
            **_record(outcome),
            "round": len(state.rounds),
            "head_sha": ctx.head_sha,
            "model": outcome.model_actual,
            "fallback": outcome.fallback,
            "cost_usd": outcome.cost,
            "platforms": [
                {"name": name, **_record(p)} for name, p in zip(names, parts, strict=True)
            ],
            "at": now_iso(),
        }
    )
    state.cost_usd += outcome.cost
    for part in parts:
        if part.model_actual and part.model_actual not in state.models_used:
            state.models_used.append(part.model_actual)
    state.last_verdict = outcome.result
    state.save(rdir)
    if log:
        log.stage(
            "validate",
            "run %d: %s%s%s"
            % (
                number,
                outcome.result,
                " (%s)" % failed_where(outcome) if outcome.result == FAIL else "",
                " (%s)" % fmt_duration(outcome.took) if outcome.took is not None else "",
            ),
        )
    return outcome


def _record(o: ValidationOutcome) -> dict:
    """The fields a validation record and each of its `platforms` entries share."""
    return {
        "result": o.result,
        "failed_step": o.failed_step,
        "cause": (o.diagnosis or {}).get("cause", ""),
        "introduced_by": o.introduced_by,
        "base_rerun": o.base_rerun.as_dict() if o.base_rerun else None,
        "issue": o.issue.number if o.issue else 0,
    }


def _whole(parts: List[ValidationOutcome]) -> ValidationOutcome:
    """The run as a whole: the first failing platform's outcome (or the first platform's),
    FAIL when any platform failed, the cost and the sandbox time of all, `platforms` = parts."""
    lead = next((p for p in parts if p.result == FAIL), parts[0])
    if len(parts) == 1:
        return replace(lead, platforms=list(parts))
    tooks = [p.took for p in parts if p.took is not None]
    return replace(
        lead,
        result=FAIL if any(p.result == FAIL for p in parts) else PASS,
        cost=sum(p.cost for p in parts),
        took=sum(tooks) if tooks else None,
        skipped_reason=parts[0].skipped_reason if all(p.skipped_reason for p in parts) else "",
        platforms=list(parts),
    )


def _validate_on(
    ctx: Context,
    state: State,
    rdir: str,
    log: Optional[RunLog],
    number: int,
    tag: str,
    which: List[str],
    suite_note: str,
) -> ValidationOutcome:
    """One platform's sandbox session; `tag` is its name when several are listed, else ""."""
    outcome = ValidationOutcome(number=number, result=PASS, platform=tag, suite_note=suite_note)
    if ctx.doc.kind == "docs":
        outcome.skipped_reason = "kind docs: nothing to run"
        return outcome
    on = _on(tag)
    # new_test names every reviewer file on the branch when it asks for {files}
    steps = steps_with_files(
        platform(ctx, tag), which, list(state.test_files), log.stage if log else None, "validate"
    )
    if not any(name in ("test", "new_test") for name, _ in steps):
        # `test` is empty and new_test (required by preflight) was skipped: `{files}` had no
        # file to name
        if suite_note:
            why = suite_note + " and no new test file"
        else:
            why = "no test command and new_test names no test file"
        outcome.skipped_reason = "nothing to run: " + why
        if log:
            log.stage("validate", "run %d%s: %s" % (number, on, outcome.skipped_reason))
        return outcome
    runner = _runner(ctx, tag)
    label = suffixed("validate-r%d" % max(1, len(state.rounds)), tag)
    if log:
        if suite_note and not tag:
            log.stage("validate", suite_note)
        log.stage(
            "validate",
            "run %d: %s on %s (%s)" % (number, ", ".join(n for n, _ in steps), runner.name, label),
        )
    started = time.monotonic()
    try:
        report = runner.run(
            ctx.repo_root,
            "HEAD",
            steps,
            sandbox_files(ctx, state, rdir),  # archive mode: the files are not in HEAD
            ctx.logs,
            label,
            log.detail if log else None,
            scope=safe_branch(ctx.branch),
            key=sandbox_key(ctx.repo_root, ctx.branch),
        )
    except RunnerError as exc:
        raise Stop(EXIT_ERROR, "sandbox failed%s: %s" % (on, exc)) from exc
    outcome.took = time.monotonic() - started
    if log:
        log.timing.sandbox(label, outcome.took)
    outcome.report = report
    failed = report.failed
    if failed is not None:
        if failed.name in ("setup", "build"):
            raise Stop(
                EXIT_ERROR,
                "sandbox %s step failed%s (exit %d); this is an environment problem, "
                "not a verdict; see %s\n%s"
                % (failed.name, on, failed.returncode, failed.log_path, tail(failed.text, 20)),
            )
        outcome.result = FAIL
        outcome.failed_step = failed.name
    if log and tag:
        log.stage(
            "validate",
            "run %d on %s: %s%s (%s)"
            % (
                number,
                tag,
                outcome.result,
                " (%s)" % outcome.failed_step if outcome.failed_step else "",
                fmt_duration(outcome.took),
            ),
        )
    return outcome


def rerun_on_base(
    ctx: Context, state: State, failed, round_no: int, log: Optional[RunLog], tag: str = ""
) -> BaseRerun:
    """After the reviewer's tests failed on the branch, run the same files (taken from the
    working tree, or from the archive in archive mode, so they exist on base too) with
    `setup`, `build`, `new_test` on the base tip preflight resolved, under the label
    `base-r<round>` (`base-r<round>-<platform>` when several platforms are listed, `tag`).
    Evidence for the diagnosis: whatever goes wrong here is a reason on the result, never a
    Stop."""
    out = BaseRerun(sha=ctx.base_sha)
    if failed.name != "new_test":
        out.reason = SUITE_FAILED_REASON
    elif not ctx.cfg.validate.rerun_on_base:
        out.reason = "[validate] rerun_on_base is false"
    extra = {}
    if not out.reason:
        try:
            if archive_mode(ctx, state):
                extra = read_archived(ctx.rdir, state.test_files)
            else:
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
            log.stage("validate", "base rerun%s not run: %s" % (_on(tag), out.reason))
        return out
    runner = _runner(ctx, tag)
    label = suffixed("base-r%d" % round_no, tag)
    files = sorted(extra)
    steps = steps_with_files(
        platform(ctx, tag),
        ["setup", "build", "new_test"],
        files,
        log.stage if log else None,
        "validate",
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
            key=sandbox_key(ctx.repo_root, ctx.branch),
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
            "base rerun%s: %s (%s)"
            % (
                _on(tag),
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
    write_text(os.path.join(ctx.logs, "prompt-diagnose-%s.md" % outcome.tag), prompt)
    if log:
        log.stage(
            "validate",
            "diagnoser%s %s%s via %s (budget $%.2f)"
            % (
                " for %s" % outcome.platform if outcome.platform else "",
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
                raw_path=os.path.join(ctx.logs, "diagnose-%s.raw.json" % outcome.tag),
                read_only=True,
            ),
            log,
        )
    except Stop as stop:
        outcome.diagnosis_error = stop.message
        if log:
            log.stage(
                "validate", "diagnosis%s unavailable: %s" % (_on(outcome.platform), stop.message)
            )
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
        os.path.join(rdir, "diagnose-%s.json" % outcome.tag),
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
    """The tests.md section; with several platforms, one `### <platform>: RESULT` block each,
    whose own headings go one level down."""
    out = ["## Validation %d: %s" % (o.number, o.result), "", "at: %s" % now_iso()]
    if o.skipped_reason:
        out += ["", o.skipped_reason, ""]
        return "\n".join(out)
    if o.suite_note:
        out += ["", o.suite_note]
    if len(o.platforms) > 1:
        for p in o.platforms:
            out += ["", "### %s: %s" % (p.platform, p.result)]
            out += ["", p.skipped_reason] if p.skipped_reason else _detail_lines(p, "####")
    else:
        out += _detail_lines(o, "###")
    out.append("")
    return "\n".join(out)


def _detail_lines(o: ValidationOutcome, h: str) -> list:
    """One outcome's steps, failure output, base rerun, diagnosis and issue, under `h`
    headings."""
    out = []
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
            "%s Failure output (%s, last 40 lines)" % (h, o.failed_step),
            "",
            "```",
            tail(o.report.failed.text, 40) or "(empty)",
            "```",
        ]
    if o.result == FAIL and o.base_rerun:
        out += ["", "%s Base rerun (%s)" % (h, o.base_rerun.sha[:10]), ""]
        out += _base_rerun_lines(o.base_rerun)
    if o.diagnosis:
        d = o.diagnosis
        out += [
            "",
            "%s Diagnosis (%s%s)" % (h, o.model_actual, ", fallback" if o.fallback else ""),
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
    return out


def render_section_summary(o: ValidationOutcome, state_dir: str) -> str:
    """The PR comment for a non-private repository: result, exit codes, diagnosis cause only;
    one `### <platform>: RESULT` block per platform when several are listed."""
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
    if len(o.platforms) > 1:
        for p in o.platforms:
            out += ["### %s: %s" % (p.platform, p.result), ""]
            out += [p.skipped_reason, ""] if p.skipped_reason else _summary_lines(p)
    else:
        out += _summary_lines(o)
    out += ["Full text: `%s/<branch>/tests.md` on the author's machine." % state_dir, ""]
    return "\n".join(out)


def _summary_lines(o: ValidationOutcome) -> list:
    """One outcome's exit codes and, on FAIL, the step, the base rerun and the cause."""
    out = []
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
    return out


def summary_for_author(o: ValidationOutcome, rdir: str) -> str:
    """The ACTION NEEDED text: the failed step, diagnosis and log; with several platforms,
    each failing platform's under its name."""
    failing = o.failing if len(o.platforms) > 1 else []
    if not failing:
        lines = ["validation %d FAILED at step %s" % (o.number, o.failed_step)]
        lines += _author_lines(o)
    else:
        lines = ["validation %d FAILED at %s" % (o.number, failed_where(o))]
        for p in failing:
            lines.append("%s:" % p.platform)
            lines += ["  " + line for line in _author_lines(p)]
    lines.append("details: %s" % os.path.join(rdir, "tests.md"))
    return "\n".join(lines)


def _author_lines(o: ValidationOutcome) -> list:
    lines = []
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
    return lines
