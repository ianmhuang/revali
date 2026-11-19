---
title: <one line, becomes the PR title>
kind: feature
author_model: <model id of the session that writes the code>
status: draft
---

<!-- Before implementation: fill in Request, Goal, Acceptance criteria,
     Out of scope, Dependencies; show the acceptance criteria to the user.
     Delete the "status: draft" line only after the user approves; revali
     refuses a draft. After implementation: fill in What and Why.
     Each AC is "- AC-n: text", optionally with a note before the colon
     ("- AC-2 (PR #30 F1): text"); its continuation lines, indented or
     not, join its text up to a blank line, a "## " section heading or a
     list item indented no deeper than the AC. Under "Acceptance criteria",
     any other line that starts like an AC stops preflight; other sections
     are not checked. A
     paragraph that merely begins with an AC id, without a bullet or a
     colon, is prose.
     Optional front matter: "release: <label>" gives the PR that release
     label instead of the one [pr] kind_labels maps the kind to (a fix that
     adds a flag is "minor", say); it must be one of [pr] release_labels,
     in any case. -->

## Request
<the user's instruction to the author session, verbatim, in its original language>

## Goal
<what must be true when this is merged>

## Acceptance criteria
- AC-1: <observable behaviour a test can check>
- AC-2: <another one; the reviewer maps every AC to a test, or records why it cannot be tested>

## Out of scope
<what this change deliberately does not do>

## Dependencies
<new or changed dependencies and why; "none" if none>

## What
<what changed, in terms of files / modules / behaviour>

## Why
<the problem this solves, when the request does not already say>
