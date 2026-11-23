"""Reviewer acceptance tests for `[project] release_branch`: `revali release` on a project
that reviews on one branch (`develop`, the base) and releases from another (`main`).

Real git with a bare `origin`; the fake gh answers `repo view`, the check runs of the commit
and the Release calls. Black-box through the CLI: exit code, the ERROR line, the tag here
and on origin, and the calls gh received.
"""

import re
import unittest

from revali import EXIT_ACTION, EXIT_ERROR, EXIT_OK
from tests.helpers import RepoCase, git, run_cli

GREEN = [{"name": "test", "status": "completed", "conclusion": "success"}]
RELEASE_YML = "changelog:\n  categories:\n    - title: Fixes\n      labels: [patch]\n"
VERSION = "4.5.6"
TAG = "v4.5.6"
TAG_REF = "refs/tags/%s" % TAG


class ReleaseCase(RepoCase):
    """On `main`, pushed, with .github/release.yml and the two `[project]` lines below."""

    with_branch = False
    base_line = 'base_branch = "main"\n'
    release_line = ""
    default_branch = "main"

    def setUp(self):
        super().setUp()
        toml = self.read("revali.toml")
        self.assertIn('base_branch = "main"\n', toml)
        lines = self.base_line + self.release_line
        self.write("revali.toml", toml.replace('base_branch = "main"\n', lines, 1))
        self.write(".github/release.yml", RELEASE_YML)
        self.commit_all("release configuration")
        git(["push", "-q", "origin", "main"], self.repo)
        self.scenario({"commit_checks": GREEN, "default_branch": self.default_branch})

    def sha(self, ref="HEAD"):
        return git(["rev-parse", ref], self.repo).strip()

    def parents(self):
        return git(["log", "-1", "--format=%P", "HEAD"], self.repo).split()

    def change(self, name):
        self.write(name, "content of %s\n" % name)
        self.commit_all("add %s" % name)

    def local_tag(self):
        return git(["tag", "-l", TAG], self.repo).strip()

    def origin_tag(self):
        """The commit the tag on origin points at, "" when origin has none."""
        out = git(["ls-remote", "origin", TAG_REF, TAG_REF + "^{}"], self.repo)
        found = {}
        for line in out.splitlines():
            parts = line.split()
            if len(parts) == 2:
                found[parts[1]] = parts[0]
        return found.get(TAG_REF + "^{}") or found.get(TAG_REF, "")

    def gh_calls(self, needle):
        return [c["argv"] for c in self.fake_calls("gh") if needle in " ".join(c["argv"])]

    def creates(self):
        return [c["argv"] for c in self.fake_calls("gh") if c["argv"][:2] == ["release", "create"]]

    def errors(self, out):
        return [ln for ln in out.splitlines() if ln.startswith("ERROR")]

    def refused(self, argv=None):
        """Exit 2 with one ERROR line and nothing created; returns that line."""
        code, out = run_cli(argv or ["release", VERSION])
        self.assertEqual(code, EXIT_ACTION, out)
        errors = self.errors(out)
        self.assertEqual(len(errors), 1, out)
        self.assertNotIn("Traceback", out)
        self.assertEqual(self.local_tag(), "")
        self.assertEqual(self.origin_tag(), "")
        self.assertEqual(self.creates(), [])
        return errors[0]


class TwoBranchCase(ReleaseCase):
    """base_branch = develop (one reviewed commit ahead of main, pushed), release_branch =
    main; the checkout is on main."""

    base_line = 'base_branch = "develop"\n'
    release_line = 'release_branch = "main"\n'

    def setUp(self):
        super().setUp()
        git(["checkout", "-q", "-b", "develop"], self.repo)
        self.change("reviewed.txt")
        git(["push", "-q", "origin", "develop"], self.repo)
        git(["checkout", "-q", "main"], self.repo)

    def on_develop(self, name, push=True):
        """One more commit on develop; back on main afterwards."""
        git(["checkout", "-q", "develop"], self.repo)
        self.change(name)
        if push:
            git(["push", "-q", "origin", "develop"], self.repo)
        git(["checkout", "-q", "main"], self.repo)

    def merge(self, *sources, push=True):
        """What a merge-commit release pull request leaves on main."""
        git(["merge", "-q", "--no-ff", "-m", "Release"] + list(sources or ["develop"]), self.repo)
        if push:
            git(["push", "-q", "origin", "main"], self.repo)


