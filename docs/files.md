# Files

Every file revali reads or writes, who writes it, who reads it, and the key
that moves it.

| Document | Written by | Read by | Default location | Config key |
|---|---|---|---|---|
| `change.md` (request, goal, AC-n) | Developer | Reviewer, diagnosis session | `.revali/<branch>/` | `[paths] state_dir` |
| `response-n.md` | Developer | Reviewer, next round | same | same |
| `review-n.md` / `.json` | revali, from the Reviewer's answer | you, the PR | same | same |
| `tests.md` | revali, from the Reviewer's answer; validation results appended | you, diagnosis session | same | same |
| `diagnose-n.json` | revali, from the diagnosis session | you | same | same |
| `state.json` (stage, rounds, validations, exit) | revali | `wait`, `status`, the next `run` | same | same; `[paths] write_retry_s` is how long a write waits for a reader to release the file (Windows) |
| `tree.lock` (pid, branch, since of the run holding the working tree) | revali | `run`, `stop` | `.revali/` | `[paths] state_dir` |
| logs, prompts, raw answers | revali | you | `.revali/<branch>/logs/` | `[paths] logs_dir` |
| acceptance tests | Reviewer | Validator; merged into `main` | `tests/test_review_<topic>.py` | `[project] test_dir`, `test_file_pattern` |
| checklist, built-in layer | revali | Reviewer | `checklists/default.md` in revali | `[review] checklist_builtin` |
| checklist, user layer | you | Reviewer | none | `checklist` in `~/.revali/config.toml` |
| checklist, project layer | the project | Developer (via `CLAUDE.md`), Reviewer | `CONVENTIONS.md` | `[review] checklist` |
| Reviewer prompt and schema | revali | Reviewer | `prompts/review.md`, `schemas/review.schema.json` in revali | `[review] prompt`, `schema` |
| diagnosis prompt and schema | revali | diagnosis session | `prompts/diagnose.md`, `schemas/diagnose.schema.json` in revali | `[validate] prompt`, `schema` |
| how tests are added here | the project | Reviewer | none | `[project] test_guide` |
| sandbox clone | Validator | Validator; deleted after the run | `~/.revali/sandbox/<repo>/<branch>/<label>/` inside WSL or on the ssh host | `[validate.<platform>] sandbox_dir` |
| run history | revali | `revali stats` | `~/.revali/history.jsonl` (one row per run; `stage_s` and `sandbox_s` hold the seconds each stage and each sandbox session took) | `history_path` or `[paths] history_file` in `~/.revali/config.toml` (user level only) |

Branch `feature/x` maps to directory `feature__x`. `~/.revali/` itself moves
with the `REVALI_HOME` environment variable.
