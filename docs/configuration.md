# Configuration

Three layers, the most specific wins, and every key may appear in any of
them:

1. `defaults.toml` in the revali checkout: every key with its default. Edit
   it only when a new model generation arrives (the model ladders live
   here, under `[engines.<name>]`).
2. `~/.revali/config.toml`: your machine, every project. `checklist`,
   `history_path`, and any `[section]` from the project file (a WSL distro
   name, a budget, a pinned model). `REVALI_HOME` moves the directory.
3. `revali.toml` in the project root: the commands to build and test, the
   platforms, anything project-specific. [templates/revali.toml](../templates/revali.toml) is a
   starting point; a key left out inherits from the layers above.

Unknown keys are errors in every layer. `[review] engine` and
`[validate] engine` name the CLI that runs the session (`claude`; the old
`prompt | hybrid` meaning moved to `strategy`). Keys that name a file
(`prompt`, `schema`, `checklist_builtin`) are relative to the project
root; empty means the file revali ships with. `[validate.platform]` in
any layer sets the defaults for every `[validate.<name>]` table.
`[project] platforms` lists `linux`, `windows` or both, each with its own
`[validate.<name>]` table; any other name is a configuration error. A
`windows` table needs `runner = "local"` on a Windows host or
`runner = "ssh"` to a Windows machine: `runner = "wsl"` (the default) is
refused there, since WSL runs Linux, and so is `local` on a host that is
not Windows; revali does not check an ssh host's system. A `linux` table
takes any runner, `local` on a Windows host included. Preflight probes the
runner of every listed platform in list order and stops at the first that
cannot start, naming it. This version validates on the first platform
only: with more than one listed, preflight prints a note naming the
platforms that are probed but not validated.
For `runner = "wsl"`, a `wsl.exe` call whose output carries one of
`wsl_transient_errors` (default `["Wsl/Service/WSAETIMEDOUT"]`, matched after
the NUL bytes of wsl.exe's UTF-16 messages are removed) is tried again after
`wsl_retry_wait_s` seconds (default 30), up to `wsl_retries` times (default 2;
0 turns it off); see [docs/sandbox.md](sandbox.md).
For `runner = "wsl"` and `runner = "ssh"`, `max_procs` (default 4096) is the
`ulimit -u` the sandbox sets, 0 for none. It is a total for every process
and thread of the user in that WSL distro or on that ssh host, other
sandboxes and the user's own programs included, not an allowance per
sandbox; see [docs/sandbox.md](sandbox.md).
`[validate] reuse_baseline` (default true) lets validation skip the
existing suite (`test`) when the baseline already ran it on the same tree:
every commit since the baseline carries the reviewer's `Revali-Round`
trailer and touches only `test_dir`. A fix round, an author commit or a
rebase brings the full suite back; `revali reset` makes the next run redo
the baseline, after which validation may reuse it again. Set the key to
false to run the suite every time.
`[validate] parallel_baseline` (default true) starts the baseline when the
other preflight checks have passed and lets it run while the PR stage and
the round 1 reviewer run, instead of before them. When it fails, the
reviewer session is stopped (its cost is then not known) or its answer is
discarded, and the run ends as a failed baseline always did (exit 1, "branch
is broken before review"), except that the branch may already be pushed with
its draft PR open. Set it to false for the old order: nothing is pushed and
no reviewer is paid for a branch whose suite fails.
`[validate] rerun_on_base` (default true) makes a validation that fails
in the reviewer's tests (`new_test`) run the same test files once more on
the base tip, so the diagnosis session can tell whether the branch
introduced the failure or inherited it (`introduced_by` in its answer);
see [docs/sandbox.md](sandbox.md). False skips that rerun.
`[validate] issue_on` (default true) opens one GitHub issue when the
diagnosis of a failed validation marks a failure `cause: code` and
`introduced_by: base`: the reviewer's test fails on the base tip for the
same reason as on the branch, so the code was wrong before the branch. The
issue is created with `gh issue create` as the author, titled
`Pre-existing: <test id> fails on <base>`, labelled `bug` when the
repository has that label (revali creates no label), and assigned to the
author's gh login when `issue_assignee = "author"` (the default; `""`
leaves it unassigned). Its body is [templates/issue.md](../templates/issue.md) in revali, a
`$placeholder` template (`$pr`, `$pr_url`, `$round`, `$validation`,
`$version`, `$branch`, `$base`, `$base_sha`, `$tests`, `$evidence`,
`$diagnosis`, `$acceptance`, `$fix`); `issue_template` names a project file
to use instead. On a repository that is not private the output, the
diagnosis text and the suggested fix are withheld, as in PR comments. One
validation opens one issue for all such failures; a later validation whose
failing tests an earlier issue of the branch already names links that issue
instead. The issue numbers are in `state.json` (`issues`), `tests.md`, the
PR comment and the `ACTION NEEDED` summary; `revali merge` comments on
every one a branch commit references with `Fixes #n` (see
[docs/side-effects.md](side-effects.md)). False opens nothing.
`[project] lint` is one shell line (`ruff check . && black --check .`, say)
that preflight runs on the working tree and stops on with exit 2; the same
line gates the reviewer's tests: after the reviewer writes them the line
runs again, a failure sends the reviewer back once, a second failure ends
the run. Whenever the line runs over the reviewer's tests, the reviewer's
new files are intent-to-add (`git add -N`), so `git ls-files` lists them and a line that checks only
tracked files sees them; they are untracked again afterwards (see
[docs/side-effects.md](side-effects.md)). Leave it empty and nothing checks formatting or style, for your
diff or for the reviewer's files; preflight prints a note when that is so.
`[project] format` (default empty) is the formatter for the reviewer's
tests, one shell line with `{files}` (`black {files}`, say). The reviewer
has no shell to run a formatter, so after each reviewer attempt revali runs
this line on the host over that attempt's new test files, then the `lint`
line: a file that only needed formatting passes without sending the
reviewer back. `{files}` is required, so the line touches nothing else;
preflight stops with exit 2 without it. The line's exit code is logged,
not judged; lint decides. With `lint` empty the format line still runs and
nothing checks its result; preflight's empty-lint note says so.
`[paths] archive_dir` (default `archive`) is where `revali merge` moves the
branch's `.revali/<branch>/` once the PR is merged, as
`<owner>__<repo>/<pr>-<branch>/` under it: the review record outlives the
branch as a local or shared knowledge base. A relative value sits under
`~/.revali/` (`REVALI_HOME`), `~` is expanded, an absolute path is used as
written; the user file and the project file may both set it. Empty deletes
the directory, as before the key existed.
`[review] tests` (default `commit`) says where the reviewer's acceptance
tests go. `commit` is the behaviour described everywhere else in these
docs: each round's files are committed on the branch with the
`Revali-Round` trailer and merge with it. `tests = "archive"` keeps them out
of the repository: the reviewer still writes into `test_dir`, the `lint` line and
the smoke run still see the files there, but at the end of the round they
move to `.revali/<branch>/tests/<their repository path>` and nothing is
committed or pushed; the working tree is clean between rounds. Before
each later round they are copied back into `test_dir` (untracked) for the
reviewer to update or delete, validation and the base rerun send them into
the sandbox as extra files at the same paths, and at `revali merge` they
move to `archive_dir` with the rest of `.revali/<branch>/` (so `archive`
needs a non-empty `archive_dir`; the pair is refused otherwise). What
archive mode does not do: it never commits a test file; a rebase or amend
does not restart the review, since there is no reviewer commit to lose
(the next push is forced with lease); the mode cannot change while a
branch has review rounds (a run with the other value stops with exit 2
before it pushes anything; start a new branch for the other mode). On
every run the state's list of the reviewer's files is rebuilt from the
archive directory, so `revali reset` loses none of them; an archived path
that HEAD tracks is yours and is removed from the archive. `revali status`
prints the recorded mode (`tests: archive`) once the branch has a round.
A commit-mode run that finds that directory non-empty (rounds in archive
mode, then a `reset` and the key switched) logs the files it will never
use, on `run --dry-run` too; delete the directory yourself, or they move
to `archive_dir` with the rest at merge.

Models: `model = "auto"` (the default) picks the Reviewer one tier above
the Developer's model (`author_model` in `change.md`) and the diagnosis
session one tier below, on the ladder of the configured engine
(`[engines.claude] tiers = ["haiku", "sonnet", "opus", "fable"]`); an
unknown or missing `author_model` means the top tier for the Reviewer and
one below the top for diagnosis. `fallback_model = "auto"` is the tiers
below the chosen one, strongest first. Any explicit model name passes
through unchanged. The chosen model and the reason are printed at spawn
time and recorded in the review and diagnosis headers.
`[review] effort` and `[validate] effort` (default `high` for both) are the
effort level the Reviewer and the diagnosis session run at.
`[engines.claude] helper_prefix` (default `claude-haiku`) tells which rows
of a session's model usage are the CLI's own helper calls: the model that
did the work, recorded as `model_actual` and checked for a fallback, is
picked among the other rows.

Reserved keys: defaults.toml carries a few keys this version accepts but
does not act on, so a configuration that sets them stays valid.
`[project] change_source` only accepts `manual` (any other value is a
configuration error); `[merge] auto_merge = true` is ignored with a warning,
merges stay manual; `[project] context_files`, `[review] small_max_lines`,
`[review] security_paths`, `[validate.platform] network` and
`[validate.platform] check_name` are read and not used. `[review] strategy`
is reserved the same way.

`REVALI_DISABLE=1` in the environment switches revali off entirely.
