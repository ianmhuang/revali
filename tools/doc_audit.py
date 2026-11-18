"""Documentation audit: one read-only session per document of the documentation graph, each
asked where the document disagrees with the code.

The documents are every Markdown page reachable by links from `[review] doc_roots` of the
repository the command runs in, plus the `.toml` files under `templates/` among them. Each
session reads the repository through revali's engine for the review role with no edits and
no shell, and answers in tools/doc_audit.schema.json: findings with a severity (high,
medium, low), the line, the statement, what the code does instead and the evidence.
docs/releasing.md describes the audit and where it sits in a release.

    python tools/doc_audit.py --budget-usd TOTAL [--doc-usd N] [--model M] [--out DIR]
                              [--only DOC ...]
    python tools/doc_audit.py --dry-run [--out DIR] [--only DOC ...]
    python tools/doc_audit.py --resume DIR --budget-usd TOTAL [--doc-usd N] [--model M]

Exit 2 when an audited document has a high or medium finding; else 1 when a document failed
or was not run (or the command could not start); else 0. --dry-run writes the prompts and
starts no session. --resume runs again the documents in DIR that are not done; the cost
recorded in DIR counts against --budget-usd.
"""

import argparse
import json
import math
import os
import string
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from typing import List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from revali import VERSION, docgraph, engines, gitops, models, procs  # noqa: E402
from revali.config import ConfigError, load_project_config, load_user_config  # noqa: E402
from revali.engines import EngineRequest  # noqa: E402
from revali.preflight import Stop  # noqa: E402
from revali.state import write_json_atomic  # noqa: E402

PROMPT = os.path.join(HERE, "doc_audit_prompt.txt")
SCHEMA = os.path.join(HERE, "doc_audit.schema.json")


def _schema_parts():
    """The answer's allowed keys (`properties`), a finding's allowed and required keys, and
    the severities, from the schema file (one source)."""
    with open(SCHEMA, encoding="utf-8") as fh:
        schema = json.load(fh)
    item = schema["properties"]["findings"]["items"]
    return (
        tuple(schema["properties"]),
        tuple(item["properties"]),
        tuple(item["required"]),
        tuple(item["properties"]["severity"]["enum"]),
    )


ANSWER_KEYS, FINDING_ALLOWED, FINDING_KEYS, SEVERITIES = _schema_parts()
BLOCKING = ("high", "medium")
FINDING_TEXT = tuple(k for k in FINDING_KEYS if k not in ("severity", "line"))
READY, DONE, ERROR, NOT_RUN, STARTED = "ready", "done", "error", "not-run", "started"
RESULTS = "results.json"
SUMMARY = "summary.md"


@dataclass
class Result:
    doc: str
    status: str
    model: str = ""
    cost: float = 0.0  # what the session reported
    charged: float = 0.0  # what counts against the budget (the cap when the cost is unknown)
    duration_s: float = 0.0
    message: str = ""
    summary: str = ""
    findings: List[dict] = field(default_factory=list)


@dataclass
class Settings:
    model: str
    fallback: str
    reason: str
    effort: str
    cap: float
    timeout_s: int


def slug(doc: str) -> str:
    return doc.replace("/", "__")


def documents(repo: str, roots: List[str]) -> List[str]:
    """Markdown pages and `templates/*.toml` of the graph from `roots`, in graph order."""
    graph = docgraph.build(repo, roots)
    found = []
    for rel in graph.nodes:
        if not os.path.isfile(os.path.join(repo, rel)):
            continue
        low = rel.lower()
        if low.endswith(".md") or (low.startswith("templates/") and low.endswith(".toml")):
            found.append(rel)
    return found


def numbered(text: str) -> str:
    """Split on "\\n" only, as docgraph does, so a line number is the editor's."""
    lines = [line.rstrip("\r") for line in text.split("\n")]
    if lines and lines[-1] == "":
        lines.pop()  # the final newline ends the last line; it does not start another
    return "\n".join("%4d| %s" % (n, line) for n, line in enumerate(lines, 1))


