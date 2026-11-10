# Workflow

How a change moves from a request to a merged PR, what the three sessions
see, and how several agent sessions share one repository. The command
reference is in the README; configuration keys are in [docs/configuration.md](configuration.md).

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
[skill/SKILL.md](../skill/SKILL.md), or you) starts a change by writing
`.revali/<branch>/change.md` from [templates/change.md](../templates/change.md) with the
`Request`, `Goal`, numbered acceptance criteria, `Out of scope`, and
`Dependencies` sections, keeps `status: draft` in the front matter, and
shows the criteria to the user. Approval means deleting the `status: draft`
line; preflight refuses a draft (`change.md: status is 'draft'; review it and
remove the status line`), so nothing runs on unapproved criteria.
Then the author implements, writes its own tests, runs the existing suite,
fills in `What`, commits, and the user types `/revali`. The reviewer's
acceptance tests come on top of the author's tests, not instead of them.

To make an authoring session do this without being asked each time, paste
[templates/CLAUDE-snippet.md](../templates/CLAUDE-snippet.md) into the project's `CLAUDE.md`.

## Several agents on one repository

One working tree has one checked-out branch, so give every agent session
its own worktree: `git worktree add ../<name> -b <branch>` from the primary
tree, then work, run and merge there. Each worktree has its own `.revali/`
state and locks, so runs in different worktrees are independent. A second
`run` in the same checkout, whatever branch it is on, is refused by
`tree.lock` with the running branch and pid; `wait --branch <b>` waits for
that run and `stop` stops it, from any branch or a detached HEAD. Sandbox
clones are keyed `<repo>/<branch>-<key>/<label>` on the WSL distro or the ssh
host, where `<key>` comes from the checkout's path and the exact branch
name (earlier revali versions used `<repo>/<branch>/<label>`), so validations
of different branches, and of different repositories whose directories
share a name, never share a directory.
`merge` from a linked worktree merges the PR, deletes the remote branch,
detaches the worktree at the merged base and drops the local branch; the
worktree itself stays until you run `git worktree remove <path>` from the
primary tree, where `git pull` brings in the merge. `merge` in the primary
tree while a linked worktree holds the base branch is refused. Two clones
with the same branch checked out are not a supported layout: they would
share the PR.
Worktree mode in `merge` (no `--delete-branch`, detach, drop the local branch
by hand) applies only when the base branch is checked out in another
worktree; a linked worktree whose base is checked out nowhere else merges
like the primary tree, with `--delete-branch`, and gh checks out the base
there.

## Running

`run`, `wait` and `status` print one identity line first,
`repo: <working tree root>  branch: <branch>`, so several sessions running
revali in different checkouts can tell their output apart; every message
that reports a live or dead run carries its pid. Which revali ran is in the
run's own records: preflight's first line, `repo <root>, branch <branch>,
revali <build>` (in `logs/revali.log`, and in `logs/run.log` for a detached
run), the `started revali run` line, and the history row's `revali_build`.
The build is the version plus `git describe --tags --always --dirty` of the
checkout revali was loaded from, e.g. `0.3.0 (v0.3.0-27-g4fbdb4e)`, or the
version alone when that directory is not the root of a git checkout or git
cannot describe it (git missing, timed out or failed). On Windows the detached
run starts every subprocess (git, gh, claude, wsl, the `lint` line and the
`local` runner's steps) with `CREATE_NO_WINDOW`, so no console windows
appear while it works. `run` checks the working tree before it detaches:
when preflight would refuse it (an uncommitted change outside `.revali/`,
other than a NEEDS_INFO round's pending files) it prints the same `working
tree is not clean` message with the paths and returns 1 without starting
anything; the refusal is recorded as stage `error` with exit 1, so `wait`
and `status` report it rather than the previous run's result. After an
interrupted review round it leaves the check to the run itself, which
first cleans up what that round left. `status` then prints the stage, the last message,
the round / fix-cycle / cost counters, the PR, and, once the branch has a
review round, `tests: commit` or `tests: archive`: the mode its rounds run
in, fixed for the branch (`[review] tests` in
[docs/configuration.md](configuration.md)).

A run goes preflight, PR, review, validate. Preflight runs `git fetch
origin <base>` and compares the branch against `origin/<base>`; a fetch
that fails is tried once more after a short pause, since worktrees of one
repository share `refs/remotes/` and of two fetches of the same ref at the
same moment one fails after the other has moved the ref (`revali merge`
from a linked worktree retries its fetch of the base the same way; from the
primary tree it runs `git pull --prune`, which is not retried). When both attempts fail,
preflight prints a note with git's error and compares against the local
`<base>`, which may be behind the remote. On the first pass of a branch
(no review round yet, not a dry run, not `kind: docs`) preflight ends with
the baseline: the existing suite on the commit preflight checked, in the
sandbox. With `[validate] parallel_baseline` (the default) the baseline only
starts there and runs on while the PR stage and the round 1 reviewer run;
revali acts on the reviewer's answer (smoke run, test commit, push, PR
comment) only once the baseline has passed, and `logs/revali.log` says when
it is waiting for it. A baseline that fails stops the reviewer session, or
discards its answer when it came first, and the run ends with exit 1 and
"branch is broken before review" as it always did, with a line saying what
became of the reviewer. A run that ends for any other reason while the
baseline runs waits for it before exiting, so no sandbox session is left
behind. `parallel_baseline = false` runs the baseline inside preflight,
before anything is pushed or paid for.

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
The cycle is owed from the exit 2 of a CHANGES_REQUESTED round or a failed
validation until a run passes preflight on a new commit and counts it, so a
run that stops before the review (preflight, the refusal before detaching,
`revali stop`, an unexpected error) neither loses nor repeats a fix cycle:
the next run on the HEAD the reviewer saw is still refused with "nothing
changed", and the next run on a new commit counts the cycle once.
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

revali's own CI ([.github/workflows/test.yml](../.github/workflows/test.yml)) runs lint and the
suite on Linux on every push, and `revali merge` waits for it as it waits for any
project's checks.

## Project setup

Copy [templates/revali.toml](../templates/revali.toml) to the repo root and edit the commands; copy
[templates/CONVENTIONS.md](../templates/CONVENTIONS.md) if the project has none; add `.revali/` to
`.gitignore`, or to `.git/info/exclude` if the line must stay out of the
tracked `.gitignore` (see [docs/side-effects.md](side-effects.md)); paste
[templates/CLAUDE-snippet.md](../templates/CLAUDE-snippet.md) into the project's
`CLAUDE.md`. Before each run the author writes
`.revali/<branch>/change.md` from [templates/change.md](../templates/change.md) (branch `feature/x`
maps to directory `feature__x`); see Workflow above.

User-level options live in `~/.revali/config.toml` (see
`templates/user-config.toml`); `REVALI_HOME` overrides the directory.
