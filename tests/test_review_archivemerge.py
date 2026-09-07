"""Review of feature/archive-on-merge: `revali merge` moves `.revali/<branch>/` to
`[paths] archive_dir` instead of deleting it.

AC-1 the key, its default, how a value resolves (relative under the user directory, `~`
expanded, absolute as written), both config layers, a non-string value is a config error;
AC-2 the destination `<archive_dir>/<owner>__<name>/<pr>-<safe branch>/`, every file moved,
nothing left behind, the summary line names the destination;
AC-3 an empty value deletes as before;
AC-4 a taken destination gets a local-time `-<YYYYMMDD-HHMMSS>` suffix, the old archive untouched;
AC-5 a failed copy leaves the directory in place, exit stays 0, one line names the error and the
path; a rename that is refused (another drive) falls back to copy and delete;
AC-6 a merge that stops before the PR is merged archives nothing;
AC-8 PROMPT_VERSION moved past 7.

Round 2: a destination taken between the look and the move is reported and never removed
(round 1 F1); a half-copied destination is removed on failure; a user or project `archive_dir`
still counts when the config on the base branch fails to load (round 1 F2).

Black-box through the CLI: a state written straight into `ready_to_merge`, real git, the fake gh
(which merges nothing locally, so the project-layer key has to be on main too: `merge` reads the
config after the checkout moved there). The only patched calls are `os.rename` (refused for the
branch directory alone, as a second drive would), `shutil.copytree` (the disk-full case) and,
for the race of round 1 F1 alone, `archive_destination` (pinned to a name that is taken)."""

import json
import os
import re
import time
import unittest
from unittest import mock

from revali import EXIT_ACTION, EXIT_ERROR, EXIT_OK, PROMPT_VERSION, gitops
from revali.config import (
    ConfigError,
    PathsCfg,
    archive_root,
    load_user_config,
    parse_project_config,
)
from revali.state import State
from tests.helpers import RepoCase, git, run_cli

SUFFIX = re.compile(r"^7-feature__mul-(\d{8}-\d{6})$")


def norm(path):
    return os.path.normcase(os.path.normpath(path)).replace("\\", "/")


def same_path_in(text, path):
    return norm(path) in os.path.normcase(text).replace("\\", "/")


def files_under(root):
    """Relative paths of every file below `root`, sorted."""
    found = []
    for base, _dirs, names in os.walk(root):
        for name in names:
            found.append(os.path.relpath(os.path.join(base, name), root).replace("\\", "/"))
    return sorted(found)


class ArchiveCase(RepoCase):
    """self.repo is the primary tree on feature/mul, main pushed to a bare origin."""

    def setUp(self):
        super().setUp()
        os.environ["REVALI_POLL_SECONDS"] = "0.01"

    def ready(self, repo="me/sample"):
        """A branch ready to merge, with the kind of record a real run leaves behind."""
        rdir = self.rdir()
        self.write(".revali/feature__mul/review-1.md", "# Round 1\n\nAPPROVE\n")
        self.write(".revali/feature__mul/tests.md", "tests/test_review_mul.py\n")
        self.write(".revali/feature__mul/logs/revali.log", "[00:00:00] review: round 1\n")
        self.write(".revali/feature__mul/logs/review-1-prompt.md", "prompt\n")
        State(
            repo=repo,
            branch="feature/mul",
            base="main",
            stage="ready_to_merge",
            message="validation 1 passed",
            last_exit=EXIT_OK,
            pr_number=7,
            head_sha=gitops.rev_parse("HEAD", self.repo),
            test_files=["tests/test_review_mul.py"],
        ).save(rdir)
        git(["push", "-q", "-u", "origin", "feature/mul"], self.repo)
        return rdir

    def user_config(self, text):
        with open(os.path.join(self.home, "config.toml"), "w", encoding="utf-8") as fh:
            fh.write(text)

    def project_archive_dir(self, value):
        """[paths] archive_dir in revali.toml on main and on the branch: after a real merge
        main carries the branch's file; the fake gh merges nothing, so both get the commit."""
        addition = "\n[paths]\narchive_dir = %s\n" % json.dumps(value)
        for branch in ("main", "feature/mul"):
            git(["checkout", "-q", branch], self.repo)
            self.write("revali.toml", self.read("revali.toml") + addition)
            self.commit_all("archive_dir = %s" % value)

    def project_config_on_main(self, addition):
        """Append `addition` to revali.toml on main only, then return to the branch: what
        `merge` reads once the checkout has moved to the base branch."""
        git(["checkout", "-q", "main"], self.repo)
        self.write("revali.toml", self.read("revali.toml") + addition)
        self.commit_all("revali.toml on main")
        git(["checkout", "-q", "feature/mul"], self.repo)

    def archive(self, *parts):
        return os.path.join(self.home, "archive", *parts)

    def default_dest(self):
        return self.archive("me__sample", "7-feature__mul")

    def line_with(self, out, needle):
        lines = [line for line in out.splitlines() if needle in line]
        self.assertEqual(len(lines), 1, "expected one line with %r in:\n%s" % (needle, out))
        return lines[0]

    def assert_merged(self, code, out):
        self.assertEqual(code, EXIT_OK, out)
        self.assertNotIn("Traceback", out)
        self.assertIn("MERGED: PR #7 into main", out)


