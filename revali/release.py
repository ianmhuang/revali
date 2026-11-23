"""`revali release <version>`: tag the pushed tip of the base branch, or of `[project]
release_branch` when it names another branch (a merge of a base commit with that commit's
tree, checked by `check_release_merge`), push the tag and publish the GitHub Release
(docs/releasing.md, step 5 and "Releasing from another branch").

Every check runs before anything is created; a failed check exits 2 and changes nothing. A
release that stopped half-way (the tag made or pushed, the Release not created) finishes on a
rerun: a tag already on HEAD is kept, and only the missing steps run.

The notes are GitHub's generated notes (`releases/generate-notes`, grouped by
.github/release.yml), with the `--notes` line above them and each section named in
`[pr] collapse_headings` folded into a `<details>` block.
"""

import html
import json
import os
import re
import tempfile
from dataclasses import dataclass
from typing import List

from revali import EXIT_ACTION, EXIT_ERROR, EXIT_OK, gitops
from revali.config import load_project_config
from revali.procs import ExeNotFound, ProcError, one_line, resolve, run, run_retry

# ASCII digits only (`\d` takes any Unicode digit), no leading zeros, as semantic versioning
VERSION_RE = re.compile(r"^v?(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
RELEASE_YML = ".github/release.yml"
RERUN = "run the same command again to finish"
GENERATE_NOTES = "repos/{owner}/{repo}/releases/generate-notes"
NOTES_BELOW = "<the notes below>"  # the notes file, as a dry run names it


class Refused(Exception):
    """A check failed: the user must act (exit 2). Nothing was created."""


class Failed(Exception):
    """A git or gh call failed (exit 1)."""


@dataclass
class Plan:
    tag: str
    version: str
    head: str
    branch: str  # the release branch HEAD is on
    base: str  # the review base it was checked against; the same name on one branch
    repo_name: str
    make_tag: bool
    push_tag: bool
    release_url: str  # "" when the Release does not exist yet
    notes: str = ""
    title: str = ""  # the Release's title and body, when it does not exist yet
    body: str = ""

    def branches(self) -> str:
        if self.branch == self.base:
            return "release branch %s (the base branch)" % self.branch
        return "release branch %s, checked against the base branch %s" % (self.branch, self.base)

    def tag_message(self) -> str:
        message = "%s %s" % (self.repo_name, self.version)
        return "%s: %s" % (message, self.notes) if self.notes else message

    def commands(self) -> List[List[str]]:
        """The commands the release runs, in order; none when it is already complete."""
        cmds = []
        if self.make_tag:
            cmds.append(["git", "tag", "-a", self.tag, "-m", self.tag_message(), self.head])
        if self.push_tag:
            cmds.append(["git", "push", "origin", "refs/tags/%s" % self.tag])
        if not self.release_url:
            cmds.append(self.release_create_args())
        return cmds

    def release_create_args(self, notes_file: str = NOTES_BELOW) -> List[str]:
        return [
            "gh",
            "release",
            "create",
            self.tag,
            "--verify-tag",
            "--title",
            self.title,
            "--notes-file",
            notes_file,
        ]


def tag_name(version: str) -> str:
    """`v<X.Y.Z>` for `X.Y.Z` or `vX.Y.Z`; Refused for anything else."""
    m = VERSION_RE.match(version.strip())
    if not m:
        raise Refused(
            "the version must be X.Y.Z or vX.Y.Z, plain numbers without leading zeros (got %r)"
            % version
        )
    return "v%s.%s.%s" % m.groups()


def _git(args: List[str], root: str):
    return run(resolve("git") + args, cwd=root, timeout=120)


def _gh(args: List[str], root: str, retries: int = 0):
    return run_retry(resolve("gh") + args, retries=retries, cwd=root, timeout=120)


def local_tag_commit(tag: str, root: str) -> str:
    """The commit the local tag points at, "" when there is no such tag; Refused for a tag
    that points at something else (a tree or a blob)."""
    ref = "refs/tags/%s" % tag
    if not _git(["rev-parse", "-q", "--verify", ref], root).ok:
        return ""
    res = _git(["rev-parse", "-q", "--verify", ref + "^{commit}"], root)
    if not res.ok:
        raise Refused("tag %s already exists and does not point at a commit" % tag)
    return res.stdout.strip()


def remote_tag_commit(tag: str, root: str) -> str:
    """The commit the tag on origin points at (the peeled `^{}` line of an annotated tag),
    "" when origin has no such tag."""
    ref = "refs/tags/%s" % tag
    res = _git(["ls-remote", "origin", ref, ref + "^{}"], root)
    if not res.ok:
        raise Failed("git ls-remote origin failed: %s" % one_line(res.text))
    shas = {}
    for line in res.stdout.splitlines():
        parts = line.split()
        if len(parts) == 2:
            shas[parts[1]] = parts[0]
    return shas.get(ref + "^{}") or shas.get(ref, "")


def commit_checks(sha: str, root: str) -> List[dict]:
    """Every check run GitHub has for the commit (GitHub Actions and other apps; the older
    commit statuses are not read), one JSON object per line through every page."""
    res = _gh(
        [
            "api",
            "--paginate",
            "repos/{owner}/{repo}/commits/%s/check-runs?per_page=100" % sha,
            "--jq",
            ".check_runs[] | {name, status, conclusion}",
        ],
        root,
        1,
    )
    if not res.ok:
        raise Failed("gh api check-runs failed: %s" % one_line(res.text))
    runs = []
    for line in res.stdout.splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except ValueError as exc:
            raise Failed("gh api check-runs returned invalid JSON: %s" % exc) from exc
        if isinstance(item, dict):
            runs.append(item)
    return runs


def check_ci(sha: str, root: str) -> str:
    """Refused unless every check run of the commit passed; returns a note for the output."""
    checks = commit_checks(sha, root)
    if not checks:
        return "no CI check runs on HEAD"
    pending = [str(c.get("name", "?")) for c in checks if c.get("status") != "completed"]
    failed = [
        str(c.get("name", "?"))
        for c in checks
        if c.get("status") == "completed"
        and c.get("conclusion") not in ("success", "neutral", "skipped")
    ]
    if failed:
        raise Refused("CI checks did not pass on HEAD: %s" % ", ".join(failed))
    if pending:
        raise Refused(
            "CI checks are still running on HEAD: %s; run again when they finish"
            % ", ".join(pending)
        )
    return "CI checks green on HEAD (%d)" % len(checks)


def _tree(commit: str, root: str) -> str:
    """The tree object of a commit; Failed when git cannot read it."""
    res = _git(["rev-parse", "--verify", "--quiet", "%s^{tree}" % commit], root)
    if not res.ok:
        raise Failed("git rev-parse %s^{tree} failed: %s" % (commit[:12], one_line(res.text)))
    return res.stdout.strip()


def check_release_merge(head: str, target: str, base: str, root: str) -> None:
    """Refused unless HEAD on the release branch is a merge commit of two parents whose
    second parent, the side the release pull request merged, is on `origin/<base>` and
    has the same tree as HEAD: what the tag carries is then a commit reviewed there."""
    res = _git(["rev-list", "--parents", "-n", "1", head], root)
    if not res.ok:
        raise Failed("git rev-list %s failed: %s" % (head[:12], one_line(res.text)))
    parents = res.stdout.split()[1:]
    if len(parents) != 2:
        raise Refused(
            "HEAD %s on %s is not a merge commit of two parents (it has %d); merge the release "
            "pull request from %s with a merge commit, not squashed or rebased"
            % (head[:12], target, len(parents), base)
        )
    merged = parents[1]
    ancestor = "refs/remotes/origin/%s" % base
    if not gitops.rev_parse(ancestor, root):
        # the fetch succeeded but filled FETCH_HEAD only: a refspec that leaves the base out
        # (a `--single-branch` clone)
        # (review round 2 F4) the advice adds the base to the refspec, so every later
        # `git fetch origin <base>` moves origin/<base> too, not only this one
        raise Refused(
            "origin/%s is missing after `git fetch origin %s` (the remote's fetch refspec "
            "leaves it out); run `git remote set-branches --add origin %s` and try again"
            % (base, base, base)
        )
    res = _git(["merge-base", "--is-ancestor", merged, ancestor], root)
    if res.returncode == 1:
        raise Refused(
            "the side HEAD %s merged (%s) is not on origin/%s, so the release would carry "
            "commits not reviewed there; bring them to %s and release from a merge of it"
            % (head[:12], merged[:12], base, base)
        )
    if not res.ok:
        raise Failed("git merge-base --is-ancestor failed: %s" % one_line(res.text))
    # the merge's own tree, not only its history: a commit made straight on the release
    # branch below the merge, or an edit in the merge itself, leaves the trees different.
    # The trees are compared as objects; `git diff` only names the paths, as the user's
    # `diff.ignoreSubmodules` can hide a submodule pointer from it (review round 2)
    if _tree(merged, root) != _tree(head, root):
        args = ["diff", "--name-only", "--no-renames", "--ignore-submodules=none"]
        res = _git(args + [merged, head, "--"], root)
        if not res.ok:
            raise Failed("git diff %s %s failed: %s" % (merged[:12], head[:12], one_line(res.text)))
        paths = [ln for ln in res.stdout.splitlines() if ln.strip()]
        more = len(paths) - 5
        shown = ", ".join(paths[:5]) + (" and %d more" % more if more > 0 else "")
        shown = shown or "paths git diff does not list"
        raise Refused(
            "the tree of HEAD %s differs from the side it merged (%s, on origin/%s) in %s, "
            "so the release would carry changes not reviewed there; bring them to %s "
            "(a cherry-pick, or a merge of %s into %s) and release from a new merge"
            % (head[:12], merged[:12], base, shown, base, target, base)
        )


def release_url(tag: str, root: str) -> str:
    """The URL of the GitHub Release for the tag, "" when there is none."""
    res = _gh(["release", "view", tag, "--json", "url", "--jq", ".url"], root)
    if res.ok:
        return res.stdout.strip()
    if "release not found" in res.text.lower():  # not a repository-level 404
        return ""
    raise Failed("gh release view %s failed: %s" % (tag, one_line(res.text)))


def generated_notes(tag: str, head: str, root: str):
    """(title, body) GitHub generates for the tag on HEAD, grouped by the release.yml of
    HEAD; the request stores nothing. Failed when it fails or answers something else."""
    what = "gh api %s" % GENERATE_NOTES
    args = ["api", "-X", "POST", GENERATE_NOTES]
    args += ["-f", "tag_name=%s" % tag, "-f", "target_commitish=%s" % head]
    try:
        res = _gh(args, root, retries=2)
    except ProcError as exc:
        raise Failed("%s failed: %s; %s" % (what, one_line(str(exc)), RERUN)) from exc
    if not res.ok:
        raise Failed("%s failed: %s; %s" % (what, one_line(res.text), RERUN))
    try:
        data = json.loads(res.stdout)
        title, body = data["name"], data["body"]
    except (ValueError, KeyError, TypeError):
        title = body = None
    if not isinstance(title, str) or not isinstance(body, str):
        raise Failed(
            "%s answered something other than the notes: %s; %s"
            % (what, one_line(res.stdout), RERUN)
        )
    return title or tag, body


def collapse_sections(body: str, headings: List[str]) -> str:
    """The generated notes with each `### <heading>` section of `headings` and its
    `* ` entries folded into `<details><summary><heading> (<n>)</summary>`; a heading with
    no entries, and every other line, stays as it is. The blank lines around the block
    end it as an HTML block, so the list inside and the heading after it render."""
    wanted = set(headings)
    lines = body.split("\n")
    out: List[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        title = line[4:].strip() if line.startswith("### ") else None
        if title in wanted:
            j = i + 1
            while j < len(lines) and lines[j].startswith("* "):
                j += 1
            entries = lines[i + 1 : j]
            if entries:
                summary = "<details><summary>%s (%d)</summary>" % (
                    html.escape(title, quote=False),
                    len(entries),
                )
                out += ["", summary, ""] + entries + ["", "</details>", ""]
                i = j
                continue
        out.append(line)
        i += 1
    return "\n".join(out)


def release_notes(notes: str, body: str, headings: List[str]) -> str:
    """The Release's body: the `--notes` line, a blank line, the collapsed notes."""
    body = collapse_sections(body, headings)
    return "%s\n\n%s" % (notes, body) if notes else body


def plan_release(root: str, version: str, notes: str, report) -> Plan:
    """Run every check in order; Refused or Failed on the first that does not pass."""
    tag = tag_name(version)
    try:
        resolve("gh")
    except ExeNotFound as exc:
        raise Failed(str(exc)) from exc
    cfg = load_project_config(root)
    try:
        info = gitops.gh_repo_info(root)
    except gitops.GhError as exc:
        raise Failed(str(exc)) from exc
    base = cfg.project.base_branch or info.default_branch
    if not base:
        raise Failed("cannot determine the base branch; set project.base_branch")
    target = cfg.project.release_branch or base
    branch = gitops.current_branch(root)
    if branch != target:
        which = "release branch" if target != base else "base branch"
        raise Refused("on %s, not the %s %s; check out %s first" % (branch, which, target, target))
    # revali's own state directory does not count, as for preflight and merge
    dirty = gitops.dirty_paths(root, (cfg.paths.state_dir.rstrip("/") + "/",))
    if dirty:
        raise Refused("the working tree is not clean: %s" % ", ".join(dirty[:5]))
    # the base too when it is another branch: the merge check reads origin/<base>
    for name in [target] + ([base] if target != base else []):
        res = gitops.fetch("origin", name, root)
        if not res.ok:
            raise Failed("git fetch origin %s failed: %s" % (name, one_line(res.text)))
    head = gitops.rev_parse("HEAD", root) or ""
    remote = gitops.rev_parse("refs/remotes/origin/%s" % target, root) or ""
    if head != remote:
        raise Refused(
            "HEAD (%s) is not origin/%s (%s); pull or push first"
            % (head[:12], target, remote[:12] or "missing")
        )
    if not _git(["cat-file", "-e", "HEAD:%s" % RELEASE_YML], root).ok:
        raise Refused(
            "%s is not in HEAD; GitHub groups the generated notes by it (revali init writes it)"
            % RELEASE_YML
        )
    if target != base:
        check_release_merge(head, target, base, root)
    local = local_tag_commit(tag, root)
    if local and local != head:
        raise Refused("tag %s already exists on %s, not on HEAD %s" % (tag, local[:12], head[:12]))
    on_origin = remote_tag_commit(tag, root)
    if on_origin and on_origin != head:
        raise Refused(
            "tag %s already exists on origin on %s, not on HEAD %s"
            % (tag, on_origin[:12], head[:12])
        )
    if on_origin:
        # the release passed this check before it pushed the tag, and the push itself may
        # have started new check runs on HEAD (a workflow `on: push` fires for tags too)
        report("tag %s already on origin on HEAD; CI is not checked again" % tag)
    else:
        report(check_ci(head, root))
        if local:
            report("tag %s already on HEAD here, not on origin yet" % tag)
    plan = Plan(
        tag=tag,
        version=tag[1:],
        head=head,
        branch=target,
        base=base,
        repo_name=info.name or os.path.basename(root),
        make_tag=not local and not on_origin,
        push_tag=not on_origin,
        release_url=release_url(tag, root),
        notes=notes,
    )
    if not plan.release_url:
        # asked before anything is made, so a failure here leaves nothing half-done
        title, body = generated_notes(tag, head, root)
        plan.title = title
        plan.body = release_notes(notes, body, cfg.pr.collapse_headings)
    return plan


def _step(what: str, call):
    """Run one step of `execute`; a failure, a timeout included, says how to finish."""
    try:
        res = call()
    except ProcError as exc:
        raise Failed("%s failed: %s; %s" % (what, one_line(str(exc)), RERUN)) from exc
    if not res.ok:
        raise Failed("%s failed: %s; %s" % (what, one_line(res.text), RERUN))
    return res


def _write_notes(body: str) -> str:
    """The Release body in a new temporary file (LF); an OSError is a `Failed`."""
    path = ""
    try:
        fd, path = tempfile.mkstemp(prefix="revali-release-", suffix=".md")
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(body)
    except OSError as exc:
        if path:
            _remove_notes(path, lambda _line: None)
        raise Failed(
            "could not write the release notes to a temporary file: %s; %s"
            % (one_line(str(exc)), RERUN)
        ) from exc
    return path


def _remove_notes(path: str, report) -> None:
    """Remove the temporary notes file; a failure is reported, not raised, since what
    the file was for is done (or has its own error)."""
    try:
        os.remove(path)
    except OSError as exc:
        report("warning: could not remove %s: %s" % (path, one_line(str(exc))))


def execute(plan: Plan, root: str, report) -> str:
    """Make, push and publish what the plan lacks; returns the Release URL. The notes
    file is written first, so a failure to write it leaves nothing made."""
    notes_file = "" if plan.release_url else _write_notes(plan.body)
    try:
        if plan.make_tag:
            _step(
                "git tag %s" % plan.tag,
                lambda: _git(["tag", "-a", plan.tag, "-m", plan.tag_message(), plan.head], root),
            )
            report("tagged %s on %s" % (plan.tag, plan.head[:12]))
        if plan.push_tag:
            _step(
                "git push origin %s" % plan.tag,
                lambda: run_retry(
                    resolve("git") + ["push", "origin", "refs/tags/%s" % plan.tag],
                    cwd=root,
                    timeout=300,
                ),
            )
            report("pushed %s to origin" % plan.tag)
        if plan.release_url:
            return plan.release_url
        res = _step(
            "gh release create %s" % plan.tag,
            lambda: _gh(plan.release_create_args(notes_file)[1:], root),
        )
    finally:
        if notes_file:
            _remove_notes(notes_file, report)
    report("published the GitHub Release %s" % plan.tag)
    return gitops.first_line(res.stdout)


def cmd_release(args) -> int:
    root = gitops.repo_root(os.getcwd())
    if not root:
        print(
            "ERROR: not inside a git working tree; run `revali release` in the project's repository"
        )
        return EXIT_ERROR
    print("repo: %s" % root)
    report = print
    try:
        plan = plan_release(root, args.version, args.notes or "", report)
        if args.dry_run:
            print("dry run: %s" % plan.branches())
            cmds = plan.commands()
            if not cmds:
                print("dry run: %s is already released: %s" % (plan.tag, plan.release_url))
            for cmd in cmds:
                print("dry run: would run %s" % " ".join(_quoted(a) for a in cmd))
            if plan.body:
                print("dry run: the release notes:")
                print(plan.body)
            return EXIT_OK
        if not plan.commands():
            print("%s is already released on %s: %s" % (plan.tag, plan.head[:12], plan.release_url))
            return EXIT_OK
        url = execute(plan, root, report)
    except Refused as exc:
        print("ERROR: %s" % exc)
        return EXIT_ACTION
    except (Failed, gitops.GitError, ProcError) as exc:
        print("ERROR: %s" % one_line(str(exc), 400))
        return EXIT_ERROR
    print("RELEASED: %s on %s: %s" % (plan.tag, plan.head[:12], url))
    return EXIT_OK


def _quoted(arg: str) -> str:
    plain = arg and not any(ch.isspace() or ch in "<>" for ch in arg)
    return arg if plain else '"%s"' % arg
