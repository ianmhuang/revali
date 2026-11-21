# Releasing

Before a release, every document a reader can reach is checked against the code. Review
covers the documents a change touches, those that link to a file it changes, and those
whose code spans name a file it changes or a word on a line it adds or removes (see
`doc_roots` in [configuration.md](configuration.md)); a statement elsewhere that has gone stale is only
caught by reading each document against the code, which is what the audit does.

## The documentation audit

[tools/doc_audit.py](../tools/doc_audit.py) runs one session per document. A document is
every Markdown page in the documentation graph built from `[review] doc_roots` (the pages
reachable by links from README.md, CONVENTIONS.md and CLAUDE.md), plus the `.toml` files
under `templates/` that the graph reaches. Code, schemas and other configuration files
are not audited.

Each session goes through revali's engine for the review role, read-only: it reads the
repository with its file tools, cannot edit and cannot run commands. It answers in
[tools/doc_audit.schema.json](../tools/doc_audit.schema.json); the prompt is
[tools/doc_audit_prompt.txt](../tools/doc_audit_prompt.txt). Neither is under `prompts/` or
`schemas/`, so changing them does not change `PROMPT_VERSION`. A finding has a severity:

- high: following the document fails or misleads about a public interface (a flag, a key,
  an exit code, a file or a side effect that does not exist or behaves otherwise)
- medium: a statement about behaviour that is wrong or stale
- low: wording, a missing detail, or a statement that is stale but harmless

Run it from the repository root:

```
python tools/doc_audit.py --dry-run                                  # list the documents, write the prompts
python tools/doc_audit.py --budget-usd 40 --doc-usd 2                # audit every document
python tools/doc_audit.py --budget-usd 3 --doc-usd 1.5 --only README.md docs/files.md
python tools/doc_audit.py --resume <dir> --budget-usd 50
```

A document starts only while the spending so far plus its cap stays within the budget, so
the total has to leave room for one cap after the documents before it. With no
`--doc-usd` the cap is `[review] budget_usd`, which is set per project (8 in revali's own
revali.toml), so give `--doc-usd` whenever the budget is small.

| flag | meaning |
|---|---|
| `--budget-usd TOTAL` | required unless `--dry-run`, above 0; a document whose cap would take the spending past it is not started, nor is any later one |
| `--doc-usd N` | cap per document, above 0; default `[review] budget_usd` |
| `--model M` | default `[review] model` (`auto` = the top tier, as there is no author); effort and timeout are `[review]`'s |
| `--only DOC ...` | just these documents (paths as `--dry-run` lists them), audited once each in the list's order; an unknown name stops the command |
| `--out DIR` | output directory; default a new temporary directory, printed at the start; a DIR that already holds a run is refused (continue it with `--resume`), one written by `--dry-run` is not |
| `--resume DIR` | run again the documents in DIR that are not done, with DIR's selection, model and cap; DIR's earlier spending counts against `--budget-usd`; a `--doc-usd` given here replaces the recorded cap, a `--model` other than the recorded one is refused; refused for a DIR a dry run wrote, another repository, or when the document list has changed; cannot be combined with `--dry-run`, `--out` or `--only` |
| `--dry-run` | write the prompts, start no session |

The output directory holds `results.json` (per document: status `done`, `error`,
`not-run` or `started`, or `ready` after a dry run; the model, cost, duration and
findings), `<document>.md` (one report per document whose session ran, `/` in the path
written as `__`), `<document>.prompt.txt` and `<document>.raw.json` (the session's raw
output), and, except after a `--dry-run`, `summary.md`: the counts per severity per
document, every high and medium finding with its evidence, and the documents not audited. `results.json` and `summary.md`
are the latest run's; a run after a dry run in the same directory rewrites the prompt of
each document it selects, and the dry run's other prompt files stay.

