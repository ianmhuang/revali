"""AC-1..AC-10 of feature/tests-archive: `[review] tests = "archive"` keeps the reviewer's
tests under `.revali/<branch>/tests/` instead of committing them."""

import os
import shutil
import unittest
from unittest import mock

from revali import EXIT_ACTION, EXIT_ERROR, EXIT_OK, STATE_VERSION
from revali.config import ConfigError, parse_project_config
from revali.review import short_shas
from revali.state import State
from revali.testarchive import archive_dir, archived_files
from tests.helpers import (
    ROOT,
    TEST_REVIEW_MUL,
    RepoCase,
    approve_response,
    claude_entry,
    git,
    run_cli,
)

FILE = "tests/test_review_mul.py"
SECOND = "tests/test_review_zero.py"
SECOND_TEXT = TEST_REVIEW_MUL.replace("MulTests", "ZeroTests")
HELPER = "tests/review_data/mul.json"  # a file off test_file_pattern, under test_dir
BOTH = sorted([FILE, HELPER])  # the state's order


def read(path):
    with open(path, "r", encoding="utf-8", newline="") as fh:
        return fh.read()


def approving(**files):
    """An approving entry writing exactly `files` (path -> text), listed as its tests."""
    tests = [
        {"path": p, "purpose": "acceptance", "covers": ["AC-1", "AC-2"], "expected": "per AC"}
        for p in files
        if p.endswith(".py")
    ]
    entry = claude_entry(approve_response(tests=tests), write_tests=False)
    entry["write_files"] = dict(files)
    return entry


def requesting_changes(**files):
    finding = {
        "id": "F1",
        "file": "src/calc.py",
        "line": 3,
        "severity": "high",
        "kind": "correctness",
        "text": "mul ignores negative numbers",
        "suggestion": "handle them",
    }
    entry = approving(**files)
    entry["structured_output"].update(verdict="CHANGES_REQUESTED", findings=[finding])
    return entry


def asking(**files):
    entry = approving(**files)
    entry["structured_output"].update(verdict="NEEDS_INFO", questions=["Which integers?"], tests=[])
    return entry


class ArchiveCase(RepoCase):
    def setUp(self):
        super().setUp()
        os.environ["REVALI_POLL_SECONDS"] = "0.01"
        self.set_mode("archive")

    def set_mode(self, mode):
        text = self.read("revali.toml")
        text = text.replace('exclude = ["*.lock"]\n', 'exclude = ["*.lock"]\ntests = "%s"\n' % mode)
        self.write("revali.toml", text)
        self.commit_all("tests mode %s" % mode)

    def switch_mode(self, mode):
        text = self.read("revali.toml")
        current = "archive" if 'tests = "archive"' in text else "commit"
        self.write("revali.toml", text.replace('tests = "%s"' % current, 'tests = "%s"' % mode))
        self.commit_all("switch to %s" % mode)

    def state(self):
        return State.load(self.rdir())

    def status(self):
        return git(["status", "--porcelain"], self.repo).strip()

    def isdir(self, rel):
        return os.path.isdir(os.path.join(self.repo, rel))

    def head(self):
        return git(["rev-parse", "HEAD"], self.repo).strip()

    def remote_tip(self):
        return git(["rev-parse", "feature/mul"], self.info["remote"]).strip()

    def trailer_commits(self):
        log = git(["log", "--format=%H%n%B", "main..HEAD"], self.repo)
        return [line for line in log.splitlines() if line.startswith("Revali-Round:")]

    def archived(self, rel):
        return os.path.join(archive_dir(self.rdir()), rel)

    def run_ok(self, *entries):
        self.claude(*entries)
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        return out

    def runner_calls(self, label_prefix):
        return [c for c in self.fake_calls("runner") if c.get("label", "").startswith(label_prefix)]


