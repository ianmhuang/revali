# revali

Review, validate, merge: a headless pipeline for feature branches on your own
GitHub repositories. Three roles take part.

- **Developer**: the authoring session (Claude Code, or you). Writes the
  acceptance criteria before the code, gets them approved, implements, and
  types `/revali`.
- **Reviewer**: an independent headless session revali spawns. Reviews the
  diff against the acceptance criteria and writes the acceptance tests. It
  does not run them.
- **Validator**: a sandbox run of the existing suite plus the Reviewer's
  tests. On failure only, a diagnosis session reads the output and says
  whether the code, the test, or the environment is at fault.

**Why separate sessions.** Each role runs in its own session with its own
inputs, so nobody grades their own work: the Developer never reviews the
change it wrote; the Reviewer gets the acceptance criteria and the diff, not
the Developer's reasoning, and derives its tests from the criteria; the
Validator is a test run, not an opinion; the diagnosis session sees only the
failure output. The models can differ by tier (`auto` puts the Reviewer one
tier above the Developer); the engine seam is where a second vendor would
plug in (only `claude` exists today). Fresh
context removes the author's bias toward its own change; it does not remove
blind spots the models share, which is why `revali stats` tracks the
first-try approval rate.

```mermaid
sequenceDiagram
    actor U as User
    participant D as Developer
    participant R as revali
    participant B as Reviewer
    participant V as Validator
    participant G as GitHub

    D->>U: change.md (Request, Goal, AC-n), status: draft
    U-->>D: approves the AC
    D->>D: implements, writes own tests, commits
    U->>D: /revali
    loop until APPROVE + PASS (max 2 fix cycles)
        D->>R: revali run
        R->>R: preflight: clean tree, own repo, base, diff size, secrets, lint, baseline
        R->>G: push, draft PR
        R->>B: diff, change.md, checklist, previous round
        B-->>R: verdict, findings, acceptance tests
        R->>G: commit tests, PR comment
        alt APPROVE
            R->>V: sandbox: existing suite + new tests
            alt PASS
                R->>G: PR marked ready
                R-->>D: READY TO MERGE, exit 0
            else FAIL
                V->>V: diagnosis session (code / test / env)
                R-->>D: exit 2
            end
        else CHANGES_REQUESTED / NEEDS_INFO
            R-->>D: exit 2
        end
        opt exit 2
            D->>D: fixes or answers (response-n.md), commits
            U->>D: /revali
        end
    end
    U->>D: merge
    D->>R: revali merge
    R->>G: wait for CI, squash merge, delete branch
```

The preflight baseline (the existing suite in the sandbox) runs on every
`revali run` until the first review round is recorded, and not for
`kind: docs`. A sandbox `setup` or `build` failure is exit 1 (environment),
not a FAIL verdict.

| Role | Started by | Reads | Writes | Model |
|---|---|---|---|---|
| Developer | the user | the request, the repo | `change.md`, code, its own tests | whatever the user's session runs; recorded as `author_model` |
| Reviewer | revali, on `/revali` | diff, `change.md`, three-layer checklist, the previous round and `response-n.md` | tests in `test_dir`, and its answer, which revali turns into `review-n.md`, `tests.md`, and a PR comment; does not run the tests | `auto`: one tier above the Developer |
| Validator | revali, after APPROVE | a sandbox clone of the branch | logs; revali appends the result to `tests.md`; on FAIL only, the diagnosis session answers and revali writes `diagnose-n.json` | the runner needs none; diagnosis `auto`: one tier below the Developer |

Three user actions (approve the AC, `/revali`, `revali merge`) are the gates;
everything between them is automatic. Exit codes: `0` done / ready to merge,
`1` pipeline error (not a verdict), `2` the Developer must act (fix, rebase,
answer a question), `3` a human must decide, `4` (`wait` only) still running.

Status: v1.0 feature set complete (package version 0.1.0). Verified end to
end on a private GitHub repository with real Reviewer sessions and a real
WSL sandbox; revali reviews its own changes.

