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
| issue body for a pre-existing bug | revali, from [`templates/issue.md`](../templates/issue.md) | GitHub (`gh issue create`); a copy in `logs/issue-<validation>.md`, numbered by the validation that opened it | [`templates/issue.md`](../templates/issue.md) in revali | `[validate] issue_template` |
| issue comment after the merge (the merged PR, the fixing commit, the cause and fix recorded with the issue) | `revali merge` | GitHub (`gh issue comment`); a copy in `logs/issue-<issue number>-merged.md`, gone with `.revali/<branch>/` when the merge removes it | `.revali/<branch>/logs/` | `[paths] logs_dir` |
| `tree.lock` (pid, branch, since of the run holding the working tree) | revali | `run`, `stop` | `.revali/` | `[paths] state_dir` |
| logs, prompts, raw answers (`<label>-<step>.log` per sandbox step; `base-r<round>-*` is the rerun on the base tip) | revali | you | `.revali/<branch>/logs/` | `[paths] logs_dir` |
| acceptance tests | Reviewer | Validator; merged into `main` | `tests/test_review_<topic>.py` | `[project] test_dir`, `test_file_pattern` |
| checklist, built-in layer | revali | Reviewer | [`checklists/default.md`](../checklists/default.md) in revali | `[review] checklist_builtin` |
| checklist, user layer | you | Reviewer | none | `checklist` in `~/.revali/config.toml` |
| checklist, project layer | the project | Developer (via `CLAUDE.md`), Reviewer | `CONVENTIONS.md` | `[review] checklist` |
| Reviewer prompt and schema | revali | Reviewer | [`prompts/review.md`](../prompts/review.md), [`schemas/review.schema.json`](../schemas/review.schema.json) in revali | `[review] prompt`, `schema` |
| diagnosis prompt and schema | revali | diagnosis session | [`prompts/diagnose.md`](../prompts/diagnose.md), [`schemas/diagnose.schema.json`](../schemas/diagnose.schema.json) in revali | `[validate] prompt`, `schema` |
| how tests are added here | the project | Reviewer | none | `[project] test_guide` |
| sandbox clone | Validator | Validator; deleted after the run | `~/.revali/sandbox/<repo>/<branch>/<label>/` inside WSL or on the ssh host | `[validate.<platform>] sandbox_dir` |
| run history | revali | `revali stats` | `~/.revali/history.jsonl` (one row per `run`, `merge` or `stop`; rows written by `run` carry `stage_s` and `sandbox_s`, the seconds each stage and each sandbox session took) | `history_path` or `[paths] history_file` in `~/.revali/config.toml` (user level only) |

Branch `feature/x` maps to directory `feature__x`. `~/.revali/` itself moves
with the `REVALI_HOME` environment variable.
