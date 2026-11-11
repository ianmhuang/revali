---
title: Run the baseline sandbox session alongside the first review round
kind: feature
author_model: claude-opus-5-5
---

## Request
As you suggest.

(M4.2 in the author's plan, approved 2026-11-10: the baseline runs in
parallel with the round 1 review, and a failing baseline stops the reviewer.)

## Goal
On a run's first pass the existing-suite baseline no longer delays the
reviewer: it starts when the other preflight checks have passed and runs while
the PR stage and the reviewer session run. A failing baseline still ends the
run the way it does today (exit 1, "branch is broken before review", no review
round recorded); the only differences are that the branch may already be
pushed with its draft PR open, and that a reviewer started in the meantime has
cost something. `[validate] parallel_baseline = false` keeps today's order.

Why: in `~/.revali/history.jsonl` (first passes with a baseline, since
2026-10-01) the baseline is the median 315 s of tinybmc's 711 s run, 115 s of
bmc-toolkit's 621 s and 64 s of line-social-bot's 624 s, and nothing in the
reviewer session needs its result.

## Acceptance criteria
- AC-1: on a first pass with a baseline to run and `parallel_baseline` on,
  the baseline sandbox session starts after lint, before the PR stage, and is
  still running while the reviewer session runs; the run's wall time with a
  slow baseline and a slow reviewer is close to the longer of the two, not
  their sum.
- AC-2: the baseline runs on the commit preflight saw (`ctx.head_sha`), not on
  whatever `HEAD` is when its clone starts.
- AC-3: a passing baseline changes nothing downstream: `state.baseline_sha`
  is that commit, written by the main thread, and validation reuses it exactly
  as today. The baseline thread never writes the state file.
- AC-4: the baseline fails before the reviewer would be spawned: no reviewer
  session is started, and the run ends with exit 1 and today's message.
- AC-5: the baseline fails while the reviewer session runs: the reviewer
  process tree is killed within a few seconds, and the run ends with exit 1 and
  today's message plus one line saying the reviewer was stopped and its cost is
  not known. No round is recorded, no test commit, push or review comment is
  made, and the reviewer's files left in test_dir are cleaned up the way an
  interrupted round's are.
- AC-6: the reviewer session returns before the baseline ends: revali waits
  for the baseline before it acts on the answer (smoke runs, test commit, push,
  comment, round record). If the baseline then fails, the answer is discarded
  and the run ends as in AC-5, except that the reviewer's reported cost is
  added to `state.cost_usd`.
- AC-7: whenever the run ends for another reason while the baseline still
  runs (the reviewer errors or times out, the PR stage fails, any Stop), revali
  waits for the baseline to end before it exits, so no sandbox session is left
  behind, and it reports the original reason as today.
- AC-8: `[validate] parallel_baseline = false` gives today's order: the
  baseline inside preflight, nothing pushed and no reviewer when it fails. A
  dry run, a second pass and a `kind: docs` change run no baseline either way.
- AC-9 (corrected during implementation, see What): the history row's
  `stage_s.preflight` no longer contains the baseline when it ran in
  parallel; `sandbox_s.baseline` still records it and overlaps the `pr` and
  `review` stages, and a wait for it after the reviewer answered counts in
  `review`; docs/files.md says so.
- AC-10: docs updated: docs/side-effects.md (with the default, a failing
  baseline can leave the branch pushed and a draft PR open), docs/workflow.md
  (stage order), docs/configuration.md and templates/revali.toml
  (`parallel_baseline`).
- AC-11 (measured): one real revali run of this branch shows the overlap in
  run.log, with its first-pass wall time and stage times recorded in `What`
  next to the median of revali's first passes on main.

## Out of scope
- The reviewer's own time (M4.1: effort and the test writer are separate work).
- Cancelling a running baseline: it is waited for, never killed.
- Smoke and validation sandbox sessions keep their order.

## Dependencies
none

## What
- `revali/procs.py`: `run(..., cancel=Event)` takes a path of its own
  (`_run_cancellable`): Popen plus `communicate` in 0.25 s slices, the input
  sent with the first slice. When the event is set the child is stopped
  (Windows: `kill_tree`, i.e. `taskkill /T /F`; POSIX: `terminate()`, then
  `kill()` after 5 s) and `ProcCancelled` is raised; the timeout still
  applies. The child is not given a session of its own, so `revali stop`
  still reaches it through the run's process group on POSIX. Without
  `cancel` nothing changes for any caller.