class ReleasesFromTheReleaseBranch(TwoBranchCase):
    """AC-2, AC-3: a release merge on main is tagged, pushed and published."""

    def test_the_merge_commit_is_tagged_and_published(self):
        self.merge()
        head = self.sha()
        self.assertEqual(len(self.parents()), 2)
        code, out = run_cli(["release", VERSION])
        self.assertEqual(code, EXIT_OK, out)
        self.assertEqual(git(["cat-file", "-t", TAG_REF], self.repo).strip(), "tag")
        self.assertEqual(self.sha(TAG + "^{commit}"), head)
        self.assertEqual(self.origin_tag(), head)
        creates = self.creates()
        self.assertEqual(len(creates), 1, creates)
        self.assertEqual(creates[0][2], TAG)
        # the check runs and the generated notes are those of the merge commit
        checks = self.gh_calls("/check-runs")
        self.assertTrue(checks, self.fake_calls("gh"))
        for argv in checks:
            self.assertIn("commits/%s/check-runs" % head, " ".join(argv))
        notes = self.gh_calls("releases/generate-notes")
        self.assertEqual(len(notes), 1, notes)
        self.assertIn("target_commitish=%s" % head, notes[0])

    def test_the_base_moved_on_after_the_merge(self):
        # the merged side is an ancestor of origin/develop, not its tip
        self.merge()
        self.on_develop("later.txt")
        code, out = run_cli(["release", VERSION])
        self.assertEqual(code, EXIT_OK, out)
        self.assertEqual(self.origin_tag(), self.sha())

    def test_a_stale_origin_base_here_is_fetched_first(self):
        # this checkout's origin/develop is behind the merged side; origin itself has it
        self.merge()
        merged = self.parents()[1]
        git(["update-ref", "refs/remotes/origin/develop", merged + "~1"], self.repo)
        self.assertNotEqual(self.sha("refs/remotes/origin/develop"), merged)
        code, out = run_cli(["release", VERSION])
        self.assertEqual(code, EXIT_OK, out)
        self.assertEqual(self.origin_tag(), self.sha())

    def test_a_tag_made_by_hand_on_the_merge_commit_is_kept(self):
        # the resume behaviour of a one-branch release: only the missing steps run
        self.merge()
        git(["tag", "-a", TAG, "-m", "made by hand", "HEAD"], self.repo)
        tag_object = self.sha(TAG_REF)
        code, out = run_cli(["release", VERSION])
        self.assertEqual(code, EXIT_OK, out)
        self.assertEqual(self.sha(TAG_REF), tag_object)
        self.assertEqual(self.origin_tag(), self.sha())
        self.assertEqual(len(self.creates()), 1)


class RefusesOnTheReleaseBranch(TwoBranchCase):
    """AC-2: the checks of a one-branch release, read against the release branch."""

    def test_on_the_base_branch(self):
        self.merge()
        git(["checkout", "-q", "develop"], self.repo)
        self.assertIn("main", self.refused())

    def test_head_not_pushed(self):
        self.merge(push=False)
        self.assertIn("origin/main", self.refused())

    def test_head_behind_what_origin_has_now(self):
        # HEAD is an older release merge and this checkout's origin/main still names it;
        # only the fetch shows that origin has a newer one
        self.merge()
        older = self.sha()
        self.on_develop("second.txt")
        self.merge()
        git(["reset", "-q", "--hard", older], self.repo)
        git(["update-ref", "refs/remotes/origin/main", older], self.repo)
        self.assertEqual(len(self.parents()), 2)
        self.assertIn("origin/main", self.refused())

    def test_a_dirty_working_tree(self):
        self.merge()
        self.write("draft.txt", "not committed\n")
        self.assertIn("draft.txt", self.refused())

    def test_no_release_yml_in_head(self):
        git(["checkout", "-q", "develop"], self.repo)
        git(["rm", "-q", ".github/release.yml"], self.repo)
        git(["commit", "-q", "-m", "drop release.yml"], self.repo)
        git(["push", "-q", "origin", "develop"], self.repo)
        git(["checkout", "-q", "main"], self.repo)
        self.merge()
        self.assertIn(".github/release.yml", self.refused())

    def test_a_check_run_still_running_on_the_merge_commit(self):
        self.merge()
        self.scenario(
            {"commit_checks": GREEN + [{"name": "slow", "status": "queued", "conclusion": None}]}
        )
        self.assertIn("slow", self.refused())

    def test_a_check_run_failed_on_the_merge_commit(self):
        self.merge()
        self.scenario(
            {"commit_checks": [{"name": "unit", "status": "completed", "conclusion": "failure"}]}
        )
        self.assertIn("unit", self.refused())


