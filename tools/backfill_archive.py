"""Rebuild revali archive directories for PRs merged before `[paths] archive_dir` existed.

`revali merge` used to delete `.revali/<branch>/`. What survived is on GitHub and in the
history file:

- the PR body: change.md without its Request section (revali never publishes the request;
  PRs from before that rule carried a "withheld" marker on public repositories) plus the
  status table revali appends;
- the PR comments revali posted: one per review round, one per validation;
- the history file (`~/.revali/history.jsonl` unless the user file moves it): one row
  per run.

Logs, prompts, the reviewer's raw answers, state.json and response-<n>.md never left the
machine and are not recoverable. Each rebuilt directory gets a README.md saying so.

The layout matches `revali merge`: <archive_dir>/<owner>__<name>/<pr>-<branch>/, with
archive_dir from defaults.toml and the user file (`~/.revali/config.toml`, `REVALI_HOME`);
no project file is consulted. A PR whose directory already exists (with or without the
`-<timestamp>` suffix `merge` adds to a taken name) is skipped, so the script can run again
safely.

Requires the gh CLI, logged in.

    python backfill_archive.py owner/name [owner/name ...] [--archive-dir DIR] [--limit N]
                               [--dry-run]
"""

import argparse
import glob
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from revali.config import (  # noqa: E402
    ConfigError,
    PathsCfg,
    archive_root,
    history_path,
    load_defaults,
    load_user_config,
)
from revali.procs import ExeNotFound, resolve  # noqa: E402
from revali.state import read_history, safe_branch  # noqa: E402

PR_FIELDS = "number,title,headRefName,baseRefName,mergedAt,mergeCommit,url,body,comments,files"
# gh lists newest first and stops at --limit; a listing that reaches it may have dropped the
# oldest PRs, the ones a backfill is for, so the tool refuses rather than guesses.
LIST_LIMIT = 500
STATUS_MARKER = "\n## revali status\n"
ROUND_LINE = re.compile(r"^round: (\d+)$", re.M)
ROUND_HEAD = re.compile(r"^# Review round (\d+)\b", re.M)
VALIDATION_HEAD = re.compile(r"^## Validation (\d+)\b", re.M)


def gh_json(args):
    try:
        argv = resolve("gh") + args
    except ExeNotFound as exc:
        raise SystemExit("gh not found: %s" % exc) from None
    res = subprocess.run(
        argv, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False
    )
    if res.returncode != 0:
        raise SystemExit("gh %s failed: %s" % (" ".join(args), res.stderr.strip()))
    return json.loads(res.stdout)


def lf(text):
    return text.replace("\r\n", "\n").replace("\r", "\n")


def write(path, text):
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(text if text.endswith("\n") else text + "\n")


def user_config():
    """The user file, or None (with a note) when it does not load; the tool then runs on
    the defaults, as revali's own commands do for the history file."""
    try:
        return load_user_config()
    except ConfigError as exc:
        print("note: user config not used: %s" % "; ".join(exc.problems))
        return None


def default_root(cfg):
    """[paths] archive_dir from defaults.toml and the user file only."""
    known = PathsCfg.__dataclass_fields__
    paths = PathsCfg(**{k: v for k, v in load_defaults().get("paths", {}).items() if k in known})
    value = (cfg.sections.get("paths", {}) if cfg else {}).get("archive_dir")
    if isinstance(value, str):
        paths.archive_dir = value
    return archive_root(paths)


def history_rows(path, repo, branch):
    """The history rows for one branch, re-serialised as revali writes them; a line that
    does not parse is dropped the way `read_history` drops it."""
    return [
        json.dumps(row, ensure_ascii=False)
        for row in read_history(path)
        if row.get("repo") == repo and row.get("branch") == branch
    ]


def classify(comment):
    """(file name, kind) for one PR comment; unknown comments keep a numbered name later."""
    body = lf(comment["body"])
    head = "\n".join(body.split("\n")[:12])
    m = ROUND_LINE.search(head) or ROUND_HEAD.search(head)
    if m:
        return "review-%s.md" % m.group(1), "review"
    m = VALIDATION_HEAD.search(head)
    if m:
        return "validation-%s" % m.group(1), "validation"
    return None, "other"


def split_body(body):
    body = lf(body)
    if STATUS_MARKER in body:
        change, status = body.split(STATUS_MARKER, 1)
        return change.rstrip() + "\n", "## revali status\n" + status
    return body.rstrip() + "\n", ""


def is_revali_pr(body):
    return lf(body).startswith("---\ntitle:")