class DefaultArchive(ArchiveCase):
    """AC-1 (the default), AC-2"""

    def test_the_whole_branch_directory_moves_to_the_default_destination(self):
        rdir = self.ready()
        before = files_under(rdir)
        self.assertIn("state.json", before)
        self.assertIn("logs/revali.log", before)
        code, out = run_cli(["merge"])
        self.assert_merged(code, out)
        dest = self.default_dest()
        self.assertTrue(os.path.isdir(dest), out)
        after = files_under(dest)
        missing = sorted(set(before) - set(after))
        self.assertEqual(missing, [], "not archived: %s" % missing)
        self.assertEqual(State.load(dest).stage, "merged")  # the record of the merge itself
        self.assertEqual(State.load(dest).pr_number, 7)
        self.assertFalse(os.path.exists(rdir), "left behind: %s" % rdir)
        # nothing else under the archive root: one directory per merged PR
        self.assertEqual(os.listdir(self.archive()), ["me__sample"])
        self.assertEqual(os.listdir(self.archive("me__sample")), ["7-feature__mul"])

    def test_the_summary_line_names_the_destination_instead_of_removed(self):
        self.ready()
        code, out = run_cli(["merge"])
        self.assert_merged(code, out)
        self.assertNotIn("removed .revali", out)
        line = self.line_with(out, "archived to")
        self.assertTrue(same_path_in(line, self.default_dest()), line)
        self.assertNotIn("could not", line)
        # on the summary's own indented line, after the MERGED block
        self.assertTrue(line.startswith("  "), repr(line))
        self.assertLess(out.index("MERGED: PR #7"), out.index("archived to"))

    def test_the_repository_directory_name_stands_in_for_a_missing_repo(self):
        self.ready(repo="")
        code, out = run_cli(["merge"])
        self.assert_merged(code, out)
        dest = self.archive("sample", "7-feature__mul")
        self.assertTrue(os.path.isdir(dest), out)
        self.assertTrue(os.path.isfile(os.path.join(dest, "state.json")))
        self.assertFalse(os.path.exists(self.rdir()))

    def test_only_the_branch_directory_moves(self):
        # .revali/ itself (tree.lock's home) stays in the repository
        self.ready()
        code, out = run_cli(["merge"])
        self.assert_merged(code, out)
        self.assertTrue(os.path.isdir(os.path.join(self.repo, ".revali")))
        self.assertNotIn(".revali", os.listdir(self.default_dest()))


