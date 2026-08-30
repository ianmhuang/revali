# revali project conventions

Read by the author session (via CLAUDE.md) and by the revali reviewer.
Things a linter cannot check; formatting is not listed.

## Structure
- `revali/` is standard-library only (Python 3.11+: `tomllib`, `subprocess`,
  `json`, `re`). No third-party imports anywhere in the package.
- One stage per module (`preflight`, `pr`, `review`, `validate`, `merge`),
  orchestrated by `pipeline.py`; runners sit behind the `Runner` interface in
  `runners.py`; every Reviewer / diagnosis session goes through an `Engine`
  in `revali/engines/` (`revali/engines/claude.py` is the only place that
  knows a `claude` flag); every
  external executable is resolved by `procs.resolve` so tests can replace it
  with `REVALI_<NAME>_CMD`.
- Exit codes are fixed: 0 ok / ready, 1 pipeline error, 2 the author must
  act, 3 a human must decide, 4 (`wait` only) still running. A new failure
  must map onto one of these, not add a code.

## Behaviour changes
- Every behaviour change ships with a test in `tests/` built on
  `tests/helpers.RepoCase` (fake `gh` / `claude` / runner, real `git`).
- Changing `prompts/`, `schemas/`, or `checklists/` bumps `PROMPT_VERSION` in
  `revali/__init__.py`; changing the state file layout bumps `STATE_VERSION`.
- Existing tests are not weakened to make a change pass; if an assertion
  must change, the change description says why.

## Interfaces
- CLI flags, `revali.toml` keys, exit codes, and the files under
  `.revali/<branch>/` are public interface: a change updates `README.md`
  (and `templates/` when a key is involved) in the same change.
- Every git or GitHub side effect the tool performs is listed in README
  under "What revali does to your repository".

## Portability
- Runs on the Windows host (PowerShell / Git Bash) and inside WSL or Linux:
  no absolute paths, no user names, no platform-only tools in `revali/`.
- No default value in code: models, budgets, timeouts, paths, and file
  names live in `defaults.toml`; a CLI flag lives only in `revali/engines/`.
- Files are written with `newline=""` and LF; subprocesses use UTF-8 with
  `errors="replace"`.
- Prompts, schemas, comments, commit messages, and docs are in English.
