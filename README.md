# revali

Headless review / validate / merge pipeline for feature branches on your own
GitHub repositories. An authoring session (Claude Code, or you) writes
`.revali/<branch>/change.md`, gets its acceptance criteria approved, then
implements the change on a branch (see Workflow below); revali then:

1. runs preflight (clean tree, private repo you own, base not ahead, diff size,
   credential scan, lint, existing suite),
2. pushes the branch and opens a draft PR,
3. spawns an independent reviewer session (`claude -p`) that reviews the diff
   against the acceptance criteria and writes acceptance tests,
4. runs the existing suite plus the new tests in a WSL sandbox, spawning a
   diagnoser session only on failure,
5. stops at READY TO MERGE for a human `revali merge`.

Status: v1.0 feature set complete (preflight, review, validate, merge,
stats). The WSL runner is verified against a real Ubuntu distro; the full
pipeline against a real GitHub repository and real reviewer sessions is
being exercised now.

## Requirements

- Python 3.11+ (stdlib only), git, GitHub CLI (`gh auth login` done)
- Claude Code CLI on PATH (`claude`), for the reviewer / diagnoser sessions
- On Windows: WSL with an Ubuntu distro for the linux sandbox

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

After exit code 2 the author fixes or answers in
`.revali/<branch>/response-<n>.md` (`- F1: fixed` / `- F1: wontfix: <reason>`),
commits, and runs again; each such cycle counts against `review.max_fixes`.

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
root; empty means the file revali ships with.

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
filesystem (`~/.revali/sandbox/<repo>/<label>/`), runs `setup`, `build`,
`test`, `new_test` there with a per-step timeout, copies the logs back, and
deletes the clone. The distro needs git and whatever `setup` installs; on
Ubuntu 24.04 that means `python3-venv` and `python3-pip` for a Python project.
`runner = "local"` uses a git worktree on the host with no isolation.

```
```

Exit codes: `0` done / ready to merge, `1` pipeline error (not a verdict),
`2` the author must act (fix, rebase, answer a question), `3` a human must
decide, `4` (`wait` only) still running.

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

- appends `.revali/` to `.gitignore` if missing
- `git push -u origin <branch>` and `gh pr create --draft`
- commits the reviewer's test files into `test_dir`, with a
  `Co-Authored-By: Claude` trailer
- posts review and validation results as PR comments
- on `revali merge` (human-started, refused unless the last run ended READY
  TO MERGE and HEAD has not moved): waits for CI checks if the PR has any,
  then `gh pr merge --<method> --delete-branch`, which also deletes the
  local branch and checks out the base branch; then `git pull --prune` and
  deletion of `.revali/<branch>/`
- on resume after `stop`, may delete untracked files matching
  `test_file_pattern` left behind by an interrupted reviewer

It never modifies files outside `test_dir` and `.revali/`, never merges on
its own, and never runs on a repo you do not own or that is public.

## Development

```
python -m unittest discover -s tests -t .
python tests/fixtures/make_sample_repo.py "<dir>"   # throwaway sample project
```

Tests use a fake `gh` (via `REVALI_GH_CMD`) and real `git` in temp repos.

## License

MIT, see `LICENSE`.
