# Project conventions

Read by the author session (via CLAUDE.md) and by the revali reviewer.
Keep it to things a linter cannot check. Delete what does not apply.

## Structure
- Modules stay independent: removing one feature must not break another.
- Core logic uses the standard library only; tooling may use vetted packages,
  each listed with a reason in the dependency manifest.

## Behaviour changes
- Every behaviour change ships with a test that fails without it.
- Existing tests are not weakened to make a change pass; if an assertion must
  change, the change description says why.

## Interfaces
- Public API or CLI changes update the README, or the page under `docs/`
  that describes them, in the same change.
- Error paths are handled and tested, not just the happy path.

## Portability
- No hardcoded absolute paths, user names, or platform-only tools.
