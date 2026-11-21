"""`revali release <version>` (M3.29): checks, then tag, push and GitHub Release; a rerun
finishes a release that stopped half-way; `--dry-run` changes nothing.

Real git with a bare `origin`; the fake gh answers `repo view`, the commit's check runs and
`release view` / `release create` from the scenario.
"""

import os
import unittest

from revali import EXIT_ACTION, EXIT_ERROR, EXIT_OK
from tests.helpers import RepoCase, git
from tests.test_config_not_utf8 import run

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

    def local_tag(self, tag="v0.5.0"):
        return git(["tag", "-l", tag], self.repo).strip()

    def tag_commit(self, tag="v0.5.0"):
        return git(["rev-parse", "%s^{commit}" % tag], self.repo).strip()

    def origin_tag(self, tag="v0.5.0"):
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
        code, out = run(["release", "0.5.0", "--notes", "init and config file fixes"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertEqual(git(["cat-file", "-t", "refs/tags/v0.5.0"], self.repo).strip(), "tag")
        self.assertEqual(self.tag_commit(), head)
        message = git(["tag", "-l", "--format=%(contents)", "v0.5.0"], self.repo).strip()
        self.assertEqual(message, "sample 0.5.0: init and config file fixes")
        self.assertEqual(self.origin_tag(), head)
        self.assertEqual(
            self.gh_release_calls("create"),
            [
                [
                    "release",
                    "create",
                    "v0.5.0",
                    "--verify-tag",
                    "--generate-notes",
                    "--notes",
                    "init and config file fixes",
                ]
            ],
        )
        url = "https://github.example/me/sample/releases/tag/v0.5.0"
        self.assertIn("RELEASED: v0.5.0 on %s: %s" % (head[:12], url), out.splitlines())

    def test_a_v_prefix_and_no_notes(self):
        code, out = run(["release", "v0.5.0"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertEqual(self.tag_commit(), self.head())
        message = git(["tag", "-l", "--format=%(contents)", "v0.5.0"], self.repo).strip()
        self.assertEqual(message, "sample 0.5.0")
        self.assertEqual(
            self.gh_release_calls("create"),
            [["release", "create", "v0.5.0", "--verify-tag", "--generate-notes"]],
        )

    def test_a_commit_with_no_check_runs_is_released(self):
        self.scenario({"commit_checks": []})
        code, out = run(["release", "0.5.0"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("no CI checks on HEAD", out)

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
        code, out = run(["release", "0.5.0"])
        self.assertEqual(code, EXIT_OK, out)


class Refuses(Case):
    """AC-2: exit 2, one ERROR line, no tag here or on origin, no Release."""

    def test_a_version_that_is_not_x_y_z(self):
        for version in ("0.5", "1.2.3-rc1", "va.b.c", "0.5.0.1"):
            with self.subTest(version=version):
                self.assert_refused(["release", version], "X.Y.Z")

    def test_not_on_the_base_branch(self):
        git(["checkout", "-q", "-b", "topic"], self.repo)
        self.assert_refused(["release", "0.5.0"], "not the base branch main")

    def test_the_base_branch_from_github_when_the_config_leaves_it_empty(self):
        toml = self.read("revali.toml")
        self.write("revali.toml", toml.replace('base_branch = "main"', 'base_branch = ""'))
        self.commit_all("no base in config")
        git(["push", "-q", "origin", "main"], self.repo)
        self.scenario({"default_branch": "trunk"})
        self.assert_refused(["release", "0.5.0"], "not the base branch trunk")

    def test_a_dirty_working_tree(self):
        self.write("notes.txt", "draft\n")
        out = self.assert_refused(["release", "0.5.0"], "not clean")
        self.assertIn("notes.txt", out)

    def test_head_not_pushed(self):
        self.write("more.txt", "x\n")
        self.commit_all("not pushed")
        self.assert_refused(["release", "0.5.0"], "is not origin/main")

    def test_head_behind_origin(self):
        self.write("more.txt", "x\n")
        self.commit_all("pushed, then dropped here")
        git(["push", "-q", "origin", "main"], self.repo)
        git(["reset", "-q", "--hard", "HEAD~1"], self.repo)
        self.assert_refused(["release", "0.5.0"], "is not origin/main")

    def test_no_release_yml_in_head(self):
        git(["rm", "-q", ".github/release.yml"], self.repo)
        git(["commit", "-q", "-m", "drop it"], self.repo)
        git(["push", "-q", "origin", "main"], self.repo)
        self.assert_refused(["release", "0.5.0"], ".github/release.yml is not in HEAD")

    def test_a_local_tag_on_another_commit(self):
        git(["tag", "-a", "v0.5.0", "-m", "old", "HEAD~1"], self.repo)
        code, out = run(["release", "0.5.0"])
        self.assertEqual(code, EXIT_ACTION, out)
        self.assertIn("tag v0.5.0 already exists on", self.error_lines(out)[0])
        self.assertEqual(self.origin_tag(), "")
        self.assertEqual(self.gh_release_calls("create"), [])

    def test_a_tag_on_origin_on_another_commit(self):
        old = git(["rev-parse", "HEAD~1"], self.repo).strip()
        git(["tag", "-a", "v0.5.0", "-m", "old", old], self.repo)
        git(["push", "-q", "origin", "v0.5.0"], self.repo)
        git(["tag", "-d", "v0.5.0"], self.repo)
        code, out = run(["release", "0.5.0"])
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
        self.assert_refused(["release", "0.5.0"], "CI checks did not pass on HEAD: lint")

    def test_ci_still_running_is_refused_without_waiting(self):
        self.scenario(
            {
                "commit_checks": GREEN
                + [{"name": "macos", "status": "in_progress", "conclusion": None}]
            }
        )
        self.assert_refused(["release", "0.5.0"], "still running on HEAD: macos")
        checks = [c for c in self.fake_calls("gh") if "/check-runs" in " ".join(c["argv"])]
        self.assertEqual(len(checks), 1)


class Resumes(Case):
    """AC-3: a rerun keeps a tag already on HEAD and runs only the missing steps."""

    def test_a_local_tag_not_pushed_yet(self):
        git(["tag", "-a", "v0.5.0", "-m", "made by hand", "HEAD"], self.repo)
        tag_object = git(["rev-parse", "refs/tags/v0.5.0"], self.repo).strip()
        code, out = run(["release", "0.5.0"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertEqual(git(["rev-parse", "refs/tags/v0.5.0"], self.repo).strip(), tag_object)
        self.assertEqual(self.origin_tag(), self.head())
        self.assertEqual(len(self.gh_release_calls("create")), 1)
        self.assertNotIn("tagged v0.5.0", out)

    def test_a_tag_on_origin_and_no_release(self):
        git(["tag", "-a", "v0.5.0", "-m", "made by hand", "HEAD"], self.repo)
        git(["push", "-q", "origin", "v0.5.0"], self.repo)
        git(["tag", "-d", "v0.5.0"], self.repo)
        code, out = run(["release", "0.5.0"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertNotIn("tagged v0.5.0", out)
        self.assertNotIn("pushed v0.5.0", out)
        self.assertEqual(len(self.gh_release_calls("create")), 1)

    def test_everything_done_already(self):
        url = "https://github.example/me/sample/releases/tag/v0.5.0"
        git(["tag", "-a", "v0.5.0", "-m", "made by hand", "HEAD"], self.repo)
        git(["push", "-q", "origin", "v0.5.0"], self.repo)
        self.scenario({"releases": {"v0.5.0": url}})
        code, out = run(["release", "0.5.0"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn(url, out)
        self.assertEqual(self.gh_release_calls("create"), [])

    def test_a_failed_release_create_finishes_on_the_rerun(self):
        self.scenario({"release_create_exit": 1})
        code, out = run(["release", "0.5.0"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertIn("run the same command again", out)
        self.assertEqual(self.origin_tag(), self.head())
        tag_object = git(["rev-parse", "refs/tags/v0.5.0"], self.repo).strip()
        self.scenario({"release_create_exit": 0})
        code, out = run(["release", "0.5.0"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertEqual(git(["rev-parse", "refs/tags/v0.5.0"], self.repo).strip(), tag_object)
        self.assertEqual(len(self.gh_release_calls("create")), 2)

    def test_a_failed_push_finishes_on_the_rerun(self):
        hook = os.path.join(self.info["remote"], "hooks", "pre-receive")
        with open(hook, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("#!/bin/sh\necho refused by the test >&2\nexit 1\n")
        os.chmod(hook, 0o755)
        code, out = run(["release", "0.5.0"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertIn("git push origin v0.5.0 failed", out)
        self.assertEqual(self.tag_commit(), self.head())
        self.assertEqual(self.origin_tag(), "")
        self.assertEqual(self.gh_release_calls("create"), [])
        os.remove(hook)
        code, out = run(["release", "0.5.0"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertEqual(self.origin_tag(), self.head())
        self.assertEqual(len(self.gh_release_calls("create")), 1)


class DryRun(Case):
    """AC-4."""

    def test_prints_the_commands_and_changes_nothing(self):
        code, out = run(["release", "0.5.0", "--notes", "two words", "--dry-run"])
        self.assertEqual(code, EXIT_OK, out)
        lines = out.splitlines()
        self.assertIn(
            'dry run: would run git tag -a v0.5.0 -m "sample 0.5.0: two words" %s' % self.head(),
            lines,
        )
        self.assertIn("dry run: would run git push origin refs/tags/v0.5.0", lines)
        self.assertIn(
            "dry run: would run gh release create v0.5.0 --verify-tag --generate-notes"
            ' --notes "two words"',
            lines,
        )
        self.assertEqual(self.local_tag(), "")
        self.assertEqual(self.origin_tag(), "")
        self.assertEqual(self.gh_release_calls("create"), [])

    def test_a_failed_check_still_refuses(self):
        self.scenario({"commit_checks": [{"name": "t", "status": "queued", "conclusion": None}]})
        self.assert_refused(["release", "0.5.0", "--dry-run"], "still running")


if __name__ == "__main__":
    unittest.main()
