"""Rebuild revali archive directories for PRs merged before `[paths] archive_dir` existed.

`revali merge` used to delete `.revali/<branch>/`. What survived is on GitHub and in the
history file:

- the PR body: change.md (the Request is withheld on public repositories) plus the
  status table revali appends;
- the PR comments revali posted: one per review round, one per validation;
- ~/.revali/history.jsonl: one row per run.

Logs, prompts, the reviewer's raw answers, state.json and response-<n>.md never left the
machine and are not recoverable. Each rebuilt directory gets a README.md saying so.

The layout matches `revali merge`: <archive_dir>/<owner>__<name>/<pr>-<branch>/. A PR whose
directory already exists is skipped, so the script can run again safely.

Requires the gh CLI, logged in.

    python backfill_archive.py owner/name [owner/name ...] [--archive-dir DIR] [--dry-run]
"""

import argparse
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from revali.config import archive_root, paths_for, user_home  # noqa: E402
from revali.procs import resolve  # noqa: E402
from revali.state import safe_branch  # noqa: E402

PR_FIELDS = "number,title,headRefName,baseRefName,mergedAt,mergeCommit,url,body,comments,files"
STATUS_MARKER = "\n## revali status\n"
ROUND_LINE = re.compile(r"^round: (\d+)$", re.M)
ROUND_HEAD = re.compile(r"^# Review round (\d+)\b", re.M)
VALIDATION_HEAD = re.compile(r"^## Validation (\d+)\b", re.M)


def gh_json(args):
    res = subprocess.run(resolve("gh") + args, capture_output=True, text=True, encoding="utf-8")
    if res.returncode != 0:
        raise SystemExit("gh %s failed: %s" % (" ".join(args), res.stderr.strip()))
    return json.loads(res.stdout)


def lf(text):
    return text.replace("\r\n", "\n").replace("\r", "\n")


def write(path, text):
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(text if text.endswith("\n") else text + "\n")


def history_rows(repo, branch):
    path = os.path.join(user_home(), "history.jsonl")
    if not os.path.isfile(path):
        return []
    rows = []
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            if row.get("repo") == repo and row.get("branch") == branch:
                rows.append(line)
    return rows


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


def plan_files(pr, repo):
    """The files to write for one PR: {name: text}."""
    change, status = split_body(pr["body"] or "")
    files = {"change.md": change}
    validations = []
    others = []
    for comment in pr.get("comments") or []:
        name, kind = classify(comment)
        if kind == "review":
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
    rows = history_rows(repo, pr["headRefName"])
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
        "- change.md: the PR body without the status table (the Request is withheld on a",
        "  public repository)",
        "- review-<n>.md: the review comments (full text on a private repository, summaries",
        "  on a public one)",
        "- tests.md: the validation comments, in order",
        "- history.jsonl: this branch's rows from ~/.revali/history.jsonl",
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
    return files


def backfill(repo, root, dry_run):
    prs = gh_json(
        ["pr", "list", "-R", repo, "--state", "merged", "--limit", "500", "--json", PR_FIELDS]
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
        if os.path.exists(dest):
            skipped += 1
            print("  #%d exists, skipped: %s" % (pr["number"], dest))
            continue
        files = plan_files(pr, repo)
        names = sorted(n for n in files if n not in ("change.md", "README.md"))
        print("  #%d -> %s (%s)" % (pr["number"], dest, ", ".join(names) or "no comments"))
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
    ap.add_argument("--archive-dir", help="default: [paths] archive_dir as revali resolves it")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    root = args.archive_dir or archive_root(paths_for(os.path.dirname(HERE)))
    if not root:
        raise SystemExit("archive_dir is empty; pass --archive-dir")
    root = os.path.abspath(os.path.expanduser(root))
    print("archive root: %s%s" % (root, " (dry run)" if args.dry_run else ""))
    total = 0
    for repo in args.repos:
        total += backfill(repo, root, args.dry_run)
    print("total: %d directories%s" % (total, " would be created" if args.dry_run else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