- `revali/engines/base.py`: `EngineRequest.cancel`, and `SessionCancelled`
  (a `Stop`, exit 1, no cost). `engines/claude.py` passes the event to
  `run` and turns `ProcCancelled` into `SessionCancelled`.
- `revali/validate.py`: `baseline()` now calls `run_baseline(ctx, ref, log)`,
  which runs the sandbox session on `ctx.head_sha` (was `"HEAD"`), returns
  the commit it passed on and touches no state; `baseline_steps(ctx)` says
  whether there is a baseline at all. `BaselineJob` runs it on a daemon
  thread (`revali-baseline`): `failed` is the event the reviewer's session
  is cancelled through; `raise_if_failed()` (no wait), `settle()` (wait,
  log "waiting for the baseline to finish", raise its Stop with a note or
  record `baseline_sha` from the main thread), `record()` (store a passed
  sha once it ended), `finish()` (join). A crash in the thread becomes a
  Stop. `with_note()` puts a line under the Stop's first line.
- `revali/review.py`: `run_round(..., baseline=job)`. Before each attempt
  the reviewer is not spawned when the baseline has already failed; the
  session gets `cancel=job.failed`; after it returns and its cost is
  counted, `settle` waits for the baseline and, if it failed, raises with
  "the reviewer's answer was discarded ($x counted in the run's cost)", so
  no smoke run, commit, push, comment or round record happens and
  `run_round`'s existing cleanup removes the reviewer's files.
- `revali/pipeline.py`: the preflight hook starts the job when
  `[validate] parallel_baseline` is on and there is a baseline to run, and
  runs the old sequential `baseline()` otherwise. The rest of `_stages`
  moved to `_after_preflight(..., baseline)`, wrapped so that on a Stop the
  run waits for the baseline and, when it failed and its failure was not the
  Stop already, raises the baseline's Stop with a note (a cancelled reviewer:
  "the reviewer session was stopped when the baseline failed; its cost is
  not known"; anything else: "the run also stopped for this: <its first
  line>"). A `finally` always waits for the thread and records a passed
  baseline's sha, so a run that ends for another reason still keeps it.
- `[validate] parallel_baseline = true` in `defaults.toml`, `ValidateCfg`,
  `templates/revali.toml`; documented in docs/configuration.md,
  docs/workflow.md (stage order), docs/side-effects.md (push and draft PR
  can happen before a failing baseline is known), docs/files.md (`stage_s`
  versus `sandbox_s`).
- Test support: the fake runner takes `block_until` / `block_timeout_s` /
  `delay_after_block_s` per label and logs `t0`, `t1`, `held`, `finished`;
  the claude stub takes `touch` (writes its start time), `sleep` and
  `done_marker`. A held baseline only ends once the reviewer has started, so
  the overlap tests cannot pass on a sequential run.
- Tests: `tests/test_parallel_baseline.py` (22: AC-1 to AC-10 and the
  cancellable `procs.run`); `RepoCase.sequential_baseline()` in
  tests/helpers.py.
- Existing tests that pin the old order now say so in their fixture, their
  assertions unchanged:
  `test_validate.BaselineTests.test_broken_baseline_stops_before_review` and
  `test_pipeline.ApprovePath.test_preflight_time_covers_the_baseline_even_when_it_fails`
  (author's), and, by Ian's decision, the reviewer's
  `test_review_stagetiming.TheTimingLine.test_a_run_that_stops_in_preflight_exit_1_still_names_preflight`:
  each calls `self.sequential_baseline()` first. With the default those three
  outcomes (no PR, preflight holding the baseline, a run ending in preflight)
  cannot happen by design; `parallel_baseline = false` keeps them, and these
  tests now guard that. The parallel counterparts are in
  `test_parallel_baseline`.
- `test_review_suitereuse...test_a_passed_baseline_is_recorded_before_the_review_starts`
  failed on the first implementation (a reviewer error left a passed
  baseline unrecorded); fixed in the code (`BaselineJob.record` in the
  `finally`), the test is untouched.
- AC-9 as first written said the stages of a run could sum to more than its
  wall time. They cannot: `Timing` runs one stage clock at a time and the
  baseline only goes to `sandbox_s`; the AC now says what happens instead.
- Suite: WSL 2026 tests OK (5 skipped); Windows host from PowerShell 2026 OK
  (2 skipped), 296 s.

## Why
