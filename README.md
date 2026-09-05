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
inputs, so nobody grades their own work: the Reviewer gets the acceptance
criteria and the diff, not the Developer's reasoning, the Validator is a
test run, not an opinion, and the diagnosis session sees only the failure
output. The models can differ by tier (`auto` puts the Reviewer one tier
above the Developer) and, at the engine seam, by vendor
(only `claude` exists today). Fresh context removes the author's bias toward
its own change, not the blind spots the models share, which is why
`revali stats` tracks the first-try approval rate. The full argument is in
`docs/workflow.md`.

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

Status: package version 0.2.0, the v1.0 feature set plus the multi-session
work of PR #21 to #24: no console windows, identity lines, one run per
working tree, sandbox directories per branch, merge from a worktree.
Verified end to end on a private GitHub repository with real Reviewer
sessions and real WSL and ssh sandboxes (`docs/sandbox.md` has the record);
revali reviews its own changes on this public one.

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
python <path-to>/revali.py --version        # same line as `version`
```

What a run does, in order: preflight (including the existing suite in the
sandbox as a baseline), push + draft PR, reviewer round (`claude -p` with the
diff, change.md, and the checklist; writes tests into `test_dir`; the script
checks AC coverage, smoke-runs the new tests, commits them), validation
(existing suite + new tests in the sandbox; a diagnoser session only on
failure), then READY TO MERGE. Every result lands in `.revali/<branch>/`
(`review-<n>.md`, `tests.md`, `diagnose-<n>.json`, `logs/`) and as PR comments.

## Documentation

- `docs/workflow.md`: why the sessions are separate, the acceptance
  criteria before the code, project setup, what `run` prints and checks,
  what happens after exit 2 or a dead run, several agents on one repository
- `docs/configuration.md`: the three layers, models, `REVALI_DISABLE`
- `docs/files.md`: every file revali reads or writes and the key that moves it
- `docs/sandbox.md`: the `wsl`, `ssh` and `local` runners, and the
  verification record
- `docs/side-effects.md`: every git and GitHub action revali takes, read
  before the first run

## Development

```
python tests/run_parallel.py                        # the suite, one worker per CPU (-j N)
python tests/run_parallel.py tests.test_pipeline    # a module, a class or a test, as for unittest
python -m unittest discover -s tests -t .           # the same tests, serially
python tests/fixtures/make_sample_repo.py "<dir>"   # throwaway sample project
```

Tests use a fake `gh` (via `REVALI_GH_CMD`) and real `git` in temp repos; every
test builds its own repository, which is why the parallel runner exists.

## License

MIT, see `LICENSE`.
