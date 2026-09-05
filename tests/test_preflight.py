import os
import unittest

from revali import EXIT_ACTION, EXIT_ERROR, EXIT_OK
from revali.preflight import Stop, preflight
from tests.fixtures.make_sample_repo import PY
from tests.helpers import RepoCase, git, run_cli


class _ListLog:
    """Collects preflight stage lines; detail lines are dropped."""

    def __init__(self, lines):
        self.lines = lines

    def stage(self, stage, msg):
        self.lines.append("%s: %s" % (stage, msg))

    def detail(self, msg):
        pass


class PreflightHappyPath(RepoCase):
    def test_passes_on_fixture(self):
        ctx = preflight(self.repo)
        self.assertEqual(ctx.branch, "feature/mul")
        self.assertEqual(ctx.base, "main")
        self.assertEqual(ctx.base_ref, "origin/main")
        self.assertEqual(ctx.doc.kind, "feature")
        self.assertEqual(ctx.changed_files, ["src/calc.py"])
        self.assertGreater(ctx.diff_lines, 0)
        self.assertTrue(ctx.head_sha and ctx.base_sha and ctx.head_sha != ctx.base_sha)
        calls = [c["argv"][:2] for c in self.fake_calls()]
        self.assertIn(["auth", "status"], calls)
        self.assertIn(["repo", "view"], calls)

    def test_cli_preflight(self):
        code, out = run_cli(["preflight"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("preflight OK", out)

    def test_base_from_gh_default_when_unset(self):
        self.write(
            "revali.toml",
            self.read("revali.toml").replace('base_branch = "main"', 'base_branch = ""'),
        )
        self.commit_all("config")
        ctx = preflight(self.repo)
        self.assertEqual(ctx.base, "main")

    def test_excluded_files_not_counted(self):
        self.write("poetry.lock", "x\n" * 5000)
        self.commit_all("lock")
        ctx = preflight(self.repo)
        self.assertIn("poetry.lock", ctx.excluded_files)
        self.assertNotIn("poetry.lock", ctx.changed_files)


class PreflightFailures(RepoCase):
    def assert_stop(self, code, needle, **kw):
        with self.assertRaises(Stop) as cm:
            preflight(self.repo, **kw)
        self.assertEqual(cm.exception.exit_code, code, cm.exception.message)
        self.assertIn(needle, cm.exception.message)
        return cm.exception

    def test_disabled(self):
        os.environ["REVALI_DISABLE"] = "1"
        self.assert_stop(EXIT_ERROR, "REVALI_DISABLE")

    def test_not_a_repo(self):
        with self.assertRaises(Stop) as cm:
            preflight(self.tmp)
        self.assertIn("not inside a git repository", cm.exception.message)

    def test_missing_config_and_change_reported_together(self):
        os.remove(os.path.join(self.repo, "revali.toml"))
        os.remove(self.change_md())
        git(["commit", "-q", "-am", "drop config"], self.repo)
        stop = self.assert_stop(EXIT_ERROR, "revali.toml not found")
        self.assertIn("change.md not found", stop.message)

    def test_invalid_change_md(self):
        text = self.read(".revali/feature__mul/change.md").replace("kind: feature", "kind: hotfix")
        self.write(".revali/feature__mul/change.md", text)
        self.assert_stop(EXIT_ERROR, "not available in this version")

    def test_dirty_tree(self):
        self.write("src/calc.py", "# dirty\n")
        self.assert_stop(EXIT_ERROR, "not clean")

    def test_untracked_file_is_dirty(self):
        self.write("notes.txt", "x\n")
        self.assert_stop(EXIT_ERROR, "not clean")

    def test_on_base_branch(self):
        git(["checkout", "-q", "main"], self.repo)
        self.write(".revali/main/change.md", self.read(".revali/feature__mul/change.md"))
        self.assert_stop(EXIT_ERROR, "reviews a feature branch")

    def test_gh_not_logged_in(self):
        self.scenario({"auth_exit": 1})
        self.assert_stop(EXIT_ERROR, "not logged in")

    def test_owner_mismatch(self):
        self.scenario({"owner": "someone-else"})
        self.assert_stop(EXIT_ERROR, "only runs on your own repos")

    def test_public_repo_allowed_with_a_note(self):
        self.scenario({"visibility": "PUBLIC"})
        lines = []
        ctx = preflight(self.repo, log=_ListLog(lines))
        self.assertEqual(ctx.repo.visibility, "PUBLIC")
        self.assertTrue(
            any(
                "public repository: PR comments will carry summaries only" in line for line in lines
            ),
            lines,
        )

    def test_stale_base(self):
        # Advance origin/main behind the branch's back.
        git(["checkout", "-q", "main"], self.repo)
        self.write("README.md", "# sample\n\nmoved on\n")
        git(["commit", "-q", "-am", "main moves"], self.repo)
        git(["push", "-q", "origin", "main"], self.repo)
        git(["reset", "-q", "--hard", "HEAD~1"], self.repo)
        git(["checkout", "-q", "feature/mul"], self.repo)
        self.assert_stop(EXIT_ACTION, "rebase")

    def test_no_commits_on_branch(self):
        git(["checkout", "-q", "-b", "feature/empty", "main"], self.repo)
        self.write(".revali/feature__empty/change.md", self.read(".revali/feature__mul/change.md"))
        self.assert_stop(EXIT_ERROR, "nothing to review")

    def test_diff_too_large(self):
        self.write(
            "revali.toml",
            self.read("revali.toml").replace("max_diff_lines = 800", "max_diff_lines = 3"),
        )
        self.commit_all("tiny limit")
        self.assert_stop(EXIT_ACTION, "split the change")

    def test_secret_blocks(self):
        self.write("src/secrets.py", 'AWS = "AKIAIOSFODNN7EXAMPLE"\n')
        self.commit_all("oops")
        stop = self.assert_stop(EXIT_ERROR, "credentials")
        self.assertIn("revoke", stop.message)
        self.assertNotIn("AKIAIOSFODNN7EXAMPLE", stop.message)

    def test_secret_allow_marker(self):
        self.write("src/secrets.py", 'AWS = "AKIAIOSFODNN7EXAMPLE"  # revali:allow-secret\n')
        self.commit_all("documented example")
        preflight(self.repo)

    def test_lint_failure(self):
        toml_line = 'lint = "%s -c \\"import sys; print(\'style: bad\'); sys.exit(3)\\""' % PY
        self.write("revali.toml", self.read("revali.toml").replace('lint = ""', toml_line))
        self.commit_all("lint")
        stop = self.assert_stop(EXIT_ACTION, "lint failed")
        self.assertIn("style: bad", stop.message)

    def test_lint_success(self):
        self.write(
            "revali.toml", self.read("revali.toml").replace('lint = ""', 'lint = "%s -c pass"' % PY)
        )
        self.commit_all("lint")
        preflight(self.repo)

    def test_base_override(self):
        git(["branch", "-q", "release", "main"], self.repo)
        git(["push", "-q", "origin", "release"], self.repo)
        ctx = preflight(self.repo, base_override="release")
        self.assertEqual(ctx.base_ref, "origin/release")


class PreflightWithoutRemote(RepoCase):
    with_remote = False

    def test_compares_against_local_base(self):
        ctx = preflight(self.repo)
        self.assertEqual(ctx.base_ref, "main")


if __name__ == "__main__":
    unittest.main()
