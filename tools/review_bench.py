"""Reviewer catch-rate benchmark: run one round-1 reviewer session per case and score whether
a finding names the bug the case was built from.

A case is a directory with `case.toml` and `change.md` (docs/benchmark.md says how to build
one from an archived round). For each case the tool clones the case's repository into a
temporary directory, fetches the listed refs, drops the remote, checks out the head commit
on the case's branch, places change.md, and runs the reviewer through revali's own
`build_prompt` and `spawn_reviewer`. Nothing is pushed, `gh` is never run, the sandbox and
the reviewer's tests are not run, and the clone is removed afterwards; the prompt, the raw
answer and the parsed answer stay under the output directory.

    python tools/review_bench.py --budget-usd 15 [--cases DIR] [--model M]
                                 [--session-budget-usd N] [--line-margin N] [--out DIR]
                                 [case ...]

Exit 0 when every case that ran was scored (caught or missed), 1 when a case was malformed,
failed, or was not run for the budget.
"""

import argparse
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import tomllib
from dataclasses import asdict, dataclass, field
from typing import List, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from revali import gitops  # noqa: E402
from revali.preflight import Stop, locate  # noqa: E402
from revali.review import CHANGES_REQUESTED, build_prompt, spawn_reviewer  # noqa: E402
from revali.state import State, safe_branch  # noqa: E402

CASES_DIR = os.path.join(ROOT, "bench", "cases")
CASE_FILE = "case.toml"
CHANGE_FILE = "change.md"
SCORED_KINDS = ("correctness", "security")
STRONG = ("medium", "high")
DEFAULT_MARGIN = 15


class CaseError(Exception):
    """A case directory that cannot be run as written."""


@dataclass
class Case:
    name: str
    path: str
    repo: str
    fetch: List[str]
    base: str
    head: str
    branch: str
    file: str
    lines: List[int]
    description: str
    pr: int
    round: int
    finding: str
    change_md: str


@dataclass
class Result:
    case: str
    outcome: str  # caught-strict | caught-loose | missed | error | not-run | malformed
    finding: str = ""
    severity: str = ""
    verdict: str = ""
    model: str = ""
    cost: float = 0.0
    duration_s: float = 0.0
    message: str = ""
    other_findings: List[str] = field(default_factory=list)


def _norm(path: str) -> str:
    path = path.replace("\\", "/").strip()
    return path[2:] if path.startswith("./") else path


def load_case(path: str) -> Case:
    """Read and check one case directory; CaseError names every missing or bad field."""
    name = os.path.basename(os.path.normpath(path))
    toml_path = os.path.join(path, CASE_FILE)
    doc_path = os.path.join(path, CHANGE_FILE)
    if not os.path.isfile(toml_path):
        raise CaseError("%s: no %s" % (name, CASE_FILE))
    if not os.path.isfile(doc_path):
        raise CaseError("%s: no %s" % (name, CHANGE_FILE))
    try:
        with open(toml_path, "rb") as fh:
            data = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise CaseError("%s: %s is not valid TOML: %s" % (name, CASE_FILE, exc)) from exc
    problems = []

    def get(table, key, kind):
        value = data.get(table, {}).get(key) if isinstance(data.get(table), dict) else None
        ok = isinstance(value, kind) and not isinstance(value, bool)
        if kind is str:
            ok = ok and bool(value.strip())
        if not ok:
            problems.append("%s.%s" % (table, key))
        return value

    repo = get("case", "repo", str)
    base = get("case", "base", str)
    head = get("case", "head", str)
    branch = get("case", "branch", str)
    fetch = data.get("case", {}).get("fetch", []) if isinstance(data.get("case"), dict) else []
    if not isinstance(fetch, list) or not all(isinstance(r, str) and r for r in fetch):
        problems.append("case.fetch")
    file = get("expected", "file", str)
    lines = get("expected", "lines", list)
    if lines is not None and not (
        len(lines) == 2
        and all(isinstance(n, int) and not isinstance(n, bool) and n > 0 for n in lines)
        and lines[0] <= lines[1]
    ):
        problems.append("expected.lines")
    description = get("expected", "description", str)
    pr = get("source", "pr", int)
    round_no = get("source", "round", int)
    finding = get("source", "finding", str)
    if problems:
        raise CaseError(
            "%s: missing or malformed in %s: %s" % (name, CASE_FILE, ", ".join(problems))
        )
    with open(doc_path, "r", encoding="utf-8") as fh:
        change_md = fh.read()
    if not change_md.strip():
        raise CaseError("%s: %s is empty" % (name, CHANGE_FILE))
    return Case(
        name=name,
        path=path,
        repo=repo,
        fetch=list(fetch),
        base=base.strip(),
        head=head.strip(),
        branch=branch.strip(),
        file=_norm(file),
        lines=list(lines),
        description=description,
        pr=pr,
        round=round_no,
        finding=finding,
        change_md=change_md,
    )


