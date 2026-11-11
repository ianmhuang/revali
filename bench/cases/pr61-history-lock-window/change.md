---
title: Lock the history file while a row is appended
kind: fix
author_model: claude-opus-5-5
---

## Request
好

(M3.4.2 in the author's plan, from the M3.4.1 audit of concurrent runs:
every run, merge and stop appends one row to `~/.revali/history.jsonl`
with no lock. Measured on 2026-11-10 with revali's own `append_history`,
4 processes appending 200 rows each to one file at the same moment: on
Windows (where revali runs) 91 to 226 of the 800 rows were lost in each of
6 tries, with empty lines left behind, for rows of 0.5 KB and 20 KB alike;
in WSL on ext4 all 800 rows came back every time. Required by the
author's plan from PR #60's round 2 review: `IssueRef.existing`'s comment
names issues found outside the branch, and a listed issue whose `url` is
null is stored as an empty string, not "None".)

## Goal
Runs, merges and stops that finish at the same moment in different
projects or worktrees all keep their history row, on Windows and Linux.

## Acceptance criteria
- AC-1: 4 processes calling `append_history` 200 times each on one file
  at the same moment leave 800 rows, each line valid JSON, each (writer,
  i) present once, on Windows and on Linux.
- AC-2: the lock is an OS lock on a separate file, `<history file>.lock`
  next to the history file (`msvcrt.locking` on Windows, `fcntl.flock`
  elsewhere; both standard library), taken around the append and released
  after it. The history file itself is never locked, so `read_history`
  (`revali stats`) is never blocked by a writer.
- AC-3: a writer that died while holding the lock leaves nothing that
  blocks the next one: the OS releases the lock with the process, and the
  `.lock` file left on disk does not count as held.
- AC-4: when another process holds the lock for longer than
  `[paths] write_retry_s` (default 2 s), the row is appended without the
  lock rather than dropped; that is never slower than the window.
- AC-5: when the lock file cannot be opened at all (a read-only
  directory, a path that is a directory), the row is appended as today;
  an error writing the row itself still propagates to the caller as an
  `OSError` (which `_record_history` ignores, as today).
- AC-6 (PR #60 round 2 follow-up): `IssueRef.existing`'s comment says it
  is also set for an open issue found outside the branch; `gh_open_issues`
  returns `""` for a `url` that is null or missing, not `"None"`.
- AC-7: docs/files.md lists `history.jsonl.lock` next to the history row
  and says what it is for.

## Out of scope
- A history file on a network share whose filesystem does not honour
  `flock` or `LockFileEx` across machines.
- Reading: `read_history` keeps skipping lines that are not JSON; the
  empty lines and lost rows already in a history file are not repaired.

## Dependencies
none (`msvcrt` and `fcntl` are standard library, each on its own platform)

## What
- `revali/state.py`: `append_history(path, record, retry_s=None)` takes an
  OS lock on `<path>.lock` (`_lock_history`, `_try_os_lock`, `_os_unlock`:
  `msvcrt.locking` on byte 0 on Windows, `fcntl.flock` elsewhere,
  non-blocking attempts with a growing pause up to 0.1 s) around the
  append. The window is `retry_s`, else `write_retry_s(path)`, looked up
  only once a first attempt was refused. A lock file that cannot be
  opened, or a lock held past the window, means an unlocked append.
- `revali/gitops.py`: `gh_open_issues` stores `""` for a null url.
- `revali/issues.py`: `IssueRef.existing` comment.
- `docs/files.md`: a row for `history.jsonl.lock`.
- `tests/test_history_lock.py`: 4 writer processes x 200 rows; a holder
  process that takes the lock with `msvcrt` / `fcntl` directly (not
  through revali) for the wait, the killed holder, the window and the
  reader cases; the lock file being a directory; the history path being a
  directory; the null url.

Measured after the change on 2026-11-10 with the same script as before:
Windows 4 x 200 (0.5 KB and 20 KB rows) and 8 x 200 (both sizes), WSL
8 x 200 (20 KB): every row present, every line JSON. Before: Windows lost
91 to 226 of 800.

## Why
See Request.