def build_prompt(repo: str, doc: str, docs: List[str], roots: List[str]) -> str:
    with open(os.path.join(repo, doc), encoding="utf-8", errors="replace") as fh:
        text = fh.read()
    with open(PROMPT, encoding="utf-8") as fh:
        template = string.Template(fh.read())
    return template.safe_substitute(
        doc=doc,
        roots=", ".join("`%s`" % r for r in roots),
        documents="\n".join("- `%s`" % d for d in docs),
        text=numbered(text),
    )


def schema_problem(data) -> str:
    """ "" when `data` matches the schema; else what does not."""
    if not isinstance(data, dict):
        return "the answer is not an object"
    extra = sorted(set(data) - set(ANSWER_KEYS))
    if extra:
        return "unknown key(s): %s" % ", ".join(extra)
    if not isinstance(data.get("summary"), str):
        return "no summary"
    found = data.get("findings")
    if not isinstance(found, list):
        return "findings is not a list"
    for n, f in enumerate(found, 1):
        if not isinstance(f, dict):
            return "finding %d is not an object" % n
        extra = sorted(set(f) - set(FINDING_ALLOWED))
        if extra:
            return "finding %d: unknown key(s): %s" % (n, ", ".join(extra))
        if f.get("severity") not in SEVERITIES:
            return "finding %d: severity %r" % (n, f.get("severity"))
        line = f.get("line")
        if not isinstance(line, int) or isinstance(line, bool) or line < 1:
            return "finding %d: line %r" % (n, line)
        for key in FINDING_TEXT:
            if not isinstance(f.get(key), str) or not f[key].strip():
                return "finding %d: no %s" % (n, key)
    return ""


def settings(cfg, args) -> Tuple[engines.Engine, Settings]:
    engine = engines.for_role(cfg, "review")
    review = cfg.review
    chosen = models.resolve(
        models.REVIEWER,
        args.model or review.model,
        review.fallback_model,
        "",
        engine.tiers,
        cfg.foreign_ladders(engine.name),
    )
    return engine, Settings(
        model=chosen.model,
        fallback=chosen.fallback,
        reason=chosen.reason,
        effort=review.effort,
        cap=args.doc_usd if args.doc_usd is not None else review.budget_usd,
        timeout_s=review.timeout_min * 60,
    )


def audit_one(engine, s: Settings, repo: str, doc: str, prompt: str, out: str) -> Result:
    started = time.monotonic()
    try:
        rr = engine.run(
            EngineRequest(
                role="auditor",
                prompt=prompt,
                schema_text=_read(SCHEMA),
                model=s.model,
                fallback_model=s.fallback,
                effort=s.effort,
                budget_usd=s.cap,
                timeout_s=s.timeout_s,
                cwd=repo,
                raw_path=os.path.join(out, slug(doc) + ".raw.json"),
                read_only=True,
            )
        )
    except Stop as stop:
        result = Result(doc, ERROR, cost=stop.cost, charged=stop.cost, message=stop.message)
        if not stop.cost:  # a timeout or unreadable output: it may have spent up to its cap
            result.charged = s.cap
            result.message += " (cost unknown; the cap $%.2f counts against the budget)" % s.cap
    else:
        result = Result(doc, DONE, model=rr.model_actual or rr.model_requested, cost=rr.cost)
        result.charged = rr.cost
        problem = schema_problem(rr.data)
        if problem:
            result.status = ERROR
            result.message = "the answer does not match the schema: %s" % problem
        else:
            result.summary = rr.data["summary"]
            order = {sev: i for i, sev in enumerate(SEVERITIES)}
            result.findings = sorted(
                rr.data["findings"], key=lambda f: (order[f["severity"]], f["line"])
            )
    result.duration_s = round(time.monotonic() - started, 1)
    return result