def score(case: Case, data: dict, margin: int) -> Result:
    """Caught when a correctness or security finding names the expected file at a line inside
    the expected range widened by `margin`; strict when it is medium or high and the verdict
    asks for changes."""
    low, high = case.lines[0] - margin, case.lines[1] + margin
    verdict = str(data.get("verdict", ""))
    match, others = None, []
    for f in data.get("findings", []):
        line = f.get("line") if isinstance(f.get("line"), int) else 0
        hit = (
            _norm(str(f.get("file", ""))) == case.file
            and line > 0
            and low <= line <= high
            and f.get("kind") in SCORED_KINDS
        )
        if hit and (match is None or _rank(f) > _rank(match)):
            if match is not None:
                others.append(_label(match))
            match = f
        else:
            others.append(_label(f))
    if match is None:
        return Result(case.name, "missed", verdict=verdict, other_findings=others)
    strict = match.get("severity") in STRONG and verdict == CHANGES_REQUESTED
    return Result(
        case.name,
        "caught-strict" if strict else "caught-loose",
        finding=str(match.get("id", "")),
        severity=str(match.get("severity", "")),
        verdict=verdict,
        other_findings=others,
    )


def _rank(f: dict) -> int:
    return {"high": 2, "medium": 1}.get(f.get("severity"), 0)


def _label(f: dict) -> str:
    return "%s %s %s:%s" % (
        f.get("id", "?"),
        f.get("severity", "?"),
        f.get("file", "?"),
        f.get("line", "?"),
    )


def _git(args: List[str], cwd: str) -> str:
    res = subprocess.run(
        ["git"] + args, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    if res.returncode != 0:
        raise CaseError("git %s failed: %s" % (" ".join(args), res.stderr.strip()))
    return res.stdout


def _rmtree(path: str) -> None:
    """Remove the clone; git makes its object files read-only on Windows."""

    def again(func, target, _exc):
        try:
            os.chmod(target, stat.S_IWRITE)
            func(target)
        except OSError:
            pass

    try:
        shutil.rmtree(path, onexc=again)
    except TypeError:  # Python 3.11: no onexc
        shutil.rmtree(path, onerror=lambda func, target, _info: again(func, target, None))


def prepare_clone(case: Case, tmp: str) -> str:
    """The case's repository at its head commit on its branch, with no remote left."""
    clone = os.path.join(tmp, "repo")
    _git(["clone", "--quiet", "--no-checkout", case.repo, clone], tmp)
    for ref in case.fetch:
        _git(["fetch", "--quiet", "origin", ref], clone)
    _git(["remote", "remove", "origin"], clone)  # nothing can be pushed from here
    for sha in (case.base, case.head):
        _git(["cat-file", "-e", "%s^{commit}" % sha], clone)
    _git(["checkout", "--quiet", "-b", case.branch, case.head], clone)
    return clone


def run_case(case: Case, args, spent: float, out_dir: str) -> Result:
    started = time.monotonic()
    case_out = os.path.join(out_dir, case.name)
    os.makedirs(case_out, exist_ok=True)
    tmp = tempfile.mkdtemp(prefix="revali bench ")
    try:
        clone = prepare_clone(case, tmp)
        ctx = locate_case(case, clone)
        if args.model:
            ctx.cfg.review.model = args.model
        if args.session_budget_usd:
            ctx.cfg.review.budget_usd = args.session_budget_usd
        cap = ctx.cfg.review.budget_usd
        if spent + cap > args.budget_usd:
            return Result(
                case.name,
                "not-run",
                message="$%.2f spent + this session's cap $%.2f would pass the budget $%.2f"
                % (spent, cap, args.budget_usd),
            )
        prompt = build_prompt(ctx, State(), ctx.rdir, 1)
        _write(os.path.join(case_out, "prompt.md"), prompt)
        try:
            rr = spawn_reviewer(ctx, prompt, ctx.rdir, 1, 1, None)
        except Stop as exc:
            _keep_raw(ctx, case_out)
            return Result(
                case.name,
                "error",
                cost=exc.cost,
                duration_s=time.monotonic() - started,
                message=exc.message,
            )
        _keep_raw(ctx, case_out)
        _write(os.path.join(case_out, "answer.json"), json.dumps(rr.data, indent=1))
        result = score(case, rr.data, args.line_margin)
        result.model = rr.model_actual or rr.model_requested
        result.cost = rr.cost
        result.duration_s = time.monotonic() - started
        return result
    except CaseError as exc:
        return Result(case.name, "error", duration_s=time.monotonic() - started, message=str(exc))
    except Stop as exc:
        return Result(
            case.name,
            "error",
            cost=exc.cost,
            duration_s=time.monotonic() - started,
            message=exc.message,
        )
    finally:
        _rmtree(tmp)


def locate_case(case: Case, clone: str):
    """revali's own context for the clone, with what preflight would have filled in, minus
    every check that needs the network."""
    probe = locate_paths(clone, case.branch)
    os.makedirs(probe, exist_ok=True)
    _write(os.path.join(probe, CHANGE_FILE), case.change_md)
    ctx = locate(clone)
    ctx.base_ref = case.base
    ctx.head_sha = _git(["rev-parse", "HEAD"], clone).strip()
    ctx.base_sha = _git(["rev-parse", case.base], clone).strip()
    exclude = list(ctx.cfg.review.exclude)
    exclude.append(ctx.cfg.project.test_file_pattern.replace("{topic}", "*"))
    for _added, _deleted, path in gitops.diff_numstat(case.base, "HEAD", clone):
        (ctx.excluded_files if gitops.matches_any(path, exclude) else ctx.changed_files).append(
            path
        )
    return ctx


def locate_paths(clone: str, branch: str) -> str:
    from revali.config import paths_for

    return os.path.join(clone, paths_for(clone).state_dir, safe_branch(branch))


def _keep_raw(ctx, case_out: str) -> None:
    raw = os.path.join(ctx.logs, "review-r1-1.raw.json")
    if os.path.isfile(raw):
        shutil.copyfile(raw, os.path.join(case_out, "review.raw.json"))


def _write(path: str, text: str) -> None:
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)


