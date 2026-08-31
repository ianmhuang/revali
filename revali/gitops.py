"""Thin wrappers over git and gh. Every call goes through procs.run (UTF-8, logged)."""
import fnmatch
import json
import os
from dataclasses import dataclass
from typing import Callable, List, Optional, Tuple

from revali.procs import ExeNotFound, Result, resolve, run, run_retry


class GitError(Exception):
    pass


class GhError(Exception):
    pass


Logger = Optional[Callable[[str], None]]


def _git(args: list, cwd: str, log: Logger = None, timeout: float = 120) -> Result:
    return run(resolve("git") + list(args), cwd=cwd, log=log, timeout=timeout)


def git_ok(args: list, cwd: str, log: Logger = None) -> Result:
    res = _git(args, cwd, log)
    if not res.ok:
        raise GitError("git %s failed: %s" % (" ".join(args), res.text.strip()))
    return res


def repo_root(cwd: str) -> Optional[str]:
    try:
        res = _git(["rev-parse", "--show-toplevel"], cwd)
    except ExeNotFound:
        return None
    if not res.ok:
        return None
    return os.path.normpath(res.stdout.strip())


def current_branch(cwd: str) -> str:
    res = git_ok(["rev-parse", "--abbrev-ref", "HEAD"], cwd)
    return res.stdout.strip()


def rev_parse(ref: str, cwd: str) -> Optional[str]:
    res = _git(["rev-parse", "--verify", "--quiet", ref], cwd)
    return res.stdout.strip() if res.ok else None


def status_porcelain(cwd: str) -> List[Tuple[str, str]]:
    """(XY, path) per entry of `git status --porcelain -z`: NUL-separated, so paths with
    spaces or non-ASCII characters arrive unquoted. A rename or copy carries the
    original path in a second field, which is skipped."""
    res = git_ok(["status", "--porcelain", "-z", "--untracked-files=all"], cwd)
    fields = res.stdout.split("\0")
    entries = []
    i = 0
    while i < len(fields):
        entry = fields[i]
        i += 1
        if len(entry) < 4:
            continue
        code, path = entry[:2], entry[3:]
        if code[0] in "RC":
            i += 1  # the path it was renamed or copied from
        entries.append((code, path))
    return entries


def dirty_paths(cwd: str, ignore_prefixes: Tuple[str, ...]) -> List[str]:
    out = []
    for code, path in status_porcelain(cwd):
        p = path.replace("\\", "/")
        if any(p.startswith(pre) for pre in ignore_prefixes):
            continue
        out.append("%s %s" % (code.strip() or "??", path))
    return out


def fetch(remote: str, ref: str, cwd: str, log: Logger = None) -> bool:
    res = _git(["fetch", "--quiet", remote, ref], cwd, log, timeout=300)
    return res.ok


def has_remote(remote: str, cwd: str) -> bool:
    res = _git(["remote", "get-url", remote], cwd)
    return res.ok


def remote_repo(remote: str, cwd: str) -> str:
    """'owner/name' (lowercased, the last two path components) from a hosted remote URL
    (https, ssh, scp-like); '' for a local path or no remote."""
    res = _git(["remote", "get-url", remote], cwd)
    if not res.ok:
        return ""
    url = res.stdout.strip()
    if "://" in url:
        scheme, rest = url.split("://", 1)
        if scheme.lower() == "file":
            return ""
        path = rest.split("/", 1)[1] if "/" in rest else ""
    elif ":" in url and "@" in url.split(":", 1)[0]:
        path = url.split(":", 1)[1]
    else:
        return ""
    parts = [p for p in path.strip("/").split("/") if p]
    if len(parts) < 2:
        return ""
    name = parts[-1]
    if name.lower().endswith(".git"):
        name = name[:-4]
    return ("%s/%s" % (parts[-2], name)).lower()


def is_ancestor(ancestor: str, descendant: str, cwd: str) -> bool:
    res = _git(["merge-base", "--is-ancestor", ancestor, descendant], cwd)
    return res.returncode == 0


def commits_between(base: str, head: str, cwd: str) -> int:
    res = git_ok(["rev-list", "--count", "%s..%s" % (base, head)], cwd)
    return int(res.stdout.strip() or 0)


def diff_numstat(base: str, head: str, cwd: str) -> List[Tuple[int, int, str]]:
    res = git_ok(["diff", "--numstat", "%s...%s" % (base, head)], cwd)
    out = []
    for line in res.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        added = 0 if parts[0] == "-" else int(parts[0])
        deleted = 0 if parts[1] == "-" else int(parts[1])
        out.append((added, deleted, parts[2]))
    return out


