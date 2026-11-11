---
title: The PR body carries no Request section
kind: feature
author_model: claude-fable-5-1
---

## Request
AC-1 改成：PR body 不含 ## Request 段，不分 public / private；其他 AC 不變。
（前情：「如果是這樣，PR的Request欄位跟內容有存在的必要嗎？反正都有Goal了，對吧？Request本機端還是存在的，應該就夠了吧？」
「AC-1照你說的。然後功能做好後，要能回去導入既有的PR: revali, bmc-toolkit, line-social-bot」）

## Goal
The PR body revali writes is change.md without the `## Request` section, on every
repository. The request stays in the local change.md, where the reviewer reads it; the PR
shows Goal, acceptance criteria, Out of scope, Dependencies, What, Why and the status table.

## Acceptance criteria
- AC-1: The PR body (`gh pr create --body-file`, and every `gh pr edit` after a round) has no
  `## Request` heading and none of the request's text, on a private repository as on a public
  one; the frontmatter, Goal, Acceptance criteria, Out of scope, Dependencies, What and Why
  sections and the appended `## revali status` table are unchanged.
- AC-2: The section is dropped wherever it sits in change.md: first section (the template),
  in the middle, or last with no heading after it; a section whose body mentions the words
  `## Request` inline is not touched, only a heading line at the start of a line counts.
- AC-3: The local change.md is not modified and its validation is unchanged: a change.md
  with an empty `## Request` still stops `revali run` before it pushes.
- AC-4: PR comments are unchanged: summaries on a repository that is not private, full text
  on a private one; the `public repository: PR comments will carry summaries only` log line
  stays.
- AC-5: docs/side-effects.md says the PR body drops the `Request` section on every
  repository; tools/backfill_archive.py's module docstring and the README it writes say the
  same instead of "withheld on public repositories"; `tests/test_doc_links.py` and
  `tests/test_review_docssplit.py` stay green.

## Out of scope
- Editing the bodies of PRs that already exist on GitHub (revali, bmc-toolkit,
  line-social-bot): done after this merges with the scratchpad script that withheld them
  today, not by a repo tool.
- The archived change.md files under `[paths] archive_dir`: they keep whatever the PR body
  was when it was archived or backfilled.
- The reviewer still reads the request: the review prompt is built from the local
  change.md and does not change.

## Dependencies
none

## What
- `revali/pr.py`: `strip_request(text)` removes the `## Request` heading line and its text up
  to the next `## ` heading or the end of the file (a heading line only, `re.M`); `pr_body`
  uses it on every repository instead of the public-only "withheld" substitution.
  `is_public` stays for the comment rules.
- `tests/test_pr_body.py` (new): the section first, middle, last with and without a trailing
  newline, the words inline in another section, a longer heading, a document without the
  section, a multi-paragraph request.
- `tests/test_public_repo.py`: the public-repo test asserts no `## Request`, no marker, no
  request text, Goal and AC-1 present; the private-repo test asserts the same absence.
- `tests/test_review_publicrepo.py` (the reviewer's file from PR #11): two assertions changed
  because they pin the old behaviour AC-1 replaces. `test_pr_body_withholds_the_request`
  asserted the body contains `## Request` and `withheld`; it is now
  `test_pr_body_has_no_request_section` asserting neither. The private-repo test asserted the
  body contains the request text `multiplies two numbers`; it now asserts it does not, and
  that the Goal text is there. Nothing else in that file changed.
- `docs/side-effects.md`: the PR body is change.md without its Request section on every
  repository. `tools/backfill_archive.py`: the docstring and the README it writes say the
  same, and that older PRs may show the "withheld" marker.
- Unchanged: change.md validation (`## Request` still required), the review prompt, the
  comment rules, `templates/change.md`, `skill/SKILL.md`.

## Why
The Request was withheld on public repositories so the user's verbatim words (often in
another language, sometimes with local context) do not go public; the same words then showed
up in full on private repositories, and the two behaviours were one more thing to explain.
Goal and the acceptance criteria are the reviewer-facing statement of the change and are on
the PR already; the request itself is only needed locally, where the reviewer reads change.md
in full.