class ConfigKey(RepoCase):
    def parse(self, review_key="", addition=""):
        """The fixture's revali.toml with `review_key` inside its [review] table and
        `addition` appended."""
        text = self.read("revali.toml").replace(
            'exclude = ["*.lock"]\n', 'exclude = ["*.lock"]\n' + review_key
        )
        return parse_project_config(text + addition)

    def problems(self, review_key="", addition=""):
        with self.assertRaises(ConfigError) as caught:
            self.parse(review_key, addition)
        return caught.exception.problems

    def test_default_is_commit_and_both_values_are_accepted(self):
        # AC-1
        self.assertEqual(self.parse().review.tests, "commit")
        self.assertEqual(self.parse('tests = "archive"\n').review.tests, "archive")
        self.assertEqual(self.parse('tests = "commit"\n').review.tests, "commit")

    def test_another_value_is_a_configuration_error(self):
        # AC-1
        problems = self.problems('tests = "branch"\n')
        self.assertTrue(
            any("review.tests must be" in p and "branch" in p for p in problems), problems
        )

    def test_archive_with_an_empty_archive_dir_names_both_keys(self):
        # AC-1
        problems = self.problems('tests = "archive"\n', '\n[paths]\narchive_dir = ""\n')
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("review.tests", problems[0])
        self.assertIn("paths.archive_dir", problems[0])
        self.parse('tests = "commit"\n', '\n[paths]\narchive_dir = ""\n')  # fine

    def test_defaults_and_template_carry_the_key(self):
        # AC-1, AC-9
        self.assertRegex(read(os.path.join(ROOT, "defaults.toml")), r'\ntests\s*=\s*"commit"')
        self.assertRegex(
            read(os.path.join(ROOT, "templates", "revali.toml")), r'\ntests\s*=\s*"commit"'
        )


class FirstRound(ArchiveCase):
    def test_an_approved_round_archives_and_commits_nothing(self):
        # AC-2, AC-4, AC-8
        head = self.head()
        out = self.run_ok(approving(**{FILE: TEST_REVIEW_MUL, HELPER: "{}\n"}))
        self.assertEqual(self.head(), head)  # HEAD unchanged
        self.assertEqual(self.status(), "")  # tree clean
        self.assertEqual(self.trailer_commits(), [])  # no test commit
        self.assertFalse(self.exists(FILE))
        self.assertEqual(read(self.archived(FILE)), TEST_REVIEW_MUL)
        self.assertEqual(read(self.archived(HELPER)), "{}\n")
        self.assertEqual(archived_files(self.rdir()), BOTH)
        state = self.state()
        self.assertEqual(state.test_files, BOTH)
        self.assertEqual(state.test_commits, [])
        self.assertEqual(state.pending_test_files, [])
        self.assertEqual(state.tests_mode, "archive")  # AC-7
        self.assertEqual(state.rounds[-1]["test_commit"], "")
        # lint and smoke saw the files in the tree: the smoke run read them from there
        smoke = self.runner_calls("smoke-r1")
        self.assertEqual(len(smoke), 1, smoke)
        self.assertEqual(smoke[0]["extra_files"], BOTH)
        # validation sends the archived files as extra files at their repository paths
        validate = self.runner_calls("validate-r1")
        self.assertEqual(len(validate), 1, validate)
        self.assertEqual(validate[0]["extra_files"], BOTH)
        # HEAD is still the baseline commit: the existing suite is not rerun
        self.assertNotIn("test", validate[0]["steps"])
        self.assertIn("new_test", validate[0]["steps"])
        self.assertIn("  tests archived: %s" % ", ".join(BOTH), out)
        self.assertNotIn("tests landing", out)
        self.assertEqual(self.remote_tip(), head)  # the branch was pushed, nothing on top

    def test_a_test_dir_with_no_tracked_files_survives_the_round(self):
        # AC-2: the reviewer's files move out, the directory the config names stays
        text = self.read("revali.toml").replace('test_dir = "tests"', 'test_dir = "acceptance"')
        self.write("revali.toml", text)
        self.commit_all("tests in acceptance/")
        path = "acceptance/test_review_mul.py"
        self.run_ok(approving(**{path: TEST_REVIEW_MUL}))
        self.assertTrue(os.path.isdir(os.path.join(self.repo, "acceptance")))
        self.assertFalse(self.exists(path))
        self.assertEqual(self.status(), "")
        self.assertEqual(archived_files(self.rdir()), [path])
        self.assertEqual(read(self.archived(path)), TEST_REVIEW_MUL)

    def test_commit_mode_is_unchanged(self):
        # AC-1 (default behaviour), AC-8
        self.switch_mode("commit")
        out = self.run_ok(approving(**{FILE: TEST_REVIEW_MUL}))
        self.assertEqual(len(self.trailer_commits()), 1)
        self.assertIn("  tests landing: " + FILE, out)
        self.assertNotIn("archived test file(s) under", out)  # no archive, no note
        self.assertFalse(os.path.isdir(archive_dir(self.rdir())))
        state = self.state()
        self.assertEqual(state.tests_mode, "commit")
        self.assertEqual(len(state.test_commits), 1)

    def test_state_version_and_field(self):
        # AC-7
        self.assertGreaterEqual(STATE_VERSION, 6)
        self.assertEqual(State().tests_mode, "")


