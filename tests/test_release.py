"""`revali release <version>` (M3.29): checks, then tag, push and GitHub Release; a rerun
finishes a release that stopped half-way; `--dry-run` changes nothing.

Real git with a bare `origin`; the fake gh answers `repo view`, the commit's check runs and
`release view` / `release create` from the scenario.
"""

import os
import unittest
from unittest import mock

from revali import EXIT_ACTION, EXIT_ERROR, EXIT_OK, release
from revali.procs import ProcTimeout
from tests.helpers import RepoCase, git
from tests.helpers import run_cli as run  # review round 1 follow-up 1

GREEN = [{"name": "test", "status": "completed", "conclusion": "success"}]
RELEASE_YML = "changelog:\n  categories:\n    - title: Fixes\n      labels: [patch]\n"


class Case(RepoCase):
    with_branch = False  # on main, pushed to origin

    def setUp(self):
        super().setUp()
        self.write(".github/release.yml", RELEASE_YML)
        self.commit_all("release notes config")
        git(["push", "-q", "origin", "main"], self.repo)
        self.scenario({"commit_checks": GREEN})

    def head(self):
        return git(["rev-parse", "HEAD"], self.repo).strip()

    def local_tag(self, tag="v7.8.9"):
        return git(["tag", "-l", tag], self.repo).strip()

    def tag_commit(self, tag="v7.8.9"):
        return git(["rev-parse", "%s^{commit}" % tag], self.repo).strip()

    def origin_tag(self, tag="v7.8.9"):
        """The commit origin's tag points at, "" when origin has none."""
        out = git(["ls-remote", "origin", "refs/tags/%s^{}" % tag, "refs/tags/%s" % tag], self.repo)
        lines = dict(reversed(ln.split()) for ln in out.splitlines() if ln.strip())
        return lines.get("refs/tags/%s^{}" % tag) or lines.get("refs/tags/%s" % tag, "")

    def gh_release_calls(self, verb):
        return [c["argv"] for c in self.fake_calls("gh") if c["argv"][:2] == ["release", verb]]

    def error_lines(self, out):
        return [ln for ln in out.splitlines() if ln.startswith("ERROR")]

    def assert_refused(self, argv, words):
        code, out = run(argv)
        self.assertEqual(code, EXIT_ACTION, out)
        errors = self.error_lines(out)
        self.assertEqual(len(errors), 1, out)
        self.assertIn(words, errors[0])
        self.assertNotIn("Traceback", out)
        self.assertEqual(self.local_tag(), "")
        self.assertEqual(self.origin_tag(), "")
        self.assertEqual(self.gh_release_calls("create"), [])
        return out