Exit codes: `0` no high or medium finding and every document audited, or a `--dry-run`
that wrote its prompts; `2` at least one
high or medium finding; `1` otherwise (a document failed or was not run, or the command
could not start, for instance with the engine's executable missing). A session that fails,
or answers outside the schema, marks its document `error` and the audit goes on with the
next one; a failed session that reported no cost (a timeout) is charged its cap. A session
stopped by its model's usage limit or by an overloaded model ends its document as `error`,
charged what it reported, and leaves the documents after it `not-run`; `--resume` after the
reset, or later, runs them. The auditor runs on its model only: like the Reviewer it has no
auto fallback ([docs/configuration.md](configuration.md)).

## Making a release

1. When: a release is made when the maintainer decides, normally when a milestone is done;
   there is no fixed schedule.
2. Audit main with [tools/doc_audit.py](../tools/doc_audit.py), as above. A high or medium
   finding blocks the release until a change fixes it; low findings do not block and go
   into the release's changelog section as known issues. CHANGELOG.md is audited too, but
   a released section records what that release did and is not rewritten: a finding that
   only says a past section no longer matches the code does not block.
3. Pick the version. Before 1.0, raise the minor number for new behaviour or an interface
   change (a CLI flag, a `revali.toml` key, an exit code, a file under `.revali/<branch>/`)
   and the patch number for fixes only.
4. One change, through revali like any other, that:
   - sets `VERSION` in [revali/__init__.py](../revali/__init__.py)
   - adds the section `## <version> - <YYYY-MM-DD>` at the top of
     [CHANGELOG.md](../CHANGELOG.md), dated the day the tag is planned: what the changes
     merged since the previous tag added or changed, with their PR numbers
     (`git log --oneline v<previous>..main`), and the audit's low findings as known issues
   - updates the version in README's Status paragraph
   - updates the tests that pin the version:
     [tests/test_docs_split.py](../tests/test_docs_split.py) and
     [tests/test_review_docssplit.py](../tests/test_review_docssplit.py).
     [tests/test_changelog.py](../tests/test_changelog.py) fails when the newest changelog
     section is not `VERSION`, and when a test that pins the version is not named here. It
     sees a pin written as a string literal inside an `assert...()` call or an `assert`
     statement; a version held in a variable and compared later is not seen. A test that
     checks a changelog section looks its heading up instead of taking it by position, so
     the next release's section does not break it; the pin detector does not see a heading.
   - adds the new version and its date to `RELEASED` in tests/test_changelog.py, and the
     PR numbers its section must name to that test's list; nothing forces this, as the
     test checks only the tail of the list.
5. After the merge, and only on the maintainer's word: an annotated tag on the merge commit,
   pushed to origin, and the GitHub Release for it. The section's heading carries the tag's
   date; when the tag is made on another day than step 4 planned, a change that corrects the
   heading comes first, and the tag goes on that change's merge commit. With the base branch
   checked out and pulled to that commit, `revali release` does all three:

   ```
   python <revali dir>/revali.py release <version> --notes "<lead line>"
   ```

   It first checks, and on a failed check exits 2 having created nothing: the version is
   `X.Y.Z` (a leading `v` is accepted; the tag is `v<version>`); HEAD is on the base branch
   (`[project] base_branch`, else the repository's default branch), the working tree is
   clean, and HEAD is `origin/<base>` after a fetch; `.github/release.yml` is in HEAD; the
   tag does not exist here or on origin on another commit; and every GitHub check run on
   HEAD has passed (one still running is a refusal, not a wait). Only check runs are read
   (GitHub Actions and other apps; the older commit statuses are not), every page of them;
   a commit with none passes with a note, which is also what GitHub reports in the first
   seconds after a push, before a workflow has registered its runs. Then it asks GitHub for
   the notes it generates for the tag on HEAD (`gh api -X POST
   repos/{owner}/{repo}/releases/generate-notes`, which stores nothing): the merged PRs
   since the previous tag, grouped by their release labels through
   [.github/release.yml](../.github/release.yml). `--notes` puts one lead line above them,
   saying why the version exists (the first sentence of its changelog section will do), and
   each section whose heading `[pr] collapse_headings` lists (by default `Internal
   Changes`) is folded into a `<details>` block: the Release shows it closed, with its
   number of entries, and a click opens it. Then it makes the tag, its message
   `<repository name> <version>: <lead line>`, pushes it, and publishes the Release with
   those notes (`gh release create --verify-tag --title <title> --notes-file <file>`). When a step fails (exit 1), run the same command again: a tag already on HEAD
   is kept, and only the steps still missing run; with the Release already there it prints
   its URL and exits 0. Once the tag is on origin on HEAD the rerun does not check CI again:
   when `revali release` pushed it, the check ran before the push, and a workflow that runs
   on every push runs for the tag too, on the same commit; a tag pushed by hand is taken as
   it is, with no CI check. `--dry-run` runs the checks and prints the commands it would
   run and the notes it would publish. By hand, the same three steps are below; GitHub's
   `--generate-notes` lists the Internal Changes section open, as release.yml has no way to
   fold a section:

   ```
   git tag -a v<version> -m "revali <version>: <what it brings>" <merge commit>
   git push origin v<version>
   gh release create v<version> --verify-tag --generate-notes --notes "<lead line>"
   ```

   Last, once the release is published, move the checkout other projects run from to the
   new tag while no run from that checkout is in progress
   (`git -C <revali dir> fetch --tags`, then `git -C <revali dir> checkout v<version>`;
   see Installing revali in [workflow.md](workflow.md)).

## Release notes for a project that uses revali

Every PR revali opens carries one release label (`major`, `minor`, `patch`, `docs`,
`internal` or `skip-changelog` by default) taken from its change.md: the `release:` field
when it has one, else the label `[pr] kind_labels` gives its kind (`minor`, `patch` or
`docs`); `major`, `internal` and `skip-changelog` come from `release:` only
([docs/configuration.md](configuration.md)). `internal` is for a change no user of the
project can notice (tests, CI, internal tooling): it is still listed, under Internal
Changes, which `revali release` publishes collapsed, so a label put on the wrong change
hides nothing. `skip-changelog` leaves a PR out of the notes; keep it for what needs no line
at all, such as a release's own version bump. The Reviewer checks the label against the
diff ([checklists/default.md](../checklists/default.md)).
`revali init` writes `.github/release.yml` from [templates/release.yml](../templates/release.yml)
when the project has none; a project set up without `init` copies it there by hand. With
it, `gh release create v<version> --verify-tag --generate-notes --notes "<lead line>"` after
each tag gives the same grouped notes: Major, Minor, Patch and Docs changes, the rest under
Other, then Internal Changes, a PR labelled `skip-changelog` (`release: skip-changelog` in change.md) left out. A project that labels PRs another way sets
`[pr] release_labels = []`, and revali leaves its labels alone. A project whose
`.github/release.yml` was copied from an earlier template has no Internal Changes category
until it takes the current one: an `internal` PR then falls under Other Changes, still
listed, and is not collapsed.

GitHub reads `.github/release.yml` from the tagged commit, so commit the file before the
tag: a release whose tag predates the file lists its PRs ungrouped under "What's Changed",
and `--generate-notes` cannot be pointed at a newer copy.
