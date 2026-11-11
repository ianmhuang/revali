# Reviewer catch-rate benchmark

`tools/review_bench.py` measures how often the reviewer finds a bug it is known to have
found before. Each case is a real review round of a project revali reviews, in which the
reviewer named a correctness or security bug that the next round found fixed; the case
replays that round's head commit and description against the reviewer as it is configured
now, as a first round, and checks whether a finding names the same place.

```
python tools/review_bench.py --dry-run                             # check every case, run no session
python tools/review_bench.py --budget-usd 55 --session-budget-usd 8   # every case under bench/cases/
python tools/review_bench.py --budget-usd 55 --model claude-sonnet-5-5
python tools/review_bench.py --budget-usd 8 pr69-staged-removal    # one case
python tools/review_bench.py --budget-usd 95 --session-budget-usd 8 \
    --cases bench/cases --cases ~/.revali/bench/cases --out ../bench-out
```

A session cost $2.80 to $6.10 with claude-fable-5-2; the 10 committed cases cost $43.70 and
the 18 with the private ones $81.13. The budget has to leave one session cap free for the last
case (see below), so 10 cases at a cap of $8 need about $55 and 18 about $95. `~` in a
`--cases` value is expanded by the tool, and a `--cases` value that is not a directory stops
the run before anything starts.

`--dry-run` does everything up to the session for each case (clone, isolate, configuration,
prompt, written to `<case>/prompt.md`), spends nothing and needs no budget; each case is
`ready` or `error`, and exit 1 means one is not ready. Run it after adding a case.

`--cases` may be given more than once; the cases of every directory run under one budget,
and a case name found in two directories stops the run before anything starts.

`--budget-usd` is required for a real run. Before each case the tool adds that session's own cap (`[review]
budget_usd` of the case's repository, or `--session-budget-usd`) to what it has spent; when
that would pass the budget the case and every later one are reported as `not-run`. revali's
own cap is $8, so a $15 budget runs three cases only with a lower `--session-budget-usd`. A
session that fails still costs what it spent, and that counts; one that fails without saying
what it spent (a timeout, output that is not JSON) counts as its whole cap against the
budget, and its line says the cost is unknown. `--model` sets the reviewer model for every
case; without it the case repository's own configuration chooses, as `revali run` would.

A case that fails in any way is reported as `error` and the next case runs. Each case's line
is printed and `results.json` rewritten as soon as the case ends, and once more just before
its session starts, with the case marked `started` and its session cap charged. A run stopped
with Ctrl-C marks the case `interrupted` (its cap charged if the session had started, since
the session never reports what it spent) and the rest `not-run`; a run that ends without
Python noticing (the machine shuts down) leaves the case `started`. No case found (a
mistyped `--cases`) is exit 1, and so is a case named twice on the command line.

## Continuing a stopped run

```
python tools/review_bench.py --resume ../bench-out --budget-usd 95
```

`--resume DIR` continues the run whose `results.json` is in DIR and writes to the same
directory. Cases that ended with a result (`caught-strict`, `caught-loose`, `missed`,
`error`, `malformed`) are kept and reported as kept; cases that are `started`,
`interrupted` or `not-run`, and cases in the case directories that the file does not list,
run. A `started` or `interrupted` case keeps the cap already charged for it, said in its
message, and runs again. To run an `error` case again, run it by name into a new output
directory.

`--budget-usd` is the total of every invocation: what `results.json` already charges counts
against it before the first new session. The model, session cap, line margin and case
directories come from `results.json`; given on the command line they must match it, and so
must `PROMPT_VERSION`, or the run is refused before anything starts (a set measured with two
settings has no single rate). `--out` and `--dry-run` cannot go with `--resume`. Case names
narrow the run as usual; the cases not named stay in `results.json` as they were, and the
summary always covers every case the file holds.

## What a run does

For each case: `git clone` the repository into a temporary directory, fetch the refs the
case lists, remove the remote, check out the head commit on the case's branch, point the
base branch (`[project] base_branch` of the case's repository, else the branch the source
had checked out) at the case's base commit, delete every other ref, `FETCH_HEAD` and the
reflogs, put the case's `change.md` in the state directory, build the round-1 prompt with
revali's `build_prompt` and run one session with `spawn_reviewer`. The clone then looks as
the original round did: `git log main`, `git diff main...HEAD` and the like show the base,
not today's main with the fix merged. The objects of later commits are still in the clone's
object store, reachable by hash only. Nothing is pushed, no `gh` command runs, the sandbox
and the reviewer's own tests are not run, and the clone is deleted afterwards (a clone that
could not be deleted is named on the case's line). The output directory (`--out`, else a new
temporary directory) keeps `<case>/prompt.md`, `<case>/review.raw.json`,
`<case>/answer.json` and `results.json`, which also records the revali version and
`PROMPT_VERSION` that ran.

The prompt, schema and checklist are the ones of the revali running the benchmark; the case
repository contributes its own `revali.toml` (model, budget, test pattern, project
checklist). A project that keeps `revali.toml` out of git has none in the clone: its cases
carry a copy in the case directory, which is written into the clone first and replaces any
the clone has. A case whose `revali.toml` this revali does not accept ends as `error`, and
one that is not valid TOML as `malformed`.

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
<pr>-<branch>/`. `tools/bench_case.py` turns one finding of one archived round into a case:

