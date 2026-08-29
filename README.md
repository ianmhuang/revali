# revali

Headless review / validate / merge pipeline for feature branches on your own
GitHub repositories. An authoring session (Claude Code, or you) finishes a
change on a branch and writes `.revali/<branch>/change.md`; revali then:

1. runs preflight (clean tree, private repo you own, base not ahead, diff size,
   credential scan, lint, existing suite),
2. pushes the branch and opens a draft PR,
3. spawns an independent reviewer session (`claude -p`) that reviews the diff
   against the acceptance criteria and writes acceptance tests,
4. runs the existing suite plus the new tests in a WSL sandbox, spawning a
   diagnoser session only on failure,
5. stops at READY TO MERGE for a human `revali merge`.

Status: milestone 1 (skeleton, config, preflight, state, CLI). Steps 2-5 are
not implemented yet; `revali run` stops after preflight.

## Requirements

- Python 3.11+ (stdlib only), git, GitHub CLI (`gh auth login` done)
- Claude Code CLI on PATH (`claude`), for the reviewer / diagnoser sessions
- On Windows: WSL with an Ubuntu distro for the linux sandbox

## Usage

```
python <path-to>/revali.py preflight        # checks only, changes nothing
python <path-to>/revali.py run              # detached; then:
python <path-to>/revali.py wait --timeout 9m
python <path-to>/revali.py status | stop | reset | clean <branch> | version
```

Exit codes: `0` done / ready to merge, `1` pipeline error (not a verdict),
`2` the author must act (fix, rebase, answer a question), `3` a human must
decide, `4` (`wait` only) still running.

`REVALI_DISABLE=1` in the environment switches revali off entirely.

## Project setup

Copy `templates/revali.toml` to the repo root and edit the commands; copy
`templates/CONVENTIONS.md` if the project has none; add `.revali/` to
`.gitignore`. Before each run the author writes
`.revali/<branch>/change.md` from `templates/change.md` (branch `feature/x`
maps to directory `feature__x`).

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
- on `revali merge`: `gh pr merge --squash --delete-branch`, which also deletes
  the local branch and checks out the base branch; then deletes
  `.revali/<branch>/`
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
