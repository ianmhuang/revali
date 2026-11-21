"""Reviewer acceptance tests for `revali release <version>` (feature/release).

Black box: `revali.cli.main` in-process on a RepoCase repository checked out on `main` with a
bare `origin` (real git) and the fake gh, which answers the commit's check runs and
`release view` / `release create` from the scenario.

AC-1: every check passes -> annotated tag on HEAD, pushed, `gh release create`, exit 0.
AC-2: a failed check -> exit 2, one ERROR line, nothing created or pushed.
AC-3: a tag already on HEAD (here or on origin) is kept and only the missing steps run.
AC-4: `--dry-run` checks, prints the commands, changes nothing.

Round 2: a rerun whose tag is already on origin on HEAD finishes whatever the check runs say
by then (the tag push may have started new ones); a tag that never left this repository still
needs green CI; a gh that cannot be started is one ERROR line and exit 1.

Round 3: a `gh release view` that fails for another reason than a missing Release does not
say the Release is absent: exit 1, one ERROR line, nothing made, pushed or published.
"""

import contextlib
import io
import os
import unittest

from tests.helpers import RepoCase, git

TAG = "v1.2.3"
REF = "refs/tags/" + TAG
URL = "https://github.example/me/sample/releases/tag/" + TAG
PASSED = [
    {"name": "unit", "status": "completed", "conclusion": "success"},
    {"name": "lint", "status": "completed", "conclusion": "success"},
]
RELEASE_YML = "changelog:\n  categories:\n    - title: Fixes\n      labels: [patch]\n"


def run(argv):
    """(exit code, stdout + stderr) of the command line run in-process."""
    from revali.cli import main

    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        try:
            code = main(argv)
        except SystemExit as exc:
            code = int(exc.code or 0)
    return code, out.getvalue()


class ReleaseCase(RepoCase):
    """On `main`, pushed to origin, `.github/release.yml` committed, CI green."""

    with_branch = False

    def setUp(self):
        super().setUp()
        self.write(".github/release.yml", RELEASE_YML)
        self.commit_all("release notes groups")
        self.push("main")
        self.scenario({"commit_checks": PASSED})

    def push(self, ref):
        git(["push", "-q", "origin", ref], self.repo)

    def head(self):
        return git(["rev-parse", "HEAD"], self.repo).strip()

    def has_local_tag(self):
        return git(["tag", "-l", TAG], self.repo).strip() == TAG

    def local_tag_object(self):
        return git(["rev-parse", REF], self.repo).strip()

    def local_tag_commit(self):
        return git(["rev-parse", REF + "^{commit}"], self.repo).strip()

    def origin_tags(self):
        """ref name -> object id of every tag ref origin lists (peeled `^{}` lines too)."""
        out = git(["ls-remote", "--tags", "origin"], self.repo)
        refs = {}
        for line in out.splitlines():
            parts = line.split()
            if len(parts) == 2:
                refs[parts[1]] = parts[0]
        return refs

    def origin_tag_commit(self):
        refs = self.origin_tags()
        return refs.get(REF + "^{}") or refs.get(REF, "")

    def creates(self):
        return [c["argv"] for c in self.fake_calls("gh") if c["argv"][:2] == ["release", "create"]]

    def tag_by_hand(self, commit="HEAD", push=False, keep_local=True):
        git(["tag", "-a", TAG, "-m", "made by hand", commit], self.repo)
        if push:
            self.push(REF)
        if not keep_local:
            git(["tag", "-d", TAG], self.repo)

    def assert_released(self, argv):
        code, out = run(argv)
        self.assertEqual(code, 0, out)
        self.assertNotIn("Traceback", out)
        return out

    def assert_refused(self, argv, says=""):
        """Exit 2, one ERROR line (naming `says`), no Release created."""
        code, out = run(argv)
        self.assertEqual(code, 2, out)
        self.assertNotIn("Traceback", out)
        errors = [ln for ln in out.splitlines() if ln.startswith("ERROR")]
        self.assertEqual(len(errors), 1, out)
        if says:
            self.assertIn(says, errors[0])
        self.assertEqual(self.creates(), [], out)
        return out

    def assert_nothing_made(self):
        self.assertFalse(self.has_local_tag())
        self.assertEqual(self.origin_tags(), {})
        self.assertEqual(self.creates(), [])


