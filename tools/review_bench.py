"""Reviewer catch-rate benchmark: run one round-1 reviewer session per case and score whether
a finding names the bug the case was built from.

A case is a directory with `case.toml` and `change.md` (bench/README.md says how to build
one from an archived round). For each case the tool clones the case's repository into a
temporary directory, fetches the listed refs, drops the remote, checks out the head commit
on the case's branch, points the base branch at the case's base commit, deletes every other
ref and the reflogs (so later commits, the fix among them, are out of the reviewer's
reach), places change.md, and runs the reviewer through revali's own `build_prompt` and
`spawn_reviewer`. Nothing is pushed, `gh` is never run, the sandbox and the reviewer's tests
are not run, and the clone is removed afterwards; the prompt, the raw answer and the parsed
answer stay under the output directory.

    python tools/review_bench.py --budget-usd 15 [--cases DIR ...] [--model M]
                                 [--session-budget-usd N] [--line-margin N] [--out DIR]
                                 [case ...]
    python tools/review_bench.py --dry-run [--cases DIR ...] [case ...]
    python tools/review_bench.py --resume DIR --budget-usd TOTAL [case ...]

Exit 0 when every case was scored (caught or missed), 1 when a case was malformed, failed,
was interrupted, or was not run for the budget. With --dry-run no session runs: exit 0 when
every case got as far as its prompt. --resume continues a stopped run in DIR with the
settings recorded there: cases with a result are kept, the others run.
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
from dataclasses import asdict, dataclass, field, fields
from typing import List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from revali import PROMPT_VERSION, VERSION, gitops  # noqa: E402
from revali.preflight import Stop, locate  # noqa: E402
from revali.review import CHANGES_REQUESTED, build_prompt, spawn_reviewer  # noqa: E402
from revali.state import State, safe_branch, write_json_atomic  # noqa: E402

CASES_DIR = os.path.join(ROOT, "bench", "cases")
CASE_FILE = "case.toml"
CHANGE_FILE = "change.md"
CONFIG_FILE = "revali.toml"
SCORED_KINDS = ("correctness", "security")
STRONG = ("medium", "high")
DEFAULT_MARGIN = 15
READY = "ready"  # --dry-run: the case would run
RERUN = ("started", "interrupted", "not-run")  # --resume runs these again
STARTED = "session started; if the run ends here, its cap counts and --resume runs it again"


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
    config: Optional[str] = None  # the case's own revali.toml (a project keeps it out of git)


@dataclass
class Result:
    case: str
    # caught-strict | caught-loose | missed | error | malformed | ready (--dry-run)
    # | not-run | started (a session in progress) | interrupted (Ctrl-C)
    outcome: str
    finding: str = ""
    severity: str = ""
    verdict: str = ""
    model: str = ""
    cost: float = 0.0  # what the session reported; 0 when it failed without saying
    charged: float = 0.0  # what counts against --budget-usd: the cap when the cost is unknown
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
    except (OSError, ValueError) as exc:  # ValueError: TOMLDecodeError, UnicodeDecodeError
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
    if isinstance(lines, list) and not (
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
    try:
        with open(doc_path, "r", encoding="utf-8") as fh:
            change_md = fh.read()
    except (OSError, UnicodeDecodeError) as exc:
        raise CaseError("%s: cannot read %s: %s" % (name, CHANGE_FILE, exc)) from exc
    if not change_md.strip():
        raise CaseError("%s: %s is empty" % (name, CHANGE_FILE))
    config = None
    config_path = os.path.join(path, CONFIG_FILE)
    if os.path.exists(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as fh:
                config = fh.read()
            tomllib.loads(config)
        except (OSError, ValueError) as exc:
            raise CaseError("%s: %s is not valid TOML: %s" % (name, CONFIG_FILE, exc)) from exc
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
        config=config,
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


def prepare_clone(case: Case, tmp: str) -> Tuple[str, str]:
    """The case's repository at its head commit on its branch, with no remote left, and the
    name of the branch the clone's source had checked out (its default branch)."""
    clone = os.path.join(tmp, "repo")
    _git(["clone", "--quiet", "--no-checkout", case.repo, clone], tmp)
    default = subprocess.run(
        ["git", "symbolic-ref", "--quiet", "--short", "HEAD"],
        cwd=clone,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    ).stdout.strip()
    for ref in case.fetch:
        _git(["fetch", "--quiet", "origin", ref], clone)
    _git(["remote", "remove", "origin"], clone)  # nothing can be pushed from here
    for sha in (case.base, case.head):
        _git(["cat-file", "-e", "%s^{commit}" % sha], clone)
    _git(["checkout", "--quiet", "-B", case.branch, case.head], clone)
    return clone, default


