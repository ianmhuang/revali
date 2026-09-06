opened by revali from PR #$pr round $round

## Found by
- PR: $pr_url (branch `$branch` against `$base`)
- validation $validation, revali $version
- the reviewer's tests for this PR fail on `$base` at $base_sha for the same reason they
  fail on the branch, so the code they exercise was already wrong before this branch

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
Fix it on `$base`, or in PR #$pr, with `Fixes #<this issue>` in the commit message. When a
PR revali handles merges with such a commit, revali adds a comment here with the cause and
the suggested fix it recorded.