class TagsPushesAndPublishes(ReleaseCase):
    """AC-1."""

    def test_annotated_tag_on_head_pushed_and_released_with_the_notes(self):
        head = self.head()
        out = self.assert_released(["release", "1.2.3", "--notes", "faster cold start"])
        # an annotated tag (a tag object), on HEAD
        self.assertEqual(git(["cat-file", "-t", REF], self.repo).strip(), "tag")
        self.assertEqual(self.local_tag_commit(), head)
        # the same tag object is on origin, and peels to HEAD there
        refs = self.origin_tags()
        self.assertEqual(refs.get(REF), self.local_tag_object())
        self.assertEqual(refs.get(REF + "^{}"), head)
        # one `gh release create v1.2.3 --verify-tag --generate-notes --notes <text>`
        creates = self.creates()
        self.assertEqual(len(creates), 1, creates)
        argv = creates[0]
        self.assertEqual(argv[:3], ["release", "create", TAG])
        self.assertIn("--verify-tag", argv)
        self.assertIn("--generate-notes", argv)
        self.assertIn("--notes", argv)
        self.assertEqual(argv[argv.index("--notes") + 1], "faster cold start")
        # the tagged commit and the Release URL are printed
        self.assertIn(head[:7], out)
        self.assertIn(URL, out)

    def test_a_leading_v_gives_the_same_tag_and_no_notes_flag_without_notes(self):
        head = self.head()
        out = self.assert_released(["release", "v1.2.3"])
        self.assertEqual(git(["tag", "-l"], self.repo).split(), [TAG])
        self.assertEqual(self.local_tag_commit(), head)
        self.assertEqual(self.origin_tag_commit(), head)
        creates = self.creates()
        self.assertEqual(len(creates), 1, creates)
        self.assertEqual(creates[0][:3], ["release", "create", TAG])
        self.assertIn("--verify-tag", creates[0])
        self.assertIn("--generate-notes", creates[0])
        self.assertNotIn("--notes", creates[0])
        self.assertIn(URL, out)

    def test_the_configured_base_branch_wins_over_the_default_branch(self):
        # GitHub's default branch stays `main`; revali.toml names `trunk`
        toml = self.read("revali.toml")
        self.assertIn('base_branch = "main"', toml)
        self.write("revali.toml", toml.replace('base_branch = "main"', 'base_branch = "trunk"'))
        self.commit_all("base is trunk")
        self.push("main")
        self.assert_refused(["release", "1.2.3"], "trunk")
        self.assert_nothing_made()
        git(["checkout", "-q", "-b", "trunk"], self.repo)
        self.push("trunk")
        self.assert_released(["release", "1.2.3"])
        self.assertEqual(self.origin_tag_commit(), self.head())

    def test_an_empty_base_branch_takes_the_default_branch(self):
        toml = self.read("revali.toml")
        self.write("revali.toml", toml.replace('base_branch = "main"', 'base_branch = ""'))
        self.commit_all("no base in the config")
        self.push("main")
        self.scenario({"default_branch": "develop"})
        self.assert_refused(["release", "1.2.3"], "develop")
        self.assert_nothing_made()
        self.scenario({"default_branch": "main"})
        self.assert_released(["release", "1.2.3"])
        self.assertEqual(self.origin_tag_commit(), self.head())


