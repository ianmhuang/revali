# Changelog

One section per release, newest first, dated by its tag. A section is written when the
release is made ([docs/releasing.md](docs/releasing.md)); changes merged since the newest
section are not listed until then.

## 0.5.0 - 2026-11-21

Two commands, `revali init` to set a project up and `revali release` to tag and publish a
version, and config files that are not UTF-8 reported in one line instead of a traceback
(PR #101 to #112).

Upgrading from 0.4.0:
- New commands: `revali init` (#103) writes the missing `revali.toml`, `CONVENTIONS.md`
  and `.github/release.yml`, appends the CLAUDE.md block and has git ignore the state
  directory; `revali release` (#112) checks that HEAD is the pushed tip of the base branch
  with green CI, then tags it, pushes the tag and publishes the GitHub Release, and a rerun
  finishes a release that stopped half-way.
- A config file that is not UTF-8 stops the command with one ERROR line naming the file and
  the line of the bad byte, instead of a traceback (#109); a leading BOM is read (#110);
  `init` stops on a config file that is not valid TOML instead of going on with the default
  state directory (#111).
- Unchanged: PROMPT_VERSION (16), `defaults.toml`, `templates/` and the layout of
  `state.json`.

Setup and releases:
- Installing revali at a release tag, and moving it at each release, is documented (#101).
- `revali init` (#103), the config file reads of every command (#109, #110), `init` on a
  config file that is not valid TOML (#111) and `revali release` (#112), as above.

CI and the suite:
- The Windows CI leg runs as six shards (#104), balanced by the recorded time of each test
  class; the suite also runs on macOS (#106), in three shards (#107), and each Ubuntu leg
  in two (#108).
- Tests copy a fixture repository built once per process instead of building it for every
  test (#105), and concurrent writers of the test call log no longer tear its lines on
  Windows (#102).

Known issues:
- The released 0.2.0 section's "every command names its repository and branch" is broader
  than the code: `version` and `stats` name neither, and `init` and `release` name only the
  repository. Released sections are not rewritten.

## 0.4.0 - 2026-11-19

Validation on Windows as well as Linux, a Reviewer that is shown the documents a change
concerns and runs only on the model it is given, and a benchmark and a documentation audit
to measure revali itself. It holds PR #44 to #99.

Upgrading from 0.3.0:
- The Reviewer no longer falls back to a lower model by itself: `[review] fallback_model =
  "auto"` now means no fallback. A usage limit or an overloaded model stops the run with
  exit 1 and a message saying how to go on; `revali run --reviewer-model <model>` picks
  another model for one run (#99).
- PROMPT_VERSION goes from 8 to 16. A project with its own `[review] prompt` should compare
  it with prompts/review.md: the diff now holds the changes to test files that are on the
  base, earlier reviewer tests among them (#95), and the line on modified reviewer tests
  changed (#95, #98).
- New interface: `revali archive` (#81); `[project] format` (#44); `[review] doc_roots` and
  `doc_map_max` (#80); `[validate] parallel_baseline` (#55); the platform keys
  `wsl_retries`, `wsl_retry_wait_s`, `wsl_transient_errors` (#50) and `max_procs` (#59);
  `windows` as a platform (#76); the `[pr]` section and change.md's `release:` (#96).
- A branch's `state.json` written by 0.3.0 is still read.

Validation and the sandbox:
- The baseline runs alongside the push, the draft PR and review round 1 (#55).
- `windows` is a platform (#76); the baseline and validation run on every listed platform
  (#77), and the diagnosis prompt and the issue name the platform (#78).
- A transient WSL service failure is retried (#50); `wait` shows the retries, and a
  failed diagnosis session's cost counts (#89).
- A sandbox session that ends early is a sandbox error, never a pass (#57); a failed
  cleanup no longer replaces a session's result (#65).
- Every checkout and branch gets its own sandbox directory (#58), and every ssh session its
  own remote directories (#63); the sandbox process limit is configurable (#59).
- A passed validation stays mergeable when a later run stops before pushing (#87).
- In `[review] tests = "archive"` mode the diagnosis prompt says where each archived test
  file can be read (this release).

Review:
- change.md AC lines with a note or on several lines are parsed, malformed ones stop
  preflight (#46), and a prose paragraph that opens with an AC id is not one (#47).
- The reviewer's tests are formatted before lint, and every reviewer attempt's cost
  counts (#44); a stopped round restores the reviewer's files it modified, and lint sees
  its new files (#49); the log says when the files are formatted without lint, and which
  (#90), and the format call skips deleted paths (#91).
- The Reviewer is shown the documents related to the change (#80), without counting the
  changed lines of test files (#83, #85), and the earlier reviewer tests a change modifies
  (#95); the diff leaves out exactly what preflight lists (#82).
- A CHANGES_REQUESTED round owes its fix as soon as it is saved (#86).
- A usage limit or an overloaded model is named, and the Reviewer runs only on its model
  (#99).

Runs, state and merge:
- `revali run` refuses a dirty working tree before it detaches (#51), and a run while git
  tracks files in the state directory (#69); `[project] test_dir` is read once as a plain
  path (#84).
- A fix cycle is counted when the next run stops in preflight (#52).
- State files: null fields are normalized and wrong types refused with a clear error
  (#53); a BOM is read, a state error names the branch, and `reset` salvages the file
  (#88).
- The PR title follows change.md's title (#56), and each PR carries a release label (#96).
- A fetch that lost a ref race is retried, and an open issue with the same title is reused
  (#60); the history file is locked while a row is appended (#61); the log and the history
  row say which revali build ran (#62), and the row stores stage and sandbox seconds to the
  millisecond (#45).
- After a merge, the primary tree's pull is retried and reported (#64); a PR merged or
  closed outside `revali merge` is recorded with `revali archive` (#81).
- On Windows a process counts as alive until its handle is signalled (#75).

CI and the suite:
- GitHub Actions runs lint and the suite on Linux (#48) and on a Windows leg that `revali
  merge` waits for (#67), split into four shards (#74); the stray change.md left the
  tree and CI prints the runner's CPU (#68).
- The suite passes when the temp directory is an 8.3 short path (#66), and test stubs run
  scripts with Git Bash on Windows (#54).

Tools and documents:
- tools/review_bench.py measures how often the Reviewer catches known bugs (#70), with
  cases built from the review archive (#71) and resumable runs (#72).
- The documentation graph and the names the documents use are checked mechanically (#73).
- tools/doc_audit.py audits every document against the code (#92); the release steps and
  this changelog (#93); the audit's findings fixed (#94, #97, #98, this release).

Known issues:
- No audit finding is open except one in the released 0.2.0 section below: "every command
  names its repository and branch" is broader than the code (`version` and `stats` do
  not). Released sections are not rewritten.

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
