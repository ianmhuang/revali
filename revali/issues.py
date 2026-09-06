"""GitHub issues for pre-existing bugs.

After a validation FAIL, when the diagnosis says a reviewer test fails because code the base
branch already had is wrong (`cause: code`, `introduced_by: base`), one issue is opened for
that validation with the author's gh identity. At `revali merge`, every such issue that a
branch commit says it fixes gets a comment naming the merged PR and the commit. All via gh.
"""

import os
import re
import string
from dataclasses import dataclass
from typing import List, Optional, Tuple

from revali import VERSION, gitops
from revali.preflight import Context
from revali.secretscan import scan_text
from revali.state import RunLog, State, now_iso, read_text, write_text

LABEL = "bug"  # attached only when the repository has it; never created
WITHHELD = "(withheld: non-private repository)"
OUTPUT_LINES = 40
# `Fixes #12`, `closes #3`, `Resolved #7`: one of GitHub's closing keywords, whitespace, `#n`.
# Not `fix#12` or `fixes: #12`, which GitHub does not close on. Repository-qualified references
# (`owner/repo#n`) and issue URLs are not recognised: there is no slug here to check them with.
FIX_REF = re.compile(r"\b(?:fix(?:e[sd])?|close[sd]?|resolve[sd]?)\s+#(\d+)\b", re.I)


@dataclass
class IssueRef:
    number: int
    url: str
    existing: bool = False  # opened by an earlier validation of this branch
    also: Tuple[int, ...] = ()  # older issues that, with this one, name the failing tests

    def line(self) -> str:
        """`issue: #12 <url>`, with a note when it was already open (and which other issues
        share the coverage)."""
        note = ""
        if self.existing:
            others = ", ".join("#%d" % n for n in self.also)
            note = " (already open%s)" % (", with " + others if others else "")
        return "issue: #%d%s %s" % (self.number, note, self.url)


def base_failures(diagnosis: Optional[dict]) -> List[dict]:
    """The diagnosed failures that mean a pre-existing bug: `cause: code` and
    `introduced_by: base`, each with a non-empty test id."""
    if not diagnosis:
        return []
    return [
        f
        for f in diagnosis.get("failures", [])
        if isinstance(f, dict)
        and f.get("cause") == "code"
        and f.get("introduced_by") == "base"
        and str(f.get("test", "")).strip()
    ]


def test_ids(failures: List[dict]) -> List[str]:
    return sorted({str(f["test"]).strip() for f in failures})


def title(tests: List[str], base: str) -> str:
    if len(tests) == 1:
        return "Pre-existing: %s fails on %s" % (tests[0], base)
    return "Pre-existing: %s and %d more fail on %s" % (tests[0], len(tests) - 1, base)


def already_open(state: State, tests: List[str]) -> List[dict]:
    """The issues of this branch that together name every one of `tests`, newest first: the
    newest single issue naming them all when there is one, else the newest issues whose union
    does (each contributing at least one test not named by a newer one), else nothing."""
    wanted = set(tests)
    for issue in reversed(state.issues):
        if wanted <= set(issue.get("tests", [])):
            return [issue]
    out: List[dict] = []
    left = set(wanted)
    for issue in reversed(state.issues):
        named = left & set(issue.get("tests", []))
        if named:
            out.append(issue)
            left -= named
            if not left:
                return out
    return []


def _covers_for(test: str, test_files: List[str], reviewer_tests: List[dict]) -> List[str]:
    """AC ids the reviewer said the file behind `test` covers. A test id names the file
    either as a path (`tests/test_x.py::T::m`) or as a dotted module
    (`m (tests.test_x.T)`)."""
    out: List[str] = []
    for path in test_files:
        module = path[:-3] if path.endswith(".py") else path
        module = module.replace("/", ".")
        if path in test or module in test:
            for t in reviewer_tests:
                if t.get("path") == path:
                    out.extend(str(ac) for ac in t.get("covers", []) or [])
    return out


