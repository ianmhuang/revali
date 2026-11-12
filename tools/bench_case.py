"""Build a reviewer benchmark case from an archived revali round (bench/README.md).

    python tools/bench_case.py <archive pr dir> --round N --finding FID --out DIR
                               [--repo URL_OR_PATH] [--request TEXT] [--config REVALI_TOML]
    python tools/bench_case.py --list <archive repo dir>

`<archive pr dir>` is one PR's archive, `~/.revali/archive/<owner>__<repo>/<pr>-<branch>/`.
The case gets the round's head commit, the merge base of that head with the base branch,
`refs/pull/<pr>/head` to fetch, the change.md the reviewer saw (cut from the round's
prompt), and the finding's file, line and first sentence as the expected bug. Only a
medium or high correctness or security finding with a line, which the next round marked
resolved, makes a case. `--request` replaces the body of the `## Request` section (a case
committed to a public repository carries a paraphrase, not the user's words). `--config`
puts a revali.toml in the case, which the benchmark writes into the clone: most projects keep
theirs out of git, so a clone has none.

`--list` prints the candidates of one repository's archive in the order the selection rule
takes them: PRs newest first, one per PR, the first qualifying finding of the earliest round
that has one.

Exit 0 on success, 1 when the case is refused (the reason is printed).
"""

import argparse
import json
import os
import re
import sys
import tempfile
from typing import List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import review_bench as bench  # noqa: E402

PROMPT_START = "## The author's description (change.md)\n\n"
PROMPT_END = "\n\n## The diff"
STRONG = ("medium", "high")
KINDS = ("correctness", "security")
DESCRIPTION_MAX = 200


class BuildError(Exception):
    """An archived round or finding that does not make a case."""