def isolate(clone: str, branch: str, base_branch: str, base: str) -> None:
    """Make the clone look like the original round: the base branch at the case's base
    commit, the case's branch, and nothing else. The source's default branch, tags, the
    fetched refs and the reflogs would otherwise lead to commits made after the round,
    the fix of the very bug the case measures among them."""
    if base_branch == branch:
        raise CaseError("the base branch %r is the case's own branch" % base_branch)
    _git(["branch", "--force", base_branch, base], clone)
    keep = {"refs/heads/" + branch, "refs/heads/" + base_branch}
    for ref in _git(["for-each-ref", "--format=%(refname)"], clone).split():
        if ref not in keep:
            _git(["update-ref", "-d", ref], clone)
    git_dir = os.path.join(clone, ".git")
    for name in ("FETCH_HEAD", "ORIG_HEAD"):
        if os.path.isfile(os.path.join(git_dir, name)):
            os.remove(os.path.join(git_dir, name))
    _git(["reflog", "expire", "--expire=now", "--all"], clone)


def run_case(case: Case, args, spent: float, out_dir: str, on_session=None) -> Result:
    """One case. `on_session(cap)` is called just before the session starts."""
    started = time.monotonic()
    tmp = tempfile.mkdtemp(prefix="revali bench ")
    try:
        result = _run_in(case, args, spent, out_dir, tmp, on_session)
    finally:
        _rmtree(tmp)
    result.duration_s = time.monotonic() - started
    if os.path.exists(tmp):
        result.message = "; ".join(m for m in (result.message, "clone left at " + tmp) if m)
    return result


def _run_in(case: Case, args, spent: float, out_dir: str, tmp: str, on_session=None) -> Result:
    cap = args.session_budget_usd
    if not args.dry_run and cap and spent + cap > args.budget_usd:  # known before the clone
        return _not_run(case, spent, cap, args)
    session = False
    try:
        case_out = os.path.join(out_dir, case.name)
        os.makedirs(case_out, exist_ok=True)
        clone, default = prepare_clone(case, tmp)
        ctx = locate_case(case, clone, default)
        if args.model:
            ctx.cfg.review.model = args.model
        if cap:
            ctx.cfg.review.budget_usd = cap
        cap = ctx.cfg.review.budget_usd
        if not args.dry_run and spent + cap > args.budget_usd:
            return _not_run(case, spent, cap, args)
        prompt = build_prompt(ctx, State(), ctx.rdir, 1)
        _write(os.path.join(case_out, "prompt.md"), prompt)
        if args.dry_run:  # everything a session needs is in place; nothing is spent
            return Result(case.name, READY, message="prompt.md %d lines" % prompt.count("\n"))
        if on_session:
            on_session(cap)
        session = True
        try:
            rr = spawn_reviewer(ctx, prompt, ctx.rdir, 1, 1, None)
        finally:
            _keep_raw(ctx, case_out)
        session = False
        _write(os.path.join(case_out, "answer.json"), json.dumps(rr.data, indent=1))
        result = score(case, rr.data, args.line_margin)
        result.model = rr.model_actual or rr.model_requested
        result.cost = result.charged = rr.cost
        return result
    except Stop as exc:
        return _error(case, exc.message, exc.cost, cap if session else 0.0)
    except (CaseError, gitops.GitError, OSError, ValueError) as exc:
        return _error(case, str(exc), 0.0, cap if session else 0.0)


def _not_run(case: Case, spent: float, cap: float, args) -> Result:
    return Result(
        case.name,
        "not-run",
        message="$%.2f spent + this session's cap $%.2f would pass the budget $%.2f"
        % (spent, cap, args.budget_usd),
    )


def _error(case: Case, message: str, cost: float, cap: float) -> Result:
    """A failed case. A session that failed without reporting its cost (a timeout, output
    that is not JSON) may have spent up to its cap, so the cap counts against the budget."""
    result = Result(case.name, "error", cost=cost, charged=cost, message=message)
    if cap and not cost:
        result.charged = cap
        result.message += " (cost unknown; the session cap $%.2f counts against the budget)" % cap
    return result


