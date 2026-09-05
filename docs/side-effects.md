# What revali does to your repository

Read this before the first run. All of it is on the feature branch unless
stated.

- appends the state directory (`.revali/`) to `.gitignore` and commits that,
  unless `git check-ignore` says the directory is already ignored by any
  rule. To keep the line out of the tracked `.gitignore`, put `.revali/` in
  `.git/info/exclude` (or your global excludes file) before the first run
- holds one lock per working tree (`.revali/tree.lock`) for the length of a
  run, so a second `run` in the same checkout is refused whatever branch it
  is on; before spawning the reviewer, committing its tests, pushing, and
  validating, checks that the checked-out branch and HEAD are still the
  run's own, and stops with exit 1 (stage `error`) otherwise, committing and
  pushing nothing from that point; `merge` holds the same lock while it
  checks out the base branch and pulls
- `git push -u origin <branch>` and `gh pr create --draft`
- commits the reviewer's test files into `test_dir`, with
  `Co-Authored-By: Claude` and `Revali-Round: <n>` trailers; only new files
  and the files the reviewer wrote in an earlier round. The
  `Revali-Round` trailer is what marks a file as the reviewer's own: on every
  run the commits between the base and HEAD that carry it, and the files
  under `test_dir` they added or modified, are read back into the state, so
  a rebase or amend that gives those commits new SHAs (the review then
  starts over from round 1) keeps them the reviewer's. A rewrite that
  drops the trailer, such as squashing the reviewer's commit into your
  own, turns those files into existing files the reviewer must not
  modify. Any other
  tracked file under `test_dir` the reviewer modified or deleted is
  restored from HEAD (`git checkout HEAD -- <path>`, index and working
  tree) before the commit, the reviewer is sent back once
  with the names already taken, and a second offence ends the run with
  exit 1 and no test commit. A NEEDS_INFO round keeps its test files
  uncommitted in `test_dir`: the state file lists them, the clean-tree check
  tolerates exactly those paths, and the next round commits or removes
  them. Do not commit them by hand; a file you commit becomes one the
  reviewer did not write and is protected like any other
- posts review and validation results as PR comments; on a repository that
  is not private the comments are summaries (verdict, model, cost, finding
  ids with severity and location, test files, AC coverage, validation exit
  codes, diagnosis cause) and the PR body withholds the `Request` section;
  the full text stays in the state directory
- on `revali merge` (human-started, refused unless the last run ended READY
  TO MERGE and HEAD has not moved): waits for CI checks if the PR has any,
  then `gh pr merge --<method> --delete-branch`, which also deletes the
  local branch and checks out the base branch; then `git pull --prune` and
  deletion of the branch's state directory. In a linked worktree whose base
  branch is checked out elsewhere: `gh pr merge --<method>` without
  `--delete-branch`, then `git push origin --delete <branch>`,
  `git fetch --prune origin <base>`, `git checkout --detach FETCH_HEAD`,
  `git branch -D <branch>`, each reported if it fails; the worktree is left
  for you to remove. When `gh pr merge` exits non-zero but `gh pr view`
  reports the PR as MERGED, the merge counts as done and the local
  follow-up still runs
- after a review round that stops before its tests are committed (a failed
  or timed-out Reviewer session, unusable output, a smoke run that fails
  twice), deletes the untracked files matching `test_file_pattern` under
  `test_dir` that the session left behind, and names them in the log. The
  same cleanup runs at the start of the next `run` when a reviewer session
  was started and its round never finished (`revali stop`, Ctrl-C, a killed
  process); the state file remembers that until the cleanup has run, so a
  dry run or a failed preflight in between does not lose it. A run that
  ended with a verdict never triggers it (a NEEDS_INFO round's uncommitted
  files are pending, not leftovers; they go only when the round after it
  is interrupted, and a pending file that is a tracked file of the
  reviewer's own is then restored from HEAD instead). A leftover that
  cannot be deleted (a file another program holds open) stays in the
  tolerated list: the next run accepts it in the tree and lists it for the
  reviewer to update or delete, the log names it for you. `revali reset`
  runs the same cleanup before it drops the state, since the drafts would
  otherwise outlive the list that tolerates them: after an interrupted
  round the whole pattern, otherwise only the pending list (your own
  untracked draft on the pattern survives); with no pending files
  and no interrupted round it touches nothing under `test_dir`, and when
  the project does not load, or a file cannot be deleted, it prints the
  paths for you to delete by hand.
  A finished
  `run --dry-run` is not an interrupted run, and neither `revali preflight`
  nor `run --dry-run` deletes.
  This is the only deletion inside `test_dir` revali performs, so keep
  your own files off `test_file_pattern`

It never modifies files outside `test_dir` and the state directory, never
commits a change to a test file the reviewer did not write,
never merges on its own, and never runs on a repo you do not own.
