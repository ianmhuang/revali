"""AC-2: a run that stops in preflight records `repo` as owner/name from the
origin URL, `stats` groups it under that repo, and a non-hosted origin still
records "". Black-box through the CLI: the history file and `stats` output are
the interface, the origin URL is the input. Every run here stops on a dirty
tree, which is checked before any fetch, so no network is touched.
"""

import unittest

from revali import EXIT_ERROR, EXIT_OK
from revali.config import history_path, load_user_config
from revali.state import read_history
from tests.helpers import RepoCase, claude_entry, git, run_cli


class _PreflightStop(RepoCase):
    def set_origin(self, url):
        git(["remote", "set-url", "origin", url], self.repo)

    def stop_in_preflight(self):
        self.write("src/calc.py", "# uncommitted edit\n")
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertIn("working tree is not clean", out)
        return out

    def last_repo(self):
        rows = read_history(history_path(load_user_config()))
        self.assertTrue(rows, "no history row was written")
        return rows[-1]["repo"]


class HostedOrigins(_PreflightStop):
    def test_https_with_dot_git(self):
        self.set_origin("https://github.com/me/sample.git")
        self.stop_in_preflight()
        self.assertEqual(self.last_repo(), "me/sample")

    def test_https_without_dot_git_and_trailing_slash(self):
        self.set_origin("https://github.com/me/sample/")
        self.stop_in_preflight()
        self.assertEqual(self.last_repo(), "me/sample")

    def test_https_with_credentials_in_url(self):
        # user:token@host must not be mistaken for the path
        self.set_origin("https://someone:placeholder@github.com/me/sample.git")
        self.stop_in_preflight()
        self.assertEqual(self.last_repo(), "me/sample")

    def test_ssh_scheme(self):
        self.set_origin("ssh://git@github.com/me/sample.git")
        self.stop_in_preflight()
        self.assertEqual(self.last_repo(), "me/sample")

    def test_scp_style(self):
        self.set_origin("git@github.com:me/sample.git")
        self.stop_in_preflight()
        self.assertEqual(self.last_repo(), "me/sample")

    def test_mixed_case_url_is_lowercased(self):
        # round 1 F2: the clone URL keeps whatever casing the user typed, gh answers with the
        # canonical login; both must land on the same string or stats splits one repo in two
        self.set_origin("https://github.com/Me/Sample.git")
        self.stop_in_preflight()
        self.assertEqual(self.last_repo(), "me/sample")

    def test_mixed_case_scp_style_is_lowercased(self):
        self.set_origin("git@GitHub.com:ME/Sample.GIT")
        self.stop_in_preflight()
        self.assertEqual(self.last_repo(), "me/sample")

    def test_stats_groups_the_stopped_run_under_the_repo(self):
        self.set_origin("https://github.com/me/sample.git")
        self.stop_in_preflight()
        code, out = run_cli(["stats"])
        self.assertEqual(code, 0, out)
        self.assertNotIn("(unknown repo)", out)
        rows = [line for line in out.splitlines() if line.startswith("| me/sample |")]
        self.assertEqual(len(rows), 1, out)


class NonHostedOrigins(_PreflightStop):
    def test_local_bare_directory_stays_blank(self):
        # the fixture's origin is a bare repository on disk (an absolute path, drive letter on
        # Windows)
        self.stop_in_preflight()
        self.assertEqual(self.last_repo(), "")

    def test_file_scheme_stays_blank(self):
        self.set_origin("file:///somewhere/on/disk/sample.git")
        self.stop_in_preflight()
        self.assertEqual(self.last_repo(), "")

    def test_relative_path_stays_blank(self):
        self.set_origin("../sample.git")
        self.stop_in_preflight()
        self.assertEqual(self.last_repo(), "")

    def test_hosted_url_with_only_one_path_component_stays_blank(self):
        self.set_origin("https://github.com/sample.git")
        self.stop_in_preflight()
        self.assertEqual(self.last_repo(), "")


class NoRemoteAtAll(_PreflightStop):
    with_remote = False

    def test_missing_origin_records_blank_and_does_not_crash(self):
        self.stop_in_preflight()
        self.assertEqual(self.last_repo(), "")


class StoppedAndCompletedRunsShareOneRow(RepoCase):
    def test_stats_shows_a_single_repo_row(self):
        # first run stops in preflight with a hosted origin; the second completes with the
        # local origin (so the push works) and gh naming the same owner/name. Both rows
        # must land in one stats row; before the change the first one was "(unknown repo)".
        # The URL and the gh answer use different casing on purpose (round 1 F2): the two
        # writers of `repo` must agree on one string.
        local_origin = git(["remote", "get-url", "origin"], self.repo).strip()
        git(["remote", "set-url", "origin", "https://github.com/Me/Sample.git"], self.repo)
        self.write("src/calc.py", "# uncommitted edit\n")
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR, out)
        git(["checkout", "--", "src/calc.py"], self.repo)
        git(["remote", "set-url", "origin", local_origin], self.repo)

        # gh reports the canonical login casing; preflight compares owner and login
        # case-insensitively
        self.scenario({"owner": "ME", "name": "Sample", "login": "me"})
        self.claude(claude_entry())
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)

        rows = read_history(history_path(load_user_config()))
        self.assertEqual([r["repo"] for r in rows], ["me/sample", "me/sample"])
        code, out = run_cli(["stats"])
        self.assertNotIn("(unknown repo)", out)
        repo_rows = [line for line in out.splitlines() if line.startswith("| me/sample |")]
        self.assertEqual(len(repo_rows), 1, out)
        self.assertIn("| me/sample | 2 |", out)


if __name__ == "__main__":
    unittest.main()
