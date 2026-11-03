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
     ("- AC-2 (PR #30 F1): text") and indented continuation lines; any
     other line that starts like an AC stops preflight. A paragraph that
     merely begins with an AC id, without a bullet or a colon, is prose. -->

## Request
<the user's instruction to the author session, verbatim, in its original language>

## Goal
<what must be true when this is merged>

## Acceptance criteria
- AC-1: <observable behaviour a test can check>
- AC-2: <another one; every AC maps to at least one test>

## Out of scope
<what this change deliberately does not do>

## Dependencies
<new or changed dependencies and why; "none" if none>

## What
<what changed, in terms of files / modules / behaviour>

## Why
<the problem this solves, when the request does not already say>
