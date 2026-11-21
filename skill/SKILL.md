---
name: revali
description: Send the current branch through revali (independent review, acceptance tests, sandbox validation, manual merge). Use only when the user types /revali.
---

# revali

A change goes through three phases. The user opens two of them by typing:
approval of the acceptance criteria, and `/revali`. Never run revali on your
own; typing the command is the user's authorization for the git actions it
performs (push, draft PR, test commits).

## Phase 1: acceptance criteria, before any code

Do this when a task starts in a repository that has `revali.toml` (the
project's CLAUDE.md says so; see `templates/CLAUDE-snippet.md`).

1. Create the feature branch (not the base branch). When another session
   is active in this repository, create it in a worktree of your own
   (`git worktree add ../<name> -b <branch>` from the primary tree) and work
   there: a checkout runs one revali pipeline at a time, and one working
   tree has one checked-out branch.
2. Write `.revali/<branch>/change.md` from `templates/change.md` in the
   revali repo. Branch `feature/x` maps to directory `feature__x`. Keep
   `status: draft`. Fill in:
   - `title`: one line, becomes the PR title.
   - `kind`: feature | fix | docs.
   - `author_model`: your model id.
   - `release` (optional): the PR's release label when the kind's label
     (`[pr] kind_labels`: feature minor, fix patch, docs docs) is wrong for
     it, e.g. `release: minor` for a fix that adds a flag, `release: major`
     for a change users must prepare for, `release: internal` for one no
     user of the project can notice (tests, CI, internal tooling; listed
     collapsed), `release: skip-changelog` only for what needs no line at
     all, such as a release's own version bump. The Reviewer checks it.
   - `Request`: the user's instruction to you, verbatim, original language.
   - `Goal`: what must be true when this is merged.
   - `Acceptance criteria` as `- AC-n: ...`, each an observable behaviour
     one test can check. Derive them from the request and from what you
     know of the code; list edge and failure cases, not only the happy path.
     Write each as `- AC-n: text`; a note in parentheses before the colon
     (`- AC-2 (PR #30 F1): text`) is fine, and continuation lines, indented
     or not, join the AC's text up to a blank line, a `## ` section heading or a
     list item indented no deeper than it. In that section, any other line that starts like an
     AC stops preflight; other sections are not checked. A paragraph
     that merely begins with an AC id, without a bullet or a colon, is prose.
   - `Out of scope`, `Dependencies`.
   Leave `What` and `Why` for phase 2.
3. Show the acceptance criteria to the user and wait. In plan mode, they
   are part of the plan; otherwise ask plainly. If the user changes an AC,
   rewrite it as they said. Acceptance criteria cannot be waived: revali
   refuses a change.md without at least one `- AC-n:` line (or with one
   shorter than 8 characters), `kind: docs` included. For a small change
   one AC that states the request's observable outcome is enough.
4. When the user approves, delete the `status: draft` line. revali refuses
   a draft, so nothing downstream can run on unapproved criteria.

## Phase 2: implement

Write the code and your own tests (the project's rules on tests apply as
usual; the reviewer's acceptance tests come on top, they do not replace
yours). Run the existing suite plus your tests locally before committing.
On a branch's first run (no review round yet, not `kind: docs`) revali runs
the existing suite in the sandbox as a baseline and stops with exit 1 if it
is red; by default (`[validate] parallel_baseline`) it runs alongside the
push, the draft PR and review round 1, so a red suite can leave the branch
pushed with its draft PR open. Later runs do not repeat it before review.
Fill in `What` (and `Why` if the request does not already
say) in `change.md`. Commit everything on the feature branch (with the
user's go-ahead when their rules require one for commits).

## Phase 3: `/revali`, typed by the user

Say "invoking revali", then:

```
python "<revali dir>/revali.py" run
python "<revali dir>/revali.py" wait --timeout 9m
```

`run` detaches and returns at once. Call `wait` repeatedly until it returns
something other than 4. Use the Bash tool's background mode if it has one.
When `run` itself returns 1 (a dirty working tree, another run in progress),
nothing was started: report its message and fix the cause before running
again (but see the note below).

Acting on the result:

- Any result from `run` or `wait`, exit 1 or 2, whose last line says
  "validation <n> still stands for HEAD <sha>": the branch is still ready.
  Tell the user `revali merge` may still merge it once the cause (a dirty
  tree, a changed environment) is gone, instead of committing and running
  again, which pays for a new review.

- `2` (ACTION NEEDED): the `wait` line gives the stage, the finding counts
  and the path of `review-<n>.md`; read the findings or the question there
  (for a failed validation, in `tests.md`; a rebase request or a lint
  failure is in the line itself; the full ACTION NEEDED text is at the end
  of `.revali/<branch>/logs/run.log`). Do what it says, commit, run again. If a question needs the user, ask them.
  An answer that changes an AC goes back to the user before the rerun.
  Answer each finding you do not fix in
  `.revali/<branch>/response-<n>.md` (`- F1: wontfix: <reason>`).
- `1` (ERROR): report the message to the user and stop; do not retry blindly.
  When the message says a model "hit its usage limit" or "is overloaded", tell
  the user what it quotes (the reset time for a limit) and the two ways on (run
  again later, or `revali run --reviewer-model <model>` for this run), and ask
  which; run with `--reviewer-model` only when the user names the model.
- `3` (NEEDS A HUMAN): summarise both sides' reasons to the user and stop.
- `0`: report "ready to merge" with the summary (tests landing or archived, rounds, cost)
  from the end of `.revali/<branch>/logs/run.log`, or from `revali status`.
  Do not merge. The user runs `python "<revali dir>/revali.py" merge` when they
  decide to; if they ask you to run it, run it in the foreground and relay the
  result (it waits for CI checks, so allow up to the configured timeout).
  After a merge from a worktree while another tree (normally the primary one)
  has the base branch checked out, revali leaves the worktree detached at the
  merged base; when the user asks, run `git worktree remove <path>` and
  `git pull` in the tree the merge output's `worktree:` line names. When no
  other tree has the base, the worktree ends on the updated base branch.

Do not edit files under `test_dir` that the reviewer wrote unless a finding
asks for it, and say so in `response-<n>.md`.

## Ending the day

Run `revali status`; if a run is in progress, either wait for the stage to
finish or `revali stop`. State resumes on the next `run`.
