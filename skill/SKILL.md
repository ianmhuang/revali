---
name: revali
description: Send the current branch through revali (independent review, acceptance tests, sandbox validation, manual merge). Use only when the user types /revali.
---

# revali

Only act on this skill when the user typed `/revali`. Do not decide on your
own that a change is ready; typing the command is the user's authorization
for the git actions revali performs (push, draft PR, test commits).

## Before invoking

1. Everything is committed on a feature branch (not the base branch).
2. Write `.revali/<branch>/change.md` from `templates/change.md` in the
   revali repo. Branch `feature/x` maps to directory `feature__x`.
   - `request`: the user's instruction to you, verbatim, original language.
   - `kind`: feature | fix | docs.
   - Acceptance criteria as `- AC-n: ...`, each one checkable by a test.
   - `author_model`: your model id.
3. Say "invoking revali" to the user.

## Invoking

```
python "<revali dir>/revali.py" run
python "<revali dir>/revali.py" wait --timeout 9m
```

`run` detaches and returns at once. Call `wait` repeatedly until it returns
something other than 4. Use the Bash tool's background mode if it has one.

## Acting on the result

- `2` (ACTION NEEDED): read the printed findings / question / rebase request,
  do what it says, commit, run again. If a question needs the user, ask them.
- `1` (ERROR): report the message to the user and stop; do not retry blindly.
- `3` (NEEDS A HUMAN): summarise both sides' reasons to the user and stop.
- `0`: report "ready to merge" with the summary (tests landing, rounds, cost).
  Do not merge. The user runs `python "<revali dir>/revali.py" merge` when they
  decide to; if they ask you to run it, run it in the foreground and relay the
  result (it waits for CI checks, so allow up to the configured timeout).

Never use `override`. Do not edit files under `test_dir` that the reviewer
wrote unless a finding asks for it, and say so in `response-<n>.md`.

## Ending the day

Run `revali status`; if a run is in progress, either wait for the stage to
finish or `revali stop`. State resumes on the next `run`.