class TheMergeCommitRule(TwoBranchCase):
    """AC-3: HEAD is a merge commit of exactly two parents whose second parent is on
    origin/<base>; anything else is exit 2 with nothing created. The tree of the merge is
    TheTreeOfTheReleaseMerge below."""

    def assert_says_merge_commit(self, line):
        self.assertIn("merge commit", line)
        self.assertIn("squash", line)

    def test_a_squashed_release_pull_request(self):
        git(["merge", "-q", "--squash", "develop"], self.repo)
        git(["commit", "-q", "-m", "Release (squashed)"], self.repo)
        git(["push", "-q", "origin", "main"], self.repo)
        self.assertEqual(len(self.parents()), 1)
        self.assert_says_merge_commit(self.refused())

    def test_a_commit_made_straight_on_the_release_branch(self):
        self.merge()
        self.change("hotfix.txt")
        git(["push", "-q", "origin", "main"], self.repo)
        self.assertEqual(len(self.parents()), 1)
        self.assert_says_merge_commit(self.refused())

    def test_a_merge_of_three_parents(self):
        git(["checkout", "-q", "-b", "side", "main"], self.repo)
        self.change("side.txt")
        git(["checkout", "-q", "main"], self.repo)
        self.merge("develop", "side")
        self.assertEqual(len(self.parents()), 3)
        self.assert_says_merge_commit(self.refused())

    def test_a_merged_branch_that_never_reached_the_base(self):
        git(["checkout", "-q", "-b", "hotfix", "main"], self.repo)
        self.change("hotfix.txt")
        git(["checkout", "-q", "main"], self.repo)
        self.merge("hotfix")
        self.assertEqual(len(self.parents()), 2)
        line = self.refused()
        self.assertIn("origin/develop", line)
        self.assertNotIn("squash", line)  # its own message, not the merge-commit one

    def test_base_commits_merged_before_they_were_pushed(self):
        self.on_develop("local-only.txt", push=False)
        self.merge()
        self.assertEqual(len(self.parents()), 2)
        self.assertIn("origin/develop", self.refused())

    def test_a_base_rewound_on_origin_is_seen_after_the_fetch(self):
        # this checkout's origin/develop still has the merged side; origin's develop was
        # moved back, so after the fetch the merged side is not on origin/develop
        self.on_develop("second.txt")
        self.merge()
        merged = self.parents()[1]
        git(["push", "-q", "-f", "origin", "%s~1:refs/heads/develop" % merged], self.repo)
        git(["update-ref", "refs/remotes/origin/develop", merged], self.repo)
        line = self.refused()
        self.assertIn("origin/develop", line)
        self.assertNotIn("squash", line)

    def test_the_dry_run_refuses_too(self):
        git(["merge", "-q", "--squash", "develop"], self.repo)
        git(["commit", "-q", "-m", "Release (squashed)"], self.repo)
        git(["push", "-q", "origin", "main"], self.repo)
        self.assert_says_merge_commit(self.refused(["release", VERSION, "--dry-run"]))


