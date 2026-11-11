# Reviewer catch-rate benchmark

`tools/review_bench.py` measures how often the reviewer finds a bug it is known to have
found before. Each case is a real round-1 review of one of revali's own PRs in which the
reviewer named a correctness bug; the case replays that round's head commit and description
against the reviewer as it is configured now, and checks whether a finding names the same
place.

```
python tools/review_bench.py --budget-usd 15                       # every case under bench/cases/
python tools/review_bench.py --budget-usd 15 --model claude-sonnet-5-5
python tools/review_bench.py --budget-usd 5 pr69-staged-removal    # one case
python tools/review_bench.py --budget-usd 15 --cases ../private-cases --out ../bench-out
```

`--budget-usd` is required. Before each case the tool adds that session's own cap (`[review]
budget_usd` of the case's repository, or `--session-budget-usd`) to what it has spent; when
that would pass the budget the case and every later one are reported as `not-run`. revali's
own cap is $8, so a $15 budget runs three cases only with a lower `--session-budget-usd`. A session that fails
still costs what it spent, and that counts. `--model` sets the reviewer model for every case;
without it the case repository's own configuration chooses, as `revali run` would.

## What a run does

For each case: `git clone` the repository into a temporary directory, fetch the refs the
case lists, remove the remote, check out the head commit on the case's branch, put the
case's `change.md` in the state directory, build the round-1 prompt with revali's
`build_prompt` and run one session with `spawn_reviewer`. Nothing is pushed, no `gh`
command runs, the sandbox and the reviewer's own tests are not run, and the clone is
deleted afterwards. The output directory (`--out`, else a new temporary directory) keeps
`<case>/prompt.md`, `<case>/review.raw.json`, `<case>/answer.json` and `results.json`.

The prompt, schema and checklist are the ones of the revali running the benchmark; the case
repository contributes its own `revali.toml` (model, budget, test pattern, project
checklist). A case whose old `revali.toml` this revali no longer accepts ends as `error`.

## Scoring

A case is **caught** when a finding of kind correctness or security names the expected
file at a line inside the expected range widened by `--line-margin` (15 by default).
It is **strict** when that finding is medium or high and the verdict is
`CHANGES_REQUESTED`, **loose** otherwise. Other findings are listed under the case, not
scored. The summary line gives both rates over the cases that were scored (not `error`,
`malformed` or `not-run`) and the total cost.

What the number does not say:

- Each case is one sample. The same reviewer on the same commit answers differently from
  run to run; a single run of three cases shows whether the scoring works, not a rate.
- A finding at the right place may describe something else, and a correct finding may
  point a few lines away from where the original one did. Read `answer.json` before trusting
  a borderline case either way.
- The cases are bugs a reviewer already caught once, so they are within its reach.

## Building a case from an archived round

`revali merge` archives `.revali/<branch>/` under `~/.revali/archive/<owner>__<repo>/
<pr>-<branch>/`. A round that asked for changes for a correctness bug gives:

- `head`: `head_sha` of `review-<n>.json`. revali adds fixes on top of it, so it stays
  reachable through `refs/pull/<pr>/head` after the branch is deleted.
- `base`: `git merge-base <head> origin/main`.
- `change.md`: the text between "## The author's description (change.md)" and "## The diff"
  in `logs/prompt-r<n>-1.md`. The archived `change.md` is the final one; its What may
  already describe the fix.
- `[expected]`: `file` and `line` of the finding, and its first sentence as the
  description.

```toml
[case]
repo = "https://github.com/ianmhuang/revali"   # a URL or a local path
fetch = ["refs/pull/69/head"]
base = "<40-hex sha>"
head = "<40-hex sha>"
branch = "feature/state-dir-untracked"

[expected]
file = "revali/preflight.py"
lines = [188, 188]                             # first and last line, 1-based
description = "The check reads the index only"

[source]
pr = 69
round = 1
finding = "F1"
```

A case from a private repository does not belong in this public repository: keep it in a
directory of its own and pass `--cases`.