def counts(result: Result) -> dict:
    return {sev: sum(1 for f in result.findings if f["severity"] == sev) for sev in SEVERITIES}


def verdict(results: List[Result]) -> Tuple[int, str]:
    done = [r for r in results if r.status == DONE]
    blocking = sum(counts(r)[sev] for r in done for sev in BLOCKING)
    missing = [r for r in results if r.status != DONE]
    if blocking:
        return 2, "FAIL: %d high or medium finding(s)" % blocking
    if missing:
        return 1, "INCOMPLETE: %d document(s) not audited" % len(missing)
    return 0, "PASS: no high or medium finding"


def flat(text: str) -> str:
    """One line, so a multi-line quote does not break a list item."""
    return " ".join(str(text).split())


def report(result: Result) -> str:
    lines = [
        "# Audit of `%s`" % result.doc,
        "",
        "status: %s" % result.status,
        "model: %s" % (result.model or "-"),
        "cost: $%.2f" % result.cost,
        "duration: %.0f s" % result.duration_s,
    ]
    if result.message:
        lines.append("message: %s" % result.message)
    if result.summary:
        lines += ["", result.summary]
    if result.status == DONE:
        lines += ["", "## Findings", ""]
        if not result.findings:
            lines.append("none")
        for f in result.findings:
            lines += [
                "- **%s**, line %d: %s" % (f["severity"], f["line"], flat(f["quote"])),
                "  - actual: %s" % flat(f["actual"]),
                "  - evidence: `%s`" % flat(f["evidence"]),
                "  - suggestion: %s" % flat(f["suggestion"]),
            ]
    return "\n".join(lines) + "\n"


def summary(results: List[Result], spent: float, budget: float) -> str:
    _code, line = verdict(results)
    out = [
        "# Documentation audit",
        "",
        "result: %s" % line,
        "spent: $%.2f of $%.2f" % (spent, budget),
        "",
        "| document | status | high | medium | low | cost |",
        "|---|---|---|---|---|---|",
    ]
    for r in results:
        c = counts(r)
        out.append(
            "| `%s` | %s | %d | %d | %d | $%.2f |"
            % (r.doc, r.status, c["high"], c["medium"], c["low"], r.cost)
        )
    out += ["", "## High and medium findings", ""]
    blocking = [(r, f) for r in results for f in r.findings if f["severity"] in BLOCKING]
    if not blocking:
        out.append("none")
    for r, f in blocking:
        out.append(
            "- %s:%d **%s**: %s; actual: %s (`%s`)"
            % (
                r.doc,
                f["line"],
                f["severity"],
                flat(f["quote"]),
                flat(f["actual"]),
                flat(f["evidence"]),
            )
        )
    out += ["", "## Not audited", ""]
    missing = [r for r in results if r.status != DONE]
    if not missing:
        out.append("none")
    for r in missing:
        out.append("- `%s`: %s%s" % (r.doc, r.status, ": " + r.message if r.message else ""))
    return "\n".join(out) + "\n"


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _write(path: str, text: str) -> None:
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)


def load_results(out: str):
    """(data, [Result]) from `out`, or (None, why it cannot be resumed)."""
    path = os.path.join(out, RESULTS)
    if not os.path.isfile(path):
        return None, "no %s in %s; nothing to resume" % (RESULTS, out)
    try:
        data = json.loads(_read(path))
        results = [Result(**r) for r in data["results"]]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return None, "cannot read %s: %s" % (path, exc)
    if data.get("dry_run"):
        return None, "%s is from a dry run; nothing to resume" % path
    return (data, results), ""


def _held_run(out: str) -> str:
    """Why `out` cannot take a new run ("" when it can): it holds a paid run. A dry run's
    directory holds only prompts, so a run may start there."""
    path = os.path.join(out, RESULTS)
    if not os.path.isfile(path):
        return ""
    try:
        if json.loads(_read(path)).get("dry_run"):
            return ""
    except (OSError, ValueError, AttributeError):
        pass
    return "%s already holds a run; continue it with --resume %s, or pick another --out" % (
        out,
        out,
    )