class TheTreeOfTheReleaseMerge(TwoBranchCase):
    """AC-3 (review round 1 F1): HEAD's tree is its second parent's tree, so what the tag
    carries is a commit on origin/<base>; a difference is exit 2 with the paths named."""

    def on_main(self, name="hotfix.txt"):
        """A commit made straight on the release branch, pushed; returns its sha."""
        self.change(name)
        git(["push", "-q", "origin", "main"], self.repo)
        return self.sha()

    def to_develop(self, *args):
        """Run one git command on develop and push it; back on main afterwards."""
        git(["checkout", "-q", "develop"], self.repo)
        git(list(args), self.repo)
        git(["push", "-q", "origin", "develop"], self.repo)
        git(["checkout", "-q", "main"], self.repo)

    def assert_released(self):
        head = self.sha()
        code, out = run_cli(["release", VERSION])
        self.assertEqual(code, EXIT_OK, out)
        self.assertEqual(self.sha(TAG + "^{commit}"), head)
        self.assertEqual(self.origin_tag(), head)
        self.assertEqual(len(self.creates()), 1)

    def test_a_commit_on_the_release_branch_below_the_merge(self):
        self.on_main()
        self.merge()
        self.assertEqual(len(self.parents()), 2)
        # the merged side is on origin/develop (the call raises otherwise): only the tree
        # shows what main added
        merged = self.parents()[1]
        git(["merge-base", "--is-ancestor", merged, "refs/remotes/origin/develop"], self.repo)
        line = self.refused()
        self.assertIn("hotfix.txt", line)
        self.assertNotIn("squash", line)  # not the merge-commit message

    def test_a_file_deleted_on_the_release_branch_below_the_merge(self):
        # an earlier release brought reviewed.txt to main; main then dropped it, so the next
        # release merge lacks a file develop still has
        self.merge()
        git(["rm", "-q", "reviewed.txt"], self.repo)
        git(["commit", "-q", "-m", "drop reviewed.txt on main"], self.repo)
        git(["push", "-q", "origin", "main"], self.repo)
        self.on_develop("second.txt")
        self.merge()
        self.assertEqual(len(self.parents()), 2)
        self.assertIn("reviewed.txt", self.refused())

    def test_a_change_made_in_the_merge_commit(self):
        git(["merge", "-q", "--no-ff", "--no-commit", "develop"], self.repo)
        self.write("reviewed.txt", "edited in the merge\n")
        git(["add", "reviewed.txt"], self.repo)
        git(["commit", "-q", "-m", "Release"], self.repo)
        git(["push", "-q", "origin", "main"], self.repo)
        self.assertEqual(len(self.parents()), 2)
        self.assertIn("reviewed.txt", self.refused())

    def test_the_dry_run_refuses_too(self):
        self.on_main()
        self.merge()
        self.assertIn("hotfix.txt", self.refused(["release", VERSION, "--dry-run"]))

    def test_an_earlier_release_merge_below_this_one(self):
        # main's history holds an older release merge; the new one still has develop's tree
        self.merge()
        self.on_develop("second.txt")
        self.merge()
        self.assertEqual(len(self.parents()), 2)
        self.assert_released()

    def test_a_fix_cherry_picked_to_the_base_before_the_merge(self):
        fix = self.on_main()
        self.to_develop("cherry-pick", fix)
        self.merge()
        self.assert_released()

    def test_the_release_branch_merged_into_the_base_before_the_merge(self):
        self.on_main()
        self.to_develop("merge", "-q", "--no-ff", "-m", "Merge main into develop", "main")
        self.merge()
        self.assert_released()

    def test_a_refused_merge_is_released_once_the_fix_reached_the_base(self):
        # the way out the refusal names: the fix goes to develop and a new merge is released
        fix = self.on_main()
        self.merge()
        self.assertIn("hotfix.txt", self.refused())
        self.to_develop("cherry-pick", fix)
        self.merge()
        self.assert_released()


class NoTrackingRefForTheBase(TwoBranchCase):
    """AC-3 (review round 1 F3): a checkout whose fetch refspec leaves the base out has no
    origin/<base> to check against after the fetch; that is a refusal, not a git error."""

    def test_a_checkout_that_tracks_the_release_branch_only(self):
        self.merge()
        refspec = "+refs/heads/main:refs/remotes/origin/main"
        git(["config", "remote.origin.fetch", refspec], self.repo)
        git(["update-ref", "-d", "refs/remotes/origin/develop"], self.repo)
        line = self.refused()
        self.assertIn("origin/develop", line)
        self.assertIn("git fetch", line)