def diff_text(base: str, head: str, cwd: str, exclude: Optional[List[str]] = None) -> str:
    args = ["diff", "%s...%s" % (base, head)]
    if exclude:
        args.append("--")
        args.append(".")
        for pat in exclude:
            args.append(":(exclude,glob)%s" % pat)
    return git_ok(args, cwd).stdout


def changed_files(base: str, head: str, cwd: str) -> List[str]:
    res = git_ok(["diff", "--name-only", "%s...%s" % (base, head)], cwd)
    return [l.strip() for l in res.stdout.splitlines() if l.strip()]


def matches_any(path: str, patterns: List[str]) -> bool:
    p = path.replace("\\", "/")
    for pat in patterns:
        if fnmatch.fnmatch(p, pat) or fnmatch.fnmatch(os.path.basename(p), pat):
            return True
        if pat.endswith("/**") and p.startswith(pat[:-3] + "/"):
            return True
    return False


def head_contains(sha: str, cwd: str) -> bool:
    return is_ancestor(sha, "HEAD", cwd)


def push_branch(branch: str, cwd: str, log: Logger = None, force: bool = False) -> Result:
    args = ["push", "--quiet", "-u"]
    if force:
        args.append("--force-with-lease")
    return run_retry(resolve("git") + args + ["origin", branch], cwd=cwd, log=log, timeout=300)


def ensure_gitignore(repo: str, entry: str) -> bool:
    """Append entry to .gitignore if missing. Returns True when the file was changed."""
    path = os.path.join(repo, ".gitignore")
    lines = []
    if os.path.isfile(path):
        with open(path, "r", encoding="utf-8", newline="") as fh:
            lines = fh.read().splitlines()
    if any(l.strip() in (entry, entry.rstrip("/")) for l in lines):
        return False
    with open(path, "a", encoding="utf-8", newline="\n") as fh:
        if lines and lines[-1].strip():
            fh.write("\n")
        fh.write(entry + "\n")
    return True


# ---- gh ---------------------------------------------------------------------

@dataclass
class RepoInfo:
    owner: str
    name: str
    visibility: str      # PRIVATE / PUBLIC / INTERNAL
    default_branch: str
    url: str = ""


def _gh(args: list, cwd: str, log: Logger = None, timeout: float = 120, retries: int = 1) -> Result:
    return run_retry(resolve("gh") + list(args), retries=retries, cwd=cwd, log=log, timeout=timeout)


def gh_available() -> bool:
    try:
        resolve("gh")
    except ExeNotFound:
        return False
    return True


def gh_auth_ok(cwd: str, log: Logger = None) -> bool:
    return _gh(["auth", "status"], cwd, log, retries=0).ok


def gh_login(cwd: str, log: Logger = None) -> str:
    res = _gh(["api", "user", "--jq", ".login"], cwd, log)
    if not res.ok:
        raise GhError("gh api user failed: %s" % res.text.strip())
    return res.stdout.strip()


def gh_repo_info(cwd: str, log: Logger = None) -> RepoInfo:
    res = _gh(["repo", "view", "--json", "owner,name,visibility,defaultBranchRef,url"], cwd, log)
    if not res.ok:
        raise GhError("gh repo view failed: %s" % res.text.strip())
    try:
        data = json.loads(res.stdout)
    except ValueError as exc:
        raise GhError("gh repo view returned invalid JSON: %s" % exc)
    owner = data.get("owner") or {}
    default_ref = data.get("defaultBranchRef") or {}
    return RepoInfo(
        owner=owner.get("login", "") if isinstance(owner, dict) else str(owner),
        name=data.get("name", ""),
        visibility=str(data.get("visibility", "")).upper(),
        default_branch=default_ref.get("name", "") if isinstance(default_ref, dict) else "",
        url=data.get("url", ""),
    )


def gh_pr_open(branch: str, cwd: str, log: Logger = None) -> Optional[dict]:
    res = _gh(["pr", "list", "--head", branch, "--state", "open", "--json", "number,url,isDraft,title"], cwd, log)
    if not res.ok:
        raise GhError("gh pr list failed: %s" % res.text.strip())
    try:
        items = json.loads(res.stdout or "[]")
    except ValueError:
        return None
    return items[0] if items else None


def gh_pr_any(branch: str, cwd: str, log: Logger = None) -> List[dict]:
    res = _gh(["pr", "list", "--head", branch, "--state", "all", "--json", "number,url,state,mergedAt"], cwd, log)
    if not res.ok:
        return []
    try:
        return json.loads(res.stdout or "[]")
    except ValueError:
        return []
