"""Thin wrappers over git and gh. Every call goes through procs.run (UTF-8, logged)."""

import fnmatch
import json
import os
import re
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple

from revali.procs import ExeNotFound, ProcError, Result, resolve, run, run_retry


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


def describe(cwd: str) -> str:
    """`git describe --tags --always --dirty` of the checkout at `cwd`; "" when git is missing,
    times out or fails. Several projects' runs describe the one revali checkout at the same
    moment: the index refresh behind --dirty writes the index back only when it gets the lock
    and does not fail when another process holds it (git 2.55; GIT_OPTIONAL_LOCKS=0 does not
    stop that write; docs/side-effects.md lists it)."""
    try:
        res = _git(["describe", "--tags", "--always", "--dirty"], cwd)
    except (ProcError, OSError):
        return ""
    return res.stdout.strip() if res.ok else ""


def current_branch(cwd: str) -> str:
    res = git_ok(["rev-parse", "--abbrev-ref", "HEAD"], cwd)
    return res.stdout.strip()


def rev_parse(ref: str, cwd: str) -> Optional[str]:
    res = _git(["rev-parse", "--verify", "--quiet", ref], cwd)
    return res.stdout.strip() if res.ok else None


def status_porcelain(cwd: str) -> List[Tuple[str, str]]:
    """(XY, path) per entry of `git status --porcelain -z`: NUL-separated, so paths with
    spaces or non-ASCII characters arrive unquoted. `--no-renames`: every path is its own
    entry (a rename is a `D` plus an `A`). Without it an intent-to-add file and a deleted
    tracked one with similar content pair up as a work-tree rename (` R`), whose second
    field the old index-only skip below did not expect. A rename or copy that still
    arrives carries the original path in a second field, which is skipped."""
    res = git_ok(["status", "--porcelain", "-z", "--untracked-files=all", "--no-renames"], cwd)
    fields = res.stdout.split("\0")
    entries = []
    i = 0
    while i < len(fields):
        entry = fields[i]
        i += 1
        if len(entry) < 4:
            continue
        code, path = entry[:2], entry[3:]
        if code[0] in "RC" or code[1] in "RC":
            i += 1  # the path it was renamed or copied from
        entries.append((code, path))
    return entries


def tracked_under(cwd: str, path: str) -> List[str]:
    """The files git tracks under `path` (relative to `cwd`), read from the index by
    `git ls-files -z`, so a file only staged counts as much as a committed one. The pathspec
    is `:(literal)` so a name with glob characters matches only itself."""
    res = git_ok(["ls-files", "-z", "--", ":(literal)%s" % path], cwd)
    return [p for p in res.stdout.split("\0") if p]


def committed_under(cwd: str, path: str, ref: str = "HEAD") -> List[str]:
    """The files commit `ref` holds under `path` (what a push of it would carry), [] when
    `ref` does not resolve (a branch with no commit yet). Literal pathspec, as tracked_under."""
    if rev_parse(ref, cwd) is None:
        return []
    res = git_ok(["ls-tree", "-r", "-z", "--name-only", ref, "--", ":(literal)%s" % path], cwd)
    return [p for p in res.stdout.split("\0") if p]


def dirty_paths(cwd: str, ignore_prefixes: Tuple[str, ...]) -> List[str]:
    out = []
    for code, path in status_porcelain(cwd):
        p = path.replace("\\", "/")
        if any(p.startswith(pre) for pre in ignore_prefixes):
            continue
        out.append("%s %s" % (code.strip() or "??", path))
    return out


def fetch(remote: str, ref: str, cwd: str, log: Logger = None, prune: bool = False) -> Result:
    """`git fetch`, tried once more when it fails: worktrees of one repository share
    refs/remotes/, and of two fetches of the same ref at the same moment one fails ("cannot
    lock ref", "incorrect old value provided") after the other has moved the ref."""
    args = ["fetch", "--quiet"] + (["--prune"] if prune else []) + [remote, ref]
    return run_retry(resolve("git") + args, retries=1, cwd=cwd, log=log, timeout=300)


