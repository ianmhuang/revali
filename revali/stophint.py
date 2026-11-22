"""The stop hint: a round whose findings are all low and all sit in the lines the author's
fix of the previous round changed. The run suggests answering them instead of paying for
another round. It never changes an outcome."""

import os
from typing import Callable, Dict, List, Optional, Tuple

from revali import gitops
from revali.procs import ProcError
from revali.state import State

MARGIN = 3  # lines around a hunk that still count as the fix

Note = Optional[Callable[[str], None]]


def _in_fix(finding: dict, ranges: Dict[str, List[Tuple[int, int]]]) -> bool:
    line = finding.get("line")
    if not isinstance(line, int) or line < 1:
        return False
    hunks = ranges.get(finding.get("file") or "")
    return bool(hunks) and any(a - MARGIN <= line <= b + MARGIN for a, b in hunks)


def derived_lows(state: State, repo_root: str, note: Note = None) -> List[dict]:
    """The last round's findings when that round is a derived-low round, else []. `note`
    gets one line when git cannot tell (a rebase, a missing commit)."""
    if len(state.rounds) < 2:
        return []
    prev, cur = state.rounds[-2], state.rounds[-1]
    data = cur.get("data") or {}
    findings = data.get("findings") or []
    if not findings or any(f.get("severity") != "low" for f in findings):
        return []
    if any(p.get("status") == "unresolved" for p in data.get("previous_findings") or []):
        return []
    old = prev.get("test_commit") or prev.get("head_sha") or ""
    new = cur.get("head_sha") or ""
    try:
        if not old or not new or not gitops.is_ancestor(old, new, repo_root):
            if note:
                note(
                    "no stop hint: round %d's tip %s is not an ancestor of round %d's head %s "
                    "(a rebase, or a commit that is gone)"
                    % (prev.get("round", 0), old[:10] or "-", cur.get("round", 0), new[:10] or "-")
                )
            return []
        ranges = gitops.changed_line_ranges(old, new, repo_root)
    except (gitops.GitError, ProcError, OSError) as exc:
        if note:
            note("no stop hint: %s" % str(exc).strip().splitlines()[0])
        return []
    return list(findings) if all(_in_fix(f, ranges) for f in findings) else []


def hint_line(findings: List[dict], round_no: int, rdir: str, ready: bool) -> str:
    """`ready`: READY TO MERGE, where the branch can merge as it is. After CHANGES_REQUESTED
    another round runs anyway, so the hint is about how to answer, not whether to stop."""
    response = os.path.join(rdir, "response-%d.md" % round_no)
    head = "hint: the %d low finding(s) of round %d all sit in lines the last fix changed; " % (
        len(findings),
        round_no,
    )
    if ready:
        return head + (
            "consider answering them in %s (wontfix / follow-up) instead of another round"
            % response
        )
    return head + (
        "consider answering them in %s (wontfix / follow-up) rather than changing those lines "
        "again; the next round still runs" % response
    )


def short(findings: List[dict], ready: bool) -> str:
    """The stage message's form: what `wait` and `status` print."""
    head = "hint: %d low finding(s) in lines the last fix changed, " % len(findings)
    return head + ("consider stopping" if ready else "consider answering, not changing them")
