"""`[project] release_branch` (M3.41): `revali release` tags a branch other than the review
base, a merge commit whose merged side is already on `origin/<base>`.

Real git with a bare `origin`: changes land on `develop` (the base), and a release is a
`git merge --no-ff develop` on `main` (the release branch), as a merge-commit release PR
leaves it. The fake gh answers the check runs and the Release calls.
"""

import os
import unittest

from revali import EXIT_ERROR, EXIT_OK
from tests.helpers import git
from tests.helpers import run_cli as run
from tests.test_release import Case

IDENTITY = ["-c", "user.name=Other", "-c", "user.email=other@example.invalid"]


class TwoBranches(Case):
    """base_branch = develop, release_branch = main; HEAD is a release merge on main."""

    release_branch = "main"

    def setUp(self):
        super().setUp()
        toml = self.read("revali.toml")
        self.assertIn('base_branch = "main"\n', toml)
        lines = 'base_branch = "develop"\nrelease_branch = "%s"\n' % self.release_branch
        self.write("revali.toml", toml.replace('base_branch = "main"\n', lines))
        self.commit_all("review on develop, release from main")
        git(["push", "-q", "origin", "main"], self.repo)
        git(["checkout", "-q", "-b", "develop"], self.repo)
        self.write("feature.txt", "reviewed\n")
        self.commit_all("a reviewed change")
        git(["push", "-q", "origin", "develop"], self.repo)
        git(["checkout", "-q", "main"], self.repo)

    def merge_release(self, source="develop"):
        """A merge-commit release PR: `source` merged into main with --no-ff, pushed."""
        git(["merge", "-q", "--no-ff", "-m", "Release (#9)", source], self.repo)
        git(["push", "-q", "origin", "main"], self.repo)

    def parents(self):
        return git(["log", "-1", "--format=%P", "HEAD"], self.repo).split()