def first_line(text: str) -> str:
    """The first non-empty line of a command's output, stripped; for one-line notes."""
    return next((ln.strip() for ln in (text or "").splitlines() if ln.strip()), "")


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


def diff_numstat(base: str, head: str, cwd: str) -> List[Tuple[int, int, Tuple[str, ...]]]:
    """(added, deleted, names) per changed file; names is (path,) or, for a rename, (old, new).
    Read with -z, so a rename never comes back as git's `dir/{old => new}` and a name is never
    quoted. Renames are found with -M whatever `diff.renames` says, as in `_entries` and
    `diff_text`, so a copy is never reported."""
    res = git_ok(["diff", "-M", "--numstat", "-z", "%s...%s" % (base, head)], cwd)
    fields = res.stdout.split("\0")
    out: List[Tuple[int, int, Tuple[str, ...]]] = []
    i = 0
    while i < len(fields):
        parts = fields[i].split("\t", 2)  # the counts hold no tab, a name may
        i += 1
        if len(parts) != 3:
            continue
        added = 0 if parts[0] == "-" else int(parts[0])
        deleted = 0 if parts[1] == "-" else int(parts[1])
        if parts[2]:
            names: Tuple[str, ...] = (parts[2],)
        else:  # a rename or copy: the two names follow as fields of their own
            names = tuple(fields[i : i + 2])
            i += 2
        out.append((added, deleted, names))
    return out


def _entries(base: str, head: str, cwd: str) -> List[Tuple[str, Tuple[str, ...]]]:
    """(status letter, names) of each changed file in git's order, names "/" separated:
    (path,), or (old, new) for a rename (found with -M, as in `diff_numstat`)."""
    args = ["diff", "-M", "--name-status", "-z", "%s...%s" % (base, head)]
    fields = git_ok(args, cwd).stdout.split("\0")
    entries: List[Tuple[str, Tuple[str, ...]]] = []
    i = 0
    while i < len(fields) and fields[i]:
        status = fields[i][:1]
        count = 2 if status in ("R", "C") else 1
        entries.append((status, tuple(p for p in fields[i + 1 : i + 1 + count] if p)))
        i += 1 + count
    return entries


def left_out(names: Tuple[str, ...], exclude: Optional[List[str]]) -> bool:
    """The one rule for what the review leaves out: a changed file is out when its current name
    (the new one, for a rename) matches a pattern by `matches_any`. A rename left out is left
    out under both names, so its old name is not shown as a deletion; a file renamed out of
    an excluded name is reviewed like any other."""
    return bool(exclude) and matches_any(names[-1], exclude)


def _plain_name(name: str) -> bool:
    """git writes this name in a `diff --git` header as it is, never quoted."""
    return name.isascii() and name.isprintable() and '"' not in name and "\\" not in name


def _without_sections(text: str, entries, exclude: List[str]) -> Optional[str]:
    """`text` without the sections of the entries `left_out` leaves out, or None when its
    sections cannot be paired with `entries`. A section starts at a `diff --git ` line (a
    content line always starts with a space, `+`, `-` or `\\`); there is one per entry, in
    the same order, and two for a type change (git shows it as a deletion and an addition).
    A section whose names git writes unquoted must start with them."""
    cuts = [m.start() for m in re.finditer(r"(?m)^diff --git ", text)]
    if not cuts or cuts[0] != 0:
        return None
    sections = [text[a:b] for a, b in zip(cuts, cuts[1:] + [len(text)], strict=True)]
    kept: List[str] = []
    i = 0
    for status, names in entries:
        mine = sections[i : i + (2 if status == "T" else 1)]
        i += 2 if status == "T" else 1
        header = "diff --git a/%s b/%s\n" % (names[0], names[-1])
        if i > len(sections) or (
            all(_plain_name(n) for n in names) and not all(s.startswith(header) for s in mine)
        ):
            return None
        if not left_out(names, exclude):
            kept += mine
    return "".join(kept) if i == len(sections) else None


