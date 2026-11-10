"""feature/revali-version-in-logs (M3.4.9): the build of revali that ran is in a run's logs and
its history row, while the whole-line outputs tests compare stay as they were."""

import os
import tempfile
import threading
import unittest
from unittest import mock

from revali import EXIT_OK, NAME, VERSION, build, gitops
from revali.pipeline import _record_history
from revali.state import RunLog, State, read_history, write_retry_s
from revali.stats import summarise
from tests.helpers import ROOT, RepoCase, _quote, git, rmtree_force, run_cli

DEAD_PID = 999999999


class BuildCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="revali build ")
        self.addCleanup(rmtree_force, self.tmp)
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)

    def checkout(self, tag="v9.9.9", commit=True):
        """A git checkout standing in for the one revali was loaded from."""
        path = os.path.join(self.tmp, "revali checkout")
        os.makedirs(path)
        git(["init", "-q"], path)
        git(["config", "user.email", "t@example.invalid"], path)
        git(["config", "user.name", "t"], path)
        git(["config", "core.autocrlf", "false"], path)
        with open(os.path.join(path, "a.txt"), "w", encoding="utf-8", newline="\n") as fh:
            fh.write("a\n")
        if commit:
            git(["add", "a.txt"], path)
            git(["commit", "-q", "-m", "a"], path)
            if tag:
                git(["tag", tag], path)
        return path

    def fresh_build(self, checkout):
        """build() as a new process sees it: nothing cached, loaded from `checkout`."""
        with (
            mock.patch.object(build, "CHECKOUT", checkout),
            mock.patch.object(build, "_build", None),
        ):
            return build.build()


class BuildString(BuildCase):
    """AC-1."""

    def test_version_and_describe(self):
        self.assertEqual(self.fresh_build(self.checkout()), "%s (v9.9.9)" % VERSION)

    def test_commits_past_the_tag(self):
        path = self.checkout()
        with open(os.path.join(path, "b.txt"), "w", encoding="utf-8", newline="\n") as fh:
            fh.write("b\n")
        git(["add", "b.txt"], path)
        git(["commit", "-q", "-m", "b"], path)
        sha = git(["rev-parse", "--short", "HEAD"], path).strip()
        got = self.fresh_build(path)
        self.assertEqual(got, "%s (v9.9.9-1-g%s)" % (VERSION, sha))

    def test_uncommitted_change_is_dirty(self):
        path = self.checkout()
        with open(os.path.join(path, "a.txt"), "a", encoding="utf-8", newline="\n") as fh:
            fh.write("more\n")
        self.assertEqual(self.fresh_build(path), "%s (v9.9.9-dirty)" % VERSION)

    def test_no_tag_gives_the_sha(self):
        path = self.checkout(tag=None)
        sha = git(["rev-parse", "--short", "HEAD"], path).strip()
        self.assertEqual(self.fresh_build(path), "%s (%s)" % (VERSION, sha))

    def test_not_a_checkout(self):
        path = os.path.join(self.tmp, "copied package")
        os.makedirs(path)
        self.assertEqual(self.fresh_build(path), VERSION)

    def test_inside_another_projects_checkout_is_not_that_project(self):
        # the package copied into a subdirectory of some other repository: describing that
        # repository would name the wrong commit
        path = os.path.join(self.checkout(), "vendor", "revali")
        os.makedirs(path)
        self.assertEqual(self.fresh_build(path), VERSION)

    def test_git_missing(self):
        os.environ["REVALI_GIT_CMD"] = _quote(os.path.join(self.tmp, "no-such-git"))
        self.assertEqual(self.fresh_build(self.checkout()), VERSION)

    def test_git_fails(self):
        # a repository with no commit: describe exits non-zero
        self.assertEqual(self.fresh_build(self.checkout(commit=False)), VERSION)

    def test_git_times_out(self):
        from revali.procs import ProcTimeout

        with mock.patch("revali.gitops.run", side_effect=ProcTimeout("timed out")):
            self.assertEqual(self.fresh_build(self.checkout()), VERSION)

    def test_git_runs_at_most_once_per_process(self):
        path = self.checkout()
        real = gitops.describe
        calls = []

        def counting(cwd):
            calls.append(cwd)
            return real(cwd)

        got = []
        with (
            mock.patch.object(build, "CHECKOUT", path),
            mock.patch.object(build, "_build", None),
            mock.patch("revali.gitops.describe", side_effect=counting),
        ):
            threads = [threading.Thread(target=lambda: got.append(build.build())) for _ in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            got.append(build.build())
        self.assertEqual(calls, [path])
        self.assertEqual(set(got), {"%s (v9.9.9)" % VERSION})

    def test_a_failed_describe_is_not_retried(self):
        path = os.path.join(self.tmp, "copied package")
        os.makedirs(path)
        with (
            mock.patch.object(build, "CHECKOUT", path),
            mock.patch.object(build, "_build", None),
            mock.patch("revali.gitops.describe") as describe,
        ):
            self.assertEqual(build.build(), VERSION)
            self.assertEqual(build.build(), VERSION)
        describe.assert_not_called()

    def test_describe_while_another_process_holds_the_index_lock(self):
        # other projects' runs describe the same revali tree at the same moment; the refresh
        # behind --dirty may want to write the index (a stale stat here), and a held lock must
        # neither fail the describe nor be removed
        path = self.checkout()
        later = os.path.getmtime(os.path.join(path, "a.txt")) + 10
        os.utime(os.path.join(path, "a.txt"), (later, later))
        lock = os.path.join(path, ".git", "index.lock")
        open(lock, "w").close()
        self.assertEqual(gitops.describe(path), "v9.9.9")
        self.assertTrue(os.path.exists(lock))


class BuildInTheRun(RepoCase):
    """AC-2, AC-3, AC-4."""

    def test_preflight_first_line_names_the_build(self):
        code, out = run_cli(["preflight"])
        self.assertEqual(code, EXIT_OK, out)
        line = "repo %s, branch feature/mul, revali %s" % (
            gitops.repo_root(self.repo),
            build.build(),
        )
        first = [ln for ln in out.splitlines() if "repo " in ln][0]
        self.assertTrue(first.endswith(line), out)
        with open(os.path.join(self.rdir(), "logs", "revali.log"), encoding="utf-8") as fh:
            self.assertIn(line, fh.read())

    def test_detached_start_line_ends_with_the_build(self):
        with mock.patch("revali.pipeline.spawn_detached", return_value=DEAD_PID):
            code, out = run_cli(["run"])
        self.assertEqual(code, EXIT_OK, out)
        log_path = os.path.join(self.rdir(), "logs", "run.log")
        self.assertIn(
            "started revali run (pid %d); log: %s; revali %s\n"
            % (DEAD_PID, log_path, build.build()),
            out,
        )

    def test_history_row_carries_the_build(self):
        log = RunLog(self.rdir(), quiet=True, logs_dir="logs")
        _record_history(State(repo="o/r", branch="b", base="main", stage="error"), 1, log)
        rows = read_history(os.path.join(self.home, "history.jsonl"))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["revali_build"], build.build())
        self.assertEqual(rows[0]["revali_version"], VERSION)

    def test_stats_reads_old_and_new_rows(self):
        old = {"repo": "o/r", "branch": "a", "pr": 1, "stage": "merged", "revali_version": "0.2.0"}
        new = dict(old, branch="b", pr=2, revali_version=VERSION, revali_build=build.build())
        text = summarise([old, new])
        self.assertIn("history rows: 2, pipelines: 2", text)
        self.assertIn("| o/r | 2 | 2 |", text)


