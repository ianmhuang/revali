# Workflow

How a change moves from a request to a merged PR, what the three sessions
see, and how several agent sessions share one repository. The command
reference is in the README; configuration keys are in `configuration.md`.

## Why separate sessions

Each role runs in its own session with its own inputs, so nobody grades
their own work: the Developer never reviews the change it wrote; the
Reviewer gets the acceptance criteria and the diff, not the Developer's
reasoning, and derives its tests from the criteria; the Validator is a test
run, not an opinion; the diagnosis session sees only the failure output. The
models can differ by tier (`auto` puts the Reviewer one tier above the
Developer); the engine seam is where a second vendor would plug in
(only `claude` exists today). Fresh context removes the author's bias toward
its own change; it does not remove blind spots the models share, which is
why `revali stats` tracks the first-try approval rate.

## Workflow

The acceptance criteria come before the code. In a project that has
`revali.toml`, the author session (Claude Code following
`skill/SKILL.md`, or you) starts a change by writing
`.revali/<branch>/change.md` from `templates/change.md` with the
`Request`, `Goal`, numbered acceptance criteria, `Out of scope`, and
`Dependencies` sections, keeps `status: draft` in the front matter, and
shows the criteria to the user. Approval means deleting the `status: draft`
line; preflight refuses a draft (`change.md: status is 'draft'; review it and
remove the status line`), so nothing runs on unapproved criteria.
Then the author implements, writes its own tests, runs the existing suite,
fills in `What`, commits, and the user types `/revali`. The reviewer's
acceptance tests come on top of the author's tests, not instead of them.

To make an authoring session do this without being asked each time, paste
`templates/CLAUDE-snippet.md` into the project's `CLAUDE.md`.

## Several agents on one repository

One working tree has one checked-out branch, so give every agent session
its own worktree: `git worktree add ../<name> -b <branch>` from the primary
tree, then work, run and merge there. Each worktree has its own `.revali/`
state and locks, so runs in different worktrees are independent. A second
`run` in the same checkout, whatever branch it is on, is refused by
`tree.lock` with the running branch and pid; `wait --branch <b>` waits for
that run and `stop` stops it, from any branch or a detached HEAD. Sandbox
clones are keyed `<repo>/<branch>/<label>` on the WSL distro or the ssh
host, so validations of different branches never share a directory.
`merge` from a linked worktree merges the PR, deletes the remote branch,
detaches the worktree at the merged base and drops the local branch; the
worktree itself stays until you run `git worktree remove <path>` from the
primary tree, where `git pull` brings in the merge. `merge` in the primary
tree while a linked worktree holds the base branch is refused. Two clones
with the same branch checked out are not a supported layout: they would
share the PR and the sandbox directory.
Worktree mode in `merge` (no `--delete-branch`, detach, drop the local branch
by hand) applies only when the base branch is checked out in another
worktree; a linked worktree whose base is checked out nowhere else merges
like the primary tree, with `--delete-branch`, and gh checks out the base
there.

## Running

`run`, `wait` and `status` print one identity line first,
`repo: <working tree root>  branch: <branch>`, so several sessions running
revali in different checkouts can tell their output apart; every message
that reports a live or dead run carries its pid. On Windows the detached
run starts every subprocess (git, gh, claude, wsl, the `lint` line and the
`local` runner's steps) with `CREATE_NO_WINDOW`, so no console windows
appear while it works.

A working tree runs one pipeline at a time: `run` takes `.revali/tree.lock`
next to the branch lock, and a second `run` in the same checkout, on any
branch, is refused with the running branch and pid. `wait --branch <b>`
waits for another branch's run from wherever you are; `stop` stops the run
of this working tree whatever branch is checked out. Two worktrees of one
repository (`git worktree add`) have separate state directories and locks.
While it works, a run checks before spawning the reviewer, before committing
its tests, before every push and before validation that the checked-out
branch and HEAD are still its own; when another session moved them it stops
with exit 1 (stage `error`) and commits and pushes nothing from that point.

After exit code 2 the author fixes or answers in
`.revali/<branch>/response-<n>.md` (`- F1: fixed` / `- F1: wontfix: <reason>`),
commits, and runs again; each such cycle counts against `review.max_fixes`.
The exit 2 message (and the `revali wait` line) lists what blocks and also
the findings that did not block, with the blocking / non-blocking counts and
the path of `review-<n>.md`: a `low` or `medium convention` finding left
unanswered comes back as unresolved in the next round.

A run that stops without a result (the process was killed, the machine went
down, an unexpected error) is reported as such: an unexpected error is
recorded as stage `error` with exit 1 and the traceback in `logs/run.log`;
when nothing could record anything, `wait` prints `died at stage '<stage>'
without a result` and returns 1, and `status` says the same after its
`stage:` line. `revali stop` acknowledges such a run: with no live process
and no result recorded it sets stage `stopped` (exit 1) and removes the
stale lock, after which `wait` and `status` report a stopped run; a state
that already holds a result is left alone (`no run in progress`). Every
`stop` that records a run as stopped, killed or found dead, also appends a
history row (stage `stopped`, exit 1) so `stats` counts the episode; when
the state file cannot be written (a reader holding it on Windows past the
retry window) `stop` says so on one `ERROR:` line and returns 1, the process
is still killed, and the run keeps reading as dead until `stop` is run
again. The next
`run` continues where the last one stopped when it
can: if the last reviewer round was APPROVE, no validation is recorded for it,
and HEAD is still the commit that round left (the reviewer's test commit),
the run skips the reviewer and goes to validation, so the review is not paid
twice. The rule keys on that commit: amend or rebase it and the next run is a
normal new round.

## Project setup

Copy `templates/revali.toml` to the repo root and edit the commands; copy
`templates/CONVENTIONS.md` if the project has none; add `.revali/` to
`.gitignore`; paste `templates/CLAUDE-snippet.md` into the project's
`CLAUDE.md`. Before each run the author writes
`.revali/<branch>/change.md` from `templates/change.md` (branch `feature/x`
maps to directory `feature__x`); see Workflow above.

User-level options live in `~/.revali/config.toml` (see
`templates/user-config.toml`); `REVALI_HOME` overrides the directory.