class AFailedFetch(TwoBranchCase):
    """AC-2 (added): a fetch of either branch that fails is exit 1, nothing created."""

    def failed(self):
        code, out = run_cli(["release", VERSION])
        self.assertEqual(code, EXIT_ERROR, out)
        errors = self.errors(out)
        self.assertEqual(len(errors), 1, out)
        self.assertNotIn("Traceback", out)
        self.assertIn("fetch", errors[0])
        self.assertEqual(self.local_tag(), "")
        self.assertEqual(self.creates(), [])
        return errors[0]

    def test_the_base_branch_is_gone_from_origin(self):
        self.merge()
        git(["push", "-q", "origin", "--delete", "develop"], self.repo)
        self.assertIn("develop", self.failed())
        self.assertEqual(self.origin_tag(), "")

    def test_origin_cannot_be_reached(self):
        self.merge()
        git(["remote", "set-url", "origin", self.info["remote"] + "-gone"], self.repo)
        self.assertIn("main", self.failed())


class DryRunNamesTheBranches(TwoBranchCase):
    """AC-4."""

    def test_the_release_branch_and_the_base(self):
        self.merge()
        code, out = run_cli(["release", VERSION, "--dry-run"])
        self.assertEqual(code, EXIT_OK, out)
        named = [
            ln
            for ln in out.splitlines()
            if ln.startswith("dry run") and re.search(r"\bmain\b", ln) and "develop" in ln
        ]
        self.assertEqual(len(named), 1, out)
        self.assertEqual(self.local_tag(), "")
        self.assertEqual(self.origin_tag(), "")
        self.assertEqual(self.creates(), [])


class TheBaseFromGitHub(TwoBranchCase):
    """AC-2, AC-3: with `base_branch` empty the base is the repository's default branch, and
    the release branch is checked against that one."""

    base_line = 'base_branch = ""\n'
    default_branch = "develop"

    def test_a_merge_of_the_default_branch_is_released(self):
        self.merge()
        code, out = run_cli(["release", VERSION])
        self.assertEqual(code, EXIT_OK, out)
        self.assertEqual(self.origin_tag(), self.sha())

    def test_a_merge_of_another_branch_is_refused(self):
        git(["checkout", "-q", "-b", "hotfix", "main"], self.repo)
        self.change("hotfix.txt")
        git(["checkout", "-q", "main"], self.repo)
        self.merge("hotfix")
        self.assertIn("origin/develop", self.refused())


class TheKeyEmpty(ReleaseCase):
    """AC-1: `release_branch = ""` is the one-branch release of 0.6.0."""

    release_line = 'release_branch = ""\n'

    def test_a_plain_commit_on_the_base_is_released(self):
        head = self.sha()
        self.assertEqual(len(self.parents()), 1)  # no merge commit is asked for
        code, out = run_cli(["release", VERSION])
        self.assertEqual(code, EXIT_OK, out)
        self.assertEqual(self.sha(TAG + "^{commit}"), head)
        self.assertEqual(self.origin_tag(), head)
        self.assertEqual(len(self.creates()), 1)

    def test_another_branch_is_refused(self):
        git(["checkout", "-q", "-b", "topic"], self.repo)
        self.assertIn("main", self.refused())

    def test_the_dry_run_names_the_branch(self):
        # AC-4 on one branch: the release branch is the base branch
        code, out = run_cli(["release", VERSION, "--dry-run"])
        self.assertEqual(code, EXIT_OK, out)
        named = [
            ln
            for ln in out.splitlines()
            if ln.startswith("dry run") and "would run" not in ln and re.search(r"\bmain\b", ln)
        ]
        self.assertEqual(len(named), 1, out)
        self.assertEqual(self.local_tag(), "")


class TheKeyNamesTheBase(ReleaseCase):
    """AC-1: `release_branch` naming the base branch asks for no merge commit either."""

    release_line = 'release_branch = "main"\n'

    def test_a_plain_commit_on_the_base_is_released(self):
        head = self.sha()
        self.assertEqual(len(self.parents()), 1)
        code, out = run_cli(["release", VERSION])
        self.assertEqual(code, EXIT_OK, out)
        self.assertEqual(self.origin_tag(), head)


if __name__ == "__main__":
    unittest.main()
