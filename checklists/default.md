# Built-in review checklist (generic, language-neutral)

Findings from this list are kind `convention` unless they also break
behaviour, in which case they are `correctness`.

1. Behaviour changes have tests that fail without the change.
2. Existing tests are not weakened, skipped, or deleted without a stated reason.
3. No credentials, tokens, or private endpoints in code, tests, or fixtures.
4. New or changed dependencies are justified in the change description.
5. Public interfaces (API, CLI, config format) that changed have their
   documentation updated in the same change.
6. Error and failure paths are handled; no silent `except: pass`.
7. No dead code, commented-out blocks, or debugging leftovers.
8. The diff does only what the change description says (no unrelated edits),
   and the description does not claim more than the diff does.
