# Files

Every file revali reads or writes, who writes it, who reads it, and the key
that moves it.

| Document | Written by | Read by | Default location | Config key |
|---|---|---|---|---|
| `change.md` (request, goal, AC-n) | Developer | Reviewer, diagnosis session | `.revali/<branch>/` | `[paths] state_dir` |
| `response-n.md` | Developer | Reviewer, next round | same | same |
| `review-n.md` / `.json` | revali, from the Reviewer's answer | you, the PR | same | same |
| `tests.md` | revali, from the Reviewer's answer; validation results appended, on FAIL with a `### Base rerun` block (the reviewer's tests on the base tip) and the diagnosis | you, diagnosis session | same | same |
| `diagnose-n.json` | revali, from the diagnosis session | you | same | same |
| `state.json` (stage, rounds, validations with their base rerun result, issues opened for pre-existing bugs, exit) | revali | `wait`, `status`, the next `run`, `merge` | same | same; `[paths] write_retry_s` is how long a write waits for a reader, and a read (`wait`, `status`, the next `run`) waits for a writer, to release the file (Windows) |
| issue body for a pre-existing bug | revali, from [templates/issue.md](../templates/issue.md) | GitHub (`gh issue create`); a copy in `logs/issue-<validation>.md`, numbered by the validation that opened it | [templates/issue.md](../templates/issue.md) in revali | `[validate] issue_template` |
| issue comment after the merge (the merged PR, the fixing commit, the cause and fix recorded with the issue) | `revali merge` | GitHub (`gh issue comment`); a copy in `logs/issue-<issue number>-merged.md`, archived with `.revali/<branch>/` after the merge | `.revali/<branch>/logs/` | `[paths] logs_dir` |
| the archive of a merged branch: its whole `.revali/<branch>/` (state, reviews, `tests.md`, logs) | `revali merge`; [tools/backfill_archive.py](../tools/backfill_archive.py) rebuilds it for PRs merged before the key existed, from the PR body and comments and `history.jsonl` (no logs, no state) | you; nothing in revali reads it | `~/.revali/archive/<owner>__<repo>/<pr>-<branch>/` (a taken name gets a `-<YYYYMMDD-HHMMSS>` suffix) | `[paths] archive_dir`; relative = under `~/.revali/`, `""` = delete instead |
| `tree.lock` (pid, branch, since of the run holding the working tree) | revali | `run`, `stop` | `.revali/` | `[paths] state_dir` |
| logs, prompts, raw answers (`<label>-<step>.log` per sandbox step, `<label>-script.log` for the wsl or ssh sandbox script's own errors; `base-r<round>-*` is the rerun on the base tip) | revali | you | `.revali/<branch>/logs/` | `[paths] logs_dir` |
| acceptance tests | Reviewer | Validator; merged into `main` with `[review] tests = "commit"` (the default); with `tests = "archive"` never committed: kept under `.revali/<branch>/tests/<repository path>`, copied into `test_dir` for each round, sent into the sandbox as extra files, and moved to `archive_dir` with the branch directory at merge | `tests/test_review_<topic>.py` | `[project] test_dir`, `test_file_pattern`; `[review] tests` |
| checklist, built-in layer | revali | Reviewer | [checklists/default.md](../checklists/default.md) in revali | `[review] checklist_builtin` |
| checklist, user layer | you | Reviewer | none | `checklist` in `~/.revali/config.toml` |
| checklist, project layer | the project | Developer (via `CLAUDE.md`), Reviewer | `CONVENTIONS.md` | `[review] checklist` |
| Reviewer prompt and schema | revali | Reviewer | [prompts/review.md](../prompts/review.md), [schemas/review.schema.json](../schemas/review.schema.json) in revali | `[review] prompt`, `schema` |
| diagnosis prompt and schema | revali | diagnosis session | [prompts/diagnose.md](../prompts/diagnose.md), [schemas/diagnose.schema.json](../schemas/diagnose.schema.json) in revali | `[validate] prompt`, `schema` |
| how tests are added here | the project | Reviewer | none | `[project] test_guide` |
| sandbox clone | Validator | Validator; deleted after the run | `~/.revali/sandbox/<repo>/<branch>-<key>/<label>/` inside WSL or on the ssh host (`<key>`: see [sandbox.md](sandbox.md)) | `[validate.<platform>] sandbox_dir` |
| run history | revali | `revali stats` | `~/.revali/history.jsonl` (one row per `run` that started, `merge` or `stop`; a `run` refused before it detaches writes none; rows written by `run` carry `stage_s` and `sandbox_s`, the seconds each stage and each sandbox session took, rounded to the millisecond; with `[validate] parallel_baseline` the baseline's sandbox session overlaps the `pr` and `review` stages, so `sandbox_s.baseline` is no longer inside `stage_s.preflight`, and a wait for it after the reviewer answered counts in `review`) | `history_path` or `[paths] history_file` in `~/.revali/config.toml` (user level only) |

Branch `feature/x` maps to directory `feature__x`. `~/.revali/` itself moves
with the `REVALI_HOME` environment variable.

`state.json` is revali's own file, but a hand edit is read as follows. A
field set to `null` takes its default, mostly an empty list, `0`, `""` or
`false`. Five fields default to something else: `stage` reads as
`preflight`; `last_exit` reads as `-1`, a run that ended without a result,
so `wait` and `status` report it as died; `version` is the current layout
version; `revali_version` and `prompt_version` are those of the running
revali. A key that is not a field is ignored. A file that is not JSON, not an
object, or has a field of the wrong type (a string where a number belongs,
`true` where a counter belongs, a list holding a `null`) stops every command
that reads it with exit 1 and one `ERROR:` line naming the file and the
field; `revali stop` still stops the run first. `revali reset` removes such a
file, though it cannot clean up the reviewer's uncommitted test files the
file may have listed. Every save writes the current state layout version in
`version`, whatever version the file was read as.