class Releases(Case):
    """AC-1."""

    def test_tags_pushes_and_publishes(self):
        head = self.head()
        code, out = run(["release", "7.8.9", "--notes", "init and config file fixes"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertEqual(git(["cat-file", "-t", "refs/tags/v7.8.9"], self.repo).strip(), "tag")
        self.assertEqual(self.tag_commit(), head)
        message = git(["tag", "-l", "--format=%(contents)", "v7.8.9"], self.repo).strip()
        self.assertEqual(message, "sample 7.8.9: init and config file fixes")
        self.assertEqual(self.origin_tag(), head)
        self.assertEqual(
            self.gh_release_calls("create"),
            [
                [
                    "release",
                    "create",
                    "v7.8.9",
                    "--verify-tag",
                    "--generate-notes",
                    "--notes",
                    "init and config file fixes",
                ]
            ],
        )
        url = "https://github.example/me/sample/releases/tag/v7.8.9"
        self.assertIn("RELEASED: v7.8.9 on %s: %s" % (head[:12], url), out.splitlines())

    def test_a_v_prefix_and_no_notes(self):
        code, out = run(["release", "v7.8.9"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertEqual(self.tag_commit(), self.head())
        message = git(["tag", "-l", "--format=%(contents)", "v7.8.9"], self.repo).strip()
        self.assertEqual(message, "sample 7.8.9")
        self.assertEqual(
            self.gh_release_calls("create"),
            [["release", "create", "v7.8.9", "--verify-tag", "--generate-notes"]],
        )

    def test_a_commit_with_no_check_runs_is_released(self):
        self.scenario({"commit_checks": []})
        code, out = run(["release", "7.8.9"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("no CI check runs on HEAD", out)

    def test_skipped_and_neutral_runs_count_as_passed(self):
        self.scenario(
            {
                "commit_checks": GREEN
                + [
                    {"name": "docs", "status": "completed", "conclusion": "skipped"},
                    {"name": "info", "status": "completed", "conclusion": "neutral"},
                ]
            }
        )
        code, out = run(["release", "7.8.9"])
        self.assertEqual(code, EXIT_OK, out)


class Refuses(Case):
    """AC-2: exit 2, one ERROR line, no tag here or on origin, no Release."""

    def test_a_version_that_is_not_x_y_z(self):
        # review round 1 F4: ASCII digits only, and no leading zeros
        full_width = ".".join(chr(0xFF10 + d) for d in (0, 5, 0))
        arabic_indic = ".".join(chr(0x0660 + d) for d in (0, 5, 0))
        for version in (
            "0.5",
            "1.2.3-rc1",
            "va.b.c",
            "7.8.9.1",
            "01.2.3",
            "1.02.3",
            full_width,
            arabic_indic,
        ):
            with self.subTest(version=ascii(version)):
                self.assert_refused(["release", version], "X.Y.Z")

    def test_zero_parts_are_plain_numbers(self):
        code, out = run(["release", "0.0.0", "--dry-run"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("refs/tags/v0.0.0", out)

    def test_the_state_directory_does_not_count_as_dirty(self):
        # review round 1 F3: revali's own records, even where git does not ignore them
        kept = [ln for ln in self.read(".gitignore").splitlines() if ln.strip(" /") != ".revali"]
        self.write(".gitignore", "".join(ln + "\n" for ln in kept))
        self.commit_all("stop ignoring the state directory")
        git(["push", "-q", "origin", "main"], self.repo)
        self.write(".revali/main/state.json", "{}\n")
        code, out = run(["release", "7.8.9"])
        self.assertEqual(code, EXIT_OK, out)

    def test_a_tag_that_is_not_a_commit(self):
        # review round 1 follow-up 2: a refusal, not a failed `git tag`
        tree = git(["rev-parse", "HEAD^{tree}"], self.repo).strip()
        git(["tag", "v7.8.9", tree], self.repo)
        code, out = run(["release", "7.8.9"])
        self.assertEqual(code, EXIT_ACTION, out)
        self.assertIn("does not point at a commit", self.error_lines(out)[0])
        self.assertEqual(self.gh_release_calls("create"), [])

    def test_not_on_the_base_branch(self):
        git(["checkout", "-q", "-b", "topic"], self.repo)
        self.assert_refused(["release", "7.8.9"], "not the base branch main")

    def test_the_base_branch_from_github_when_the_config_leaves_it_empty(self):
        toml = self.read("revali.toml")
        self.write("revali.toml", toml.replace('base_branch = "main"', 'base_branch = ""'))
        self.commit_all("no base in config")
        git(["push", "-q", "origin", "main"], self.repo)
        self.scenario({"default_branch": "trunk"})
        self.assert_refused(["release", "7.8.9"], "not the base branch trunk")

    def test_a_dirty_working_tree(self):
        self.write("notes.txt", "draft\n")
        out = self.assert_refused(["release", "7.8.9"], "not clean")
        self.assertIn("notes.txt", out)

    def test_head_not_pushed(self):
        self.write("more.txt", "x\n")
        self.commit_all("not pushed")
        self.assert_refused(["release", "7.8.9"], "is not origin/main")

    def test_head_behind_origin(self):
        self.write("more.txt", "x\n")
        self.commit_all("pushed, then dropped here")
        git(["push", "-q", "origin", "main"], self.repo)
        git(["reset", "-q", "--hard", "HEAD~1"], self.repo)
        self.assert_refused(["release", "7.8.9"], "is not origin/main")

    def test_no_release_yml_in_head(self):
        git(["rm", "-q", ".github/release.yml"], self.repo)
        git(["commit", "-q", "-m", "drop it"], self.repo)
        git(["push", "-q", "origin", "main"], self.repo)
        self.assert_refused(["release", "7.8.9"], ".github/release.yml is not in HEAD")

    def test_a_local_tag_on_another_commit(self):
        git(["tag", "-a", "v7.8.9", "-m", "old", "HEAD~1"], self.repo)
        code, out = run(["release", "7.8.9"])
        self.assertEqual(code, EXIT_ACTION, out)
        self.assertIn("tag v7.8.9 already exists on", self.error_lines(out)[0])
        self.assertEqual(self.origin_tag(), "")
        self.assertEqual(self.gh_release_calls("create"), [])

    def test_a_tag_on_origin_on_another_commit(self):
        old = git(["rev-parse", "HEAD~1"], self.repo).strip()
        git(["tag", "-a", "v7.8.9", "-m", "old", old], self.repo)
        git(["push", "-q", "origin", "v7.8.9"], self.repo)
        git(["tag", "-d", "v7.8.9"], self.repo)
        code, out = run(["release", "7.8.9"])
        self.assertEqual(code, EXIT_ACTION, out)
        self.assertIn("already exists on origin on %s" % old[:12], self.error_lines(out)[0])
        self.assertEqual(self.local_tag(), "")
        self.assertEqual(self.gh_release_calls("create"), [])

    def test_ci_failed(self):
        self.scenario(
            {
                "commit_checks": GREEN
                + [{"name": "lint", "status": "completed", "conclusion": "failure"}]
            }
        )
        self.assert_refused(["release", "7.8.9"], "CI checks did not pass on HEAD: lint")

    def test_ci_still_running_is_refused_without_waiting(self):
        self.scenario(
            {
                "commit_checks": GREEN
                + [{"name": "macos", "status": "in_progress", "conclusion": None}]
            }
        )
        self.assert_refused(["release", "7.8.9"], "still running on HEAD: macos")
        checks = [c for c in self.fake_calls("gh") if "/check-runs" in " ".join(c["argv"])]
        self.assertEqual(len(checks), 1)


class Resumes(Case):
    """AC-3: a rerun keeps a tag already on HEAD and runs only the missing steps."""

    def test_a_local_tag_not_pushed_yet(self):
        git(["tag", "-a", "v7.8.9", "-m", "made by hand", "HEAD"], self.repo)
        tag_object = git(["rev-parse", "refs/tags/v7.8.9"], self.repo).strip()
        code, out = run(["release", "7.8.9"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertEqual(git(["rev-parse", "refs/tags/v7.8.9"], self.repo).strip(), tag_object)
        self.assertEqual(self.origin_tag(), self.head())
        self.assertEqual(len(self.gh_release_calls("create")), 1)
        self.assertNotIn("tagged v7.8.9", out)

    def test_a_tag_on_origin_and_no_release(self):
        git(["tag", "-a", "v7.8.9", "-m", "made by hand", "HEAD"], self.repo)
        git(["push", "-q", "origin", "v7.8.9"], self.repo)
        git(["tag", "-d", "v7.8.9"], self.repo)
        code, out = run(["release", "7.8.9"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertNotIn("tagged v7.8.9", out)
        self.assertNotIn("pushed v7.8.9", out)
        self.assertEqual(len(self.gh_release_calls("create")), 1)

    def test_everything_done_already(self):
        url = "https://github.example/me/sample/releases/tag/v7.8.9"
        git(["tag", "-a", "v7.8.9", "-m", "made by hand", "HEAD"], self.repo)
        git(["push", "-q", "origin", "v7.8.9"], self.repo)
        self.scenario({"releases": {"v7.8.9": url}})
        code, out = run(["release", "7.8.9"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn(url, out)
        self.assertEqual(self.gh_release_calls("create"), [])

    def test_a_failed_release_create_finishes_on_the_rerun(self):
        self.scenario({"release_create_exit": 1})
        code, out = run(["release", "7.8.9"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertIn("run the same command again", out)
        self.assertEqual(self.origin_tag(), self.head())
        tag_object = git(["rev-parse", "refs/tags/v7.8.9"], self.repo).strip()
        self.scenario({"release_create_exit": 0})
        code, out = run(["release", "7.8.9"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertEqual(git(["rev-parse", "refs/tags/v7.8.9"], self.repo).strip(), tag_object)
        self.assertEqual(len(self.gh_release_calls("create")), 2)

    def test_a_failed_push_finishes_on_the_rerun(self):
        hook = os.path.join(self.info["remote"], "hooks", "pre-receive")
        with open(hook, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("#!/bin/sh\necho refused by the test >&2\nexit 1\n")
        os.chmod(hook, 0o755)
        code, out = run(["release", "7.8.9"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertIn("git push origin v7.8.9 failed", out)
        self.assertEqual(self.tag_commit(), self.head())
        self.assertEqual(self.origin_tag(), "")
        self.assertEqual(self.gh_release_calls("create"), [])
        os.remove(hook)
        code, out = run(["release", "7.8.9"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertEqual(self.origin_tag(), self.head())
        self.assertEqual(len(self.gh_release_calls("create")), 1)

    def test_ci_started_by_the_tag_push_does_not_block_the_rerun(self):
        # review round 1 F1: a workflow `on: push` also runs for the pushed tag, on the same
        # commit; the rerun after a failed `release create` must not wait for it
        self.scenario({"release_create_exit": 1})
        code, out = run(["release", "7.8.9"])
        self.assertEqual(code, EXIT_ERROR, out)
        for state, conclusion in (("in_progress", None), ("completed", "failure")):
            with self.subTest(state=state):
                self.scenario(
                    {
                        "release_create_exit": 0,
                        "commit_checks": GREEN
                        + [{"name": "tag push", "status": state, "conclusion": conclusion}],
                        "releases": {},
                    }
                )
                code, out = run(["release", "7.8.9"])
                self.assertEqual(code, EXIT_OK, out)
                self.assertIn("CI is not checked again", out)

    def test_a_finished_release_with_ci_running_on_the_tag(self):
        url = "https://github.example/me/sample/releases/tag/v7.8.9"
        git(["tag", "-a", "v7.8.9", "-m", "made by hand", "HEAD"], self.repo)
        git(["push", "-q", "origin", "v7.8.9"], self.repo)
        self.scenario(
            {
                "releases": {"v7.8.9": url},
                "commit_checks": [{"name": "t", "status": "queued", "conclusion": None}],
            }
        )
        code, out = run(["release", "7.8.9"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn(url, out)

    def test_a_local_tag_only_still_checks_ci(self):
        # the tag never reached origin, so no push started anything: CI is checked as before
        git(["tag", "-a", "v7.8.9", "-m", "made by hand", "HEAD"], self.repo)
        self.scenario({"commit_checks": [{"name": "t", "status": "queued", "conclusion": None}]})
        code, out = run(["release", "7.8.9"])
        self.assertEqual(code, EXIT_ACTION, out)
        self.assertIn("still running", out)
        self.assertEqual(self.origin_tag(), "")


class Failures(Case):
    """Review round 1 F2: a git or gh call that cannot run or times out is one ERROR line."""

    def test_a_gh_that_cannot_be_started(self):
        os.environ["REVALI_GH_CMD"] = os.path.join(self.tmp, "no-such-gh")
        code, out = run(["release", "7.8.9"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertEqual(len(self.error_lines(out)), 1, out)
        self.assertEqual(self.local_tag(), "")

    def test_a_repository_not_found_is_not_a_missing_release(self):
        # review round 2 follow-up 3: only "release not found" means there is no Release
        self.scenario(
            {
                "release_view_exit": 1,
                "release_view_error": "HTTP 404: Not Found (https://api.github.com/repos/me/sample)",
            }
        )
        code, out = run(["release", "7.8.9"])
        self.assertEqual(code, EXIT_ERROR, out)
        errors = self.error_lines(out)
        self.assertEqual(len(errors), 1, out)
        self.assertIn("gh release view v7.8.9 failed", errors[0])
        self.assertIn("HTTP 404", errors[0])
        self.assertEqual(self.local_tag(), "")
        self.assertEqual(self.origin_tag(), "")
        self.assertEqual(self.gh_release_calls("create"), [])

    def test_a_release_create_that_times_out_after_the_push(self):
        real = release._gh

        def gh(args, root, retries=0):
            if args[:2] == ["release", "create"]:
                raise ProcTimeout("gh release create: timed out after 120 s")
            return real(args, root, retries)

        with mock.patch.object(release, "_gh", gh):
            code, out = run(["release", "7.8.9"])
        self.assertEqual(code, EXIT_ERROR, out)
        errors = self.error_lines(out)
        self.assertEqual(len(errors), 1, out)
        self.assertIn("timed out", errors[0])
        self.assertIn("run the same command again to finish", errors[0])
        self.assertEqual(self.origin_tag(), self.head())
        code, out = run(["release", "7.8.9"])  # and the rerun finishes it
        self.assertEqual(code, EXIT_OK, out)


class DryRun(Case):
    """AC-4."""

    def test_prints_the_commands_and_changes_nothing(self):
        code, out = run(["release", "7.8.9", "--notes", "two words", "--dry-run"])
        self.assertEqual(code, EXIT_OK, out)
        lines = out.splitlines()
        self.assertIn(
            'dry run: would run git tag -a v7.8.9 -m "sample 7.8.9: two words" %s' % self.head(),
            lines,
        )
        self.assertIn("dry run: would run git push origin refs/tags/v7.8.9", lines)
        self.assertIn(
            "dry run: would run gh release create v7.8.9 --verify-tag --generate-notes"
            ' --notes "two words"',
            lines,
        )
        self.assertEqual(self.local_tag(), "")
        self.assertEqual(self.origin_tag(), "")
        self.assertEqual(self.gh_release_calls("create"), [])

    def test_a_failed_check_still_refuses(self):
        self.scenario({"commit_checks": [{"name": "t", "status": "queued", "conclusion": None}]})
        self.assert_refused(["release", "7.8.9", "--dry-run"], "still running")


if __name__ == "__main__":
    unittest.main()
