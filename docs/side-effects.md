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
- in preflight, `git fetch origin <base>` (tried once more when it fails),
  which updates `refs/remotes/origin/<base>`, shared by every worktree of
  the repository, and `FETCH_HEAD`
- outside your repository, in the checkout revali itself runs from (when it
  runs from a git checkout): `git describe --tags --always --dirty` once per
  `run`, `preflight`, `merge`, `archive` or `stop` process, for the build named in the
  logs and the history row. The refresh behind `--dirty` takes that
  checkout's `.git/index.lock` for a moment and may write its index back
  (stat data only, no content changes); a `git add`, `commit` or `pull` you
  run in that checkout at the same moment can fail with `index.lock: File
  exists` and works when repeated
- `git push -u origin <branch>` and `gh pr create --draft`. With `[validate]
  parallel_baseline` (the default) this happens while the baseline is still
  running, so a branch whose existing suite fails can be left pushed with its
  draft PR open; the run still ends with exit 1, "branch is broken before
  review", and no review round. With `parallel_baseline = false` a failing
  baseline stops the run before anything is pushed
- `git push --force-with-lease` instead of a plain push on the first push
  after the branch's history was rewritten: in the default commit mode when
  the reviewer's test commits are no longer in HEAD (a rebase or reset; the
  review starts over from round 1), and in archive mode below. A rejected
  push ends the run with exit 1
- `gh pr edit <n> --body-file <file>` to refresh the status table in the PR
  body: after a NEEDS_INFO or CHANGES_REQUESTED round, when validation
  starts, after a failed validation and after a passing one; and `gh pr ready
  <n>` once a validation passes, which takes the PR out of draft. A failure
  of either is a warning in the log and the run goes on; a dry run does
  neither
