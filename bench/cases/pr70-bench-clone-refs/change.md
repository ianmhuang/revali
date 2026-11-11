---
title: Measure how often the reviewer catches known bugs
kind: feature
author_model: claude-opus-5-5
---

## Request
As you suggest.

(M3.3.1 in the author's plan: the design of a reviewer catch-rate benchmark plus a 3-case
pilot. The user accepted the author's suggestions: cases come from real bugs a reviewer
already caught in revali's own PRs (a round-1 head commit that still has the bug, with the
finding that named it), a 3-case pilot costing about $10 per run, and 15-20 cases later
(M3.3.2) once the pilot shows the scoring holds. The goal of M3.3 is one command that runs
the cases against a chosen model and prints the catch rate.)

## Goal
`python tools/review_bench.py` runs one round-1 reviewer session per case on a throwaway
clone of the case's repository at the commit that had the bug, scores whether a finding
names that bug, and prints the catch rate, the cost and each case's result, without pushing,
opening a PR, calling GitHub's API or running the sandbox, and without going over the budget
it is given.

## Acceptance criteria
- AC-1: a case is a directory with `case.toml` (repository URL or path, the refs to fetch,
  the base and head commits, the branch name, the expected bug: file, line range, a one-line
  description, and the PR and finding it came from) and `change.md` (the description the
  reviewer saw in that round, taken from the round's prompt, not the final change.md, whose
  What may already describe the fix). The tool refuses a case with a missing or malformed
  field and names it.
- AC-2: for each case the tool clones the repository into a temporary directory, fetches the
  listed refs, checks out the head commit on the case's branch, places `change.md`, and runs
  exactly one reviewer session through revali's own prompt and engine code (`build_prompt`,
  `spawn_reviewer`) with the round-1 prompt. It never pushes, never runs `gh`, never runs the
  sandbox or the reviewer's tests, and removes the clone afterwards, keeping the prompt and
  the raw answer under the output directory.
- AC-3: `--model` sets the reviewer model for every case (default: the case repository's own
  configuration); `--budget-usd` (required) caps the total spent. Before a case starts, when
  the spend so far plus that session's own cap would pass the budget, the tool stops and
  reports the remaining cases as not run. The cost of a failed session counts too.
- AC-4: a case is "caught" when the answer has a finding whose `file` is the expected file
  and whose `line` falls in the expected range widened by a margin (`--line-margin`, default
  15), of kind correctness or security. The result says whether the matching finding is
  medium or high and whether the verdict asks for changes, so a strict rate (medium or high,
  CHANGES_REQUESTED) and a loose rate (any matching finding) can both be read. Findings
  elsewhere are counted and listed, not scored.
- AC-5: the tool prints one line per case (caught strict / loose / missed / error / not run,
  the matching finding id, severity, verdict, model, cost, duration) and the two rates with
  the total cost, and writes the same as JSON in the output directory. A session that fails
  is reported as error with its message and does not stop the other cases.
- AC-6: `bench/cases/` holds three pilot cases from revali's own PRs (#43 `pr.py` request
  section, #61 history lock window, #69 tracked state directory), each with the round-1
  `change.md` and the expected location of the finding that caught the bug.
