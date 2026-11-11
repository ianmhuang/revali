---
title: Keep a failed cleanup from replacing a sandbox result or a merge's output
kind: fix
author_model: claude-opus-5-5
---

## Request
As you suggest; start.

(M3.4.12 in the author's plan, from PR #63's round 2 suggestion: `SshRunner.run` removes the
git bundle in a `finally` with a bare `os.remove`, so a bundle held open on Windows (an
antivirus scan) raises PermissionError and replaces the session's report or its error. Planned
acceptance: check every `finally` and cleanup path in `runners.py`; a cleanup that fails is
logged and never replaces the report or the original error. The author proposed, and the
user accepted, folding in two suggestions from PR #64's reviewer: (a) after `revali merge`,
`cmd_merge` asks git for the repository root a second time and skips the archive without a
word when that fails; (b) `_delete_local_branch` puts git's error on the `local:` /
`worktree:` line as is, so a multi-line error breaks the line, while every other error in the
merge follow-up goes through `_one_line`.)

## Goal
Cleanup and follow-up steps that fail say so in one line and never take the place of what
the step before them produced: a sandbox session's report or error, or a merge's exit code,
history row and archive.

## Acceptance criteria
- AC-1: ssh runner: when removing the local `<label>.bundle` after a session fails with an
  OSError, the session's report is returned (or its RunnerError raised) unchanged, and the
  run's log has one line naming the bundle path, the error, and that it is left for the user
  to delete.
- AC-2: ssh runner: when the remote cleanup call cannot be run for a reason other than a
  timeout or a missing ssh (any ProcError or OSError), the session's report or error is
  unchanged and the existing "could not remove ... (left for you to delete)" line is logged;
  `_cleanup` returns False, so a lost connection's message says the directories are left.
- AC-3: local runner: when `git worktree remove` or `git worktree prune` in the `finally`
  times out, cannot be started, or exits non-zero, the report (or the error a step raised) is
  unchanged, and the log has one line naming the worktree path and git's error on one line.
  A remove that fails does not stop the prune or the removal of the temp directory.
- AC-4: wsl and ssh runners: when the `<label>-extra` directory is still there after the
  runner removed it, the log names it once; the report or error is unchanged.
- AC-5: `revali merge` archives (or removes) `.revali/<branch>/` using the repository root it
  found at its start, so git failing after the merge no longer skips the archive. When the
  archive fails the existing `could not archive ...` line is printed as today.
- AC-6: in both merge modes, a `git branch -D` that fails puts git's error on one line, cut to
  200 characters, in the `local:` line (primary tree) or the `worktree:` line (linked
  worktree).
- AC-7 (found while reading, the same class as PR #64 AC-3): in worktree mode, git that times
  out, cannot be started, or fails in `git rev-parse` during the follow-up (push --delete,
  fetch, checkout --detach, branch -D, the branch check) is reported with the reason instead
  of a traceback: push, fetch and checkout as `note:` lines, the branch check and deletion in
  the `worktree:` line (as the primary tree puts them in its `local:` line); the merge exits
  0, prints `MERGED:`, writes the history row and archives the state directory.
- AC-8: docs/sandbox.md says a failed cleanup is logged and does not change the session's
  result; docs/side-effects.md's `revali merge` entry says the worktree follow-up reports its
  failures as notes.

## Out of scope
- Retrying a cleanup (removing the bundle again later, a second remote `rm`).
- Cleanup outside `runners.py` and the merge follow-up (state files, locks, archive moves).
- Changing any exit code; a merge whose follow-up failed still exits 0.
- KeyboardInterrupt / SystemExit during a cleanup; they still propagate.

## Dependencies
none

## What
- `revali/runners.py`: `remove_dir` / `remove_file` remove what a session left in the logs
  directory and log one line naming what is still there; `_cleanup_git` runs the local
  runner's `git worktree remove` and `prune`, logging a failure (non-zero exit, timeout, git
  that cannot start) on one line. The local runner now removes its temp directory before the
  prune, so a worktree git could not remove is dropped from the repository's list too. The
  wsl and ssh `finally` blocks go through `remove_dir` / `remove_file`; `SshRunner._cleanup`
  catches every ProcError and OSError (`CLEANUP_FAILURES`), not only a timeout or a missing
  ssh.
- `revali/procs.py`: `one_line` (moved from `merge._one_line`, same behaviour) so runners and
  merge fold git's errors the same way.
- `revali/merge.py`: `_delete_local_branch` puts git's error through `one_line`;
  `_worktree_follow_up` runs push, fetch and checkout through `_git_failure` (notes), and the
  branch deletion and the `where` check each catch `GIT_FAILURES` into the `worktree:` line,
  apart from each other so a failed check does not lose the deletion's result.
- `revali/pipeline.py`: `cmd_merge` archives with the root `_located` found at its start
  instead of asking git again after the merge.
- docs/sandbox.md (a paragraph on cleanup failures), docs/side-effects.md (the worktree
  follow-up's failures are reported, exit 0; the existing sentence is unchanged).
- Tests: `tests/test_cleanup_failures.py` (25 tests, AC-1 to AC-8); 21 of them failed before
  the change. The 4 that passed are controls: nothing is logged when the cleanup works (one
  per runner), and a long `git branch -D` error was already cut to 200 characters. No
  existing test changed.

## Why
See Request.