```
python tools/bench_case.py --list ~/.revali/archive/<owner>__<repo>      # candidates, in selection order
python tools/bench_case.py ~/.revali/archive/<owner>__<repo>/<pr>-<branch> \
    --round 1 --finding F1 --out bench/cases/<name> \
    --request "As you suggest." [--config <project>/revali.toml]
```

Only a medium or high correctness or security finding with a line, which the next round
marked resolved, makes a case; the tool refuses anything else, a head the repository cannot
reach, or an output directory that exists, and says why. `--request` replaces the body of
the `## Request` section; without it the section is copied as it was, which is right for a
private case only. `--config` copies the project's `revali.toml` into the case when the
project keeps it out of git. `--repo` overrides the repository (default
`https://github.com/<owner>/<repo>`). Run `--dry-run` on the new case before paying for it.

The selection rule, so the set is not hand-picked: per repository, PRs newest first, one case
per PR (the first qualifying finding of the earliest round that has one, which is what
`--list` prints), skipping a candidate the builder or the dry run refuses, until the
repository's quota. The committed set is revali 6 (the three pilot cases #43, #61, #69 and
the three newest others) and bmc-toolkit 4; 8 more come from two private projects and live
in `~/.revali/bench/cases/`, outside any repository.

What the builder takes from the archive:

- `head`: `head_sha` of `review-<n>.json`. revali adds fixes on top of it, so it stays
  reachable through `refs/pull/<pr>/head` after the branch is deleted.
- `base`: `git merge-base <head> origin/<base branch>`, in a temporary clone.
- `change.md`: the text between "## The author's description (change.md)" and "## The diff"
  in `logs/prompt-r<n>-1.md`. The archived `change.md` is the final one; its What may
  already describe the fix. A committed case's `## Request` is a short English paraphrase
  (`--request`): it holds the user's own words, which revali keeps off GitHub (the PR body
  leaves it out). The replayed description then differs from the original round in that
  section only; a test fails when a committed Request has a non-ASCII character.
- `[expected]`: `file` and `line` of the finding, and its first sentence as the
  description.
- `[source]`: the PR, the round and the finding id. A case from a later round is replayed
  as a first round: the reviewer sees the whole diff at that head, without the earlier
  rounds' findings.

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

A case from a private repository does not belong in this public repository: keep it in
`~/.revali/bench/cases/` (or any directory of its own) and pass a second `--cases`.
