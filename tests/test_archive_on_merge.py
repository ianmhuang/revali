"""`revali merge` moves `.revali/<branch>/` to `[paths] archive_dir` instead of deleting it
(AC-1 to AC-6); the docs and the reviewer prompt follow (AC-7, AC-8)."""

import os
import re
import unittest
from unittest import mock

from revali import EXIT_ACTION, EXIT_ERROR, EXIT_OK, PROMPT_VERSION
from revali.config import ConfigError, PathsCfg, archive_root, parse_project_config
from revali.state import State
from tests.helpers import ROOT, RepoCase, claude_entry, git, run_cli

SUFFIX = re.compile(r"-\d{8}-\d{6}$")


def read(path):
    with open(path, "r", encoding="utf-8", newline="") as fh:
        return fh.read()


class ArchiveCase(RepoCase):
    def setUp(self):
        super().setUp()
        os.environ["REVALI_POLL_SECONDS"] = "0.01"

    def ready(self):
        self.claude(claude_entry())
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)

    def set_archive_dir(self, value):
        """[paths] archive_dir in the project file, on the branch and on main: `merge` reads
        the config after the checkout moved to main (a real merge carries the branch's
        revali.toml there; the gh stub merges nothing locally)."""
        addition = '\n[paths]\narchive_dir = "%s"\n' % value
        for branch in ("main", "feature/mul"):
            git(["checkout", "-q", branch], self.repo)
            self.write("revali.toml", self.read("revali.toml") + addition)
            self.commit_all("archive_dir")

    def default_dest(self):
        return os.path.join(self.home, "archive", "me__sample", "7-feature__mul")


