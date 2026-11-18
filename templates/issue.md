opened by revali from PR #$pr round $round

## Found by
- PR: $pr_url (branch `$branch` against `$base`)
- validation $validation on `$platform`, revali $version
- revali's diagnosis: the tests below fail on `$base` at $base_sha for the same reason they
  fail on the branch, so the code they exercise was already wrong before this branch (the
  rerun on the base, when one ran, is under Evidence)

## Failing test
$tests

## Evidence
$evidence

## Diagnosis
$diagnosis

## Acceptance criterion
$acceptance

## Suggested fix
$fix

## How to close
Fix it on `$base`, or in PR #$pr, with `Fixes #<this issue>` in the commit message. When
`revali merge` merges the PR of a branch whose validation opened or reused this issue, and a
commit of that branch says `Fixes #<this issue>`, revali adds a comment here naming the
merged PR and commit, the cause and, on a private repository, the suggested fix it recorded.
A fix merged any other way gets no comment.
