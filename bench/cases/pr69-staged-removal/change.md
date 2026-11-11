---
title: Refuse a run while git tracks files in the state directory
kind: feature
author_model: claude-opus-5-5
---

## Request
Go ahead.

(M3.2.6 in the author's plan. In PR #67 the author force-added
`.revali/ci__windows-job/change.md` and the squash merge put it on main; PR #68 removed it and
added a suite test, which only catches it in revali's own CI after the push. The reviewer of
PR #68 suggested, three rounds running, a `revali preflight` check so a forced add stops
before anything is pushed, in every project; the author proposed it and the user said to
do it.)

## Goal
revali refuses to start a review while git tracks any file under the state directory, in
any project, before anything is pushed, and says how to fix it.

## Acceptance criteria
- AC-1: preflight stops with exit 2 when git tracks any file under the state directory
  (`[paths] state_dir`, `.revali` by default) in the checkout, whether the branch committed
  it, the base already had it, or it is only staged. The message lists the files (at most
  20, then how many more), says to run `git rm -r --cached <state_dir>` and commit, and that
  the directory belongs in `.gitignore`.
- AC-2: the check runs before anything leaves the machine: when it stops, no GitHub call is
  made, nothing is fetched or pushed, no PR is opened and no reviewer starts. `revali run`
  reports it as it reports other preflight stops (`wait` and `status` give exit 2 and the
  message).
- AC-3: files under the state directory that are only on disk (ignored or untracked) do not
  stop preflight, and neither does a tracked path that only starts with the same name
  (`.revali-notes/x`) or sits in a subdirectory (`docs/.revali/x`). With `state_dir` set to
  another directory, that directory is the one checked.
- AC-4: docs/workflow.md describes the check where it lists what preflight refuses.

## Out of scope
- Checking that the state directory is in `.gitignore` (an untracked state directory does
  not get committed by revali, and `git status` already shows it).
- Removing the tracked files for the user.

## Dependencies
M3.2.5 (#68), merged.

## What
- `revali/gitops.py`: `tracked_under(cwd, path)`, `git ls-files -z -- :(literal)<path>`, so it
  reads the index (a staged-only file counts) and a name with glob characters matches only
  itself.
- `revali/preflight.py`: `check_state_dir_untracked`, called right after the clean-tree
  check and before `check_github`, so it runs before any GitHub call, fetch or push. Exit 2
  (`EXIT_ACTION`, the author must act) with `git tracks <n> file(s) under <state_dir>/,
  revali's state directory:`, the first 20 paths, `... and <k> more`, and the fix (`git rm
  -r --cached <state_dir>`, commit; the directory belongs in .gitignore, where revali's PR
  stage adds it).
- `docs/workflow.md`: the preflight paragraph describes the check.
- Tests: `tests/test_state_dir_tracked.py` (10): committed on the branch, inherited from the
  base, only staged, 25 files (20 listed, 5 counted), and a foreground `run` then `wait` and
  `status`, each with no gh or claude call and no branch on origin (AC-1, AC-2); only on
  disk, `.revali-notes/x`, `docs/.revali/x`, and `state_dir = ".rv"` (AC-3); the literal
  pathspec. With the check stubbed out, the 6 refusal tests fail and the 4 others pass.
- Existing tests changed in their setup only, no assertion touched. Each replaced
  `.gitignore` with a file that no longer ignored `.revali/` and then ran `commit_all`
  (`git add -A`), which committed the fixture's `.revali/feature__mul/` (change.md, and logs
  of an earlier run), so preflight now refused them with exit 2 instead of the result they
  check:
  - `tests/test_pipeline.py` `test_gitignore_added_when_missing` and
    `test_gitignore_untouched_when_git_already_ignores`: commit only `.gitignore`. Both still
    start with `.revali/` missing from `.gitignore`, which is what they test.
  - `tests/test_tests_archive.py`
    `test_an_occupied_archived_path_stops_the_run_and_keeps_the_occupant`: appends its line to
    the fixture's `.gitignore` instead of replacing it.
  - Reviewer test `tests/test_review_prunedirs.py`
    `test_a_directory_with_an_ignored_file_stays`: the same append. Edited without a finding
    because the request makes its setup hit the new refusal; marked in a comment.
  That those tests ran the whole pipeline with the state directory committed is the case
  this change stops: before it, such a branch would have carried `.revali/` into the base.

## Why
See Request.
