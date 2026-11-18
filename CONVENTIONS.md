# revali project conventions

Read by the author session (via CLAUDE.md) and by the revali reviewer.
Things a linter cannot check; formatting is not listed.

## Structure
- `revali/` is standard-library only (Python 3.11+: `tomllib`, `subprocess`,
  `json`, `re`). No third-party imports anywhere in the package.
- One stage per module (`preflight`, `pr`, `review`, `validate`, `merge`),
  orchestrated by `pipeline.py`; runners sit behind the `Runner` interface in
  `runners.py`; every Reviewer / diagnosis session goes through an `Engine`
  in `revali/engines/` ([revali/engines/claude.py](revali/engines/claude.py) is the only place that
  knows a `claude` flag); every
  external executable revali names (git, gh, claude, wsl, ssh, scp) is
  resolved by `procs.resolve` so tests can replace it with
  `REVALI_<NAME>_CMD`. The exceptions are `taskkill` on Windows, the platform
  shell that runs the configured command lines, and the Python interpreter
  that starts the detached run.
- Exit codes are fixed: 0 ok / ready, 1 pipeline error, 2 the author must
  act, 3 a human must decide, 4 (`wait` only) still running. A new failure
  must map onto one of these, not add a code.

## Style
- Formatting is black's, lint is ruff's ([pyproject.toml](pyproject.toml)); revali's own
  `[project] lint` line runs both. The preflight of `revali run` runs that line and stops
  with exit 2 on a failure (`revali preflight` on its own prints the line and does not run
  it), and the reviewer's test files must pass it before they are committed, so neither the
  author nor the reviewer spends a round on style. The reviewer does not comment on what
  they check.

## Behaviour changes
- Every behaviour change ships with a test in `tests/` built on
  `tests/helpers.RepoCase` (fake `gh` / `claude` / runner, real `git`).
- Changing `prompts/`, `schemas/`, or `checklists/` bumps `PROMPT_VERSION` in
  [revali/__init__.py](revali/__init__.py); changing the state file layout bumps `STATE_VERSION`.
- Existing tests are not weakened to make a change pass; if an assertion
  must change, the change description says why.

## Interfaces
- CLI flags, `revali.toml` keys, exit codes, and the files under
  `.revali/<branch>/` are public interface: a change updates the page
  under `docs/` that describes it, or [README.md](README.md) for the command list and
  the status line (and `templates/` when a key is involved), in the same
  change.
- Every git or GitHub side effect the tool performs is listed in
  [docs/side-effects.md](docs/side-effects.md).

## Portability
- Runs on the Windows host (PowerShell / Git Bash) and inside WSL or Linux:
  no absolute paths, no user names, and a platform-only tool in `revali/` only
  behind a platform check (`taskkill`, `msvcrt` / `fcntl`, `windll`) or a
  configuration choice that preflight checks (the wsl runner).
  CI ([.github/workflows/test.yml](.github/workflows/test.yml)) runs the suite on Linux and
  Windows on every push, and lint on one Linux job, and `revali merge` waits for it.
- What a user may want to change lives in [defaults.toml](defaults.toml), not in code:
  models, budgets, the stage and platform timeouts, paths. Internal timeouts and
  margins (the `gh` and `git` calls, the runner's cancel margins), the default of
  `wait --timeout` and the fixed file names (`state.json`, `change.md`, `tests.md`,
  `config.toml`, ...) stay in code.
- Files revali writes are read with `newline=""` and written with `newline="\n"` (LF);
  the exceptions are `logs/run.log`, the detached run's standard output, which takes the
  platform's line ending (CRLF on Windows), and the fake runner's test log. Subprocesses
  use UTF-8 with `errors="replace"`.
- Prompts, schemas, comments, commit messages, and docs are in English.