def case_dirs(cases_dir: str, names: List[str]) -> List[str]:
    if names:
        return [n if os.path.isdir(n) else os.path.join(cases_dir, n) for n in names]
    if not os.path.isdir(cases_dir):
        return []
    return sorted(
        os.path.join(cases_dir, d)
        for d in os.listdir(cases_dir)
        if os.path.isdir(os.path.join(cases_dir, d))
    )


def rates(results: List[Result]) -> dict:
    scored = [r for r in results if r.outcome in ("caught-strict", "caught-loose", "missed")]
    strict = sum(r.outcome == "caught-strict" for r in scored)
    loose = sum(r.outcome in ("caught-strict", "caught-loose") for r in scored)
    return {
        "scored": len(scored),
        "strict": strict,
        "loose": loose,
        "strict_rate": strict / len(scored) if scored else None,
        "loose_rate": loose / len(scored) if scored else None,
        "cost": sum(r.cost for r in results),
    }


def _pct(n: Optional[float]) -> str:
    return "n/a" if n is None else "%.0f%%" % (100 * n)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="reviewer catch-rate benchmark")
    parser.add_argument("cases", nargs="*", help="case names (under --cases) or directories")
    parser.add_argument("--cases", dest="cases_dir", default=CASES_DIR)
    parser.add_argument("--budget-usd", type=float, required=True)
    parser.add_argument("--model", default="", help="reviewer model (default: the case's config)")
    parser.add_argument(
        "--session-budget-usd",
        type=float,
        default=0.0,
        help="each session's cap (default: the case's [review] budget_usd)",
    )
    parser.add_argument("--line-margin", type=int, default=DEFAULT_MARGIN)
    parser.add_argument("--out", default="", help="output directory (default: a new temp dir)")
    args = parser.parse_args(argv)
    if args.budget_usd <= 0:
        parser.error("--budget-usd must be more than 0")
    if args.session_budget_usd < 0:
        parser.error("--session-budget-usd must not be negative")
    out_dir = args.out or tempfile.mkdtemp(prefix="revali bench out ")
    os.makedirs(out_dir, exist_ok=True)

    results: List[Result] = []
    spent = 0.0
    stopped = ""
    for path in case_dirs(args.cases_dir, args.cases):
        name = os.path.basename(os.path.normpath(path))
        try:
            case = load_case(path)
        except CaseError as exc:
            results.append(Result(name, "malformed", message=str(exc)))
            continue
        if stopped:
            results.append(Result(name, "not-run", message=stopped))
            continue
        result = run_case(case, args, spent, out_dir)
        spent += result.cost
        if result.outcome == "not-run":
            stopped = result.message
        results.append(result)

    for r in results:
        print(
            "%-32s %-13s %-4s %-6s %-17s %-22s $%6.2f %6.0fs %s"
            % (
                r.case,
                r.outcome,
                r.finding or "-",
                r.severity or "-",
                r.verdict or "-",
                r.model or "-",
                r.cost,
                r.duration_s,
                r.message,
            )
        )
    summary = rates(results)
    print(
        "caught: strict %d/%d (%s), loose %d/%d (%s); cost $%.2f; output %s"
        % (
            summary["strict"],
            summary["scored"],
            _pct(summary["strict_rate"]),
            summary["loose"],
            summary["scored"],
            _pct(summary["loose_rate"]),
            summary["cost"],
            out_dir,
        )
    )
    with open(os.path.join(out_dir, "results.json"), "w", encoding="utf-8", newline="") as fh:
        json.dump(
            {
                "model": args.model,
                "budget_usd": args.budget_usd,
                "line_margin": args.line_margin,
                "summary": summary,
                "cases": [asdict(r) for r in results],
            },
            fh,
            indent=1,
        )
    ok = all(r.outcome in ("caught-strict", "caught-loose", "missed") for r in results)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