## Requirements

- Python 3.11+ (stdlib only), git, GitHub CLI (`gh auth login` done)
- Claude Code CLI on PATH (`claude`), for the reviewer / diagnoser sessions
- A place to run the sandbox: on Windows, WSL with an Ubuntu distro; on any
  host, a Linux machine reachable by key-based ssh (`runner = "ssh"`) with
  git, bash and coreutils installed

## Usage

```
python <path-to>/revali.py preflight        # checks only, changes nothing
python <path-to>/revali.py run              # detached; then:
python <path-to>/revali.py wait --timeout 9m
python <path-to>/revali.py merge            # only after READY TO MERGE; waits for CI
python <path-to>/revali.py status | stop | reset | clean <branch> | stats | version
```

What a run does, in order: preflight (including the existing suite in the
sandbox as a baseline), push + draft PR, reviewer round (`claude -p` with the
diff, change.md, and the checklist; writes tests into `test_dir`; the script
checks AC coverage, smoke-runs the new tests, commits them), validation
(existing suite + new tests in the sandbox; a diagnoser session only on
failure), then READY TO MERGE. Every result lands in `.revali/<branch>/`
(`review-<n>.md`, `tests.md`, `diagnose-<n>.json`, `logs/`) and as PR comments.

`run`, `wait` and `status` print one identity line first,
`repo: <working tree root>  branch: <branch>`, so several sessions running
revali in different checkouts can tell their output apart; every message
that reports a live or dead run carries its pid. On Windows the detached
run starts its git / gh / claude / wsl subprocesses with `CREATE_NO_WINDOW`,
so no console windows appear while it works.

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

## Configuration

Three layers, the most specific wins, and every key may appear in any of
them:

1. `defaults.toml` in the revali checkout: every key with its default. Edit
   it only when a new model generation arrives (the model ladders live
   here, under `[engines.<name>]`).
2. `~/.revali/config.toml`: your machine, every project. `checklist`,
   `history_path`, and any `[section]` from the project file (a WSL distro
   name, a budget, a pinned model). `REVALI_HOME` moves the directory.
3. `revali.toml` in the project root: the commands to build and test, the
   platforms, anything project-specific. `templates/revali.toml` is a
   starting point; a key left out inherits from the layers above.

Unknown keys are errors in every layer. `[review] engine` and
`[validate] engine` name the CLI that runs the session (`claude`; the old
`prompt | hybrid` meaning moved to `strategy`). Keys that name a file
(`prompt`, `schema`, `checklist_builtin`) are relative to the project
root; empty means the file revali ships with. `[validate.platform]` in
any layer sets the defaults for every `[validate.<name>]` table.

Models: `model = "auto"` (the default) picks the Reviewer one tier above
the Developer's model (`author_model` in `change.md`) and the diagnosis
session one tier below, on the ladder of the configured engine
(`[engines.claude] tiers = ["haiku", "sonnet", "opus", "fable"]`); an
unknown or missing `author_model` means the top tier for the Reviewer and
one below the top for diagnosis. `fallback_model = "auto"` is the tiers
below the chosen one, strongest first. Any explicit model name passes
through unchanged. The chosen model and the reason are printed at spawn
time and recorded in the review and diagnosis headers.

## Files