class LaterRounds(ArchiveCase):
    def test_the_next_round_gets_the_files_back_and_replaces_the_archive(self):
        # AC-3
        self.claude(requesting_changes(**{FILE: TEST_REVIEW_MUL, HELPER: "{}\n"}))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ACTION, out)
        self.assertEqual(self.status(), "")
        self.assertEqual(read(self.archived(FILE)), TEST_REVIEW_MUL)
        self.write("src/calc.py", self.read("src/calc.py") + "\n# negatives handled\n")
        self.commit_all("fix")
        updated = TEST_REVIEW_MUL + "\n# updated by round 2\n"
        entry = approving(**{FILE: updated, SECOND: SECOND_TEXT})
        entry["delete_files"] = [HELPER]
        out = self.run_ok(entry)
        self.assertIn("placed 2 archived test file(s) back", out)
        prompt = self.fake_calls("claude")[-1]["prompt"]
        self.assertIn("- " + FILE, prompt)  # listed as its own earlier file
        self.assertEqual(read(self.archived(FILE)), updated)  # updated
        self.assertEqual(read(self.archived(SECOND)), SECOND_TEXT)  # added
        self.assertFalse(os.path.exists(self.archived(HELPER)))  # deleted
        self.assertFalse(os.path.isdir(os.path.dirname(self.archived(HELPER))))
        self.assertEqual(self.state().test_files, [FILE, SECOND])
        self.assertEqual(self.status(), "")
        self.assertEqual(self.trailer_commits(), [])
        validate = self.runner_calls("validate-r2")
        self.assertEqual(validate[-1]["extra_files"], [FILE, SECOND])

    def test_a_needs_info_round_is_archived_too(self):
        # AC-3
        self.claude(asking(**{FILE: TEST_REVIEW_MUL}))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ACTION, out)
        self.assertEqual(self.status(), "")
        self.assertFalse(self.exists(FILE))
        self.assertEqual(read(self.archived(FILE)), TEST_REVIEW_MUL)
        state = self.state()
        self.assertEqual(state.pending_test_files, [])
        self.assertEqual(state.test_files, [FILE])
        self.assertNotIn("do not commit", out)
        self.write(".revali/feature__mul/response-1.md", "- integers: any Python int\n")
        self.run_ok(approving(**{FILE: TEST_REVIEW_MUL}))
        self.assertEqual(self.status(), "")
        self.assertEqual(self.trailer_commits(), [])

    def test_a_rewrite_does_not_restart_the_review(self):
        # AC-9 (documented behaviour): no reviewer commit to lose, the push is forced
        self.claude(requesting_changes(**{FILE: TEST_REVIEW_MUL}))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ACTION, out)
        git(["commit", "-q", "--amend", "--no-edit", "-m", "Add mul (amended)"], self.repo)
        out = self.run_ok(approving(**{FILE: TEST_REVIEW_MUL}))
        self.assertIn("history was rewritten", out)
        self.assertNotIn("starts over", out)
        state = self.state()
        self.assertEqual(len(state.rounds), 2)
        self.assertEqual(state.fixes, 1)
        self.assertIn("review rounds: 2, fix cycles: 1", out)


