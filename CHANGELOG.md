# Changelog

One section per release, newest first, dated by its tag. A section is written when the
release is made ([docs/releasing.md](docs/releasing.md)); changes merged since the newest
section are not listed until then.

## 0.3.0 - 2026-09-07

Validation work (PR #28 to #33):
- Stage and sandbox timings in the log and in history; `new_test` runs only the new files
  (#28).
- revali's own suite runs across worker processes (#29).
- Validation reuses the baseline when only the reviewer's test commits changed (#30).
- Before a diagnosis, the reviewer's tests are rerun on the base tip (#31).
- The reviewer's test files must pass the project's `lint` line before they are committed
  (#32).
- A GitHub issue is opened when validation finds a bug the base already had (#33), with
  follow-ups: the evidence line, docs/files.md, a state read retry, issue de-duplication
  and closing keywords (#34, #36).

Archive work (PR #38 to #43):
- `revali merge` moves the branch's review files to `[paths] archive_dir` instead of
  deleting them (#38); tools/backfill_archive.py rebuilds the archive of PRs merged before
  it existed (#39).
- `[review] tests = "archive"` keeps the reviewer's tests in the archive only (#41), with
  follow-ups: pruned cleanup, the mode in `status`, a note on a stale archive (#42).
- The PR body carries no Request section (#43).

Other changes:
- .gitignore is left alone when git already ignores the state directory (#26).
- `--version` is accepted as well as the `version` subcommand (#27).
- README renders on GitHub again, and the pages link to the files they name (#35, #40).
- Leftover lows: re-created test files are the author's, emptied trailer commits are
  logged, `stop` restores the whole state, a Windows write race (#37).

## 0.2.0 - 2026-09-05

Multi-session work (PR #21 to #24):
- No console windows from a detached run; every command names its repository and branch
  (#21).
- One run per working tree, and a run stops when the tree moves under it (#22).
- Sandbox directories per branch, so worktrees of one repository validate in parallel
  (#23).
- Several agents on one repository: the worktree rule, and merging from a worktree (#24).

Other changes:
- Reviewer rounds protect existing test files and list non-blocking findings on exit 2
  (#12).
- Guards restore from HEAD, read NUL-separated status and clean up after an interrupted
  round (#13); a NEEDS_INFO round's test files carry into the next round (#14) and are
  cleaned up on reset (#17).
- A state.json write race is survived, a crashed run is reported, and an approved round
  resumes at validation (#15); the reviewer's test file ownership is recovered from
  Revali-Round trailers after a history rewrite (#16).
- `revali stop` acknowledges a dead run and records it as stopped (#18, #19); every
  `revali <cmd> -h` describes its command (#20).
- The README is split into docs/, with the PR #24 follow-ups (#25).
- Code is formatted with black and linted with ruff, both run from preflight.

## 0.1.0 - 2026-08-30

The first public state: review, validate and merge in one pipeline.
- The pipeline (preflight, review, validate on WSL, merge), history records and the CLI.
- Acceptance criteria approved before implementation (#1); the built-in checklist covers
  confidential data and documentation claims (#2).
- The test suite runs on a stock Linux where only python3 exists (#3).
- Configuration in three layers with every default in defaults.toml (#4, #5).
- The engine interface between revali and the CLI that runs a session (#6, #8).
- README opens with the three roles, the loop as a diagram, and a role table (#7, #9).
- An ssh runner validates on a Linux host reached over ssh (#10).
- Public repositories are allowed, with summaries only on the PR (#11).