| Document | Written by | Read by | Default location | Config key |
|---|---|---|---|---|
| `change.md` (request, goal, AC-n) | Developer | Reviewer, diagnosis session | `.revali/<branch>/` | `[paths] state_dir` |
| `response-n.md` | Developer | Reviewer, next round | same | same |
| `review-n.md` / `.json` | revali, from the Reviewer's answer | you, the PR | same | same |
| `tests.md` | revali, from the Reviewer's answer; validation results appended | you, diagnosis session | same | same |
| `diagnose-n.json` | revali, from the diagnosis session | you | same | same |
| `state.json` (stage, rounds, validations, exit) | revali | `wait`, `status`, the next `run` | same | same; `[paths] write_retry_s` is how long a write waits for a reader to release the file (Windows) |
| logs, prompts, raw answers | revali | you | `.revali/<branch>/logs/` | `[paths] logs_dir` |
| acceptance tests | Reviewer | Validator; merged into `main` | `tests/test_review_<topic>.py` | `[project] test_dir`, `test_file_pattern` |
| checklist, built-in layer | revali | Reviewer | `checklists/default.md` in revali | `[review] checklist_builtin` |
| checklist, user layer | you | Reviewer | none | `checklist` in `~/.revali/config.toml` |
| checklist, project layer | the project | Developer (via `CLAUDE.md`), Reviewer | `CONVENTIONS.md` | `[review] checklist` |
| Reviewer prompt and schema | revali | Reviewer | `prompts/review.md`, `schemas/review.schema.json` in revali | `[review] prompt`, `schema` |
| diagnosis prompt and schema | revali | diagnosis session | `prompts/diagnose.md`, `schemas/diagnose.schema.json` in revali | `[validate] prompt`, `schema` |
| how tests are added here | the project | Reviewer | none | `[project] test_guide` |
| sandbox clone | Validator | Validator; deleted after the run | `~/.revali/sandbox/<repo>/<label>/` inside WSL or on the ssh host | `[validate.<platform>] sandbox_dir` |
| run history | revali | `revali stats` | `~/.revali/history.jsonl` | `history_path` or `[paths] history_file` in `~/.revali/config.toml` (user level only) |

Branch `feature/x` maps to directory `feature__x`. `~/.revali/` itself moves
with the `REVALI_HOME` environment variable.

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

## Sandbox

`[validate.linux] runner = "wsl"` clones the branch into the WSL distro's own
filesystem (`sandbox_dir`, default `~/.revali/sandbox/<repo>/<label>/`), runs `setup`, `build`,
`test`, `new_test` there with a per-step timeout, copies the logs back, and
deletes the clone. The distro needs git and whatever `setup` installs; on
Ubuntu 24.04 that means `python3-venv` and `python3-pip` for a Python project.
`runner = "local"` uses a git worktree on the host with no isolation.

`runner = "ssh"` with `host = "<destination>"` does the same as `wsl` on a Linux
host reached over ssh: the branch travels as a git bundle (the host needs no
GitHub access), the sandbox script and the step commands go up with scp, the
per-step logs come back the same way, and the staging directories under
`sandbox_dir` are removed afterwards (the repository's directory name is
reduced to `[A-Za-z0-9._-]` on the host, and `sandbox_dir` may not contain
whitespace). `host` is anything `ssh` accepts, so user, port and key belong
in `~/.ssh/config`; `connect_timeout_s` and `transfer_timeout_min` bound the
connection and each scp transfer. Every call runs with
`BatchMode=yes`: nothing prompts, so key-based login must already work and
the host key must be known (run `ssh <host>` once by hand). Preflight probes
the host (reachable, git and `timeout` present) before anything is pushed;
the same probe runs inside the WSL distro for `runner = "wsl"`. The ssh runner was
verified against sshd inside WSL (`ListenAddress 127.0.0.1`, a non-default
port, key-only login); on Ubuntu 24.04 `Port` in `sshd_config` is ignored
while `ssh.socket` is active, so disable the socket and enable `ssh.service`
if the port does not change.

`REVALI_DISABLE=1` in the environment switches revali off entirely.

## Project setup

Copy `templates/revali.toml` to the repo root and edit the commands; copy
`templates/CONVENTIONS.md` if the project has none; add `.revali/` to
`.gitignore`; paste `templates/CLAUDE-snippet.md` into the project's
`CLAUDE.md`. Before each run the author writes
`.revali/<branch>/change.md` from `templates/change.md` (branch `feature/x`
maps to directory `feature__x`); see Workflow above.

User-level options live in `~/.revali/config.toml` (see
`templates/user-config.toml`); `REVALI_HOME` overrides the directory.