class ArchiveOnMerge(ArchiveCase):
    def test_the_branch_directory_moves_to_the_default_archive(self):
        # AC-1 (default), AC-2
        self.ready()
        before = sorted(os.listdir(self.rdir()))
        self.assertIn("state.json", before)
        self.assertIn("logs", before)
        code, out = run_cli(["merge"])
        self.assertEqual(code, EXIT_OK, out)
        dest = self.default_dest()
        self.assertTrue(os.path.isdir(dest), out)
        self.assertEqual(sorted(os.listdir(dest)), before)
        self.assertEqual(State.load(dest).stage, "merged")
        self.assertFalse(os.path.exists(self.rdir()))
        self.assertIn("archived to %s" % dest, out)
        self.assertNotIn("removed .revali", out)

    def test_a_project_value_relative_to_the_user_directory(self):
        # AC-1: relative under ~/.revali (REVALI_HOME here); the project file may set it
        self.set_archive_dir("kept/here")
        self.ready()
        code, out = run_cli(["merge"])
        self.assertEqual(code, EXIT_OK, out)
        dest = os.path.join(self.home, "kept", "here", "me__sample", "7-feature__mul")
        self.assertTrue(os.path.isdir(dest), out)
        self.assertFalse(os.path.isdir(os.path.join(self.home, "archive")))

    def test_a_user_value_is_used(self):
        # AC-1: the user file may set it
        with open(os.path.join(self.home, "config.toml"), "w", encoding="utf-8") as fh:
            fh.write('[paths]\narchive_dir = "from-user"\n')
        self.ready()
        code, out = run_cli(["merge"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertTrue(
            os.path.isdir(os.path.join(self.home, "from-user", "me__sample", "7-feature__mul")), out
        )

    def test_an_absolute_project_value_is_used_as_is(self):
        elsewhere = os.path.join(self.tmp, "else where")
        self.set_archive_dir(elsewhere.replace("\\", "/"))
        self.ready()
        code, out = run_cli(["merge"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertTrue(os.path.isdir(os.path.join(elsewhere, "me__sample", "7-feature__mul")), out)

    def test_archive_root_resolution(self):
        # AC-1: "", relative, ~ and absolute values
        os.environ["REVALI_HOME"] = self.home
        self.assertEqual(archive_root(PathsCfg(archive_dir="")), "")
        self.assertEqual(
            archive_root(PathsCfg(archive_dir="archive")), os.path.join(self.home, "archive")
        )
        self.assertEqual(
            archive_root(PathsCfg(archive_dir="~/rv")), os.path.join(os.path.expanduser("~"), "rv")
        )
        absolute = os.path.abspath(os.path.join(self.tmp, "abs"))
        self.assertEqual(archive_root(PathsCfg(archive_dir=absolute)), absolute)

    def test_a_non_string_value_is_a_config_error(self):
        # AC-1
        text = self.read("revali.toml") + "\n[paths]\narchive_dir = 3\n"
        with self.assertRaises(ConfigError) as caught:
            parse_project_config(text)
        self.assertTrue(
            any("paths.archive_dir must be a string" in p for p in caught.exception.problems),
            caught.exception.problems,
        )

    def test_the_repository_directory_name_when_the_state_has_no_repo(self):
        # AC-2: <owner>__<name> falls back to the checkout's directory name
        self.ready()
        state = State.load(self.rdir())
        state.repo = ""
        state.save(self.rdir())
        code, out = run_cli(["merge"])
        self.assertEqual(code, EXIT_OK, out)
        dest = os.path.join(self.home, "archive", "sample", "7-feature__mul")
        self.assertTrue(os.path.isdir(dest), out)

    def test_an_empty_value_removes_the_directory(self):
        # AC-3
        self.set_archive_dir("")
        self.ready()
        code, out = run_cli(["merge"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertFalse(os.path.exists(self.rdir()))
        self.assertFalse(os.path.isdir(os.path.join(self.home, "archive")))
        self.assertIn("removed .revali/feature__mul/", out)
        self.assertNotIn("archived", out)

    def test_an_existing_destination_gets_a_timestamp_suffix(self):
        # AC-4
        self.ready()
        dest = self.default_dest()
        os.makedirs(dest)
        marker = os.path.join(dest, "older.txt")
        with open(marker, "w", encoding="utf-8") as fh:
            fh.write("first merge")
        code, out = run_cli(["merge"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertEqual(read(marker), "first merge")
        self.assertEqual(os.listdir(dest), ["older.txt"])
        siblings = [d for d in os.listdir(os.path.dirname(dest)) if d.startswith("7-feature__mul-")]
        self.assertEqual(len(siblings), 1, siblings)
        self.assertRegex(siblings[0], SUFFIX)
        new = os.path.join(os.path.dirname(dest), siblings[0])
        self.assertTrue(os.path.isfile(os.path.join(new, "state.json")))
        self.assertIn("archived to %s" % new, out)

    def test_a_failed_move_leaves_the_directory_in_place(self):
        # AC-5
        self.ready()
        with mock.patch("os.rename", side_effect=OSError(18, "Invalid cross-device link")):
            with mock.patch("revali.merge.shutil.copytree", side_effect=OSError("disk full")):
                code, out = run_cli(["merge"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertTrue(os.path.isfile(os.path.join(self.rdir(), "state.json")))
        self.assertIn("could not archive", out)
        self.assertIn("disk full", out)
        self.assertIn(self.rdir(), out)
        self.assertFalse(os.path.isdir(self.default_dest()))

    def test_a_name_taken_since_the_look_is_reported_not_deleted(self):
        # AC-4, AC-5 (round 1 F1): the destination appears between archive_destination's check
        # and the move; the copy route must not remove that earlier archive
        self.ready()
        taken = self.default_dest()
        os.makedirs(taken)
        with open(os.path.join(taken, "older.txt"), "w", encoding="utf-8") as fh:
            fh.write("first merge")
        with mock.patch("revali.merge.archive_destination", return_value=taken):
            with mock.patch("os.rename", side_effect=OSError(18, "Invalid cross-device link")):
                code, out = run_cli(["merge"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertEqual(os.listdir(taken), ["older.txt"])
        self.assertEqual(read(os.path.join(taken, "older.txt")), "first merge")
        self.assertTrue(os.path.isfile(os.path.join(self.rdir(), "state.json")))
        self.assertIn("could not archive", out)

    def test_the_value_survives_a_config_that_fails_to_load_on_the_base_branch(self):
        # AC-1, AC-3 (round 1 F2): merge reads [paths] after the checkout moved to main; a
        # config that does not load there falls back to the raw table, archive_dir included
        with open(os.path.join(self.home, "config.toml"), "w", encoding="utf-8") as fh:
            fh.write('[paths]\narchive_dir = ""\n')
        git(["checkout", "-q", "main"], self.repo)
        self.write("revali.toml", self.read("revali.toml") + "\n[nonsense]\nkey = 1\n")
        self.commit_all("broken config on main")
        git(["checkout", "-q", "feature/mul"], self.repo)
        self.ready()
        code, out = run_cli(["merge"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertFalse(os.path.exists(self.rdir()))
        self.assertFalse(os.path.isdir(os.path.join(self.home, "archive")))
        self.assertIn("removed .revali/feature__mul/", out)

    def test_a_move_across_devices_copies_and_deletes(self):
        # AC-5: os.rename refuses (as across drives); shutil.move falls back to copy + delete
        self.ready()
        real_rename = os.rename

        def refuse(src, dst, *args, **kwargs):
            if os.path.normcase(src) == os.path.normcase(self.rdir()):
                raise OSError(18, "Invalid cross-device link")
            return real_rename(src, dst, *args, **kwargs)

        with mock.patch("os.rename", side_effect=refuse):
            code, out = run_cli(["merge"])
        self.assertEqual(code, EXIT_OK, out)
        dest = self.default_dest()
        self.assertTrue(os.path.isfile(os.path.join(dest, "state.json")), out)
        self.assertTrue(os.path.isdir(os.path.join(dest, "logs")))
        self.assertFalse(os.path.exists(self.rdir()))

    def test_a_merge_that_stops_early_archives_nothing(self):
        # AC-6
        self.ready()
        self.scenario({"merge_exit": 1})
        code, out = run_cli(["merge"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertTrue(os.path.isfile(os.path.join(self.rdir(), "state.json")))
        self.assertFalse(os.path.isdir(os.path.join(self.home, "archive")))
        failing = [{"name": "ci", "state": "FAILURE", "bucket": "fail"}]
        self.scenario({"merge_exit": 0, "checks": failing})
        code, out = run_cli(["merge"])
        self.assertEqual(code, EXIT_ACTION, out)
        self.assertTrue(os.path.isfile(os.path.join(self.rdir(), "state.json")))
        self.assertFalse(os.path.isdir(os.path.join(self.home, "archive")))


class DocsAndPrompt(unittest.TestCase):
    def test_the_docs_describe_the_key(self):
        # AC-7
        files = read(os.path.join(ROOT, "docs", "files.md"))
        self.assertIn("archive_dir", files)
        self.assertIn("revali merge", files)
        self.assertNotIn("when the merge removes it", files)
        self.assertIn("archive_dir", read(os.path.join(ROOT, "docs", "configuration.md")))
        self.assertIn("archive_dir", read(os.path.join(ROOT, "templates", "user-config.toml")))
        side = read(os.path.join(ROOT, "docs", "side-effects.md"))
        self.assertIn("archive_dir", side)
        self.assertNotIn("deletion of the branch's state directory", side)
        defaults = read(os.path.join(ROOT, "defaults.toml"))
        self.assertRegex(defaults, r'archive_dir\s*=\s*"archive"')

    def test_the_prompt_sends_documentation_wording_to_not_testable(self):
        # AC-8
        prompt = read(os.path.join(ROOT, "prompts", "review.md"))
        self.assertRegex(prompt, r"wording of documentation[\s\S]{0,200}not_testable")
        self.assertGreaterEqual(int(PROMPT_VERSION), 8)


if __name__ == "__main__":
    unittest.main()
