---
name: revali
description: Send the current branch through revali (independent review, acceptance tests, sandbox validation, manual merge). Use only when the user types /revali.
---

# revali

A change goes through three phases. The user opens two of them by typing:
approval of the acceptance criteria, and `/revali`. Never run revali on your
own; typing the command is the user's authorization for the git actions it
performs (push, draft PR, test commits). Never use `override`.

## Phase 1: acceptance criteria, before any code

Do this when a task starts in a repository that has `revali.toml` (the
project's CLAUDE.md says so; see `templates/CLAUDE-snippet.md`).

1. Create the feature branch (not the base branch).
2. Write `.revali/<branch>/change.md` from `templates/change.md` in the
   revali repo. Branch `feature/x` maps to directory `feature__x`. Keep
   `status: draft`. Fill in:
   - `title`: one line, becomes the PR title.
   - `kind`: feature | fix | docs.
   - `author_model`: your model id.
   - `Request`: the user's instruction to you, verbatim, original language.
   - `Goal`: what must be true when this is merged.
   - `Acceptance criteria` as `- AC-n: ...`, each an observable behaviour
     one test can check. Derive them from the request and from what you
     know of the code; list edge and failure cases, not only the happy path.
   - `Out of scope`, `Dependencies`.
   Leave `What` and `Why` for phase 2.
3. Show the acceptance criteria to the user and wait. In plan mode, they
   are part of the plan; otherwise ask plainly. If the user changes an AC,
   rewrite it as they said. For `kind: docs` or a change too small for AC
   beyond the request, say so and let the user waive them.
4. When the user approves, delete the `status: draft` line. revali refuses
   a draft, so nothing downstream can run on unapproved criteria.

## Phase 2: implement

Write the code and your own tests (the project's rules on tests apply as
usual; the reviewer's acceptance tests come on top, they do not replace
yours). Run the existing suite plus your tests locally before committing;
revali's preflight runs the suite again as a safety net and stops with exit
1 if it is red. Fill in `What` (and `Why` if the request does not already
say) in `change.md`. Commit everything on the feature branch.

## Phase 3: `/revali`, typed by the user

Say "invoking revali", then:

```
python "<revali dir>/revali.py" run
python "<revali dir>/revali.py" wait --timeout 9m
```

`run` detaches and returns at once. Call `wait` repeatedly until it returns
something other than 4. Use the Bash tool's background mode if it has one.

Acting on the result:

- `2` (ACTION NEEDED): read the printed findings / question / rebase request,
  do what it says, commit, run again. If a question needs the user, ask them.
  An answer that changes an AC goes back to the user before the rerun.
  Answer each finding you do not fix in
  `.revali/<branch>/response-<n>.md` (`- F1: wontfix: <reason>`).
- `1` (ERROR): report the message to the user and stop; do not retry blindly.
- `3` (NEEDS A HUMAN): summarise both sides' reasons to the user and stop.
- `0`: report "ready to merge" with the summary (tests landing, rounds, cost).
  Do not merge. The user runs `python "<revali dir>/revali.py" merge` when they
  decide to; if they ask you to run it, run it in the foreground and relay the
  result (it waits for CI checks, so allow up to the configured timeout).

Do not edit files under `test_dir` that the reviewer wrote unless a finding
asks for it, and say so in `response-<n>.md`.

## Ending the day

Run `revali status`; if a run is in progress, either wait for the stage to
finish or `revali stop`. State resumes on the next `run`.