class RefusesAndCreatesNothing(ReleaseCase):
    """AC-2."""

    def test_a_version_that_is_not_x_y_z(self):
        for version in ("1.2", "1.2.3.4", "1.x.3", "1.2.3-rc1", "v1.2", "one.two.three"):
            with self.subTest(version=version):
                self.assert_refused(["release", version])
                self.assertEqual(git(["tag", "-l"], self.repo).strip(), "")
                self.assertEqual(self.origin_tags(), {})

    def test_a_version_whose_parts_are_not_plain_numbers(self):
        # digits of another script, and numbers with a leading zero, are not X.Y.Z
        full_width = ".".join(chr(0xFF10 + d) for d in (1, 2, 3))
        arabic_indic = ".".join(chr(0x0660 + d) for d in (1, 2, 3))
        for version in (full_width, arabic_indic, "01.2.3", "1.02.3", "1.2.03"):
            with self.subTest(version=ascii(version)):
                self.assert_refused(["release", version])
                self.assertEqual(git(["tag", "-l"], self.repo).strip(), "")
                self.assertEqual(self.origin_tags(), {})

    def test_zero_is_a_plain_number(self):
        self.assert_released(["release", "0.10.0"])
        self.assertEqual(git(["tag", "-l"], self.repo).split(), ["v0.10.0"])

    def test_the_tag_here_on_something_that_is_not_a_commit(self):
        tree = git(["rev-parse", "HEAD^{tree}"], self.repo).strip()
        git(["tag", TAG, tree], self.repo)
        self.assert_refused(["release", "1.2.3"], TAG)
        self.assertEqual(self.local_tag_object(), tree)  # not moved
        self.assertEqual(self.origin_tags(), {})  # not pushed

    def test_another_branch_checked_out(self):
        git(["checkout", "-q", "-b", "topic"], self.repo)
        self.assert_refused(["release", "1.2.3"], "main")
        self.assert_nothing_made()

    def test_a_detached_head_on_the_base_tip(self):
        git(["checkout", "-q", "--detach"], self.repo)
        self.assert_refused(["release", "1.2.3"], "main")
        self.assert_nothing_made()

    def test_an_untracked_file(self):
        self.write("scratch.txt", "x\n")
        self.assert_refused(["release", "1.2.3"])
        self.assert_nothing_made()

    def test_a_modified_tracked_file(self):
        self.write("README.md", "# sample\n\nedited, not committed\n")
        self.assert_refused(["release", "1.2.3"])
        self.assert_nothing_made()

    def test_a_commit_that_is_not_pushed(self):
        self.write("more.txt", "x\n")
        self.commit_all("local only")
        self.assert_refused(["release", "1.2.3"])
        self.assert_nothing_made()

    def test_origin_moved_on_since_the_last_fetch(self):
        # the local origin/main still says HEAD is the tip; only a fetch shows it is not
        self.write("more.txt", "x\n")
        self.commit_all("pushed by someone else")
        self.push("main")
        git(["reset", "-q", "--hard", "HEAD~1"], self.repo)
        git(["update-ref", "refs/remotes/origin/main", "HEAD"], self.repo)
        self.assertEqual(self.head(), git(["rev-parse", "origin/main"], self.repo).strip())
        self.assert_refused(["release", "1.2.3"])
        self.assert_nothing_made()

    def test_release_yml_not_in_head(self):
        git(["rm", "-q", ".github/release.yml"], self.repo)
        git(["commit", "-q", "-m", "drop the notes groups"], self.repo)
        self.push("main")
        self.assert_refused(["release", "1.2.3"], "release.yml")
        self.assert_nothing_made()

    def test_the_tag_here_on_another_commit(self):
        old = git(["rev-parse", "HEAD~1"], self.repo).strip()
        self.tag_by_hand(old)
        self.assert_refused(["release", "1.2.3"], TAG)
        self.assertEqual(self.local_tag_commit(), old)  # not moved
        self.assertEqual(self.origin_tags(), {})  # not pushed

    def test_the_tag_on_origin_on_another_commit(self):
        old = git(["rev-parse", "HEAD~1"], self.repo).strip()
        self.tag_by_hand(old, push=True, keep_local=False)
        before = self.origin_tags()
        self.assert_refused(["release", "1.2.3"], TAG)
        self.assertEqual(self.origin_tags(), before)
        self.assertEqual(self.origin_tag_commit(), old)

    def test_a_check_that_did_not_pass(self):
        for conclusion in ("failure", "cancelled", "timed_out", "action_required"):
            with self.subTest(conclusion=conclusion):
                bad = {"name": "windows-suite", "status": "completed", "conclusion": conclusion}
                self.scenario({"commit_checks": PASSED + [bad]})
                self.assert_refused(["release", "1.2.3"], "windows-suite")
                self.assert_nothing_made()

    def test_a_check_still_running_is_refused_not_waited_for(self):
        for status in ("in_progress", "queued"):
            with self.subTest(status=status):
                running = {"name": "macos-suite", "status": status, "conclusion": None}
                self.scenario({"commit_checks": PASSED + [running]})
                self.assert_refused(["release", "1.2.3"], "macos-suite")
                self.assert_nothing_made()
        # two refusals, each from what GitHub said at that moment: no polling
        queries = [c for c in self.fake_calls("gh") if "check-runs" in " ".join(c["argv"])]
        self.assertLessEqual(len(queries), 2, queries)


