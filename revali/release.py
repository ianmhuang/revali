"""`revali release <version>`: tag the pushed tip of the base branch, push the tag and
publish the GitHub Release (docs/releasing.md, step 5).

Every check runs before anything is created; a failed check exits 2 and changes nothing. A
release that stopped half-way (the tag made or pushed, the Release not created) finishes on a
rerun: a tag already on HEAD is kept, and only the missing steps run.
"""

import json
import os
import re
from dataclasses import dataclass
from typing import List

from revali import EXIT_ACTION, EXIT_ERROR, EXIT_OK, gitops
from revali.config import load_project_config
from revali.procs import ExeNotFound, one_line, resolve, run, run_retry

VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")
RELEASE_YML = ".github/release.yml"


class Refused(Exception):
    """A check failed: the user must act (exit 2). Nothing was created."""


class Failed(Exception):
    """A git or gh call failed (exit 1)."""


@dataclass
class Plan:
    tag: str
    version: str
    head: str
    base: str
    repo_name: str
    make_tag: bool
    push_tag: bool
    release_url: str  # "" when the Release does not exist yet
    notes: str = ""

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

    def release_create_args(self) -> List[str]:
        args = ["gh", "release", "create", self.tag, "--verify-tag", "--generate-notes"]
        if self.notes:
            args += ["--notes", self.notes]
        return args


def tag_name(version: str) -> str:
    """`v<X.Y.Z>` for `X.Y.Z` or `vX.Y.Z`; Refused for anything else."""
    m = VERSION_RE.match(version.strip())
    if not m:
        raise Refused("the version must be X.Y.Z or vX.Y.Z (got %r)" % version)
    return "v%s.%s.%s" % m.groups()


def _git(args: List[str], root: str):
    return run(resolve("git") + args, cwd=root, timeout=120)


def _gh(args: List[str], root: str, retries: int = 0):
    return run_retry(resolve("gh") + args, retries=retries, cwd=root, timeout=120)


def local_tag_commit(tag: str, root: str) -> str:
    """The commit the local tag points at, "" when there is no such tag."""
    res = _git(["rev-parse", "-q", "--verify", "refs/tags/%s^{commit}" % tag], root)
    return res.stdout.strip() if res.ok else ""


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
    """The check runs GitHub has for the commit (GitHub Actions and other apps; the older
    commit statuses are not read), up to 100."""
    res = _gh(["api", "repos/{owner}/{repo}/commits/%s/check-runs?per_page=100" % sha], root, 1)
    if not res.ok:
        raise Failed("gh api check-runs failed: %s" % one_line(res.text))
    try:
        data = json.loads(res.stdout or "{}")
    except ValueError as exc:
        raise Failed("gh api check-runs returned invalid JSON: %s" % exc) from exc
    runs = data.get("check_runs") if isinstance(data, dict) else None
    return [r for r in runs if isinstance(r, dict)] if isinstance(runs, list) else []


def check_ci(sha: str, root: str) -> str:
    """Refused unless every check run of the commit passed; returns a note for the output."""
    checks = commit_checks(sha, root)
    if not checks:
        return "no CI checks on HEAD"
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


def release_url(tag: str, root: str) -> str:
    """The URL of the GitHub Release for the tag, "" when there is none."""
    res = _gh(["release", "view", tag, "--json", "url", "--jq", ".url"], root)
    if res.ok:
        return res.stdout.strip()
    if "not found" in res.text.lower():
        return ""
    raise Failed("gh release view %s failed: %s" % (tag, one_line(res.text)))


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
    branch = gitops.current_branch(root)
    if branch != base:
        raise Refused("on %s, not the base branch %s; check out %s first" % (branch, base, base))
    dirty = gitops.dirty_paths(root, ())
    if dirty:
        raise Refused("the working tree is not clean: %s" % ", ".join(dirty[:5]))
    res = gitops.fetch("origin", base, root)
    if not res.ok:
        raise Failed("git fetch origin %s failed: %s" % (base, one_line(res.text)))
    head = gitops.rev_parse("HEAD", root) or ""
    remote = gitops.rev_parse("refs/remotes/origin/%s" % base, root) or ""
    if head != remote:
        raise Refused(
            "HEAD (%s) is not origin/%s (%s); pull or push first"
            % (head[:12], base, remote[:12] or "missing")
        )
    if not _git(["cat-file", "-e", "HEAD:%s" % RELEASE_YML], root).ok:
        raise Refused(
            "%s is not in HEAD; GitHub groups the generated notes by it (revali init writes it)"
            % RELEASE_YML
        )
    local = local_tag_commit(tag, root)
    if local and local != head:
        raise Refused("tag %s already exists on %s, not on HEAD %s" % (tag, local[:12], head[:12]))
    on_origin = remote_tag_commit(tag, root)
    if on_origin and on_origin != head:
        raise Refused(
            "tag %s already exists on origin on %s, not on HEAD %s"
            % (tag, on_origin[:12], head[:12])
        )
    report(check_ci(head, root))
    plan = Plan(
        tag=tag,
        version=tag[1:],
        head=head,
        base=base,
        repo_name=info.name or os.path.basename(root),
        make_tag=not local and not on_origin,
        push_tag=not on_origin,
        release_url=release_url(tag, root),
        notes=notes,
    )
    if local or on_origin:
        report("tag %s already on HEAD (%s)" % (tag, "on origin" if on_origin else "local only"))
    return plan


def execute(plan: Plan, root: str, report) -> str:
    """Make, push and publish what the plan lacks; returns the Release URL."""
    if plan.make_tag:
        res = _git(["tag", "-a", plan.tag, "-m", plan.tag_message(), plan.head], root)
        if not res.ok:
            raise Failed("git tag %s failed: %s" % (plan.tag, one_line(res.text)))
        report("tagged %s on %s" % (plan.tag, plan.head[:12]))
    if plan.push_tag:
        res = run_retry(
            resolve("git") + ["push", "origin", "refs/tags/%s" % plan.tag], cwd=root, timeout=300
        )
        if not res.ok:
            raise Failed(
                "git push origin %s failed: %s; run the same command again to finish"
                % (plan.tag, one_line(res.text))
            )
        report("pushed %s to origin" % plan.tag)
    if plan.release_url:
        return plan.release_url
    res = _gh(plan.release_create_args()[1:], root)
    if not res.ok:
        raise Failed(
            "gh release create %s failed: %s; run the same command again to finish"
            % (plan.tag, one_line(res.text))
        )
    url = gitops.first_line(res.stdout)
    report("published the GitHub Release %s" % plan.tag)
    return url


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
            cmds = plan.commands()
            if not cmds:
                print("dry run: %s is already released: %s" % (plan.tag, plan.release_url))
            for cmd in cmds:
                print("dry run: would run %s" % " ".join(_quoted(a) for a in cmd))
            return EXIT_OK
        if not plan.commands():
            print("%s is already released on %s: %s" % (plan.tag, plan.head[:12], plan.release_url))
            return EXIT_OK
        url = execute(plan, root, report)
    except Refused as exc:
        print("ERROR: %s" % exc)
        return EXIT_ACTION
    except (Failed, gitops.GitError) as exc:
        print("ERROR: %s" % exc)
        return EXIT_ERROR
    print("RELEASED: %s on %s: %s" % (plan.tag, plan.head[:12], url))
    return EXIT_OK


def _quoted(arg: str) -> str:
    return '"%s"' % arg if (not arg or any(ch.isspace() for ch in arg)) else arg
