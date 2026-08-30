<!-- Paste the block below into the project's CLAUDE.md. -->

## Changes go through revali

Before implementing any change in this repository, write
`.revali/<branch>/change.md` from `<revali dir>/templates/change.md`
(branch `feature/x` maps to directory `feature__x`). Keep `status: draft`,
fill in `Request` (the user's words, verbatim), `Goal`, numbered
`Acceptance criteria`, `Out of scope`, and `Dependencies`, then show the
acceptance criteria to the user and wait for approval. Delete the
`status: draft` line only after the user approves; revali refuses drafts.
Then implement, write your own tests, run the existing suite, fill in
`What`, and commit. Never run revali on your own; the user types `/revali`
when they want the review. Details: `<revali dir>/skill/SKILL.md`.

@CONVENTIONS.md
