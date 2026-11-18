# Releasing

Before a release, every document a reader can reach is checked against the code. Review
covers the documents a change touches, links to or names (see `doc_roots` in
[configuration.md](configuration.md)); a statement elsewhere that has gone stale is only
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
| `--resume DIR` | run again the documents in DIR that are not done, with DIR's selection, model and cap; DIR's earlier spending counts against `--budget-usd`; a `--doc-usd` given here replaces the recorded cap, a `--model` other than the recorded one is refused; refused for a dry run, another repository, or when the document list has changed |
| `--dry-run` | write the prompts, start no session |

The output directory holds `results.json` (per document: status `done`, `error`,
`not-run` or `started`, or `ready` after a dry run; the model, cost, duration and
findings), `<document>.md` (one report per document whose session ran, `/` in the path
written as `__`), `<document>.prompt.txt` and `<document>.raw.json` (the session's raw
output), and `summary.md`: the counts per severity per document, every high and medium
finding with its evidence, and the documents not audited.

Exit codes: `0` no high or medium finding and every document audited; `2` at least one
high or medium finding; `1` otherwise (a document failed or was not run, or the command
could not start, for instance with the engine's executable missing). A session that fails,
or answers outside the schema, marks its document `error` and the audit goes on with the
next one; a failed session that reported no cost (a timeout) is charged its cap.

A high or medium finding blocks the release until it is fixed; low findings do not.