def diff_text(base: str, head: str, cwd: str, exclude: Optional[List[str]] = None) -> str:
    """The unified diff without the files `left_out` leaves out, with `a/` / `b/` prefixes
    whatever the user's `diff.noprefix` or `diff.mnemonicPrefix` says (`diff_changed_lines`
    strips them). The left-out files' sections are cut from the whole diff, so the command
    line does not grow with their number; when the sections cannot be paired with the
    changed files, git is given the left-out names as literal pathspecs instead."""
    args = ["diff", "-M", "--src-prefix=a/", "--dst-prefix=b/", "%s...%s" % (base, head)]
    text = git_ok(args, cwd).stdout
    entries = _entries(base, head, cwd) if exclude else []
    if not any(left_out(names, exclude) for _, names in entries):
        return text
    cut = _without_sections(text, entries, exclude)
    if cut is not None:
        return cut
    out = [n for _, names in entries if left_out(names, exclude) for n in names]
    paths = [":(top)"] + [":(top,exclude,literal)%s" % n for n in out]
    return git_ok(args + ["--"] + paths, cwd).stdout


def diff_paths(base: str, head: str, cwd: str, exclude: Optional[List[str]] = None) -> List[str]:
    """Every path the diff touches, "/" separated: both names of a rename; a file `left_out`
    leaves out is not listed under either name."""
    return [n for _, e in _entries(base, head, cwd) if not left_out(e, exclude) for n in e]


_C_ESCAPES = {"a": 7, "b": 8, "t": 9, "n": 10, "v": 11, "f": 12, "r": 13, '"': 34, "\\": 92}


def _header_name(field: str) -> str:
    """The file name of a `---` / `+++` line. git ends a name that holds a space with a tab,
    and writes a name with a special or (by default, `core.quotePath`) non-ASCII character
    in double quotes with C escapes, octal bytes of UTF-8 included."""
    if field.endswith("\t"):
        field = field[:-1]
    if len(field) < 2 or not (field.startswith('"') and field.endswith('"')):
        return field
    body, out, i = field[1:-1], bytearray(), 0
    while i < len(body):
        ch = body[i]
        if ch == "\\" and i + 1 < len(body):
            nxt = body[i + 1]
            octal = body[i + 1 : i + 4]
            if len(octal) == 3 and all(c in "01234567" for c in octal):
                out.append(int(octal, 8) & 0xFF)
                i += 4
                continue
            if nxt in _C_ESCAPES:
                out.append(_C_ESCAPES[nxt])
                i += 2
                continue
        out.extend(ch.encode("utf-8"))
        i += 1
    return out.decode("utf-8", errors="replace")


def diff_changed_lines(diff: str) -> Dict[str, List[str]]:
    """file -> the lines a unified diff adds or removes, without their `+` / `-`. The file
    is the new name (the old one for a deletion), unquoted (`_header_name`); the `---` /
    `+++` header lines are not changed lines."""
    found: Dict[str, List[str]] = {}
    current: Optional[List[str]] = None
    in_header = False
    old = ""
    for line in diff.split("\n"):
        if line.startswith("diff --git "):
            in_header, current, old = True, None, ""
            continue
        if in_header:
            if line.startswith("--- "):
                old = _header_name(line[4:])
            elif line.startswith("+++ "):
                new = _header_name(line[4:])
                name = old if new == "/dev/null" else new
                name = name[2:] if name[:2] in ("a/", "b/") else name
                current = found.setdefault(name, [])
            elif line.startswith("@@"):
                in_header = False
            continue
        if current is not None and line[:1] in ("+", "-"):
            current.append(line[1:])
    return found


def changed_files(base: str, head: str, cwd: str) -> List[str]:
    res = git_ok(["diff", "--name-only", "%s...%s" % (base, head)], cwd)
    return [line.strip() for line in res.stdout.splitlines() if line.strip()]


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


def rev_list(base: str, head: str, cwd: str) -> List[str]:
    """The commits in base..head, newest first."""
    res = git_ok(["rev-list", "%s..%s" % (base, head)], cwd)
    return [line.strip() for line in res.stdout.splitlines() if line.strip()]