class Run:
    def __init__(self, out, repo, roots, docs, selected, dry_run, budget, s: Settings):
        self.out, self.repo, self.roots = out, repo, roots
        self.docs, self.selected, self.dry_run = docs, selected, dry_run
        self.budget, self.settings = budget, s

    def save(self, results: List[Result], spent: float) -> None:
        s = self.settings
        write_json_atomic(
            os.path.join(self.out, RESULTS),
            {
                "tool": "doc_audit",
                "revali": VERSION,
                "dry_run": self.dry_run,
                "repo": self.repo,
                "roots": self.roots,
                "documents": self.docs,
                "selected": self.selected,
                "model": s.model,
                "model_reason": s.reason,
                "doc_usd": s.cap,
                "budget_usd": self.budget,
                "spent_usd": round(spent, 4),
                "results": [asdict(r) for r in results],
            },
        )


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="audit the documentation against the code")
    parser.add_argument("--budget-usd", type=float, help="total; required unless --dry-run")
    parser.add_argument("--doc-usd", type=float, help="cap per document (default: [review])")
    parser.add_argument("--model", default="", help="model (default: [review] model)")
    parser.add_argument("--out", default="", help="output directory (default: a new temp dir)")
    parser.add_argument("--only", nargs="+", default=[], metavar="DOC", help="just these")
    parser.add_argument("--resume", default="", metavar="DIR", help="continue a run in DIR")
    parser.add_argument("--dry-run", action="store_true", help="write the prompts, run nothing")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:  # argparse's 2 would read as "findings"
        return 0 if exc.code == 0 else 1
    try:
        return _main(args)
    except (ConfigError, Stop, OSError, ValueError) as exc:
        text = "; ".join(exc.problems) if isinstance(exc, ConfigError) else str(exc)
        print("doc_audit: %s" % getattr(exc, "message", text))
        return 1