def _acceptance(ctx: Context, state: State, tests: List[str]) -> str:
    reviewer_tests = state.rounds[-1].get("data", {}).get("tests", []) if state.rounds else []
    ids: List[str] = []
    for test in tests:
        for ac in _covers_for(test, list(state.test_files), reviewer_tests):
            if ac not in ids:
                ids.append(ac)
    text = dict(ctx.doc.acs)
    lines = ["- %s: %s" % (ac, text.get(ac, "(not in change.md)")) for ac in ids]
    return "\n".join(lines) or "(no acceptance criterion could be matched to the failing tests)"


def _evidence(outcome, base: str, withhold: bool) -> str:
    rerun = outcome.base_rerun
    lines = []
    branch_step = outcome.report.failed if outcome.report else None
    if branch_step is not None:
        lines.append("On the branch: `%s` exit %d." % (branch_step.name, branch_step.returncode))
    if rerun is not None:
        lines.append("Base branch `%s`, %s." % (base, rerun.one_line()))
    if withhold:
        lines += ["", "Output: " + WITHHELD]
    elif rerun is not None and rerun.available:
        from revali.runners import tail

        lines += [
            "",
            "Output on base (last %d lines):" % OUTPUT_LINES,
            "",
            "```",
            tail(rerun.new_test.text, OUTPUT_LINES) or "(empty)",
            "```",
        ]
    return "\n".join(lines)


def _diagnosis(outcome, failures: List[dict], withhold: bool) -> str:
    d = outcome.diagnosis
    lines = [
        "cause **%s**, introduced by **%s** (%s%s)"
        % (
            d.get("cause"),
            d.get("introduced_by"),
            outcome.model_actual or "?",
            ", fallback" if outcome.fallback else "",
        ),
        "",
    ]
    if withhold:
        lines.append(WITHHELD)
        return "\n".join(lines)
    lines += [str(d.get("summary", "")).strip(), ""]
    lines += ["- `%s`: %s" % (f.get("test"), f.get("note", "")) for f in failures]
    return "\n".join(lines)


def render_body(ctx: Context, state: State, outcome, failures: List[dict], withhold: bool) -> str:
    tests = test_ids(failures)
    values = {
        "pr": state.pr_number,
        "pr_url": state.pr_url or "#%d" % state.pr_number,
        "round": max(1, len(state.rounds)),
        "validation": outcome.number,
        "version": VERSION,
        "branch": ctx.branch,
        "base": ctx.base,
        "base_sha": (outcome.base_rerun.sha if outcome.base_rerun else ctx.base_sha)[:10],
        "tests": "\n".join("- `%s`" % t for t in tests),
        "evidence": _evidence(outcome, ctx.base, withhold),
        "diagnosis": _diagnosis(outcome, failures, withhold),
        "acceptance": _acceptance(ctx, state, tests),
        "fix": WITHHELD if withhold else str(outcome.diagnosis.get("recommendation", "")).strip(),
    }
    return string.Template(read_text(ctx.issue_template)).safe_substitute(values)