def trailer_commits(base: str, head: str, key: str, cwd: str) -> List[Tuple[str, str]]:
    """(sha, value) of the commits in base..head whose message carries the trailer `key`,
    oldest first; several values of one trailer are joined with commas. Needs git 2.22
    (`%(trailers:key=...)`)."""
    res = git_ok(
        [
            "log",
            "--reverse",
            "--format=%H%x1f%(trailers:key=" + key + ",valueonly,separator=%x2c)",
            "%s..%s" % (base, head),
        ],
        cwd,
    )
    out = []
    for line in res.stdout.splitlines():
        sha, _, value = line.partition("\x1f")
        if sha.strip() and value.strip():
            out.append((sha.strip(), value.strip()))
    return out


def commit_messages(base: str, head: str, cwd: str) -> List[Tuple[str, str]]:
    """(sha, full message) of the commits in base..head, oldest first."""
    res = git_ok(["log", "--reverse", "--format=%H%x1f%B%x1e", "%s..%s" % (base, head)], cwd)
    out = []
    for record in res.stdout.split("\x1e"):
        sha, _, message = record.strip("\n").partition("\x1f")
        if sha.strip():
            out.append((sha.strip(), message))
    return out


def commit_paths(sha: str, cwd: str, diff_filter: str = "AM") -> List[str]:
    """Paths one commit touches, filtered like `git diff --diff-filter` (default: added or
    modified), forward slashes, NUL-separated so a path with spaces arrives unquoted. The
    callers pass commits from `base..HEAD`, which have a parent, so a root commit (`--root`)
    is not catered for."""
    res = git_ok(
        [
            "diff-tree",
            "--no-commit-id",
            "--name-only",
            "-r",
            "-z",
            "--diff-filter=" + diff_filter,
            sha,
        ],
        cwd,
    )
    return [p.replace("\\", "/") for p in res.stdout.split("\0") if p]


def last_add_commit(base: str, head: str, path: str, cwd: str) -> str:
    """The newest commit in base..head that added `path` (a file deleted and re-created is
    added twice; the later add wins), "" when no commit in the range added it."""
    res = git_ok(
        ["log", "-1", "--diff-filter=A", "--format=%H", "%s..%s" % (base, head), "--", path],
        cwd,
    )
    return res.stdout.strip()


def worktree_holding(branch: str, cwd: str) -> str:
    """Path of another worktree of this repository that has `branch` checked out, or "".
    (`git checkout <branch>` here would fail while that is the case.)"""
    res = _git(["worktree", "list", "--porcelain"], cwd)
    if not res.ok:
        return ""
    here = os.path.normpath(repo_root(cwd) or cwd)
    path = ""
    for line in res.stdout.splitlines():
        if line.startswith("worktree "):
            path = os.path.normpath(line[len("worktree ") :].strip())
        elif line.strip() == "branch refs/heads/%s" % branch and path and path != here:
            return path
    return ""


def is_linked_worktree(cwd: str) -> bool:
    """True in a worktree added with `git worktree add`; False in the tree that owns `.git`.
    git answers one of the two relative to `cwd` and the other absolute in its own (long)
    form, so both are compared as real paths: an 8.3 short `cwd` must not make them differ."""
    git_dir = _git(["rev-parse", "--git-dir"], cwd)
    common = _git(["rev-parse", "--git-common-dir"], cwd)
    if not git_dir.ok or not common.ok:
        return False
    a = os.path.normcase(os.path.realpath(os.path.join(cwd, git_dir.stdout.strip())))
    b = os.path.normcase(os.path.realpath(os.path.join(cwd, common.stdout.strip())))
    return a != b


def push_branch(branch: str, cwd: str, log: Logger = None, force: bool = False) -> Result:
    args = ["push", "--quiet", "-u"]
    if force:
        args.append("--force-with-lease")
    return run_retry(resolve("git") + args + ["origin", branch], cwd=cwd, log=log, timeout=300)


def is_ignored(path: str, cwd: str) -> bool:
    """True when git already ignores `path` by any rule (.gitignore, .git/info/exclude,
    core.excludesFile). Exit 1 means not ignored; anything else is treated the same."""
    return _git(["check-ignore", "-q", "--no-index", "--", path], cwd).ok