class FinishesAHalfDoneRelease(ReleaseCase):
    """AC-3."""

    def test_a_tag_here_on_head_is_kept_pushed_and_released(self):
        self.tag_by_hand()
        tag_object = self.local_tag_object()
        self.assert_released(["release", "1.2.3"])
        self.assertEqual(self.local_tag_object(), tag_object)  # not made again
        refs = self.origin_tags()
        self.assertEqual(refs.get(REF), tag_object)
        self.assertEqual(refs.get(REF + "^{}"), self.head())
        self.assertEqual(len(self.creates()), 1)

    def test_a_tag_only_on_origin_is_left_alone_and_released(self):
        self.tag_by_hand(push=True, keep_local=False)
        before = self.origin_tags()
        out = self.assert_released(["release", "1.2.3"])
        self.assertEqual(self.origin_tags(), before)
        self.assertEqual(len(self.creates()), 1)
        self.assertIn(URL, out)

    def test_a_tag_here_and_on_origin_needs_only_the_release(self):
        self.tag_by_hand(push=True)
        tag_object = self.local_tag_object()
        before = self.origin_tags()
        out = self.assert_released(["release", "1.2.3", "--notes", "second try"])
        self.assertEqual(self.local_tag_object(), tag_object)
        self.assertEqual(self.origin_tags(), before)
        creates = self.creates()
        self.assertEqual(len(creates), 1, creates)
        self.assertEqual(creates[0][:3], ["release", "create", TAG])
        self.assertEqual(creates[0][creates[0].index("--notes") + 1], "second try")
        self.assertIn(URL, out)

    def test_with_the_release_there_too_it_prints_the_url_and_exits_0(self):
        self.tag_by_hand(push=True)
        tag_object = self.local_tag_object()
        before = self.origin_tags()
        self.scenario({"releases": {TAG: URL}})
        out = self.assert_released(["release", "1.2.3"])
        self.assertIn(URL, out)
        self.assertEqual(self.creates(), [])
        self.assertEqual(self.local_tag_object(), tag_object)
        self.assertEqual(self.origin_tags(), before)

    def test_a_release_that_failed_to_publish_finishes_on_the_rerun(self):
        head = self.head()
        self.scenario({"release_create_exit": 1})
        code, out = run(["release", "1.2.3"])
        self.assertEqual(code, 1, out)
        self.assertNotIn("Traceback", out)
        # stopped half-way: the tag is made and pushed, the Release is not there
        tag_object = self.local_tag_object()
        self.assertEqual(self.origin_tag_commit(), head)
        self.scenario({"release_create_exit": 0})
        out = self.assert_released(["release", "1.2.3"])
        self.assertEqual(self.local_tag_object(), tag_object)
        self.assertEqual(self.origin_tags().get(REF), tag_object)
        self.assertEqual(len(self.creates()), 2)  # the failed one and the rerun's
        self.assertIn(URL, out)

    def test_check_runs_started_by_the_tag_push_do_not_stop_the_rerun(self):
        # the tag is on origin on HEAD: what is missing is the Release, whatever the check
        # runs on that commit say by now
        later = (
            {"name": "tag-suite", "status": "in_progress", "conclusion": None},
            {"name": "tag-suite", "status": "queued", "conclusion": None},
            {"name": "tag-suite", "status": "completed", "conclusion": "failure"},
        )
        self.scenario({"release_create_exit": 1})
        code, out = run(["release", "1.2.3"])
        self.assertEqual(code, 1, out)
        tag_object = self.local_tag_object()
        before = self.origin_tags()
        self.assertEqual(before.get(REF), tag_object)
        self.scenario({"release_create_exit": 0})
        for count, check in enumerate(later, start=2):
            with self.subTest(status=check["status"], conclusion=check["conclusion"]):
                self.scenario({"commit_checks": PASSED + [check]})
                out = self.assert_released(["release", "1.2.3"])
                self.assertIn(URL, out)
                self.assertEqual(len(self.creates()), count)
                self.assertEqual(self.local_tag_object(), tag_object)
                self.assertEqual(self.origin_tags(), before)

    def test_a_published_release_prints_its_url_whatever_the_checks_say_now(self):
        self.tag_by_hand(push=True)
        before = self.origin_tags()
        running = {"name": "tag-suite", "status": "in_progress", "conclusion": None}
        failed = {"name": "tag-suite", "status": "completed", "conclusion": "failure"}
        for check in (running, failed):
            with self.subTest(status=check["status"]):
                self.scenario({"releases": {TAG: URL}, "commit_checks": PASSED + [check]})
                out = self.assert_released(["release", "1.2.3"])
                self.assertIn(URL, out)
                self.assertEqual(self.creates(), [])
                self.assertEqual(self.origin_tags(), before)

    def test_a_tag_that_never_left_this_repository_still_needs_green_ci(self):
        # nothing was pushed, so no push started a run: AC-2's refusal stands
        self.tag_by_hand()
        tag_object = self.local_tag_object()
        for check in (
            {"name": "macos-suite", "status": "in_progress", "conclusion": None},
            {"name": "macos-suite", "status": "completed", "conclusion": "failure"},
        ):
            with self.subTest(status=check["status"]):
                self.scenario({"commit_checks": PASSED + [check]})
                self.assert_refused(["release", "1.2.3"], "macos-suite")
                self.assertEqual(self.local_tag_object(), tag_object)
                self.assertEqual(self.origin_tags(), {})