- with `runner = "local"`, for each sandbox session (baseline, the
  reviewer's smoke run, validation, the base rerun): `git worktree add
  --detach` of the commit into a temporary directory, registered in your
  repository's `.git`, then `git worktree remove --force` and `git worktree
  prune` when the session ends; a failed cleanup is logged and never stops
  the run
- when a run reuses an open PR whose title differs from change.md's `title`,
  `gh pr edit <n> --title <title>`, since a squash merge takes the PR title as
  the commit subject on the base branch; a failure is a warning in the log and
  the run goes on. A title changed on GitHub by hand is overwritten the same
  way: change.md is the source
- with `[pr] release_labels` not empty (the default), after the PR is opened
  or reused: `gh pr edit <n> --add-label <label> --remove-label <others>` so
  that of the labels in `release_labels` the PR carries exactly the one
  change.md asks for (`release:`, else `[pr] kind_labels` for its kind), or
  none of them when it asks for none; other labels are left alone, and a
  label set by hand is overwritten the same way. A wanted label the
  repository lacks (`gh api repos/{owner}/{repo}/labels/<label>` answers
  404) is first created with `gh label create <label> --description
  "Release notes group (set by revali)"`. Any
  failure is a warning in the log and the run goes on; a dry run only names
  the label
- runs `[project] format` on the host after each reviewer attempt, just
  before the `lint` line below, and only over the files under `test_dir`
  that differ from HEAD then (`{files}`): those the reviewer wrote or
  changed in the attempt, and with `[review] tests = "archive"` also the
  earlier rounds' files placed back for the round; the formatter rewrites
  them in place, uncommitted. An empty `format` line skips this
- runs `[project] lint` a second time after the reviewer has written its test
  files, over the working tree with those files in it (preflight ran the same
  line before the reviewer started, so a failure now is the reviewer's files);
  on a failure the reviewer is sent back with the command and the output
  tail. The reviewer is sent back once per round, whatever the reason (lint,
  the smoke run, a protected test file, an uncovered AC), so a lint failure on
  the attempt after that bounce ends the run with exit 1 and no test commit,
  even when it is the first lint failure. A
  NEEDS_INFO round is checked the same way, since its files stay in the tree.
  For the length of that lint command the reviewer's new, untracked files
  are marked intent-to-add (`git add -N`), so a lint line that only checks
  the files `git ls-files` lists sees them too; `git reset -q -- <files>`
  unmarks them afterwards, whatever lint returned.
  An empty `lint` line skips this and preflight prints a note saying so
- commits the reviewer's test files into `test_dir`, with
  `Co-Authored-By: Claude` and `Revali-Round: <n>` trailers; only new files
  and the files the reviewer wrote in an earlier round. The
  `Revali-Round` trailer is what marks a file as the reviewer's own: on every
  run the commits between the base and HEAD that carry it, and the files
  under `test_dir` they added or modified, are read back into the state, so
  a rebase or amend that gives those commits new SHAs (the review then
  starts over from round 1) keeps them the reviewer's. The newest commit
  in that range that added a path decides whose it is: a file you delete
  in one commit and re-create under the reviewer's file name in a later
  commit without the trailer is yours from then on (the state drops it
  and `run.log` says so). An edit changes nothing, and so does replacing
  the content in a single commit, which git records as a modification;
  to hand a file back, delete it in a commit of its own and let the
  reviewer re-create it. A rewrite that
  drops the trailer, such as squashing the reviewer's commit into your
  own, turns those files into existing files the reviewer must not
  modify. Any other
  tracked file under `test_dir` the reviewer modified or deleted is
  restored from HEAD (`git checkout HEAD -- <path>`, index and working
  tree) before the commit and the reviewer is sent back with the names already
  taken; after the round's one bounce (for this or any other reason) such a
  change ends the run with exit 1 and no test commit. After every reviewer attempt, any change
  outside `test_dir` and the state directory that `git status` reports is
  undone: a tracked file is restored with `git checkout HEAD -- <path>`, a
  new file or directory is deleted (a staged one is unstaged first with
  `git rm --cached`), and the run stops with exit 1, "the reviewer modified
  files outside <test_dir> (reverted)". revali cannot tell who made the
  change, so an edit or new file of yours outside `test_dir` made while the
  reviewer session runs is lost; ignored files are not touched.
  A NEEDS_INFO round keeps its test files
  uncommitted in `test_dir`: the state file lists them, the clean-tree check
  tolerates exactly those paths, and the next round commits or removes
  them. Do not commit them by hand; a file you commit becomes one the
  reviewer did not write and is protected like any other.
  With `[review] tests = "archive"` none of this commits anything: the
  reviewer writes into `test_dir` as before and the `lint` line and the
  smoke run see the files there, but at the end of every round (a
  NEEDS_INFO round too) they are moved to `.revali/<branch>/tests/<their
  repository path>`, the tree is clean again, HEAD is unchanged and no
  push follows. The next round starts by copying them back into `test_dir`
  (untracked) for the reviewer to update or delete; a file the reviewer
  deletes leaves the archive and the state. Validation and the base rerun
  copy the archived files into the sandbox clone at the same paths. The
  state's list of the reviewer's files is rebuilt from that directory on
  every run (`revali reset` loses none of them); an archived path that HEAD
  tracks is yours and is removed from the archive, the log says which. A
  rebase or amend does not restart the review in this mode (there is no
  reviewer commit to lose); the next push is forced with lease. The mode
  is fixed once a branch has review rounds: a run with the other value
  stops with exit 2 before it pushes anything
- posts review and validation results as PR comments; on a repository that
  is not private the comments are summaries (verdict, model, with
  `run --reviewer-model` also the reason, which names the author's model,
  cost, finding
  ids with severity and location, test files, AC coverage, validation exit
  codes, diagnosis cause); the full text stays in the state directory. The
  PR body is change.md without its `Request` section on every repository,
  private or not (the user's verbatim words stay local; Goal, acceptance
  criteria, Out of scope, Dependencies, What, Why and the status table are
  on the PR)
- after a failed validation whose diagnosis marks a failure `cause: code`
  and `introduced_by: base` (with `[validate] issue_on`, the default): one
  `gh issue create` as the author, title `Pre-existing: <test> fails on
  <base>` (`Pre-existing: <test> and <n> more fail on <base>` when there are
  several, `<test>` the first in sort order), body from [templates/issue.md](../templates/issue.md), `--label bug` only when
  `gh label list` shows that label, `--assignee <author>` unless
  `issue_assignee = ""`; on a repository that is not private the output,
  the diagnosis text and the suggested fix are withheld from the body. The
  body passes the same credential scan as a comment. A second validation
  whose base-introduced failures an issue of this branch already names
  opens nothing. Before `gh issue create`, `gh issue list --state open
  --limit 200 --json number,title,url`: when an open issue has exactly
  that title (another branch or worktree hit the same bug first), it is
  reused and nothing is opened; the log says it was opened outside this
  branch, the branch state records it so later validations and `Fixes #n`
  at merge treat it like one revali opened, and `stats` does not count it
  as opened. Two runs listing before either has opened its issue still
  open two. A failing `gh` call here is a warning in the log (a failed
  listing opens the issue as before); the validation result and exit
  code stand
- once `revali merge` has merged the PR, for every such issue that a commit
  in `<base sha>..HEAD` references with `Fixes #n`, `Closes #n` or
  `Resolves #n`, or another form of those keywords (`fix`, `fixed`, `close`,
  `closed`, `resolve`, `resolved`, in any case) followed by `#n`: one
  `gh issue comment` naming the merged PR, the commit, and the diagnosis
  cause and suggested fix recorded when the issue was opened (for an issue
  reused because it was open already, the ones recorded when this PR hit
  the failure; the fix text withheld on a repository that is not private).
  Closing the issue is left
  to GitHub's handling of those keywords
- before `revali merge` waits for CI (next item): `gh pr view <n> --json title`, and
  `gh pr edit <n> --title <title>` when the title differs from change.md's;
  when that edit fails nothing is merged (exit 1). A title that cannot be read,
  or a missing change.md, is a warning and the merge goes on. Then, with
  `[pr] release_labels` not empty, `gh pr view <n> --json labels` and the
  same label edit (and creation) as during a run; here every failure is a
  warning and the merge goes on, since labels never reach the base branch.
  A change.md edited since the run to a `release:` outside the list changes
  no label and is a warning
- on `revali merge` (human-started, refused unless a run ended READY TO
  MERGE, no run has started its `pr` stage since, and HEAD has not moved; a
  later run that stopped before that stage does not count): with `[merge]
  wait_for_checks` (the default), waits for CI checks if the PR has any, up to
  `[merge] checks_timeout_min` (30); failed or cancelled checks end the merge
  with exit 2, checks still pending with exit 1; then `gh pr merge --<method> --delete-branch`, which also deletes the
  local branch and checks out the base branch; then `git pull --prune`, tried
  twice (revali checks out the base first when gh did not, and does not pull
  when that checkout fails); a pull or checkout that fails is printed as a
  note with git's error, saying the tree is not up to date, and the merge
  still ends with exit 0; then, when the tree is on the base and gh left the
  local branch behind, `git branch -D <branch>`, its result in the `local:`
  line; then
  the branch's state directory is moved to `[paths] archive_dir`
  (`~/.revali/archive/<owner>__<repo>/<pr>-<branch>/` by default; a name
  already taken gets a timestamp suffix; a move that fails leaves the
  directory where it is and says so; an empty `archive_dir` deletes it
  instead). In a linked worktree whose base
  branch is checked out elsewhere: `gh pr merge --<method>` without
  `--delete-branch`, then `git push origin --delete <branch>`,
  `git fetch --prune origin <base>`, `git checkout --detach FETCH_HEAD`,
  `git branch -D <branch>`, each reported if it fails, and the state
  directory is archived as above; the worktree is left for you to remove.
  git that fails, times out or cannot be started there does not stop the
  merge: each failure in the worktree follow-up is reported, as a note or in
  the `worktree:` line, and the exit code stays 0.
  When `gh pr merge` exits non-zero but `gh pr view`
  reports the PR as MERGED, the merge counts as done and the local
  follow-up still runs
- on `revali archive [--branch <branch>]` (human-started, for a PR merged or
  closed without `revali merge`; refused while a run holds the branch or the
  working tree): `gh auth status` and `gh pr view <n> --json state`, nothing
  that changes the PR. An open PR, or a state gh cannot report, changes
  nothing (exit 1). MERGED: the state's stage becomes `merged`, the local
  branch is deleted with `git branch -D` unless a working tree has it checked
  out or its tip is neither the head revali last saw nor an ancestor of it or
  of `origin/<branch>` (a commit made after the last run, or a new branch of
  that name); a recorded head that is one of the reviewer's test commits does
  not count when `origin/<branch>` is known and does not hold it (its push
  did not go through); a kept branch is named in the output with the reason; a history row with
  `merged_outside: true` and `stage_before` is appended (no `merged_outside`
  when `revali merge` had already recorded the merge and stopped before its
  move), and the branch's
  state directory is moved to `[paths] archive_dir` as `revali merge` moves
  it. CLOSED: stage `closed`, a history row with `stage_before` only (no
  `merged_outside`), the same move of the state directory, and the local
  branch is left alone. The remote branch, the base branch and the issues a commit fixes
  are not touched
- on `revali init` (human-started, once per project), in the working tree it runs in:
  writes `revali.toml`, `CONVENTIONS.md`, `.github/release.yml` and `CLAUDE.md` at the
  repository root from revali's templates, each only when it does not exist; appends
  the snippet block to an existing `CLAUDE.md` that lacks its heading; makes the state
  directory ignored the way the first item above describes, without the commit. No
  existing file is overwritten, nothing is committed or pushed, and gh is not called;
  the only git calls find the repository root and ask `git check-ignore`
- on `revali release <version>` (human-started, on the base branch): `git fetch origin
  <base>`, `git ls-remote origin` for the tag, and the reads `gh repo view`, `gh api` of
  HEAD's check runs and `gh release view`; when the Release does not exist yet, `gh api -X
  POST repos/{owner}/{repo}/releases/generate-notes` for the notes (GitHub stores nothing
  for it); when every check passes, an annotated tag `v<version>` on HEAD (`git tag -a`),
  `git push origin refs/tags/v<version>`, and a GitHub Release for it (`gh release create
  --verify-tag --title <title> --notes-file <file>`, the file a temporary one written
  before the tag is made and removed afterwards; a file that cannot be removed is named
  in a warning). A tag
  already on HEAD, here or on origin, is neither made nor pushed again, and an existing
  Release is left alone. A failed check, or `--dry-run`, creates and pushes nothing; no
  branch, commit or file is touched ([releasing.md](releasing.md))
- after a review round that stops before its tests are committed (a failed
  or timed-out Reviewer session, unusable output, a smoke run that fails
  twice), deletes the untracked files matching `test_file_pattern` under
  `test_dir` that the session left behind, and names them in the log. The
  same cleanup runs at the start of the next `run` when a reviewer session
  was started and its round never finished (`revali stop`, Ctrl-C, a killed
  process); the state file remembers that until the cleanup has run, so a
  dry run or a failed preflight in between does not lose it. A run that
  ended with a verdict never triggers it (a NEEDS_INFO round's uncommitted
  files are pending, not leftovers; they go when the round after it stops
  before its test commit or is interrupted, or on `revali reset`, and a
  pending file that is a tracked file of the
  reviewer's own is then restored from HEAD instead). In both cases, and
  in `revali reset` after an interrupted round (also from a state file with
  fields of the wrong type, when the fields this cleanup reads are intact:
  [files.md](files.md)), a tracked file of the
  reviewer's own from an earlier round that the session modified or
  deleted is restored from HEAD as well, and the log names it. revali
  cannot tell who changed such a file: after an interrupted round, until
  the next `run` or `reset` has cleaned up, an uncommitted change to one
  of the reviewer's files is restored whoever made it, so commit or stash
  your own edit first. With no round interrupted, preflight refuses such
  a change as before. A draft the lint gate had marked intent-to-add when
  the run was killed is unmarked and deleted like any other leftover, and
  so is any file on the pattern there that is in the index but in no
  commit. A leftover that
  cannot be deleted (a file another program holds open) stays in the
  tolerated list: the next run accepts it in the tree and lists it for the
  reviewer to update or delete, the log names it for you. `revali reset`
  runs the same cleanup before it drops the state, since the drafts would
  otherwise outlive the list that tolerates them: after an interrupted
  round the whole pattern, otherwise only the pending list (your own
  untracked draft on the pattern survives); with no pending files
  and no interrupted round it touches nothing under `test_dir`, and when
  the project does not load, or a file cannot be deleted, it prints the
  paths for you to delete by hand. With `[review] tests = "archive"` the
  same cleanups also delete the copies of the archived files the round had
  placed in `test_dir` (by the state's list, whatever their names) and,
  when the move into the archive failed, the files the reviewer left that
  were not moved yet, and the directories that deletion leaves empty (up
  to `test_dir`, which stays); the archive under `.revali/<branch>/tests/`
  keeps the previous round's content and is never touched by a cleanup. A move
  that fails part way (a file held open on Windows) is not rolled back:
  the files moved before it hold the new content, the rest the previous
  one, and the next round places that mix back for the reviewer.
  A finished
  `run --dry-run` is not an interrupted run, and neither `revali preflight`
  nor `run --dry-run` deletes.
  This is the only deletion inside `test_dir` revali performs, so keep
  your own files off `test_file_pattern`

Outside `test_dir` and the state directory it changes only what this page
lists, among them the ignore line of the first item, the revert of a reviewer's change above, the
index marks of the lint gate, the local runner's worktree, and what `merge`
and `archive` do to branches and the checkout. It never
commits a change to a test file the reviewer did not write,
never merges on its own, and never runs on a repo you do not own.