def ensure_gitignore(repo: str, entry: str) -> bool:
    """Append entry to .gitignore unless git already ignores it. Returns True when the
    file was changed."""
    if is_ignored(entry, repo):
        return False
    path = os.path.join(repo, ".gitignore")
    text = ""
    if os.path.isfile(path):
        with open(path, "r", encoding="utf-8", newline="") as fh:
            text = fh.read()
    if any(line.strip() in (entry, entry.rstrip("/")) for line in text.splitlines()):
        return False
    with open(path, "a", encoding="utf-8", newline="\n") as fh:
        if text and not text.endswith(("\n", "\r\n")):
            fh.write("\n")
        fh.write(entry + "\n")
    return True


# ---- gh ---------------------------------------------------------------------


@dataclass
class RepoInfo:
    owner: str
    name: str
    visibility: str  # PRIVATE / PUBLIC / INTERNAL
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
        raise GhError("gh repo view returned invalid JSON: %s" % exc) from exc
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
    res = _gh(
        ["pr", "list", "--head", branch, "--state", "open", "--json", "number,url,isDraft,title"],
        cwd,
        log,
    )
    if not res.ok:
        raise GhError("gh pr list failed: %s" % res.text.strip())
    try:
        items = json.loads(res.stdout or "[]")
    except ValueError:
        return None
    return items[0] if items else None


def gh_labels(cwd: str, log: Logger = None) -> List[str]:
    """The repository's label names."""
    res = _gh(["label", "list", "--json", "name", "--limit", "200"], cwd, log)
    if not res.ok:
        raise GhError("gh label list failed: %s" % res.text.strip())
    try:
        items = json.loads(res.stdout or "[]")
    except ValueError as exc:
        raise GhError("gh label list returned invalid JSON: %s" % exc) from exc
    return [str(i.get("name", "")) for i in items if isinstance(i, dict)]


def gh_open_issues(cwd: str, log: Logger = None, limit: int = 200) -> List[dict]:
    """The repository's open issues, newest first: {number, title, url}. A plain listing, not
    `--search`: GitHub's search index lags, so an issue opened a moment ago may be missing."""
    res = _gh(
        ["issue", "list", "--state", "open", "--limit", str(limit), "--json", "number,title,url"],
        cwd,
        log,
    )
    if not res.ok:
        raise GhError("gh issue list failed: %s" % first_line(res.text))
    try:
        items = json.loads(res.stdout or "[]")
    except ValueError as exc:
        raise GhError("gh issue list returned invalid JSON: %s" % exc) from exc
    if not isinstance(items, list):
        raise GhError("gh issue list returned %s, not a list" % type(items).__name__)
    # only items a caller can use without converting: a positive int number, a str title
    return [
        {"number": i["number"], "title": i["title"], "url": str(i.get("url") or "")}
        for i in items
        if isinstance(i, dict)
        and type(i.get("number")) is int
        and i["number"] > 0
        and isinstance(i.get("title"), str)
    ]


def gh_issue_create(
    title: str, body_path: str, labels: List[str], assignee: str, cwd: str, log: Logger = None
) -> Tuple[int, str]:
    """`gh issue create`; (number, url) parsed from the URL gh prints."""
    args = ["issue", "create", "--title", title, "--body-file", body_path]
    for label in labels:
        args += ["--label", label]
    if assignee:
        args += ["--assignee", assignee]
    res = _gh(args, cwd, log)
    if not res.ok:
        raise GhError("gh issue create failed: %s" % res.text.strip())
    url = res.stdout.strip().splitlines()[-1] if res.stdout.strip() else ""
    m = re.search(r"/issues/(\d+)", url)
    if not m:
        raise GhError("gh issue create printed no issue URL: %s" % res.text.strip()[:200])
    return int(m.group(1)), url


def gh_issue_comment(number: int, body_path: str, cwd: str, log: Logger = None) -> Result:
    return _gh(["issue", "comment", str(number), "--body-file", body_path], cwd, log)


def gh_pr_any(branch: str, cwd: str, log: Logger = None) -> List[dict]:
    res = _gh(
        ["pr", "list", "--head", branch, "--state", "all", "--json", "number,url,state,mergedAt"],
        cwd,
        log,
    )
    if not res.ok:
        return []
    try:
        return json.loads(res.stdout or "[]")
    except ValueError:
        return []