def maybe_open(
    ctx: Context, state: State, rdir: str, outcome, log: Optional[RunLog]
) -> Optional[IssueRef]:
    """Open the issue for a failed validation when its diagnosis warrants one. Every failure
    here is a warning in the log: the validation's result stands whatever happens to the
    issue."""
    cfg = ctx.cfg.validate
    if ctx.dry_run or not cfg.issue_on or not outcome.diagnosis or not state.pr_number:
        return None
    failures = base_failures(outcome.diagnosis)
    if not failures:
        return None
    tests = test_ids(failures)
    known = already_open(state, tests)
    if known:
        numbers = [int(i.get("number", 0)) for i in known]
        if log:
            log.stage(
                "validate",
                "pre-existing failure already has issue %s (%s)"
                % (", ".join("#%d" % n for n in numbers), ", ".join(tests)),
            )
        return IssueRef(
            numbers[0], str(known[0].get("url", "")), existing=True, also=tuple(numbers[1:])
        )

    from revali.pr import is_public

    withhold = is_public(ctx)
    body = render_body(ctx, state, outcome, failures, withhold)
    hits = scan_text(body, label="issue")
    if hits:
        body = (
            "revali withheld this issue's body: it looked like it contained a credential (%s). "
            "The full text is in %s/ on the author's machine."
            % (", ".join(sorted({h.pattern for h in hits})), ctx.cfg.paths.state_dir)
        )
        if log:
            log.stage("validate", "issue body withheld: possible credential")
    body_path = os.path.join(ctx.logs, "issue-%d.md" % outcome.number)
    write_text(body_path, body)

    labels: List[str] = []
    try:
        if LABEL in gitops.gh_labels(ctx.repo_root, log.detail if log else None):
            labels.append(LABEL)
        elif log:
            log.stage(
                "validate", "label %r not in this repository; issue opened without it" % LABEL
            )
    except gitops.GhError as exc:
        if log:
            log.stage("validate", "warning: %s; issue opened without a label" % exc)
    assignee = ctx.login if cfg.issue_assignee == "author" else ""

    state.pending_effect = "issue"
    state.save(rdir)
    try:
        number, url = gitops.gh_issue_create(
            title(tests, ctx.base),
            body_path,
            labels,
            assignee,
            ctx.repo_root,
            log.detail if log else None,
        )
    except gitops.GhError as exc:
        state.pending_effect = ""
        state.save(rdir)
        if log:
            log.stage("validate", "warning: could not open the issue: %s" % str(exc)[:300])
        return None
    state.pending_effect = ""
    d = outcome.diagnosis
    state.issues.append(
        {
            "number": number,
            "url": url,
            "validation": outcome.number,
            "round": max(1, len(state.rounds)),
            "tests": tests,
            "cause": d.get("cause", ""),
            "summary": str(d.get("summary", "")).strip(),
            "recommendation": str(d.get("recommendation", "")).strip(),
            "withhold": withhold,
            "at": now_iso(),
        }
    )
    state.save(rdir)
    if log:
        log.stage(
            "validate",
            "opened issue #%d for the pre-existing failure (%s)%s: %s"
            % (number, ", ".join(tests), " assigned to " + assignee if assignee else "", url),
        )
    return IssueRef(number, url)


# ---- merge ------------------------------------------------------------------


def referenced_issues(
    messages: List[Tuple[str, str]], issues: List[dict]
) -> List[Tuple[dict, str, str]]:
    """(issue, sha, subject) for every issue of the branch that a commit message closes
    with a `Fixes #n`-style keyword; the first such commit wins, oldest first."""
    numbers = {int(i.get("number", 0)): i for i in issues if int(i.get("number", 0))}
    out = []
    seen = set()
    for sha, message in messages:
        for m in FIX_REF.finditer(message):
            n = int(m.group(1))
            if n in numbers and n not in seen:
                seen.add(n)
                subject = message.strip().splitlines()[0] if message.strip() else ""
                out.append((numbers[n], sha, subject))
    return out


def merge_comment(issue: dict, pr_number: int, base: str, sha: str, subject: str) -> str:
    lines = [
        "Fixed by PR #%d, merged into `%s` (commit %s: %s)." % (pr_number, base, sha[:10], subject),
        "",
        "revali's diagnosis when the issue was opened: cause **%s**." % (issue.get("cause") or "?"),
    ]
    if issue.get("withhold"):
        lines += ["", "Suggested fix at the time: " + WITHHELD]
    elif issue.get("recommendation"):
        lines += ["", "Suggested fix at the time: %s" % issue["recommendation"]]
    return "\n".join(lines) + "\n"


def comment_after_merge(
    state: State, fixes: List[Tuple[dict, str, str]], root: str, logs_dir: str, log: RunLog
) -> None:
    """One comment per issue a branch commit says the merged PR fixes (`fixes` is what
    `referenced_issues` found before the merge)."""
    for issue, sha, subject in fixes:
        number = int(issue.get("number", 0))
        path = os.path.join(logs_dir, "issue-%d-merged.md" % number)
        write_text(path, merge_comment(issue, state.pr_number, state.base, sha, subject))
        res = gitops.gh_issue_comment(number, path, root, log.detail)
        if res.ok:
            log.stage("merge", "commented on issue #%d (fixed by %s)" % (number, sha[:10]))
        else:
            log.stage(
                "merge",
                "warning: could not comment on issue #%d: %s" % (number, res.text.strip()[:200]),
            )