def locate_case(case: Case, clone: str, default_branch: str):
    """revali's own context for the clone, with what preflight would have filled in, minus
    every check that needs the network. The base branch is the case repository's
    `[project] base_branch`, else the branch its source had checked out. A case's own
    revali.toml replaces the clone's (most projects keep theirs out of git)."""
    if case.config is not None:  # first: the state directory's name comes from it
        _write(os.path.join(clone, CONFIG_FILE), case.config)
    probe = locate_paths(clone, case.branch)
    os.makedirs(probe, exist_ok=True)
    _write(os.path.join(probe, CHANGE_FILE), case.change_md)
    ctx = locate(clone)
    ctx.base = ctx.base or default_branch
    if not ctx.base:
        raise CaseError("cannot tell the base branch; set [project] base_branch in the case's repo")
    isolate(clone, case.branch, ctx.base, case.base)
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


def case_dirs(cases_dir, names: List[str]) -> List[str]:
    """The case directories to run: the named ones (a directory, or a name looked up in each
    cases directory in turn), else every directory under every cases directory."""
    roots = [cases_dir] if isinstance(cases_dir, str) else list(cases_dir)
    if names:
        found = []
        for n in names:
            hits = [os.path.join(r, n) for r in roots if os.path.isdir(os.path.join(r, n))]
            if os.path.isdir(n):
                found.append(n)
            elif hits:
                found.extend(hits)  # more than one: duplicate_names refuses the run
            else:
                found.append(os.path.join(roots[0], n))
        return found
    found = []
    for root in roots:
        if os.path.isdir(root):
            found.extend(
                os.path.join(root, d)
                for d in sorted(os.listdir(root))
                if os.path.isdir(os.path.join(root, d))
            )
    return found


def duplicate_names(paths: List[str]) -> List[str]:
    seen, dup = {}, []
    for p in paths:
        name = os.path.basename(os.path.normpath(p))
        if name in seen:
            dup.append("%s (%s and %s)" % (name, seen[name], p))
        seen.setdefault(name, p)
    return dup


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
        "charged": sum(r.charged for r in results),
    }


def _pct(n: Optional[float]) -> str:
    return "n/a" if n is None else "%.0f%%" % (100 * n)


def _key(path: str) -> str:
    return os.path.normcase(os.path.abspath(path))


def _same_dirs(a: List[str], b: List[str]) -> bool:
    def norm(dirs):
        return [os.path.normcase(os.path.abspath(os.path.expanduser(d))) for d in dirs]

    return norm(a) == norm(b)


def _prior_results(path: str):
    """The cases of an earlier run's results.json as Results, in their order; (None, reason)
    when the file cannot be resumed."""
    try:
        with open(path, encoding="utf-8") as fh:
            prior = json.load(fh)
    except FileNotFoundError:
        return None, "no results.json in %s; nothing to resume" % os.path.dirname(path)
    except (OSError, ValueError) as exc:
        return None, "cannot read %s: %s" % (path, exc)
    known = {f.name for f in fields(Result)}
    entries = prior.get("cases") if isinstance(prior, dict) else None
    dirs = prior.get("cases_dirs") if isinstance(prior, dict) else None
    ok = (
        isinstance(entries, list)
        and isinstance(dirs, list)
        and all(isinstance(d, str) for d in dirs)
        and all(
            isinstance(c, dict)
            and isinstance(c.get("case"), str)
            and isinstance(c.get("outcome"), str)
            and all(
                isinstance(c.get(k, 0), (int, float)) and not isinstance(c.get(k), bool)
                for k in ("cost", "charged", "duration_s")
            )
            for c in entries
        )
        and isinstance(prior.get("model") or "", str)
        and isinstance(prior.get("session_budget_usd") or 0.0, (int, float))
        and isinstance(prior.get("line_margin", DEFAULT_MARGIN), int)
        and not any(isinstance(prior.get(k), bool) for k in ("session_budget_usd", "line_margin"))
    )
    if not ok:
        return None, "%s is not a benchmark results.json" % path
    if prior.get("dry_run"):
        return None, "%s is from a dry run; nothing to resume" % path
    results = {}
    for c in entries:
        results[c["case"]] = Result(**{k: v for k, v in c.items() if k in known})
    return (prior, results), ""