class KeyResolution(ArchiveCase):
    """AC-1"""

    def test_a_user_value_is_relative_to_the_user_directory(self):
        self.user_config('[paths]\narchive_dir = "kept/here"\n')
        self.ready()
        code, out = run_cli(["merge"])
        self.assert_merged(code, out)
        dest = os.path.join(self.home, "kept", "here", "me__sample", "7-feature__mul")
        self.assertTrue(os.path.isfile(os.path.join(dest, "state.json")), out)
        self.assertFalse(os.path.isdir(self.archive()))
        self.assertTrue(same_path_in(self.line_with(out, "archived to"), dest))

    def test_a_project_value_is_allowed_and_wins_over_the_user_file(self):
        self.user_config('[paths]\narchive_dir = "from-user"\n')
        self.project_archive_dir("from-project")
        self.ready()
        code, out = run_cli(["merge"])
        self.assert_merged(code, out)
        self.assertTrue(
            os.path.isdir(os.path.join(self.home, "from-project", "me__sample", "7-feature__mul")),
            out,
        )
        self.assertFalse(os.path.isdir(os.path.join(self.home, "from-user")))
        self.assertFalse(os.path.isdir(self.archive()))

    def test_an_absolute_value_is_used_as_written(self):
        elsewhere = os.path.join(self.tmp, "shared archive")  # a space, as the temp dir has
        self.user_config("[paths]\narchive_dir = %s\n" % json.dumps(elsewhere))
        self.ready()
        code, out = run_cli(["merge"])
        self.assert_merged(code, out)
        dest = os.path.join(elsewhere, "me__sample", "7-feature__mul")
        self.assertTrue(os.path.isfile(os.path.join(dest, "state.json")), out)
        self.assertFalse(os.path.isdir(self.archive()))
        self.assertFalse(os.path.exists(self.rdir()))

    def test_resolution_rules(self):
        os.environ["REVALI_HOME"] = self.home
        self.assertEqual(archive_root(PathsCfg(archive_dir="")), "")
        self.assertEqual(archive_root(PathsCfg(archive_dir="   ")), "")
        self.assertEqual(
            norm(archive_root(PathsCfg(archive_dir="archive"))),
            norm(os.path.join(self.home, "archive")),
        )
        self.assertEqual(
            norm(archive_root(PathsCfg(archive_dir="~/rv-archive"))),
            norm(os.path.join(os.path.expanduser("~"), "rv-archive")),
        )
        absolute = os.path.join(self.tmp, "abs")
        self.assertEqual(norm(archive_root(PathsCfg(archive_dir=absolute))), norm(absolute))

    def test_a_non_string_project_value_is_a_config_error(self):
        text = self.read("revali.toml") + "\n[paths]\narchive_dir = 3\n"
        with self.assertRaises(ConfigError) as caught:
            parse_project_config(text)
        self.assertTrue(
            any("paths.archive_dir must be a string" in p for p in caught.exception.problems),
            caught.exception.problems,
        )

    def test_a_non_string_user_value_is_a_config_error(self):
        self.user_config("[paths]\narchive_dir = false\n")
        with self.assertRaises(ConfigError) as caught:
            load_user_config()
        self.assertTrue(
            any("paths.archive_dir must be a string" in p for p in caught.exception.problems),
            caught.exception.problems,
        )

    def test_the_default_is_archive(self):
        cfg = parse_project_config(self.read("revali.toml"))
        self.assertEqual(cfg.paths.archive_dir, "archive")


class EmptyValueDeletes(ArchiveCase):
    """AC-3"""

    def test_an_empty_value_removes_the_directory_and_says_so(self):
        self.user_config('[paths]\narchive_dir = ""\n')
        rdir = self.ready()
        code, out = run_cli(["merge"])
        self.assert_merged(code, out)
        self.assertFalse(os.path.exists(rdir))
        self.assertFalse(os.path.isdir(self.archive()))
        made = [n for n in os.listdir(self.home) if os.path.isdir(os.path.join(self.home, n))]
        self.assertEqual(made, [], "directories under the user directory: %s" % made)
        self.line_with(out, "removed .revali/feature__mul/")
        self.assertNotIn("archived", out)


class TakenDestination(ArchiveCase):
    """AC-4"""

    def test_a_taken_name_gets_a_timestamp_suffix_and_the_old_archive_is_untouched(self):
        self.ready()
        dest = self.default_dest()
        os.makedirs(dest)
        marker = os.path.join(dest, "older.txt")
        with open(marker, "w", encoding="utf-8") as fh:
            fh.write("first merge")
        started = time.localtime()
        code, out = run_cli(["merge"])
        finished = time.localtime()
        self.assert_merged(code, out)
        # the old archive: same single file, same content
        self.assertEqual(os.listdir(dest), ["older.txt"])
        with open(marker, "r", encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "first merge")
        names = sorted(os.listdir(os.path.dirname(dest)))
        self.assertEqual(len(names), 2, names)
        (new_name,) = [n for n in names if n != "7-feature__mul"]
        match = SUFFIX.match(new_name)
        self.assertIsNotNone(match, new_name)
        stamp = time.strptime(match.group(1), "%Y%m%d-%H%M%S")
        self.assertLessEqual(time.mktime(started) - 1, time.mktime(stamp))  # local time
        self.assertLessEqual(time.mktime(stamp), time.mktime(finished) + 1)
        new = os.path.join(os.path.dirname(dest), new_name)
        self.assertTrue(os.path.isfile(os.path.join(new, "state.json")))
        self.assertTrue(os.path.isfile(os.path.join(new, "logs", "revali.log")))
        self.assertFalse(os.path.exists(self.rdir()))
        self.assertTrue(same_path_in(self.line_with(out, "archived to"), new))