def plan_files(pr, repo, history):
    """The files to write for one PR: {name: text}, and notes about what was dropped."""
    change, status = split_body(pr["body"] or "")
    files = {"change.md": change}
    notes = []
    validations = []
    others = []
    for comment in pr.get("comments") or []:
        name, kind = classify(comment)
        if kind == "review":
            if name in files:
                notes.append("two comments for %s; the later one kept" % name)
            files[name] = lf(comment["body"])
        elif kind == "validation":
            validations.append(lf(comment["body"]))
        else:
            others.append(lf(comment["body"]))
    if validations:
        files["tests.md"] = (
            "<!-- rebuilt from the PR comments: the validation sections revali posted; the\n"
            "test-file summary that headed the original tests.md was never posted -->\n\n"
            + "\n\n".join(validations)
        )
    for i, text in enumerate(others, 1):
        files["comment-%d.md" % i] = text
    rows = history_rows(history, repo, pr["headRefName"])
    if rows:
        files["history.jsonl"] = "\n".join(rows) + "\n"
    reviewer_tests = sorted(
        f["path"]
        for f in pr.get("files") or []
        if os.path.basename(f["path"]).startswith("test_review_")
    )
    merge_commit = (pr.get("mergeCommit") or {}).get("oid", "")
    readme = [
        "# Rebuilt archive",
        "",
        "Built by backfill_archive.py from GitHub, not by `revali merge` (that deleted the",
        "directory when this PR was merged, before `[paths] archive_dir` existed).",
        "",
        "- PR: %s" % pr["url"],
        "- title: %s" % pr["title"],
        "- branch: `%s` into `%s`" % (pr["headRefName"], pr["baseRefName"]),
        "- merged at: %s" % (pr.get("mergedAt") or "?"),
        "- merge commit: %s" % (merge_commit or "?"),
        "",
        "## What is here",
        "",
        "- change.md: the PR body without the status table (no Request section: revali keeps",
        '  the request local; older PRs may show a "withheld" marker instead)',
        "- review-<n>.md: the review comments (full text on a private repository, summaries",
        "  on a public one)",
        "- tests.md: the validation comments, in order",
        "- history.jsonl: this branch's rows from the history file",
        "",
        "## What is not",
        "",
        "logs/ (prompts, raw reviewer answers, sandbox logs), state.json, response-<n>.md.",
        "",
        "## Reviewer test files",
        "",
        "In the repository at the merge commit:",
        "",
    ]
    readme += ["- `%s`" % p for p in reviewer_tests] or ["- (none in this PR)"]
    if status:
        readme += ["", status.rstrip()]
    files["README.md"] = "\n".join(readme) + "\n"
    return files, notes


def existing(dest):
    """The directory `merge` wrote for this PR, bare or with its timestamp suffix, or ""."""
    if os.path.exists(dest):
        return dest
    found = sorted(glob.glob(glob.escape(dest) + "-*"))
    return found[0] if found else ""


def backfill(repo, root, history, limit, dry_run):
    prs = gh_json(
        ["pr", "list", "-R", repo, "--state", "merged", "--limit", str(limit), "--json", PR_FIELDS]
    )
    if len(prs) >= limit:
        raise SystemExit(
            "%s: gh listed %d PRs, the --limit; the oldest may be missing, rerun with a higher"
            " --limit" % (repo, len(prs))
        )
    prs.sort(key=lambda p: p["number"])
    made = skipped = foreign = 0
    for pr in prs:
        if not is_revali_pr(pr.get("body") or ""):
            foreign += 1
            print("  #%d not a revali PR, skipped" % pr["number"])
            continue
        dest = os.path.join(
            root, safe_branch(repo), "%d-%s" % (pr["number"], safe_branch(pr["headRefName"]))
        )
        found = existing(dest)
        if found:
            skipped += 1
            print("  #%d exists, skipped: %s" % (pr["number"], found))
            continue
        files, notes = plan_files(pr, repo, history)
        names = sorted(n for n in files if n not in ("change.md", "README.md"))
        print("  #%d -> %s (%s)" % (pr["number"], dest, ", ".join(names) or "no comments"))
        for note in notes:
            print("  #%d: %s" % (pr["number"], note))
        if not dry_run:
            os.makedirs(dest)
            for name, text in files.items():
                write(os.path.join(dest, name), text)
        made += 1
    print("%s: %d rebuilt, %d already there, %d not revali" % (repo, made, skipped, foreign))
    return made


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("repos", nargs="+", help="owner/name")
    ap.add_argument(
        "--archive-dir", help="default: [paths] archive_dir from defaults.toml and the user file"
    )
    ap.add_argument(
        "--limit",
        type=int,
        default=LIST_LIMIT,
        help="most PRs to list per repository (default %d); a listing that reaches it is refused"
        % LIST_LIMIT,
    )
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    cfg = user_config()
    root = args.archive_dir or default_root(cfg)
    if not root:
        raise SystemExit("archive_dir is empty; pass --archive-dir")
    root = os.path.abspath(os.path.expanduser(root))
    try:
        history = history_path(cfg)
    except ConfigError as exc:
        print("note: default history file used: %s" % "; ".join(exc.problems))
        history = history_path(None)
    print("archive root: %s%s" % (root, " (dry run)" if args.dry_run else ""))
    total = 0
    for repo in args.repos:
        total += backfill(repo, root, history, args.limit, args.dry_run)
    print("total: %d directories%s" % (total, " would be created" if args.dry_run else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
