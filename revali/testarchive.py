"""The reviewer's tests in archive mode (`[review] tests = "archive"`): they live under
`<state_dir>/<branch>/tests/<repository path>` instead of in the branch's history.

Before a round the archived files are copied back into `test_dir` (untracked) so the
reviewer can update or delete them; the lint check and the smoke run see them in the
working tree like any other round; after the round every file the reviewer left under
`test_dir` is moved into the archive, the tree is clean again and nothing is committed.
Validation and the base rerun send the archived files into the sandbox as extra files.
"""

import os
import shutil
from typing import Dict, List, Optional, Sequence, Tuple

from revali import EXIT_ERROR, gitops
from revali.preflight import Context, Stop
from revali.state import RunLog, State, read_text

ARCHIVE_MODE = "archive"
SUBDIR = "tests"


def archive_mode(ctx: Context, state: Optional[State] = None) -> bool:
    """Archive mode by the configuration, or by the state's recorded rounds (the mode of a
    branch is fixed; the cleanup of an interrupted round follows either). A context without
    a loaded [review] table (a test double) counts by the state alone."""
    review = getattr(getattr(ctx, "cfg", None), "review", None)
    if getattr(review, "tests", "") == ARCHIVE_MODE:
        return True
    return state is not None and getattr(state, "tests_mode", "") == ARCHIVE_MODE


def archive_dir(rdir: str) -> str:
    return os.path.join(rdir, SUBDIR)


def archived_files(rdir: str) -> List[str]:
    """The repository paths (forward slashes, sorted) of every file under the archive."""
    root = archive_dir(rdir)
    if not os.path.isdir(root):
        return []
    out = []
    for dirpath, _, names in os.walk(root):
        for name in names:
            rel = os.path.relpath(os.path.join(dirpath, name), root)
            out.append(rel.replace("\\", "/"))
    return sorted(out)


def _prune(path: str, stop: str) -> None:
    """Remove the empty directories from `path` up to (not including) `stop`."""
    path, stop = os.path.normpath(path), os.path.normpath(stop)
    while path != stop and path.startswith(stop + os.sep):
        try:
            os.rmdir(path)
        except OSError:
            return
        path = os.path.dirname(path)


def _remove_archived(rdir: str, rel: str) -> None:
    root = archive_dir(rdir)
    full = os.path.join(root, rel)
    if os.path.isfile(full):
        os.remove(full)
    _prune(os.path.dirname(full), root)


def _tracked(ctx: Context) -> set:
    from revali.review import tracked_test_files  # review imports this module lazily

    return set(tracked_test_files(ctx))


def rebuild(ctx: Context, state: State, rdir: str, log: Optional[RunLog]) -> List[str]:
    """The state's list of the reviewer's files, from the archive: every archived file under
    `test_dir` that HEAD does not track is the reviewer's, whatever the state remembered
    (so `revali reset` loses none of them); a path HEAD tracks is the author's and leaves
    the archive and the state (a dry run only says so: it deletes nothing). Runs on every
    run; logs only what changed."""
    from revali.review import under_test_dir

    test_dir = ctx.cfg.project.test_dir
    tracked = _tracked(ctx)
    found = [p for p in archived_files(rdir) if under_test_dir(p, test_dir)]
    authors = [p for p in found if p in tracked]
    if not ctx.dry_run:
        for rel in authors:
            _remove_archived(rdir, rel)
    mine = [p for p in found if p not in tracked]
    added = [p for p in mine if p not in state.test_files]
    dropped = [p for p in state.test_files if p not in mine]
    state.test_files = list(mine)
    where = os.path.relpath(archive_dir(rdir), ctx.repo_root).replace("\\", "/")
    if log and authors:
        log.stage(
            "run",
            "%d archived test file(s) are tracked in HEAD now, so they are the author's: "
            "%s %s and forgotten, existing files the reviewer must not modify: %s"
            % (
                len(authors),
                "dry run: would be removed from" if ctx.dry_run else "removed from",
                where,
                ", ".join(authors),
            ),
        )
    if log and (added or dropped):
        parts = []
        if added:
            parts.append("recovered %d: %s" % (len(added), ", ".join(added)))
        if dropped:
            parts.append("no longer there %d: %s" % (len(dropped), ", ".join(dropped)))
        log.stage(
            "run",
            "the reviewer's test files from %s (archive mode; %d file(s)): %s"
            % (where, len(mine), "; ".join(parts)),
        )
    return mine