def _load(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError as exc:
        raise BuildError("no %s" % path) from exc
    except (OSError, ValueError) as exc:
        raise BuildError("cannot read %s: %s" % (path, exc)) from exc
    if not isinstance(data, dict):
        raise BuildError("%s is not a JSON object" % path)
    return data


def review(archive: str, round_no: int) -> dict:
    """review-N.json; BuildError when its `data` is there but not an object."""
    name = "review-%d.json" % round_no
    data = _load(os.path.join(archive, name))
    if not isinstance(data.get("data") or {}, dict):
        raise BuildError("%s: data is not a JSON object" % name)
    return data


def findings(data: dict) -> list:
    """The findings of one review-N.json; a value that is not a list holds none."""
    found = (data.get("data") or {}).get("findings")
    return [f for f in found if isinstance(f, dict)] if isinstance(found, list) else []


def qualifies(f: dict) -> bool:
    line, file = f.get("line"), f.get("file")
    return (
        isinstance(file, str)
        and bool(file.strip())
        and f.get("severity") in STRONG
        and f.get("kind") in KINDS
        and isinstance(line, int)
        and not isinstance(line, bool)
        and line > 0
    )


def resolved_next(archive: str, round_no: int, fid: str) -> bool:
    """Whether review round_no + 1 lists the finding as resolved."""
    if not os.path.isfile(os.path.join(archive, "review-%d.json" % (round_no + 1))):
        return False
    prev = (review(archive, round_no + 1).get("data") or {}).get("previous_findings") or []
    if not isinstance(prev, list):
        return False
    return any(
        isinstance(p, dict) and p.get("id") == fid and p.get("status") == "resolved" for p in prev
    )


def rounds(archive: str) -> List[int]:
    found = []
    for name in os.listdir(archive):
        m = re.fullmatch(r"review-(\d+)\.json", name)
        if m:
            found.append(int(m.group(1)))
    return sorted(found)


def first_candidate(archive: str) -> Optional[Tuple[int, str]]:
    """(round, finding id) of the first qualifying, resolved finding of the earliest round."""
    for n in rounds(archive):
        try:
            for f in findings(review(archive, n)):
                if qualifies(f) and resolved_next(archive, n, f.get("id")):
                    return n, f["id"]
        except BuildError as exc:  # the order changes: say so
            print("skipped round %d of %s: %s" % (n, archive, exc), file=sys.stderr)
    return None


def candidates(repo_archive: str) -> List[Tuple[int, str, int, str]]:
    """(pr, archive dir, round, finding id) per PR, newest PR first."""
    prs = []
    for name in os.listdir(repo_archive):
        m = re.match(r"(\d+)-", name)
        path = os.path.join(repo_archive, name)
        if m and os.path.isdir(path):
            prs.append((int(m.group(1)), path))
    out = []
    for pr, path in sorted(prs, reverse=True):
        hit = first_candidate(path)
        if hit:
            out.append((pr, path, hit[0], hit[1]))
    return out


def cut_change_md(prompt: str) -> str:
    start = prompt.find(PROMPT_START)
    end = prompt.find(PROMPT_END, start + 1)
    if start < 0 or end < 0:
        raise BuildError("the round's prompt has no author's description section")
    return prompt[start + len(PROMPT_START) : end].strip() + "\n"


def replace_request(text: str, body: str) -> str:
    if not body.strip():
        raise BuildError("--request is empty")
    m = re.search(r"^## Request\n(.*?)(?=^## |\Z)", text, re.S | re.M)
    if not m:
        raise BuildError("the round's change.md has no ## Request section")
    tail = "\n\n" if m.end(1) < len(text) else "\n"
    return text[: m.start(1)] + body.strip() + tail + text[m.end(1) :]


def first_sentence(text: str) -> str:
    one = " ".join(str(text).split())
    # a sentence ends at a stop followed by a capital or a code span, not at "i.e." or "e.g."
    one = re.split(r"(?<!i\.e\.)(?<!e\.g\.)(?<=[.!?])\s+(?=[A-Z`\"(\[])", one)[0].rstrip(".")
    return one if len(one) <= DESCRIPTION_MAX else one[: DESCRIPTION_MAX - 3] + "..."


def merge_base(repo: str, fetch: List[str], head: str, base_branch: str) -> str:
    """Clone `repo` into a temporary directory and return merge-base(head, base branch);
    BuildError when the head is not reachable through the fetched refs."""
    tmp = tempfile.mkdtemp(prefix="revali bench case ")
    try:
        clone = os.path.join(tmp, "repo")
        try:
            bench._git(["clone", "--quiet", "--no-checkout", repo, clone], tmp)
            for ref in fetch:
                bench._git(["fetch", "--quiet", "origin", ref], clone)
        except bench.CaseError as exc:
            raise BuildError(str(exc)) from exc
        try:
            bench._git(["cat-file", "-e", "%s^{commit}" % head], clone)
        except bench.CaseError as exc:
            raise BuildError(
                "the head %s is not reachable from %s" % (head, ", ".join(fetch))
            ) from exc
        try:
            base = bench._git(["merge-base", head, "origin/" + base_branch], clone).strip()
        except bench.CaseError as exc:
            raise BuildError("no merge base of %s and %s" % (head, base_branch)) from exc
        if base == head:
            raise BuildError(
                "the merge base of %s and %s is the head itself (merged without squashing); "
                "the replay would have an empty diff" % (head, base_branch)
            )
        return base
    finally:
        bench._rmtree(tmp)


def build(
    archive: str,
    round_no: int,
    fid: str,
    out: str,
    repo: str = "",
    request: Optional[str] = None,
    config: str = "",
) -> str:
    """Write the case directory `out`; return its path. `config` is a revali.toml to put in
    the case, for a project whose revali.toml is not in git."""
    if os.path.exists(out):
        raise BuildError("%s already exists" % out)
    config_text = None
    if config:
        try:
            with open(config, encoding="utf-8") as fh:
                config_text = fh.read()
        except (OSError, ValueError) as exc:  # ValueError: UnicodeDecodeError
            raise BuildError("cannot read %s: %s" % (config, exc)) from exc
    state = _load(os.path.join(archive, "state.json"))
    missing = [k for k in ("repo", "branch", "base", "pr_number") if not state.get(k)]
    if missing:
        raise BuildError("state.json lacks %s" % ", ".join(missing))
    for key in ("repo", "branch", "base"):
        if not isinstance(state[key], str):
            raise BuildError("state.json's %s is %r, not a string" % (key, state[key]))
    data = review(archive, round_no)
    finding = next((f for f in findings(data) if f.get("id") == fid), None)
    if finding is None:
        raise BuildError("round %d has no finding %s" % (round_no, fid))
    if not qualifies(finding):
        raise BuildError(
            "%s is %s %s at line %r; a case needs a medium or high correctness or security "
            "finding with a line"
            % (fid, finding.get("severity"), finding.get("kind"), finding.get("line"))
        )
    if not resolved_next(archive, round_no, fid):
        raise BuildError("round %d does not mark %s resolved" % (round_no + 1, fid))
    head = data.get("head_sha")
    if not isinstance(head, str) or not re.fullmatch(r"[0-9a-f]{40}", head):
        raise BuildError("review-%d.json has no head_sha" % round_no)
    prompt_path = os.path.join(archive, "logs", "prompt-r%d-1.md" % round_no)
    try:
        with open(prompt_path, encoding="utf-8") as fh:
            change_md = cut_change_md(fh.read())
    except (OSError, ValueError) as exc:  # ValueError: UnicodeDecodeError
        raise BuildError("cannot read %s: %s" % (prompt_path, exc)) from exc
    if request is not None:
        change_md = replace_request(change_md, request)
    pr = state["pr_number"]
    if not isinstance(pr, int) or isinstance(pr, bool) or pr <= 0:
        raise BuildError("state.json has pr_number %r, not a PR number" % (pr,))
    repo = repo or "https://github.com/%s" % state["repo"]
    fetch = ["refs/pull/%d/head" % pr]
    base = merge_base(repo, fetch, head, state["base"])

    def s(value) -> str:  # a TOML basic string; JSON's escapes are TOML's
        return json.dumps(str(value), ensure_ascii=False)

    toml = (
        "# Built by tools/bench_case.py from round %d of PR #%d (finding %s): the head commit\n"
        "# the reviewer saw, the change.md from that round's prompt.\n\n"
        "[case]\nrepo = %s\nfetch = [%s]\nbase = %s\nhead = %s\nbranch = %s\n\n"
        "[expected]\nfile = %s\nlines = [%d, %d]\ndescription = %s\n\n"
        "[source]\npr = %d\nround = %d\nfinding = %s\n"
        % (
            round_no,
            pr,
            fid,
            s(repo),
            s(fetch[0]),
            s(base),
            s(head),
            s(state["branch"]),
            s(finding["file"]),
            finding["line"],
            finding["line"],
            s(first_sentence(finding.get("text", ""))),
            pr,
            round_no,
            s(fid),
        )
    )
    try:
        os.makedirs(out)
    except OSError as exc:
        raise BuildError("cannot write %s: %s" % (out, exc)) from exc
    try:
        bench._write(os.path.join(out, bench.CASE_FILE), toml)
        bench._write(os.path.join(out, bench.CHANGE_FILE), change_md)
        if config_text is not None:
            bench._write(os.path.join(out, bench.CONFIG_FILE), config_text)
    except OSError as exc:
        bench._rmtree(out)
        raise BuildError("cannot write %s: %s" % (out, exc)) from exc
    try:
        bench.load_case(out)
    except bench.CaseError as exc:
        bench._rmtree(out)
        raise BuildError("the written case does not load: %s" % exc) from exc
    return out


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="build a reviewer benchmark case")
    parser.add_argument("archive", nargs="?", help="one PR's archive directory")
    parser.add_argument("--list", metavar="REPO_ARCHIVE", help="list one repository's candidates")
    parser.add_argument("--round", type=int)
    parser.add_argument("--finding")
    parser.add_argument("--out", help="the case directory to create")
    parser.add_argument("--repo", default="", help="URL or path (default: the GitHub repository)")
    parser.add_argument("--request", help="replaces the body of the ## Request section")
    parser.add_argument(
        "--config", default="", help="a revali.toml to put in the case (when it is not in git)"
    )
    args = parser.parse_args(argv)
    if args.list:
        try:
            found = candidates(args.list)
        except (BuildError, OSError) as exc:
            print("refused: %s" % exc, file=sys.stderr)
            return 1
        for pr, path, n, fid in found:
            print("%5d  r%-2d %-4s %s" % (pr, n, fid, path))
        return 0
    if not (args.archive and args.round and args.finding and args.out):
        parser.error("give an archive directory, --round, --finding and --out (or --list)")
    try:
        path = build(
            args.archive, args.round, args.finding, args.out, args.repo, args.request, args.config
        )
    except (BuildError, OSError) as exc:  # OSError: the temporary clone's directory
        print("refused: %s" % exc, file=sys.stderr)
        return 1
    print("case written: %s" % path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