def refuse_rename_of(path):
    """An os.rename that refuses `path` as the source, the way a move to another drive does,
    and behaves normally for everything else."""
    real_rename = os.rename

    def refuse(src, dst, *args, **kwargs):
        if os.path.normcase(os.path.normpath(src)) == os.path.normcase(os.path.normpath(path)):
            raise OSError(18, "Invalid cross-device link")
        return real_rename(src, dst, *args, **kwargs)

    return refuse


class MoveFailure(ArchiveCase):
    """AC-5"""

    def test_a_failed_copy_leaves_the_directory_in_place_and_exit_stays_0(self):
        rdir = self.ready()
        before = files_under(rdir)
        with mock.patch("os.rename", side_effect=refuse_rename_of(rdir)):
            with mock.patch("shutil.copytree", side_effect=OSError(28, "No space left on device")):
                code, out = run_cli(["merge"])
        self.assert_merged(code, out)  # the PR is merged; the archive is a courtesy
        self.assertEqual(files_under(rdir), before)
        self.assertEqual(State.load(rdir).stage, "merged")
        self.assertFalse(os.path.isdir(self.default_dest()))
        self.assertNotIn("removed .revali", out)
        lines = [line for line in out.splitlines() if "No space left on device" in line]
        self.assertEqual(len(lines), 1, out)
        self.assertTrue(same_path_in(lines[0], rdir), lines[0])
        self.assertNotIn("archived to", lines[0])

    def test_a_refused_rename_falls_back_to_copy_and_delete(self):
        rdir = self.ready()
        before = files_under(rdir)
        with mock.patch("os.rename", side_effect=refuse_rename_of(rdir)):
            code, out = run_cli(["merge"])
        self.assert_merged(code, out)
        dest = self.default_dest()
        after = files_under(dest)
        self.assertEqual(sorted(set(before) - set(after)), [])
        self.assertEqual(State.load(dest).stage, "merged")
        self.assertFalse(os.path.exists(rdir))
        line = self.line_with(out, "archived to")
        self.assertTrue(same_path_in(line, dest), line)
        self.assertNotIn("could not", line)


class TakenSinceTheLook(ArchiveCase):
    """AC-4, AC-5 (round 1 F1): the name `archive_destination` chose is taken by the time the
    move runs (a second merge inside the same second, or another process). The copy route
    must report it and leave the earlier archive as it was; only a destination this merge
    created may be removed on failure."""

    def taken_destination(self):
        taken = self.default_dest()
        os.makedirs(taken)
        with open(os.path.join(taken, "older.txt"), "w", encoding="utf-8") as fh:
            fh.write("first merge")
        with open(os.path.join(taken, "state.json"), "w", encoding="utf-8") as fh:
            fh.write('{"stage": "merged", "pr_number": 6}\n')
        return taken

    def test_a_taken_name_is_reported_and_the_earlier_archive_is_left_alone(self):
        rdir = self.ready()
        before = files_under(rdir)
        taken = self.taken_destination()
        with mock.patch("revali.merge.archive_destination", return_value=taken):
            with mock.patch("os.rename", side_effect=refuse_rename_of(rdir)):
                code, out = run_cli(["merge"])
        self.assert_merged(code, out)
        # the earlier archive: same two files, same content
        self.assertEqual(sorted(os.listdir(taken)), ["older.txt", "state.json"])
        with open(os.path.join(taken, "older.txt"), "r", encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "first merge")
        with open(os.path.join(taken, "state.json"), "r", encoding="utf-8") as fh:
            self.assertIn('"pr_number": 6', fh.read())
        # the branch directory stays, whole
        self.assertEqual(files_under(rdir), before)
        self.assertEqual(State.load(rdir).stage, "merged")
        line = self.line_with(out, "could not archive")
        self.assertTrue(same_path_in(line, rdir), line)
        self.assertNotIn("archived to", out)

    def test_a_half_copied_destination_this_merge_created_is_removed(self):
        rdir = self.ready()
        before = files_under(rdir)

        def copy_some_then_fail(src, dst, *args, **kwargs):
            # the first file lands, then the disk is full
            os.makedirs(dst, exist_ok=True)
            with open(os.path.join(dst, "partial.txt"), "w", encoding="utf-8") as fh:
                fh.write("half")
            raise OSError(28, "No space left on device")

        with mock.patch("os.rename", side_effect=refuse_rename_of(rdir)):
            with mock.patch("shutil.copytree", side_effect=copy_some_then_fail):
                code, out = run_cli(["merge"])
        self.assert_merged(code, out)
        left = os.listdir(self.archive("me__sample"))
        self.assertFalse(os.path.exists(self.default_dest()), left)
        self.assertEqual(files_under(rdir), before)
        line = self.line_with(out, "No space left on device")
        self.assertTrue(same_path_in(line, rdir), line)


