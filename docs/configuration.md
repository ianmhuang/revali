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
   platforms, anything project-specific. `templates/revali.toml` is a
   starting point; a key left out inherits from the layers above.

Unknown keys are errors in every layer. `[review] engine` and
`[validate] engine` name the CLI that runs the session (`claude`; the old
`prompt | hybrid` meaning moved to `strategy`). Keys that name a file
(`prompt`, `schema`, `checklist_builtin`) are relative to the project
root; empty means the file revali ships with. `[validate.platform]` in
any layer sets the defaults for every `[validate.<name>]` table.
`[validate] reuse_baseline` (default true) lets validation skip the
existing suite (`test`) when the baseline already ran it on the same tree:
every commit since the baseline carries the reviewer's `Revali-Round`
trailer and touches only `test_dir`. A fix round, an author commit or a
rebase brings the full suite back; `revali reset` makes the next run redo
the baseline, after which validation may reuse it again. Set the key to
false to run the suite every time.
`[validate] rerun_on_base` (default true) makes a validation that fails
in the reviewer's tests (`new_test`) run the same test files once more on
the base tip, so the diagnosis session can tell whether the branch
introduced the failure or inherited it (`introduced_by` in its answer);
see `docs/sandbox.md`. False skips that rerun.

Models: `model = "auto"` (the default) picks the Reviewer one tier above
the Developer's model (`author_model` in `change.md`) and the diagnosis
session one tier below, on the ladder of the configured engine
(`[engines.claude] tiers = ["haiku", "sonnet", "opus", "fable"]`); an
unknown or missing `author_model` means the top tier for the Reviewer and
one below the top for diagnosis. `fallback_model = "auto"` is the tiers
below the chosen one, strongest first. Any explicit model name passes
through unchanged. The chosen model and the reason are printed at spawn
time and recorded in the review and diagnosis headers.

`REVALI_DISABLE=1` in the environment switches revali off entirely.