## What revali does to your repository

Read this before the first run. All of it is on the feature branch unless
stated.

- appends the state directory (`.revali/`) to `.gitignore` if missing
- `git push -u origin <branch>` and `gh pr create --draft`
- commits the reviewer's test files into `test_dir`, with
  `Co-Authored-By: Claude` and `Revali-Round: <n>` trailers; only new files
  and the files the reviewer wrote in an earlier round. The
  `Revali-Round` trailer is what marks a file as the reviewer's own: on every
  run the commits between the base and HEAD that carry it, and the files
  under `test_dir` they added or modified, are read back into the state, so
  a rebase or amend that gives those commits new SHAs (the review then
  starts over from round 1) keeps them the reviewer's. A rewrite that
  drops the trailer, such as squashing the reviewer's commit into your
  own, turns those files into existing files the reviewer must not
  modify. Any other
  tracked file under `test_dir` the reviewer modified or deleted is
  restored from HEAD (`git checkout HEAD -- <path>`, index and working
  tree) before the commit, the reviewer is sent back once
  with the names already taken, and a second offence ends the run with
  exit 1 and no test commit. A NEEDS_INFO round keeps its test files
  uncommitted in `test_dir`: the state file lists them, the clean-tree check
  tolerates exactly those paths, and the next round commits or removes
  them. Do not commit them by hand; a file you commit becomes one the
  reviewer did not write and is protected like any other
- posts review and validation results as PR comments; on a repository that
  is not private the comments are summaries (verdict, model, cost, finding
  ids with severity and location, test files, AC coverage, validation exit
  codes, diagnosis cause) and the PR body withholds the `Request` section;
  the full text stays in the state directory
- on `revali merge` (human-started, refused unless the last run ended READY
  TO MERGE and HEAD has not moved): waits for CI checks if the PR has any,
  then `gh pr merge --<method> --delete-branch`, which also deletes the
  local branch and checks out the base branch; then `git pull --prune` and
  deletion of the branch's state directory
- after a review round that stops before its tests are committed (a failed
  or timed-out Reviewer session, unusable output, a smoke run that fails
  twice), deletes the untracked files matching `test_file_pattern` under
  `test_dir` that the session left behind, and names them in the log. The
  same cleanup runs at the start of the next `run` when a reviewer session
  was started and its round never finished (`revali stop`, Ctrl-C, a killed
  process); the state file remembers that until the cleanup has run, so a
  dry run or a failed preflight in between does not lose it. A run that
  ended with a verdict never triggers it (a NEEDS_INFO round's uncommitted
  files are pending, not leftovers; they go only when the round after it
  is interrupted, and a pending file that is a tracked file of the
  reviewer's own is then restored from HEAD instead). A leftover that
  cannot be deleted (a file another program holds open) stays in the
  tolerated list: the next run accepts it in the tree and lists it for the
  reviewer to update or delete, the log names it for you. `revali reset`
  runs the same cleanup before it drops the state, since the drafts would
  otherwise outlive the list that tolerates them: after an interrupted
  round the whole pattern, otherwise only the pending list (your own
  untracked draft on the pattern survives); with no pending files
  and no interrupted round it touches nothing under `test_dir`, and when
  the project does not load, or a file cannot be deleted, it prints the
  paths for you to delete by hand.
  A finished
  `run --dry-run` is not an interrupted run, and neither `revali preflight`
  nor `run --dry-run` deletes.
  This is the only deletion inside `test_dir` revali performs, so keep
  your own files off `test_file_pattern`

It never modifies files outside `test_dir` and the state directory, never
commits a change to a test file the reviewer did not write, never merges on
its own, and never runs on a repo you do not own.

## Development

```
python -m unittest discover -s tests -t .
python tests/fixtures/make_sample_repo.py "<dir>"   # throwaway sample project
```

Tests use a fake `gh` (via `REVALI_GH_CMD`) and real `git` in temp repos.

## License

MIT, see `LICENSE`.
