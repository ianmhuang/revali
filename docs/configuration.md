# Configuration

Three layers, the most specific wins. A key may appear in any of them,
except the user-level ones: `checklist` and `history_path` at the top level
and `[paths] history_file` belong in `~/.revali/config.toml`, and revali.toml
setting one of them is a configuration error.

1. `defaults.toml` in the revali checkout: every key with its default,
   except `[project] config_version`, which must match this revali's config
   version (1). Edit
   it only when a new model generation arrives (the model ladders live
   here, under `[engines.<name>]`).
2. `~/.revali/config.toml`: your machine, every project. `checklist`,
   `history_path`, and any `[section]` from the project file (a WSL distro
   name, a budget, a pinned model). `REVALI_HOME` moves the directory.
3. `revali.toml` in the project root: the commands to build and test, the
   platforms, anything project-specific. [templates/revali.toml](../templates/revali.toml) is a
   starting point; a key left out inherits from the layers above.

The three files are read as UTF-8; a BOM at the start, which some Windows editors write, is
ignored. One that is not UTF-8, or is not valid TOML, is a configuration error naming the
file (for a file that is not UTF-8, also the line of the first bad byte): `preflight` and
`run` list it and stop, `init` names it and stops; `status`, `wait` and `merge` fall back to
the defaults to find the state directory, and `merge` of a ready branch then stops on it. A
`defaults.toml` that does not load stops every command that reads it (all but `version`, and
`stats` when the user's file sets `history_path`) with an ERROR line naming the file.

Unknown keys are errors in every layer. `[review] engine` and
`[validate] engine` name the CLI that runs the session (`claude`; the old
`prompt | hybrid` meaning moved to `strategy`). Keys that name a file
(`prompt`, `schema`, `checklist_builtin`) are relative to the project
root; empty means the file revali ships with. `[validate.platform]` in
any layer sets the defaults for every `[validate.<name>]` table.
`[project] platforms` lists `linux`, `windows` or both, each with its own
`[validate.<name>]` table; any other name, or a name listed twice, is a
configuration error. `[project] test_dir` is a directory inside the
repository, relative to its root, read in its plain form (`./tests`, `tests/` and `tests\`
are `tests`); an empty value, `.`, an absolute path, one with a `..` component or one with
a blank at either end of a component is a configuration error. A `windows` table needs `runner = "local"` on a
Windows host or `runner = "ssh"`: `runner = "wsl"` (the default) is
refused there, since WSL runs Linux, and so is `local` on a host that is
not Windows. The ssh runner hands its commands to the remote login shell,
so a Windows machine over ssh needs bash as sshd's default shell (Git Bash,
say) with git and coreutils `timeout` on its PATH, which is what
preflight's probe checks; revali does not check the remote system itself,
and has not been run against such a host. A `linux` table takes any
runner, `local` on a Windows host included. Preflight probes the runner of
every listed platform in list order (a dry run probes none) and stops at
the first that cannot start, naming it. The baseline and every validation
run on every listed platform, one after another in list order: the
baseline stops at the first platform that fails, validation runs them all
and FAILs when any of them does, each failing platform with its own base
rerun and diagnosis. The diagnosis prompt names the platform and runner
that failed, with one platform listed too, and with more than one lists
how the other platforms of the same validation went (PASS, FAIL at a
step, or not run and why); the issue body for a pre-existing bug names
the platform and runner as well. Their times add up, and so do their
costs: each diagnosis has its own `[validate] budget_usd`, so one
validation can spend up to that budget times the number of failing
platforms. The reviewer's smoke run uses the first platform only. A
project's own `[validate] prompt` gets the diagnosis prompt's placeholders
(`$branch`, `$base`, `$kind`, `$platform`, `$runner`, `$other_platforms`,
`$failed_step`, `$failed_cmd`, `$failed_exit`, `$timed_out_note`,
`$change_md`, `$tests_md`, `$test_files`, `$log_lines`, `$log_tail`,
`$base_sha`, `$base_rerun`); one written before a placeholder existed
leaves that text out. With more than one platform listed, the sandbox
labels, logs and diagnosis files carry the platform's name
([docs/files.md](files.md)).
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
trailer and touches only `test_dir`, and the baseline ran on every platform
listed now (a state file that does not record the platforms, written
before validation covered every platform, counts as the first platform
only). A fix round, an author commit or a
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
`Pre-existing: <test id> fails on <base>` (`Pre-existing: <test id> and <n>
more fail on <base>` when there are several, `<test id>` the first in sort
order), labelled `bug` when the
repository has that label (revali does not create `bug`), and assigned to the
author's gh login when `issue_assignee = "author"` (the default; `""`
leaves it unassigned). Its body is [templates/issue.md](../templates/issue.md) in revali, a
`$placeholder` template (`$pr`, `$pr_url`, `$round`, `$validation`,
`$platform`, the platform the failure was seen on, `$version`, `$branch`,
`$base`, `$base_sha`, `$tests`, `$evidence`,
`$diagnosis`, `$acceptance`, `$fix`); `issue_template` names a project file
to use instead. On a repository that is not private the output, the
diagnosis text and the suggested fix are withheld, as in PR comments. Each
failing platform is diagnosed on its own, and each diagnosis opens at most
one issue for its base-introduced failures, so a validation that fails on two
platforms with different failing tests opens two. When the branch's earlier
issues already name every failing test, revali links them instead of opening
one, and an open issue in the repository with exactly the same title (opened
from another branch or worktree) is reused: it is recorded as found, not
opened. The issue numbers are in `state.json` (`issues`), `tests.md`, the
PR comment and the `ACTION NEEDED` summary; `revali merge` comments on
every one a branch commit references with `Fixes #n` or another closing keyword (see
[docs/side-effects.md](side-effects.md)). False opens nothing.
`[project] lint` is one shell line (`ruff check . && black --check .`, say)
that the preflight of `revali run` runs on the working tree and stops on with
exit 2 (`revali preflight` and `run --dry-run` print it and do not run it); the
same line gates the reviewer's tests: after the reviewer writes them the line
runs again and a failure sends the reviewer back, once per round whatever the
reason, so a failure on the attempt after that bounce ends the run (see
[docs/side-effects.md](side-effects.md)). Whenever the line runs over the reviewer's tests, the reviewer's
new files are intent-to-add (`git add -N`), so `git ls-files` lists them and a line that checks only
tracked files sees them; they are untracked again afterwards (see
[docs/side-effects.md](side-effects.md)). Leave it empty and nothing checks formatting or style, for your
diff or for the reviewer's files; preflight prints a note when that is so.
`[project] format` (default empty) is the formatter for the reviewer's
tests, one shell line with `{files}` (`black {files}`, say). The reviewer
has no shell to run a formatter, so after each reviewer attempt revali runs
this line on the host, then the `lint` line: a file that only needed
formatting passes without sending the reviewer back. It runs over the files
under `test_dir` that differ from HEAD after the attempt: those the reviewer
wrote or changed in it, and with `[review] tests = "archive"` also the
earlier rounds' files placed back for the round. `{files}` is required, so
the line touches nothing else; preflight stops with exit 2 without it. The line's exit code is logged,
not judged; lint decides. With `lint` empty the format line still runs and
nothing checks its result; preflight's empty-lint note says so.
`[paths] archive_dir` (default `archive`) is where `revali merge` moves the
branch's `.revali/<branch>/` once the PR is merged (and `revali archive` once
the PR was merged or closed elsewhere), as
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
`[review] doc_roots` (default `["README.md", "CONVENTIONS.md",
"CLAUDE.md"]`) are the roots of the project's documentation graph: every
file reachable from them through relative Markdown links, at any depth. The
reviewer prompt lists the Markdown documents of that graph that link to a
file the diff changes, or that name in a code span something the diff adds
or removes: a word with a `_`, `-`, `.`, `/` or camelCase hump (`max_procs`,
`--branch`; not `run`) found as a whole word on a changed line of another
file, or a changed file's whole path. The changed lines of files under `test_dir` do not
count (a test's identifiers would list documents the change does not concern); a link to a
test file, or its whole path, does. Files the diff
leaves out do not count: `[review] exclude` and the reviewer's test pattern,
each matching a changed file's whole path, its file name, or a `dir/**`
prefix (a renamed file by its new name). The test pattern leaves out only the
files the branch adds; a file already on the base, an earlier change's
reviewer test included, is reviewed and counted like any other change. So is
a file git reports as renamed from a base file the branch deletes (renames are
found at 50% similarity), even when the reviewer wrote it on this branch. The
same files are left out of the reviewer's diff and of preflight's diff size. Documents
outside the graph are not searched, and a document the diff changes itself
is marked so. The list ends with a map of the graph's documents and their
depth, at most `doc_map_max` of them (default 100; 0 leaves the map out).
The checklist's documentation item then asks the reviewer to compare those
documents with the new behaviour. A project's own `[review] prompt` gets
the section through `$related_docs_section`; one written before the
placeholder existed leaves it out. `doc_roots = []` turns all of it off; a
root that does not exist is skipped, and a graph that cannot be read is
logged and noted in the prompt without stopping the review.

Models: `model = "auto"` (the default) picks the Reviewer one tier above
the Developer's model (`author_model` in `change.md`) and the diagnosis
session one tier below, on the ladder of the configured engine
(`[engines.claude] tiers = ["haiku", "sonnet", "opus", "fable"]`); an
unknown or missing `author_model` means the top tier for the Reviewer and
one below the top for diagnosis. `fallback_model = "auto"` is the tiers
below the chosen one, strongest first. For the Reviewer `auto` means no
fallback: it runs on the model `[review] model` or `--reviewer-model` gives,
and when that model is at its usage limit or busy the run stops so the user
decides; an explicit `fallback_model` (a model the user named) is used as
written. The claude engine falls back when the model is busy or unavailable. Any explicit model name passes
through unchanged. The chosen model and the reason are printed at spawn
time and recorded in the review and diagnosis headers.
`revali run --reviewer-model <model>` puts `<model>` in place of `[review] model`
for that run only (`fallback_model` is unaffected: none with `auto`, or the
model the user named);
the reason then reads `--reviewer-model, ` and where the model stands against
the author's tier (above, the same tier as, below), and a non-private
repository's PR comment carries it too. revali never lowers the Reviewer's tier
on its own: a session that hits its model's usage limit, or whose model is
overloaded, stops the run instead
([docs/workflow.md](workflow.md)).
`[review] effort` and `[validate] effort` (default `high` for both) are the
effort level the Reviewer and the diagnosis session run at.
`[engines.claude] helper_prefix` (default `claude-haiku`) tells which rows
of a session's model usage are the CLI's own helper calls: the model that
did the work, recorded as `model_actual` and checked for a fallback, is
picked among the other rows.

Release labels: `[pr] release_labels`, by default
`["major", "minor", "patch", "docs", "skip-changelog"]`, are the PR labels
revali keeps in step, and `[pr] kind_labels`, by default
`{ feature = "minor", fix = "patch", docs = "docs" }`, maps a change.md
kind (`feature`, `fix`, `docs`) to one of them. A PR carries the label
change.md's optional `release:` field names, else the one its kind maps to,
else none of them; each run that opens or reuses the PR and `revali merge`
add that label (creating it first when the repository lacks it) and remove the
list's others ([docs/side-effects.md](side-effects.md)). A
layer that sets `kind_labels` replaces the whole table, and a layer whose
`release_labels` drops a label the table maps to (by default `minor`,
`patch`, `docs`) sets `kind_labels` too; adding a label needs no more.
Label names compare in any case, as GitHub's do, the PR gets the list's
spelling, and a name listed twice is an error. A `kind_labels` key
outside `feature`, `fix`, `docs`, `refactor`, `hotfix` and `small` or a value
outside `release_labels`, or a label with a comma or surrounding spaces, is a
configuration error; `refactor`, `hotfix` and `small` are kinds this version
refuses in change.md, so those keys are accepted and never apply. A
`release:` value outside the list stops preflight; preflight's `ok:` line
ends with `; label <name>`. If change.md is edited after the run to such a
value, `revali merge` warns and leaves the PR's labels as they are.
`release_labels = []` turns it off: no label is
added or removed, and `kind_labels` is not checked. [templates/release.yml](../templates/release.yml) groups the release
notes GitHub generates by these labels ([docs/releasing.md](releasing.md)). With the
defaults:

| change.md says | The PR's label | Its heading in the release notes |
|---|---|---|
| `kind: feature` | `minor` | Minor Changes |
| `kind: fix` | `patch` | Patch Changes |
| `kind: docs` | `docs` | Docs |
| `release: major` (any kind) | `major` | Major Changes |
| `release: skip-changelog` | `skip-changelog` | left out of the notes |
| (a PR revali did not open) | none of these | Other Changes |

Reserved keys: defaults.toml carries a few keys this version accepts but
does not act on. `[project] change_source` only accepts `manual` and
`[review] strategy` only `prompt` (any other value is a configuration
error); `[merge] auto_merge = true` is ignored with a warning, merges stay
manual; `[project] context_files`, `[review] small_max_lines`,
`[review] security_paths`, `[validate.platform] network` and
`[validate.platform] check_name` are read and not used, so a configuration
that sets them stays valid.

`REVALI_DISABLE=1` in the environment (any value but empty, `0` or `false`)
stops `run` (the detached run ends at once with exit 1, which `wait`
reports), `run --dry-run` and `preflight` with exit 1, "REVALI_DISABLE is
set; revali is switched off on this machine", and makes `reset` leave the
reviewer's leftover test files for you to delete. `wait`, `status`, `stop`,
`clean`, `merge` and `archive` do not read it.