def resume_from(args):
    """Check `--resume DIR` against the run recorded there and fill in what the command
    line left out; ({case: Result}, "") or (None, why it cannot resume)."""
    if args.out:
        return None, "--resume writes to the directory it continues; --out cannot go with it"
    if args.dry_run:
        return None, "--resume continues a paid run; --dry-run cannot go with it"
    loaded, problem = _prior_results(os.path.join(args.resume, "results.json"))
    if problem:
        return None, problem
    prior, results = loaded
    if str(prior.get("prompt_version")) != str(PROMPT_VERSION):
        return None, (
            "the earlier run used PROMPT_VERSION %s and this revali has %s; start a new run"
            % (prior.get("prompt_version"), PROMPT_VERSION)
        )
    recorded = {
        "model": prior.get("model") or "",
        "session_budget_usd": prior.get("session_budget_usd") or 0.0,
        "line_margin": prior.get("line_margin", DEFAULT_MARGIN),
        "cases_dir": prior["cases_dirs"],
    }
    labels = {
        "model": "model",
        "session_budget_usd": "session cap",
        "line_margin": "line margin",
        "cases_dir": "case directories",
    }
    differ = []
    for attr, label in labels.items():
        given, earlier = getattr(args, attr), recorded[attr]
        if given is None:
            setattr(args, attr, earlier)
        elif attr == "cases_dir":
            if not _same_dirs(given, earlier):
                differ.append("%s %s (earlier run: %s)" % (label, given, earlier))
        elif given != earlier:
            differ.append("%s %r (earlier run: %r)" % (label, given, earlier))
    if differ:
        return None, "cannot resume %s with a different %s" % (args.resume, "; ".join(differ))
    args.out = args.resume
    return results, ""


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="reviewer catch-rate benchmark")
    parser.add_argument("cases", nargs="*", help="case names (under --cases) or directories")
    parser.add_argument(
        "--cases",
        dest="cases_dir",
        action="append",
        help="a directory of cases; may be given more than once (default: bench/cases/)",
    )
    parser.add_argument(
        "--budget-usd",
        type=float,
        help="required unless --dry-run; with --resume, the total of every invocation",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="clone, isolate and build each prompt; run no session",
    )
    parser.add_argument("--model", help="reviewer model (default: the case's config)")
    parser.add_argument(
        "--session-budget-usd",
        type=float,
        help="each session's cap (default: the case's [review] budget_usd)",
    )
    parser.add_argument(
        "--line-margin",
        type=int,
        help="lines either side of the expected range (default %d)" % DEFAULT_MARGIN,
    )
    parser.add_argument("--out", default="", help="output directory (default: a new temp dir)")
    parser.add_argument(
        "--resume",
        metavar="DIR",
        help="continue the run whose results.json is in DIR, with its settings",
    )
    args = parser.parse_args(argv)
    if args.budget_usd is None and not args.dry_run:
        parser.error("--budget-usd is required (or --dry-run)")
    if args.budget_usd is not None and args.budget_usd <= 0:
        parser.error("--budget-usd must be more than 0")
    if args.session_budget_usd is not None and args.session_budget_usd < 0:
        parser.error("--session-budget-usd must not be negative")
    if args.line_margin is not None and args.line_margin < 0:
        parser.error("--line-margin must not be negative")
    if args.cases_dir is not None:  # recorded absolute: a resume may start anywhere
        args.cases_dir = [os.path.abspath(os.path.expanduser(d)) for d in args.cases_dir]
    prior = {}
    if args.resume:
        args.resume = os.path.expanduser(args.resume)
        prior, problem = resume_from(args)
        if problem:
            print(problem, file=sys.stderr)
            return 1
    args.cases_dir = args.cases_dir or [CASES_DIR]
    args.model = args.model or ""
    args.session_budget_usd = args.session_budget_usd or 0.0
    args.line_margin = DEFAULT_MARGIN if args.line_margin is None else args.line_margin
    missing = [d for d in args.cases_dir if not os.path.isdir(d)]
    if missing:  # one of several: the run would cover part of the set and say nothing
        print("no case directory under %s: not a directory" % ", ".join(missing), file=sys.stderr)
        return 1
    paths = case_dirs(args.cases_dir, args.cases)
    if not paths:
        print("no case directory under %s" % ", ".join(args.cases_dir), file=sys.stderr)
        return 1
    keys = [_key(p) for p in paths]
    twice = sorted(
        {os.path.basename(os.path.normpath(p)) for p in paths if keys.count(_key(p)) > 1}
    )
    if twice:  # the same directory twice; two directories of one name are `dup` below
        print("case named twice: %s" % ", ".join(twice), file=sys.stderr)
        return 1
    unknown = [
        os.path.basename(os.path.normpath(p))
        for p in paths
        if not os.path.isfile(os.path.join(p, CASE_FILE))
        and os.path.basename(os.path.normpath(p)) not in prior
    ]
    if prior and args.cases and unknown:  # it would be a `malformed` entry every resume keeps
        print(
            "no case %s in the earlier run or the case directories" % ", ".join(unknown),
            file=sys.stderr,
        )
        return 1
    dup = duplicate_names(paths)
    if dup:
        print("a case name is in two directories: %s" % "; ".join(dup), file=sys.stderr)
        return 1
    if hasattr(sys.stdout, "reconfigure"):  # git's or claude's text on a narrow code page
        sys.stdout.reconfigure(errors="replace")
    out_dir = args.out or tempfile.mkdtemp(prefix="revali bench out ")
    os.makedirs(out_dir, exist_ok=True)

    results: List[Result] = []

    def save(current: Optional[Result] = None) -> None:
        """results.json: this run's cases so far, the one in progress, then every case of
        the earlier run not reached yet, so a run killed at any point loses nothing."""
        rows = results + ([current] if current else [])
        done = {r.case for r in rows}
        _write_results(out_dir, args, rows + [r for n, r in prior.items() if n not in done])

    spent = sum(r.charged for r in prior.values())  # what earlier invocations counted
    stopped = ""
    for path in paths:
        name = os.path.basename(os.path.normpath(path))
        earlier = prior.get(name)
        if earlier is not None and earlier.outcome not in RERUN:
            results.append(earlier)
            _print_case(earlier, kept=True)
            continue
        carried = earlier.charged if earlier is not None else 0.0  # already in `spent`
        session = {"cap": 0.0}

        def on_session(cap, name=name, carried=carried, session=session):
            session["cap"] = cap
            save(Result(name, "started", charged=cap + carried, message=STARTED))

        try:
            case = load_case(path)
        except CaseError as exc:
            result = Result(name, "malformed", message=str(exc))
        else:
            if stopped:
                result = Result(name, "not-run", message=stopped)
            else:
                try:
                    result = run_case(case, args, spent, out_dir, on_session)
                except KeyboardInterrupt:
                    result = _interrupted(name, session["cap"])
                    stopped = "the run was interrupted"
        if carried:
            result.charged += carried
            result.message = "; ".join(
                m
                for m in (
                    result.message,
                    "includes $%.2f charged for an interrupted earlier session (cost unknown)"
                    % carried,
                )
                if m
            )
        spent += result.charged - carried
        if result.outcome == "not-run" and not stopped:
            stopped = result.message
        results.append(result)
        _print_case(result)
        save()  # an interrupted run keeps what it paid for
    done = {r.case for r in results}
    for n, r in prior.items():  # cases of the earlier run not named this time
        if n not in done:
            results.append(r)
            _print_case(r, kept=True)
    if prior:
        save()

    if args.dry_run:
        ready = sum(r.outcome == READY for r in results)
        print("ready: %d/%d; no session run; output %s" % (ready, len(results), out_dir))
        return 0 if ready == len(results) else 1
    summary = rates(results)
    print(
        "caught: strict %d/%d (%s), loose %d/%d (%s); cost $%.2f%s; output %s"
        % (
            summary["strict"],
            summary["scored"],
            _pct(summary["strict_rate"]),
            summary["loose"],
            summary["scored"],
            _pct(summary["loose_rate"]),
            summary["cost"],
            (
                " (counted against the budget $%.2f)" % summary["charged"]
                if summary["charged"] != summary["cost"]
                else ""
            ),
            out_dir,
        )
    )
    ok = all(r.outcome in ("caught-strict", "caught-loose", "missed") for r in results)
    return 0 if ok else 1