class Interrupted(ArchiveCase):
    def setUp(self):
        super().setUp()
        self.claude(requesting_changes(**{FILE: TEST_REVIEW_MUL, HELPER: "{}\n"}))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ACTION, out)
        self.write("src/calc.py", self.read("src/calc.py") + "\n# negatives handled\n")
        self.commit_all("fix")

    def assert_archive_untouched(self):
        self.assertEqual(read(self.archived(FILE)), TEST_REVIEW_MUL)
        self.assertEqual(read(self.archived(HELPER)), "{}\n")
        self.assertEqual(self.state().test_files, BOTH)

    def test_a_failed_session_removes_the_copies(self):
        # AC-5
        entry = claude_entry(is_error=True)
        entry["write_files"] = {SECOND: SECOND_TEXT}  # a new draft the failed session left
        self.claude(entry)
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertIn("removed 2 working-tree copies", out)
        self.assertEqual(self.status(), "")
        self.assert_archive_untouched()
        self.assertFalse(os.path.exists(self.archived(SECOND)))

    def test_a_file_the_archive_cannot_take_stops_the_run(self):
        # AC-5: an OSError while archiving (a file held open on Windows) is a Stop naming the
        # path, not a traceback; every file the reviewer left goes, a new one off the pattern
        # too, and the next run picks up from the archive
        real_move = shutil.move

        def move(src, dst, *args, **kwargs):
            if dst.replace("\\", "/").endswith(HELPER):  # sorted first: nothing moved yet
                raise PermissionError(13, "held open by an editor", dst)
            return real_move(src, dst, *args, **kwargs)

        new = "tests/review_data/zero.json"
        self.claude(
            approving(**{FILE: TEST_REVIEW_MUL + "\n# round 2\n", HELPER: "{}\n", new: "[]\n"})
        )
        with mock.patch("revali.testarchive.shutil.move", side_effect=move):
            code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertIn("could not archive the reviewer's test file %s" % HELPER, out)
        self.assertIn("held open by an editor", out)
        self.assertNotIn("Traceback", out)
        self.assertIn("removed 3 working-tree copies", out)
        self.assertEqual(self.status(), "")
        self.assertFalse(self.exists(new))
        self.assert_archive_untouched()
        state = self.state()
        self.assertFalse(state.reviewer_running)
        self.assertEqual(state.placed_test_files, [])
        out = self.run_ok(approving(**{FILE: TEST_REVIEW_MUL + "\n# round 3\n", HELPER: "{}\n"}))
        self.assertIn("archived 2 test file(s)", out)
        self.assertEqual(read(self.archived(FILE)), TEST_REVIEW_MUL + "\n# round 3\n")
        self.assertEqual(self.state().test_files, BOTH)

    def test_a_killed_session_is_cleaned_by_the_next_run(self):
        # AC-5, AC-6
        self.write(FILE, TEST_REVIEW_MUL + "\n# half updated\n")
        self.write(HELPER, "{}\n")
        state = self.state()
        state.reviewer_running = True  # what a session killed mid-round leaves behind
        state.placed_test_files = BOTH  # place_back records them before it copies
        state.set_stage(self.rdir(), "review", "killed", EXIT_ERROR)
        out = self.run_ok(approving(**{FILE: TEST_REVIEW_MUL + "\n# round 2\n"}))
        self.assertIn("removed 2 working-tree copies", out)
        self.assertEqual(self.status(), "")
        self.assertEqual(read(self.archived(FILE)), TEST_REVIEW_MUL + "\n# round 2\n")
        self.assertEqual(read(self.archived(HELPER)), "{}\n")  # placed back, kept, archived

    def test_reset_removes_the_copies_and_keeps_the_archive(self):
        # AC-5, AC-6
        self.write(FILE, TEST_REVIEW_MUL + "\n# half updated\n")
        self.write(HELPER, "{}\n")
        state = self.state()
        state.reviewer_running = True
        state.placed_test_files = BOTH
        state.set_stage(self.rdir(), "review", "killed", EXIT_ERROR)
        code, out = run_cli(["reset"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertEqual(self.status(), "")
        self.assertEqual(read(self.archived(FILE)), TEST_REVIEW_MUL)
        self.assertEqual(read(self.archived(HELPER)), "{}\n")
        self.assertIsNone(self.state())
        # the next run rebuilds the list from the archive and places both files back
        out = self.run_ok(approving(**{FILE: TEST_REVIEW_MUL, HELPER: "{}\n"}))
        self.assertIn("recovered 2: %s" % ", ".join(BOTH), out)
        self.assertIn("placed 2 archived test file(s) back", out)
        self.assertEqual(self.state().test_files, BOTH)

    def test_an_occupied_archived_path_stops_the_run_and_keeps_the_occupant(self):
        # AC-5: the only occupant preflight lets through is a gitignored file; place_back
        # refuses before copying anything, and the cleanup that follows deletes nothing
        self.write(".gitignore", "tests/review_data/\n")
        self.commit_all("ignore the data directory")
        self.write(HELPER, "the author's data\n")
        self.claude(approving(**{FILE: TEST_REVIEW_MUL}))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertIn("%s already exists in the working tree" % HELPER, out)
        self.assertNotIn("removed", out)
        self.assertEqual(self.read(HELPER), "the author's data\n")
        self.assertFalse(self.exists(FILE))  # nothing was placed
        self.assert_archive_untouched()
        state = self.state()
        self.assertEqual(state.placed_test_files, [])
        self.assertFalse(state.reviewer_running)


class Pruning(ArchiveCase):
    def test_the_cleanup_prunes_the_directories_it_emptied(self):
        # fix/archive-followups AC-1: a directory that held only placed copies goes, one that
        # still holds the author's file stays, test_dir stays; after a failed session and reset
        keep = "tests/review_data/keep.json"
        extra = "tests/z_data/extra.json"
        self.write(keep, "{}\n")
        self.commit_all("the author's data file")
        self.claude(requesting_changes(**{FILE: TEST_REVIEW_MUL, HELPER: "{}\n", extra: "[]\n"}))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ACTION, out)
        self.assertFalse(self.isdir("tests/z_data"))  # take prunes after a finished round
        self.write("src/calc.py", self.read("src/calc.py") + "\n# negatives handled\n")
        self.commit_all("fix")
        self.claude(claude_entry(is_error=True))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertIn("removed 3 working-tree copies", out)
        self.assertFalse(self.isdir("tests/z_data"))
        self.assertTrue(self.isdir("tests/review_data"))
        self.assertEqual(self.read(keep), "{}\n")
        self.assertTrue(self.isdir("tests"))
        self.assertEqual(self.status(), "")
        placed = sorted([FILE, HELPER, extra])
        for rel in placed:  # a killed round, then reset
            self.write(rel, "x\n")
        state = self.state()
        state.reviewer_running = True
        state.placed_test_files = placed
        state.set_stage(self.rdir(), "review", "killed", EXIT_ERROR)
        code, out = run_cli(["reset"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertFalse(self.isdir("tests/z_data"))
        self.assertTrue(self.isdir("tests/review_data"))
        self.assertTrue(self.isdir("tests"))
        self.assertEqual(self.status(), "")
        self.assertEqual(archived_files(self.rdir()), placed)


class Status(ArchiveCase):
    def test_status_prints_the_recorded_mode(self):
        # fix/archive-followups AC-2
        code, out = run_cli(["status"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertNotIn("tests:", out)  # no round yet, no recorded mode
        self.claude(requesting_changes(**{FILE: TEST_REVIEW_MUL}))
        self.assertEqual(run_cli(["run", "--foreground"])[0], EXIT_ACTION)
        code, out = run_cli(["status"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("\ntests: archive\n", out)
        self.assertIn("\nround: 1, fixes: 0,", out)  # the rounds recorded, not a dead field

    def test_status_prints_commit_for_a_state_from_before_the_key(self):
        # round 1 F1: a state with rounds and no recorded mode runs as commit, status says so
        self.claude(requesting_changes(**{FILE: TEST_REVIEW_MUL}))
        self.assertEqual(run_cli(["run", "--foreground"])[0], EXIT_ACTION)
        state = self.state()
        state.tests_mode = ""
        state.save(self.rdir())
        code, out = run_cli(["status"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("\ntests: commit\n", out)

    def test_status_prints_commit_for_the_default_mode(self):
        # fix/archive-followups AC-2
        self.switch_mode("commit")
        self.claude(requesting_changes(**{FILE: TEST_REVIEW_MUL}))
        self.assertEqual(run_cli(["run", "--foreground"])[0], EXIT_ACTION)
        code, out = run_cli(["status"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("\ntests: commit\n", out)


class Ownership(ArchiveCase):
    def test_a_path_head_tracks_is_the_authors(self):
        # AC-6
        self.claude(requesting_changes(**{FILE: TEST_REVIEW_MUL}))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ACTION, out)
        self.write(FILE, "# the author's own file under the reviewer's name\n")
        self.write("src/calc.py", self.read("src/calc.py") + "\n# negatives handled\n")
        self.commit_all("author takes the name")
        out = self.run_ok(approving(**{SECOND: SECOND_TEXT}))
        self.assertIn("tracked in HEAD now, so they are the author's", out)
        self.assertIn(FILE, out)
        self.assertFalse(os.path.exists(self.archived(FILE)))
        self.assertEqual(self.state().test_files, [SECOND])
        prompt = self.fake_calls("claude")[-1]["prompt"]
        self.assertIn("not yours", prompt)  # listed as an existing file
        self.assertEqual(self.read(FILE), "# the author's own file under the reviewer's name\n")

    def test_a_dry_run_removes_nothing_from_the_archive(self):
        # AC-6 on `run --dry-run`: the taken-over path is reported, the archive is not touched
        self.claude(requesting_changes(**{FILE: TEST_REVIEW_MUL, HELPER: "{}\n"}))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ACTION, out)
        self.write(HELPER, "the author's data\n")
        self.write("src/calc.py", self.read("src/calc.py") + "\n# negatives handled\n")
        self.commit_all("author takes the path")
        code, out = run_cli(["run", "--foreground", "--dry-run"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("dry run: would be removed from", out)
        self.assertIn(HELPER, out)
        self.assertEqual(archived_files(self.rdir()), BOTH)
        out = self.run_ok(approving(**{FILE: TEST_REVIEW_MUL}))
        self.assertIn("removed from", out)
        self.assertEqual(archived_files(self.rdir()), [FILE])
        self.assertEqual(self.state().test_files, [FILE])

    def test_the_reviewer_may_not_modify_the_authors_file(self):
        # AC-6: the protected-file rule applies to the taken-over path
        self.claude(requesting_changes(**{FILE: TEST_REVIEW_MUL}))
        self.assertEqual(run_cli(["run", "--foreground"])[0], EXIT_ACTION)
        self.write(FILE, "# the author's\n")
        self.commit_all("author takes the name")
        bad = approving(**{FILE: TEST_REVIEW_MUL})
        self.claude(bad, bad)
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertIn("modified existing test file(s) it did not write", out)
        self.assertEqual(self.read(FILE), "# the author's\n")
        self.assertEqual(self.status(), "")


class ModeSwitch(ArchiveCase):
    def test_switching_to_commit_is_refused_before_the_push(self):
        # AC-7
        self.claude(requesting_changes(**{FILE: TEST_REVIEW_MUL}))
        self.assertEqual(run_cli(["run", "--foreground"])[0], EXIT_ACTION)
        self.switch_mode("commit")
        tip = self.remote_tip()
        self.assertNotEqual(tip, self.head())
        self.claude(approving(**{FILE: TEST_REVIEW_MUL}))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ACTION, out)
        self.assertIn('[review] tests is "commit"', out)
        self.assertIn('ran with "archive"', out)
        self.assertEqual(self.remote_tip(), tip)  # nothing pushed
        self.assertEqual(self.status(), "")
        self.assertEqual(read(self.archived(FILE)), TEST_REVIEW_MUL)
        self.switch_mode("archive")
        self.run_ok(approving(**{FILE: TEST_REVIEW_MUL}))

    def test_commit_mode_reports_a_stale_archive_after_a_reset(self):
        # fix/archive-followups AC-3: the files are named, nothing is deleted
        self.claude(requesting_changes(**{FILE: TEST_REVIEW_MUL}))
        self.assertEqual(run_cli(["run", "--foreground"])[0], EXIT_ACTION)
        self.assertEqual(run_cli(["reset"])[0], EXIT_OK)
        self.switch_mode("commit")
        out = self.run_ok(approving(**{FILE: TEST_REVIEW_MUL}))
        self.assertIn(
            "1 archived test file(s) under .revali/feature__mul/tests from earlier "
            "archive-mode rounds; commit mode does not use them",
            out,
        )
        self.assertIn("archive_dir at merge unless you delete that directory: " + FILE, out)
        self.assertEqual(read(self.archived(FILE)), TEST_REVIEW_MUL)
        self.assertEqual(len(self.trailer_commits()), 1)  # commit mode as usual

    def test_switching_to_archive_is_refused_and_an_old_state_counts_as_commit(self):
        # AC-7
        self.switch_mode("commit")
        self.claude(requesting_changes(**{FILE: TEST_REVIEW_MUL}))
        self.assertEqual(run_cli(["run", "--foreground"])[0], EXIT_ACTION)
        state = self.state()
        state.tests_mode = ""  # a state written before the key existed
        state.save(self.rdir())
        self.switch_mode("archive")
        self.claude(approving(**{FILE: TEST_REVIEW_MUL}))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ACTION, out)
        self.assertIn('[review] tests is "archive"', out)
        self.assertIn('ran with "commit"', out)

    def test_a_rewrite_that_restarts_the_review_clears_the_mode(self):
        # AC-7
        self.switch_mode("commit")
        self.claude(requesting_changes(**{FILE: TEST_REVIEW_MUL}))
        self.assertEqual(run_cli(["run", "--foreground"])[0], EXIT_ACTION)
        self.assertEqual(self.state().tests_mode, "commit")
        git(["reset", "-q", "--hard", "HEAD~1"], self.repo)  # drops the reviewer's commit
        self.write("src/calc.py", self.read("src/calc.py") + "\n# negatives handled\n")
        self.commit_all("fix")
        self.switch_mode("archive")
        out = self.run_ok(approving(**{FILE: TEST_REVIEW_MUL}))
        self.assertIn("starts over", out)
        self.assertEqual(self.state().tests_mode, "archive")
        self.assertEqual(self.trailer_commits(), [])
        self.assertEqual(read(self.archived(FILE)), TEST_REVIEW_MUL)


class ValidationAndMerge(ArchiveCase):
    def test_the_base_rerun_sends_the_archived_files(self):
        # AC-4
        self.runner_scenario(
            {"default": 0, "results": {"validate-r1": {"new_test": 1}, "base-r1": {"new_test": 0}}}
        )
        diagnosis = {
            "summary": "the product test fails on the branch only.",
            "cause": "code",
            "introduced_by": "branch",
            "failures": [],
            "recommendation": "fix mul",
        }
        self.claude(
            approving(**{FILE: TEST_REVIEW_MUL}), claude_entry(diagnosis, write_tests=False)
        )
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ACTION, out)
        self.assertIn("validation 1 FAILED", out)
        base = self.runner_calls("base-r1")
        self.assertEqual(len(base), 1, self.fake_calls("runner"))
        self.assertEqual(base[0]["extra_files"], [FILE])
        self.assertEqual(self.status(), "")
        self.assertEqual(read(self.archived(FILE)), TEST_REVIEW_MUL)

    def test_merge_says_archived_and_moves_the_tests(self):
        # AC-8
        self.run_ok(approving(**{FILE: TEST_REVIEW_MUL}))
        code, out = run_cli(["merge"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("  tests archived: " + FILE, out)
        self.assertNotIn("tests landed", out)
        dest = os.path.join(self.home, "archive", "me__sample", "7-feature__mul")
        self.assertEqual(read(os.path.join(dest, "tests", FILE)), TEST_REVIEW_MUL)
        self.assertFalse(os.path.exists(self.rdir()))

    def test_merge_in_commit_mode_still_says_landed(self):
        # AC-8
        self.switch_mode("commit")
        self.run_ok(approving(**{FILE: TEST_REVIEW_MUL}))
        code, out = run_cli(["merge"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("  tests landed: " + FILE, out)


class Helper(unittest.TestCase):
    def test_short_shas(self):
        # AC-10
        self.assertEqual(short_shas(["a" * 40, "b" * 40]), "a" * 10 + ", " + "b" * 10)
        self.assertEqual(short_shas([]), "")
        source = read(os.path.join(ROOT, "revali", "review.py"))
        self.assertEqual(source.count("c[:10] for c in"), 0)


class Docs(unittest.TestCase):
    def test_the_docs_describe_the_mode(self):
        # AC-9
        for rel in (
            ("docs", "configuration.md"),
            ("docs", "files.md"),
            ("docs", "side-effects.md"),
            ("README.md",),
            ("templates", "revali.toml"),
        ):
            text = read(os.path.join(ROOT, *rel))
            self.assertIn('tests = "archive"', text, rel)
            self.assertIn(".revali/<branch>/tests/", text, rel)
        config = " ".join(read(os.path.join(ROOT, "docs", "configuration.md")).split())
        self.assertIn("never commits a test file", config)
        self.assertIn("does not restart the review", config)
        self.assertIn("cannot change while a branch has review rounds", config)


if __name__ == "__main__":
    unittest.main()