class BrokenConfigOnBase(ArchiveCase):
    """AC-1, AC-3 (round 1 F2): `merge` reads [paths] after the checkout moved to the base
    branch. When the config there does not load, the user's or project's archive_dir must
    still be honoured, not replaced by the default."""

    BROKEN = "\n[nonsense]\nkey = 1\n"  # an unknown section: a ConfigError, still valid TOML

    def test_a_user_empty_value_still_deletes(self):
        self.user_config('[paths]\narchive_dir = ""\n')
        self.project_config_on_main(self.BROKEN)
        rdir = self.ready()
        code, out = run_cli(["merge"])
        self.assert_merged(code, out)
        self.assertFalse(os.path.exists(rdir))
        self.assertFalse(os.path.isdir(self.archive()))
        made = [n for n in os.listdir(self.home) if os.path.isdir(os.path.join(self.home, n))]
        self.assertEqual(made, [], made)
        self.line_with(out, "removed .revali/feature__mul/")

    def test_a_project_value_in_the_broken_file_is_still_used(self):
        self.project_config_on_main(self.BROKEN + '\n[paths]\narchive_dir = "raw-project"\n')
        rdir = self.ready()
        code, out = run_cli(["merge"])
        self.assert_merged(code, out)
        dest = os.path.join(self.home, "raw-project", "me__sample", "7-feature__mul")
        self.assertTrue(os.path.isfile(os.path.join(dest, "state.json")), out)
        self.assertFalse(os.path.isdir(self.archive()))
        self.assertFalse(os.path.exists(rdir))
        self.assertTrue(same_path_in(self.line_with(out, "archived to"), dest))

    def test_a_project_value_wins_over_the_user_file_in_the_fallback_too(self):
        self.user_config('[paths]\narchive_dir = "from-user"\n')
        self.project_config_on_main(self.BROKEN + '\n[paths]\narchive_dir = "from-project"\n')
        self.ready()
        code, out = run_cli(["merge"])
        self.assert_merged(code, out)
        self.assertTrue(
            os.path.isdir(os.path.join(self.home, "from-project", "me__sample", "7-feature__mul")),
            out,
        )
        self.assertFalse(os.path.isdir(os.path.join(self.home, "from-user")))


class MergeStopsEarly(ArchiveCase):
    """AC-6"""

    def test_a_failed_gh_merge_archives_nothing(self):
        rdir = self.ready()
        before = files_under(rdir)
        self.scenario({"merge_exit": 1})
        code, out = run_cli(["merge"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertEqual(files_under(rdir), before)
        self.assertEqual(State.load(rdir).stage, "ready_to_merge")
        self.assertFalse(os.path.isdir(self.archive()))
        self.assertNotIn("archived", out)
        self.assertNotIn("removed .revali", out)

    def test_failed_checks_archive_nothing(self):
        rdir = self.ready()
        self.scenario({"checks": [{"name": "ci", "state": "FAILURE", "bucket": "fail"}]})
        code, out = run_cli(["merge"])
        self.assertEqual(code, EXIT_ACTION, out)
        self.assertTrue(os.path.isfile(os.path.join(rdir, "state.json")))
        self.assertTrue(os.path.isfile(os.path.join(rdir, "review-1.md")))
        self.assertFalse(os.path.isdir(self.archive()))
        self.assertNotIn("archived", out)

    def test_a_moved_head_archives_nothing(self):
        rdir = self.ready()
        self.write("src/extra.py", "x = 1\n")
        self.commit_all("moved")
        code, out = run_cli(["merge"])
        self.assertEqual(code, EXIT_ACTION, out)
        self.assertTrue(os.path.isfile(os.path.join(rdir, "state.json")))
        self.assertFalse(os.path.isdir(self.archive()))


class PromptVersion(unittest.TestCase):
    """AC-8: the prompt changed, so the version moved past 7 (the wording itself is
    documentation and is not pinned here)."""

    def test_prompt_version_is_past_7(self):
        self.assertGreaterEqual(int(PROMPT_VERSION), 8)


if __name__ == "__main__":
    unittest.main()