class StopsOnACommandThatCannotRun(ReleaseCase):
    """A gh that cannot be started: exit 1 and one ERROR line, not a traceback (an exception
    that escaped `main` would end this in-process run as a test error)."""

    def test_a_gh_that_cannot_be_started(self):
        os.environ["REVALI_GH_CMD"] = '"%s"' % os.path.join(self.tmp, "missing", "gh")
        code, out = run(["release", "1.2.3"])
        self.assertEqual(code, 1, out)
        self.assertNotIn("Traceback", out)
        errors = [ln for ln in out.splitlines() if ln.startswith("ERROR")]
        self.assertEqual(len(errors), 1, out)
        self.assertFalse(self.has_local_tag())
        self.assertEqual(self.origin_tags(), {})


class StopsWhenItCannotTellWhetherTheReleaseExists(ReleaseCase):
    """AC-3 creates the Release "if it does not exist": a `gh release view` that fails for
    another reason (the repository not found, a server error) has not said so."""

    FAILURES = (
        "HTTP 404: Not Found (https://api.github.com/repos/me/sample)",
        "HTTP 502: Bad Gateway (https://api.github.com/repos/me/sample/releases/tags/v1.2.3)",
    )

    def assert_stopped(self, argv):
        code, out = run(argv)
        self.assertEqual(code, 1, out)
        self.assertNotIn("Traceback", out)
        errors = [ln for ln in out.splitlines() if ln.startswith("ERROR")]
        self.assertEqual(len(errors), 1, out)
        self.assertEqual(self.creates(), [], out)
        return errors[0]

    def test_nothing_is_made_pushed_or_published(self):
        for text in self.FAILURES:
            with self.subTest(failure=text[:8]):
                self.scenario({"release_view_exit": 1, "release_view_error": text})
                error = self.assert_stopped(["release", "1.2.3"])
                self.assertIn(text[:8], error)  # the line carries what gh said
                self.assert_nothing_made()

    def test_a_half_done_release_is_not_published_blind(self):
        self.tag_by_hand(push=True)
        tag_object = self.local_tag_object()
        before = self.origin_tags()
        self.scenario({"release_view_exit": 1, "release_view_error": self.FAILURES[0]})
        self.assert_stopped(["release", "1.2.3"])
        self.assertEqual(self.local_tag_object(), tag_object)
        self.assertEqual(self.origin_tags(), before)
        # once gh answers again (no Release for the tag), the same command finishes it
        self.scenario({"release_view_exit": 0, "release_view_error": None})
        out = self.assert_released(["release", "1.2.3"])
        self.assertIn(URL, out)
        self.assertEqual(len(self.creates()), 1)
        self.assertEqual(self.origin_tags(), before)

    def test_the_dry_run_stops_the_same_way(self):
        self.scenario({"release_view_exit": 1, "release_view_error": self.FAILURES[0]})
        self.assert_stopped(["release", "1.2.3", "--dry-run"])
        self.assert_nothing_made()


class DryRun(ReleaseCase):
    """AC-4."""

    def test_prints_the_three_commands_and_changes_nothing(self):
        code, out = run(["release", "1.2.3", "--notes", "faster cold start", "--dry-run"])
        self.assertEqual(code, 0, out)
        self.assertIn("git tag", out)
        self.assertIn("git push", out)
        self.assertIn("gh release create", out)
        self.assertIn(TAG, out)
        self.assertIn("--verify-tag", out)
        self.assertIn("--generate-notes", out)
        self.assertIn("faster cold start", out)
        self.assert_nothing_made()

    def test_runs_the_checks(self):
        self.write("more.txt", "x\n")
        self.commit_all("local only")
        self.assert_refused(["release", "1.2.3", "--dry-run"])
        self.assert_nothing_made()
        git(["reset", "-q", "--hard", "HEAD~1"], self.repo)
        bad = {"name": "windows-suite", "status": "completed", "conclusion": "failure"}
        self.scenario({"commit_checks": [bad]})
        self.assert_refused(["release", "1.2.3", "--dry-run"], "windows-suite")
        self.assert_nothing_made()

    def test_a_half_done_release_is_left_as_it_is(self):
        self.tag_by_hand(push=True)
        tag_object = self.local_tag_object()
        before = self.origin_tags()
        code, out = run(["release", "1.2.3", "--dry-run"])
        self.assertEqual(code, 0, out)
        self.assertIn("gh release create", out)
        self.assertEqual(self.creates(), [])
        self.assertEqual(self.local_tag_object(), tag_object)
        self.assertEqual(self.origin_tags(), before)


if __name__ == "__main__":
    unittest.main()