def _main(args) -> int:
    if not args.dry_run and args.budget_usd is None:
        print("doc_audit: --budget-usd is required unless --dry-run")
        return 1
    for flag, value in (("--budget-usd", args.budget_usd), ("--doc-usd", args.doc_usd)):
        if value is not None and not (math.isfinite(value) and value > 0):
            print("doc_audit: %s must be a number above 0, not %s" % (flag, value))
            return 1
    if args.resume and (args.dry_run or args.out or args.only):
        print("doc_audit: --resume continues DIR as recorded; no --dry-run, --out or --only")
        return 1
    repo = gitops.repo_root(os.getcwd())
    if not repo:
        print("doc_audit: not inside a git repository: %s" % os.getcwd())
        return 1
    cfg = load_project_config(repo, load_user_config())
    roots = list(cfg.review.doc_roots)
    docs = documents(repo, roots)
    engine = engines.for_role(cfg, "review")
    if not args.dry_run:
        try:  # a missing executable fails every session alike; stop before anything is made
            procs.resolve(engine.name)
        except procs.ExeNotFound as exc:
            print("doc_audit: %s" % exc)
            return 1

    record: dict = {}  # document -> its latest Result; saved in selection order
    earlier = 0.0
    recorded_reason = None
    if args.resume:
        loaded, problem = load_results(args.resume)
        if loaded is None:
            print("doc_audit: %s" % problem)
            return 1
        data, prior = loaded
        recorded = data.get("repo") or ""
        if os.path.normcase(os.path.abspath(recorded)) != os.path.normcase(os.path.abspath(repo)):
            print("doc_audit: %s audited %s, not %s" % (args.resume, recorded, repo))
            return 1
        if data.get("documents") != docs:
            added = sorted(set(docs) - set(data.get("documents") or []))
            gone = sorted(set(data.get("documents") or []) - set(docs))
            print(
                "doc_audit: the document list changed since %s (added: %s; gone: %s); "
                "start a new run" % (args.resume, ", ".join(added) or "-", ", ".join(gone) or "-")
            )
            return 1
        if args.model and data.get("model") and args.model != data["model"]:
            print("doc_audit: %s ran with model %s" % (args.resume, data["model"]))
            return 1
        if not args.model:  # the recorded model, and how it was chosen
            args.model = data.get("model", "")
            recorded_reason = data.get("model_reason", "")
        if args.doc_usd is None:  # a --doc-usd given here replaces the recorded cap
            args.doc_usd = data.get("doc_usd") or None
        selected = list(data.get("selected") or docs)
        # every earlier record stays until replaced, so an interrupted resume keeps the
        # done documents that come after the one it was in
        record = {r.doc: r for r in prior}
        earlier = float(data.get("spent_usd") or 0.0)
        out = args.resume
    else:
        unknown = [d for d in args.only if d not in docs]
        if unknown:
            print("doc_audit: not among the documents: %s" % ", ".join(unknown))
            print("documents:\n" + "\n".join("  " + d for d in docs))
            return 1
        wanted = set(args.only)
        selected = [d for d in docs if d in wanted] if args.only else docs  # once, in list order
        held = _held_run(args.out) if args.out else ""
        if held:
            print("doc_audit: %s" % held)
            return 1
        out = args.out or tempfile.mkdtemp(prefix="revali doc audit ")
        os.makedirs(out, exist_ok=True)

    engine, s = settings(cfg, args)
    if recorded_reason is not None:
        s.reason = recorded_reason
    budget = args.budget_usd or 0.0
    run = Run(out, repo, roots, docs, selected, args.dry_run, budget, s)
    print("documents (%d): %s" % (len(selected), ", ".join(selected)))
    print("output: %s" % out)
    if not args.dry_run:
        print(
            "model %s%s, cap $%.2f per document, budget $%.2f (earlier $%.2f)"
            % (s.model, " (%s)" % s.reason if s.reason else "", s.cap, budget, earlier)
        )

    def results() -> List[Result]:
        return [record[d] for d in selected if d in record]

    spent = earlier
    stopped = False
    for doc in selected:
        if doc in record and record[doc].status == DONE:
            continue
        prompt = build_prompt(repo, doc, docs, roots)
        _write(os.path.join(out, slug(doc) + ".prompt.txt"), prompt)
        if args.dry_run:
            record[doc] = Result(doc, READY, message="prompt %d lines" % prompt.count("\n"))
            print("  %s: ready" % doc)
            continue
        if stopped or spent + s.cap > budget:
            stopped = True
            record[doc] = Result(
                doc,
                NOT_RUN,
                message="$%.2f spent + the cap $%.2f would pass the budget $%.2f"
                % (spent, s.cap, budget),
            )
            continue
        # recorded first: if the run ends inside the session, its cap counts on --resume
        record[doc] = Result(doc, STARTED, charged=s.cap)
        run.save(results(), spent + s.cap)
        result = audit_one(engine, s, repo, doc, prompt, out)
        spent += result.charged
        record[doc] = result
        _write(os.path.join(out, slug(doc) + ".md"), report(result))
        c = counts(result)
        print(
            "  %s: %s, %d high, %d medium, %d low, $%.2f%s"
            % (
                doc,
                result.status,
                c["high"],
                c["medium"],
                c["low"],
                result.cost,
                "; " + result.message if result.message else "",
            )
        )
        run.save(results(), spent)
    final = results()
    run.save(final, spent)
    if args.dry_run:
        return 0
    _write(os.path.join(out, SUMMARY), summary(final, spent, budget))
    code, line = verdict(final)
    print("%s; spent $%.2f; see %s" % (line, spent, os.path.join(out, SUMMARY)))
    return code


if __name__ == "__main__":
    sys.exit(main())
