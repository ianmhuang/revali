---
title: Run the suite on a Windows leg in CI and let merge wait for it
kind: feature
author_model: claude-opus-5-5
---

## Request
照你建議，等它

(M3.2.3 in the author's plan. M3.2.2 (#66) made the suite pass with an 8.3 short temp
directory, the cause of the 26 Windows failures in probe run 42928499405. That probe's
Windows leg took about 28 minutes against about 6 for Ubuntu, so the author proposed that
`revali merge` wait for it, with this project's `checks_timeout_min` raised to about 45 and a
`timeout-minutes` on the job, instead of running the Windows leg on main only. The user
accepted.)

## Goal
Every push runs the suite on Windows as well as Linux, and `revali merge` of revali itself
waits for the Windows leg instead of giving up while it is still running.

## Acceptance criteria
- AC-1: `.github/workflows/test.yml` has three matrix legs: ubuntu-latest on the pyproject
  target (3.11), ubuntu-latest on 3.x (the lint leg, as now), and windows-latest on 3.x.
  Lint still runs on the one Ubuntu 3.x leg only; every leg runs the suite unconditionally
  and `fail-fast` stays false.
- AC-2: the test job sets `timeout-minutes`, lower than the `checks_timeout_min` in revali's
  own `revali.toml`, which is set there to 45. A hung leg is then cancelled and reported as
  failed before `revali merge` stops waiting. `defaults.toml` and `templates/revali.toml`
  keep 30 for other projects.
- AC-3: the two tests that pin the legs, `tests/test_ci_workflow.py::Workflow.test_matrix_legs`
  and the reviewer's `tests/test_review_ciworkflow.py::Matrix.test_two_legs`, expect the
  three legs of AC-1. The reviewer test is edited because this request changes what it pins;
  What says so, and no other assertion in it changes.
- AC-4: CONVENTIONS.md and docs/workflow.md say CI runs the suite on Linux and Windows on
  every push and that `revali merge` waits for it; the workflow's header comment no longer
  says the Windows leg waits for the suite to pass there.
- AC-5: on this branch's push the Windows leg passes. (Manual check: the run id, each leg's
  result and duration are recorded in What before `revali merge`.)

## Out of scope
- A macOS leg.
- Making the Windows suite faster (the M4 performance work may).
- Changing the 30-minute default for other projects.

## Dependencies
M3.2.2 (#66), merged.

## What
- `.github/workflows/test.yml`: a third leg, windows-latest on 3.x (no lint marker), and
  `timeout-minutes: 40` on the test job, which applies to every leg. The header comment
  names Linux and Windows and says why the merge wait is raised.
- `revali.toml` (revali's own): `[merge] checks_timeout_min = 45`. `defaults.toml` and
  `templates/revali.toml` keep 30.
- CONVENTIONS.md, docs/workflow.md: CI runs the suite on Linux and Windows; workflow.md also
  gives the 45 / timeout-minutes reason.
- Tests: `tests/test_ci_workflow.py` `test_matrix_legs` expects the three legs, the docs test
  the new wording, and a new `test_a_hung_leg_ends_before_merge_stops_waiting` (AC-2) pins one
  `timeout-minutes` on the job below revali.toml's 45 and the 30 in the defaults and the
  template. Mutation check: dropping `timeout-minutes` or setting it to 50 fails it.
- Reviewer test changed (AC-3): `tests/test_review_ciworkflow.py::Matrix.test_two_legs` is
  renamed `test_three_legs` and its expected list gains `("windows-latest", "3.x")`; the name
  would otherwise be wrong. Nothing else in that file changes. It pins the legs this request
  changes, so it fails on the workflow the user asked for without the edit.

## Why
See Request.