def _interrupted(name: str, cap: float) -> Result:
    """Ctrl-C. During the session its cap counts, since what it spent is never reported."""
    if not cap:
        return Result(name, "interrupted", message="interrupted before its session")
    return Result(
        name,
        "interrupted",
        charged=cap,
        message="interrupted during its session; cost unknown, the session cap $%.2f counts" % cap,
    )


def _print_case(r: Result, kept: bool = False) -> None:
    message = "; ".join(m for m in (r.message, "kept from the earlier run" if kept else "") if m)
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
            message,
        ),
        flush=True,
    )


def _write_results(out_dir: str, args, results: List[Result]) -> None:
    """revali's own atomic write: a temporary file renamed over results.json, the rename
    retried while Windows refuses it to a reader, the temporary file removed on failure. A
    kill at any point leaves the previous or the new file whole: --resume reads nothing else."""
    write_json_atomic(
        os.path.join(out_dir, "results.json"),
        {
            "revali": VERSION,
            "prompt_version": PROMPT_VERSION,
            "model": args.model,
            "dry_run": args.dry_run,
            "cases_dirs": args.cases_dir,
            "budget_usd": args.budget_usd,
            "session_budget_usd": args.session_budget_usd,
            "line_margin": args.line_margin,
            "summary": rates(results),
            "cases": [asdict(r) for r in results],
        },
    )


if __name__ == "__main__":
    sys.exit(main())