class WholeLinesUnchanged(RepoCase):
    """AC-5."""

    def test_version_command_and_flag(self):
        for argv in (["version"], ["--version"]):
            with self.subTest(argv=argv):
                code, out = run_cli(argv)
                self.assertEqual(code, EXIT_OK, out)
                self.assertEqual(out, "%s %s\n" % (NAME, VERSION))

    def test_identity_and_status_version_lines(self):
        code, out = run_cli(["status"])
        self.assertEqual(code, EXIT_OK, out)
        lines = out.splitlines()
        self.assertEqual(lines[0], "repo: %s  branch: feature/mul" % gitops.repo_root(self.repo))
        self.assertEqual(lines[1], "%s %s" % (NAME, VERSION))


class StateFileWindow(RepoCase):
    """AC-6: the state-file window refuses a value that never ends, as the history lock does."""

    def user_config(self, text):
        with open(os.path.join(self.home, "config.toml"), "w", encoding="utf-8") as fh:
            fh.write(text)

    def test_nan_inf_negative_give_the_default(self):
        from revali.config import load_defaults

        default = float(load_defaults()["paths"]["write_retry_s"])
        path = os.path.join(self.rdir(), "state.json")
        for value in ("nan", "inf", "-1.0"):
            with self.subTest(value=value):
                self.user_config("[paths]\nwrite_retry_s = %s\n" % value)
                self.assertEqual(write_retry_s(path), default)

    def test_a_usable_value_is_kept(self):
        self.user_config("[paths]\nwrite_retry_s = 0.4\n")
        self.assertEqual(write_retry_s(os.path.join(self.rdir(), "state.json")), 0.4)
        self.user_config("[paths]\nwrite_retry_s = 0\n")
        self.assertEqual(write_retry_s(os.path.join(self.rdir(), "state.json")), 0.0)

    def test_a_refused_file_against_nan_still_gives_up(self):
        # the window as _until_permitted uses it: a file that stays refused gives up after the
        # default window, not never (`nan` never compares past a deadline)
        from revali.config import load_defaults
        from revali.state import _until_permitted

        self.user_config("[paths]\nwrite_retry_s = nan\n")
        path = os.path.join(self.rdir(), "state.json")
        clock = [0.0]
        attempts = []

        def refused():
            attempts.append(clock[0])
            raise PermissionError("held by a reader")

        def sleep(seconds):
            clock[0] += seconds

        with (
            mock.patch("revali.state.time.monotonic", side_effect=lambda: clock[0]),
            mock.patch("revali.state.time.sleep", side_effect=sleep),
        ):
            with self.assertRaises(PermissionError):
                _until_permitted(refused, path, None)
        default = float(load_defaults()["paths"]["write_retry_s"])
        self.assertGreaterEqual(clock[0], default)
        self.assertLess(clock[0], default + 1)


class Docs(unittest.TestCase):
    """AC-7."""

    def read(self, name):
        with open(os.path.join(ROOT, "docs", name), "r", encoding="utf-8") as fh:
            return " ".join(fh.read().split())

    def test_workflow_says_where_the_build_appears(self):
        text = self.read("workflow.md")
        self.assertIn(", revali <build>", text)
        self.assertIn("revali_build", text)

    def test_files_names_the_history_field(self):
        self.assertIn("revali_build", self.read("files.md"))


if __name__ == "__main__":
    unittest.main()