class Releases(TwoBranches):
    """AC-2 and AC-3: a release merge on main is tagged and published."""

    def test_a_merge_of_develop_is_released(self):
        self.merge_release()
        head = self.head()
        code, out = run(["release", "7.8.9"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertEqual(self.tag_commit(), head)
        self.assertEqual(self.origin_tag(), head)
        self.assertEqual(len(self.gh_release_calls("create")), 1)
        notes = [
            c["argv"]
            for c in self.fake_calls("gh")
            if "releases/generate-notes" in " ".join(c["argv"])
        ]
        self.assertEqual(len(notes), 1, notes)
        self.assertIn("target_commitish=%s" % head, notes[0])
        checks = [c for c in self.fake_calls("gh") if "/check-runs" in " ".join(c["argv"])]
        self.assertIn("commits/%s/check-runs" % head, " ".join(checks[0]["argv"]))

    def test_a_merge_of_an_older_develop_commit(self):
        # develop moved on after the release PR was opened: the merged side is still an
        # ancestor of origin/develop
        self.merge_release()
        git(["checkout", "-q", "develop"], self.repo)
        self.write("later.txt", "after the release PR\n")
        self.commit_all("later work")
        git(["push", "-q", "origin", "develop"], self.repo)
        git(["checkout", "-q", "main"], self.repo)
        code, out = run(["release", "7.8.9"])
        self.assertEqual(code, EXIT_OK, out)

    def test_the_base_is_fetched_before_the_ancestry_check(self):
        # added AC-2: another clone pushed develop and the release merge; this checkout pulled
        # main only, so its origin/develop is older than the merged commit
        other = os.path.join(self.tmp, "other")
        git(["clone", "-q", self.info["remote"], other], self.tmp)
        git(["checkout", "-q", "develop"], other)
        with open(os.path.join(other, "more.txt"), "w", encoding="utf-8", newline="\n") as fh:
            fh.write("from the other clone\n")
        git(["add", "more.txt"], other)
        git(IDENTITY + ["commit", "-q", "-m", "more"], other)
        git(["push", "-q", "origin", "develop"], other)
        git(["checkout", "-q", "main"], other)
        git(IDENTITY + ["merge", "-q", "--no-ff", "-m", "Release (#9)", "develop"], other)
        git(["push", "-q", "origin", "main"], other)
        git(["fetch", "-q", "origin", "main"], self.repo)
        git(["merge", "-q", "--ff-only", "origin/main"], self.repo)
        merged = self.parents()[1]
        stale = git(["rev-parse", "refs/remotes/origin/develop"], self.repo).strip()
        self.assertNotEqual(stale, merged)  # the scenario holds: this checkout is behind
        code, out = run(["release", "7.8.9"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertEqual(self.origin_tag(), self.head())

    def test_a_hotfix_that_reached_develop_first(self):
        # review round 1 F1: a fix on main that also landed on develop (a cherry-pick)
        # leaves the release merge's tree equal to develop's
        self.write("hotfix.txt", "fixed\n")
        self.commit_all("hotfix on main")
        git(["push", "-q", "origin", "main"], self.repo)
        fix = self.head()
        git(["checkout", "-q", "develop"], self.repo)
        git(["cherry-pick", fix], self.repo)
        git(["push", "-q", "origin", "develop"], self.repo)
        git(["checkout", "-q", "main"], self.repo)
        self.merge_release()
        code, out = run(["release", "7.8.9"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertEqual(self.origin_tag(), self.head())

    def test_a_rerun_after_a_failed_release_create(self):
        # the resume behaviour is the same on the release branch
        self.merge_release()
        self.scenario({"release_create_exit": 1})
        code, out = run(["release", "7.8.9"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertEqual(self.origin_tag(), self.head())
        self.scenario({"release_create_exit": 0})
        code, out = run(["release", "7.8.9"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertEqual(len(self.gh_release_calls("create")), 2)


class Refuses(TwoBranches):
    """AC-2 and AC-3: exit 2, one ERROR line, nothing created."""

    def test_on_the_base_branch_instead(self):
        self.merge_release()
        git(["checkout", "-q", "develop"], self.repo)
        self.assert_refused(["release", "7.8.9"], "not the release branch main")

    def test_head_not_pushed(self):
        git(["merge", "-q", "--no-ff", "-m", "Release (#9)", "develop"], self.repo)
        self.assert_refused(["release", "7.8.9"], "is not origin/main")

    def test_a_dirty_working_tree(self):
        self.merge_release()
        self.write("notes.txt", "draft\n")
        self.assert_refused(["release", "7.8.9"], "not clean")

    def test_ci_failed_on_the_merge_commit(self):
        self.merge_release()
        self.scenario(
            {"commit_checks": [{"name": "lint", "status": "completed", "conclusion": "failure"}]}
        )
        self.assert_refused(["release", "7.8.9"], "CI checks did not pass on HEAD: lint")

    def test_a_squashed_release(self):
        # a squash merge leaves one parent
        git(["merge", "-q", "--squash", "develop"], self.repo)
        git(["commit", "-q", "-m", "Release (#9)"], self.repo)
        git(["push", "-q", "origin", "main"], self.repo)
        self.assertEqual(len(self.parents()), 1)
        out = self.assert_refused(["release", "7.8.9"], "merge commit")
        self.assertIn("not squashed", self.error_lines(out)[0])

    def test_a_fast_forward_release(self):
        git(["merge", "-q", "--ff-only", "develop"], self.repo)
        git(["push", "-q", "origin", "main"], self.repo)
        out = self.assert_refused(["release", "7.8.9"], "merge commit")
        self.assertIn("not squashed", self.error_lines(out)[0])

    def test_an_octopus_merge(self):
        # added AC-3: a merge commit has exactly two parents
        git(["branch", "side", "main"], self.repo)
        git(["checkout", "-q", "side"], self.repo)
        self.write("side.txt", "side\n")
        self.commit_all("side")
        git(["checkout", "-q", "main"], self.repo)
        git(["merge", "-q", "--no-ff", "-m", "Release (#9)", "develop", "side"], self.repo)
        git(["push", "-q", "origin", "main"], self.repo)
        self.assertEqual(len(self.parents()), 3)
        out = self.assert_refused(["release", "7.8.9"], "merge commit")
        self.assertIn("not squashed", self.error_lines(out)[0])

    def test_a_merge_of_a_branch_that_is_not_on_the_base(self):
        # added AC-3: a hotfix merged straight into main carries commits develop never had
        git(["checkout", "-q", "-b", "hotfix", "main"], self.repo)
        self.write("hotfix.txt", "unreviewed\n")
        self.commit_all("hotfix")
        git(["checkout", "-q", "main"], self.repo)
        self.merge_release("hotfix")
        self.assertEqual(len(self.parents()), 2)
        out = self.assert_refused(["release", "7.8.9"], "not on origin/develop")
        self.assertNotIn("squashed", self.error_lines(out)[0])

    def test_a_merge_of_develop_commits_not_pushed_to_develop(self):
        # the merged side is in the local develop but not on origin/develop
        git(["checkout", "-q", "develop"], self.repo)
        self.write("local.txt", "not pushed\n")
        self.commit_all("local only")
        git(["checkout", "-q", "main"], self.repo)
        self.merge_release()
        self.assert_refused(["release", "7.8.9"], "not on origin/develop")

    def test_a_commit_on_main_below_the_release_merge(self):
        # review round 1 F1: the merged side is on origin/develop, but the tag would also
        # carry a commit made straight on main
        self.write("hotfix.txt", "unreviewed\n")
        self.commit_all("hotfix on main")
        git(["push", "-q", "origin", "main"], self.repo)
        self.merge_release()
        self.assertEqual(len(self.parents()), 2)
        out = self.assert_refused(["release", "7.8.9"], "tree")
        self.assertIn("hotfix.txt", self.error_lines(out)[0])
        self.assertNotIn("squashed", self.error_lines(out)[0])

    def test_a_change_made_in_the_merge_commit(self):
        # review round 1 F1: a conflict resolution, or any edit, inside the merge itself
        git(["merge", "-q", "--no-ff", "--no-commit", "develop"], self.repo)
        self.write("feature.txt", "edited in the merge\n")
        git(["add", "feature.txt"], self.repo)
        git(["commit", "-q", "-m", "Release (#9)"], self.repo)
        git(["push", "-q", "origin", "main"], self.repo)
        out = self.assert_refused(["release", "7.8.9"], "tree")
        self.assertIn("feature.txt", self.error_lines(out)[0])

    def test_the_dry_run_refuses_the_same_way(self):
        git(["merge", "-q", "--ff-only", "develop"], self.repo)
        git(["push", "-q", "origin", "main"], self.repo)
        self.assert_refused(["release", "7.8.9", "--dry-run"], "merge commit")


class Failures(TwoBranches):
    """Added AC-2: a failed fetch of the base is exit 1, before anything is made."""

    def test_the_base_branch_gone_from_origin(self):
        self.merge_release()
        git(["push", "-q", "origin", "--delete", "develop"], self.repo)
        code, out = run(["release", "7.8.9"])
        self.assertEqual(code, EXIT_ERROR, out)
        errors = self.error_lines(out)
        self.assertEqual(len(errors), 1, out)
        self.assertIn("git fetch origin develop failed", errors[0])
        self.assertNotIn("Traceback", out)
        self.assertEqual(self.local_tag(), "")
        self.assertEqual(self.origin_tag(), "")
        self.assertEqual(self.gh_release_calls("create"), [])

    def test_no_tracking_ref_for_the_base(self):
        # review round 1 F3: a single-branch clone fetches develop into FETCH_HEAD only
        self.merge_release()
        refspec = "+refs/heads/main:refs/remotes/origin/main"
        git(["config", "remote.origin.fetch", refspec], self.repo)
        git(["update-ref", "-d", "refs/remotes/origin/develop"], self.repo)
        out = self.assert_refused(["release", "7.8.9"], "origin/develop")
        self.assertIn(
            "git fetch origin develop:refs/remotes/origin/develop", self.error_lines(out)[0]
        )


class DryRun(TwoBranches):
    """AC-4."""

    def test_names_the_release_branch_and_the_base(self):
        self.merge_release()
        code, out = run(["release", "7.8.9", "--dry-run"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn(
            "dry run: release branch main, checked against the base branch develop",
            out.splitlines(),
        )
        self.assertEqual(self.local_tag(), "")
        self.assertEqual(self.gh_release_calls("create"), [])


class SameAsBase(TwoBranches):
    """AC-1: release_branch naming the base branch is the one-branch release of 0.6.0."""

    release_branch = "develop"

    def test_a_plain_commit_on_the_base_is_released(self):
        git(["checkout", "-q", "develop"], self.repo)
        self.assertEqual(len(self.parents()), 1)
        code, out = run(["release", "7.8.9"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertEqual(self.origin_tag(), self.head())

    def test_the_dry_run_names_one_branch(self):
        git(["checkout", "-q", "develop"], self.repo)
        code, out = run(["release", "7.8.9", "--dry-run"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("dry run: release branch develop (the base branch)", out.splitlines())


class Unset(Case):
    """AC-1 and AC-4 with the key left out: the base branch is the release branch."""

    def test_the_dry_run_names_the_base_as_the_release_branch(self):
        code, out = run(["release", "7.8.9", "--dry-run"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("dry run: release branch main (the base branch)", out.splitlines())

    def test_a_value_that_is_not_a_string(self):
        toml = self.read("revali.toml")
        self.write("revali.toml", toml.replace("[project]\n", "[project]\nrelease_branch = 5\n", 1))
        self.commit_all("bad key")
        git(["push", "-q", "origin", "main"], self.repo)
        code, out = run(["release", "7.8.9"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertIn("release_branch", out)
        self.assertNotIn("Traceback", out)
        self.assertEqual(self.local_tag(), "")


if __name__ == "__main__":
    unittest.main()