- AC-7: the tests run the tool offline against the fixture repository and the fake claude:
  a caught case, a missed case, a finding in the right file outside the range, a session
  that fails, the budget stop, a malformed case, and that nothing is pushed and no `gh` is
  called. A manual pilot run against the real reviewer is recorded in What (command, model,
  each case's result, total cost), within $15.
- AC-8: docs: a page or section says how to run the benchmark, how a case is built from an
  archived round, and what "caught" means and does not mean (one sample per case, a
  location match is not proof the reviewer understood the bug).

## Out of scope
- More than the three pilot cases, and cases from other projects (M3.3.2; private
  repositories' cases stay outside this public repository, which `--cases DIR` allows).
- Repeated samples per case and confidence intervals.
- Planted (synthetic) bugs.
- A `revali` subcommand: the tool stays under `tools/` so the CLI does not grow.

## Dependencies
none

## What
- `tools/review_bench.py` (new, standard library plus revali's own modules):
  - `load_case` reads `case.toml` and `change.md` and names every missing or malformed field
    (`case.repo/base/head/branch/fetch`, `expected.file/lines/description`,
    `source.pr/round/finding`; `lines` must be two positive integers in order).
  - `prepare_clone`: `git clone --no-checkout` into a temp directory, `git fetch origin
    <ref>` for each listed ref, `git remote remove origin` (nothing can be pushed from the
    clone), both commits checked with `cat-file -e`, `checkout -b <branch> <head>`.
  - `locate_case` writes change.md into the clone's state directory, calls `locate` (no
    network), and fills what preflight would: `base_ref`/`base_sha`/`head_sha`, and
    `changed_files`/`excluded_files` from `diff_numstat` with the same exclude list as
    `check_diff_size` (no size limit). Then `build_prompt(ctx, State(), rdir, 1)` and
    `spawn_reviewer(..., 1, 1, None)`; `--model` sets `cfg.review.model`.
  - Budget: before a case, spent + the session cap (`[review] budget_usd`, or
    `--session-budget-usd`, added because revali's own cap is $8 and a $15 pilot would stop
    after one case) must fit `--budget-usd`, else that case and the rest are `not-run`. A
    failed session's `Stop.cost` counts.
  - `score`: the strongest correctness/security finding on the expected file within the
    range widened by `--line-margin` (15); strict when medium/high and CHANGES_REQUESTED.
    Paths compare with `./` and backslashes normalized; `line` 0 never matches.
  - Output: one line per case, the summary line, `results.json`, and per case `prompt.md`,
    `review.raw.json`, `answer.json`. Exit 1 when any case is malformed, error or not-run.
  - The clone is removed with a chmod-and-retry rmtree (git's read-only objects on Windows).
- `bench/cases/`: `pr43-request-section`, `pr61-history-lock-window`, `pr69-staged-removal`,
  built from the archived round 1 of each PR (head from `review-1.json`, base by `git
  merge-base` with origin/main, change.md cut from `logs/prompt-r1-1.md`, expected file and
  line from the medium correctness finding F1). All three heads are reachable through
  `refs/pull/<n>/head`.
- `bench/README.md`: how to run it, what a run does, scoring and its limits, how to build a
  case. It is under `bench/`, not `docs/`: `docs/` is pinned to five pages by two tests
  (one of them a reviewer's) and README is at its 170-line cap.
- `tests/test_bench_tool.py` (15, offline, fixture repo + fake claude): caught strict;
  loose for a low finding and for an approving verdict; missed for a line past the margin,
  another file, a convention finding, line 0; the margin option; a failed session that does
  not stop the next and whose cost counts; `--model` reaching the session; budget below one
  session runs nothing, the stop before the session that could pass it, a lower session cap;
  `--budget-usd` required and positive; malformed fields named and the good case still run;
  a head the repository lacks; score edge cases; the three pilot cases load. Each run test
  also checks one claude session per case on a clone that is gone afterwards, no `gh` call,
  and the source repository's refs and its origin unchanged.

AC-7 pilot, 2026-11-11, from the author's Windows host:
`python tools/review_bench.py --budget-usd 15 --session-budget-usd 4 --out <dir>` (model
from revali.toml: auto, one tier above the author, claude-fable-5-2):

| case | result | finding | verdict | cost | time |
|---|---|---|---|---|---|
| pr43-request-section | caught-strict | F1 medium pr.py:122 | CHANGES_REQUESTED | $2.62 | 265 s |
| pr61-history-lock-window | caught-strict | F1 medium state.py:492 | CHANGES_REQUESTED | $2.63 | 320 s |
| pr69-staged-removal | caught-strict | F1 medium preflight.py:188 | CHANGES_REQUESTED | $1.99 | 201 s |

Strict 3/3, loose 3/3, $7.24. The author read the three F1 texts: each describes the same
bug as the original finding (the `## Request` regex narrower than changedoc's parser; the
lock window ignoring the configured `write_retry_s`; the index-only check missing a staged
removal), at the same line. One sample each, of bugs a reviewer had already caught: this
shows the replay and the scoring work, not a catch rate.

## Why
See Request.