def place_back(ctx: Context, state: State, rdir: str, log: Optional[RunLog]) -> List[str]:
    """Copy the archived files into `test_dir` for the round that starts. A file that is
    already there is a copy the previous cleanup could not delete (it is in the tolerated
    list) and is overwritten; any other occupant (a gitignored file, since preflight refused
    an untracked one) ends the run before anything is copied, so the cleanup that follows
    has nothing to delete. The paths go into the state (saved) before the copies are
    written: a round killed at any later point leaves exactly these for remove_placed."""
    root = archive_dir(rdir)
    placed = [rel for rel in state.test_files if os.path.isfile(os.path.join(root, rel))]
    for rel in placed:
        dst = os.path.join(ctx.repo_root, rel)
        if os.path.exists(dst) and rel not in state.pending_test_files:
            raise Stop(
                EXIT_ERROR,
                "cannot place the archived test file back: %s already exists in the working "
                "tree" % rel,
            )
    if placed:
        state.placed_test_files = list(placed)
        state.save(rdir)
    for rel in placed:
        dst = os.path.join(ctx.repo_root, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copyfile(os.path.join(root, rel), dst)
    if placed and log:
        log.stage(
            "review",
            "placed %d archived test file(s) back into %s for the reviewer to update: %s"
            % (len(placed), ctx.cfg.project.test_dir, ", ".join(placed)),
        )
    return placed


def take(
    ctx: Context, state: State, rdir: str, files: Sequence[str], log: Optional[RunLog]
) -> None:
    """After the round: move `files` (what the reviewer left under `test_dir`, all untracked)
    into the archive, replacing the previous content, drop the archived files the reviewer
    deleted, and make `files` the state's list."""
    root = archive_dir(rdir)
    kept = [p.replace("\\", "/") for p in files]
    for rel in kept:
        src = os.path.join(ctx.repo_root, rel)
        dst = os.path.join(root, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        if os.path.exists(dst):
            os.remove(dst)
        shutil.move(src, dst)
    gone = [p for p in archived_files(rdir) if p not in kept]
    for rel in gone:
        _remove_archived(rdir, rel)
    stop = os.path.join(ctx.repo_root, ctx.cfg.project.test_dir)  # test_dir itself stays
    for rel in kept:  # the directories the reviewer made under test_dir are empty now
        _prune(os.path.dirname(os.path.join(ctx.repo_root, rel)), stop)
    state.test_files = sorted(kept)
    state.placed_test_files = []
    if log:
        where = os.path.relpath(root, ctx.repo_root).replace("\\", "/")
        log.stage(
            "review",
            "archived %d test file(s) under %s (nothing committed): %s%s"
            % (
                len(kept),
                where,
                ", ".join(kept) or "none",
                (
                    "; dropped %d the reviewer deleted: %s" % (len(gone), ", ".join(gone))
                    if gone
                    else ""
                ),
            ),
        )


def remove_placed(
    ctx: Context, state: State, log: Optional[RunLog], stage: str
) -> Tuple[List[str], List[str]]:
    """Delete the copies a round placed in `test_dir` and did not take back (the round stopped
    early); the archive keeps the previous round's content. Only the paths place_back
    recorded go (untracked ones; a file that was already there when place_back refused to
    copy is not among them): an archived path an author's file occupies is never deleted.
    Returns (removed, stuck), like discard_unfinished_tests."""
    tracked = _tracked(ctx)
    removed = []
    stuck = []
    reasons = []
    for rel in state.placed_test_files:
        if rel in tracked:
            continue
        full = os.path.join(ctx.repo_root, rel)
        if not os.path.isfile(full):
            continue
        try:
            os.remove(full)
            removed.append(rel)
        except OSError as exc:
            stuck.append(rel)
            reasons.append("%s (%s)" % (rel, exc))
    state.placed_test_files = []
    if removed and log:
        log.stage(
            stage,
            "removed %d working-tree cop%s of archived test file(s); the archive keeps the "
            "previous round's content: %s"
            % (len(removed), "y" if len(removed) == 1 else "ies", ", ".join(removed)),
        )
    if stuck and log:
        log.stage(
            stage,
            "could not remove %d working-tree cop%s of archived test file(s): %s"
            % (len(stuck), "y" if len(stuck) == 1 else "ies", ", ".join(reasons)),
        )
    return removed, stuck


def read_archived(rdir: str, paths: Sequence[str]) -> Dict[str, str]:
    """{repository path: content} of the archived files among `paths`, for the sandbox."""
    root = archive_dir(rdir)
    out = {}
    for rel in paths:
        full = os.path.join(root, rel)
        if os.path.isfile(full):
            out[rel] = read_text(full)
    return out


def sandbox_files(ctx: Context, state: State, rdir: str) -> Dict[str, str]:
    """The extra files a validation session needs: the archived reviewer files in archive
    mode (they are not in the checkout the sandbox clones), nothing in commit mode."""
    if not archive_mode(ctx, state):
        return {}
    try:
        return read_archived(rdir, state.test_files)
    except (OSError, UnicodeDecodeError) as exc:
        raise Stop(EXIT_ERROR, "could not read an archived test file: %s" % exc) from exc


def history_rewritten(ctx: Context, state: State) -> bool:
    """In archive mode there is no reviewer commit to lose, so a rewrite is seen through the
    HEAD the last run recorded: gone from HEAD's history means rebased or amended."""
    return bool(
        state.rounds and state.head_sha and not gitops.head_contains(state.head_sha, ctx.repo_root)
    )
