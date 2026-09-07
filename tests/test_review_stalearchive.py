"""fix/archive-followups AC-3: a commit-mode run that finds a non-empty
`.revali/<branch>/tests/` (files archived by earlier archive-mode rounds that a `reset`
dropped) logs one `run` line naming the count and the directory, says commit mode does not
use them and that they travel to `archive_dir` at merge unless deleted, and deletes nothing.
No line when the directory is absent or empty, none in archive mode."""

import os
import re
import unittest

from revali import EXIT_ACTION, EXIT_OK
from revali.state import State
from revali.testarchive import archive_dir, archived_files
from tests.helpers import TEST_REVIEW_MUL, RepoCase, approve_response, claude_entry, run_cli

FILE = "tests/test_review_mul.py"
DATA = "tests/review_data/mul.json"
BOTH = {FILE: TEST_REVIEW_MUL, DATA: "{}\n"}
ARCHIVE = ".revali/feature__mul/tests"
NOTE = re.compile(
    r"^\[\d\d:\d\d:\d\d\] run: (\d+) archived test file\(s\) under (\S+) .*$", re.MULTILINE
)


def read(path):
    with open(path, "r", encoding="utf-8", newline="") as fh:
        return fh.read()


def approving(files):
    tests = [
        {"path": p, "purpose": "acceptance", "covers": ["AC-1", "AC-2"], "expected": "per AC"}
        for p in files
        if p.endswith(".py")
    ]
    entry = claude_entry(approve_response(tests=tests), write_tests=False)
    entry["write_files"] = dict(files)
    return entry


def requesting_changes(files):
    entry = approving(files)
    entry["structured_output"].update(
        verdict="CHANGES_REQUESTED",
        findings=[
            {
                "id": "F1",
                "file": "src/calc.py",
                "line": 3,
                "severity": "high",
                "kind": "correctness",
                "text": "mul ignores negative numbers",
                "suggestion": "handle them",
            }
        ],
    )
    return entry


def notes(out):
    """(count, directory, whole line) of every stale-archive note in the run's output."""
    return [(int(m.group(1)), m.group(2), m.group(0)) for m in NOTE.finditer(out)]


class StaleArchiveCase(RepoCase):
    def setUp(self):
        super().setUp()
        os.environ["REVALI_POLL_SECONDS"] = "0.01"

    def set_mode(self, mode):
        text = self.read("revali.toml")
        if "\ntests = " in text:
            text = "".join(ln for ln in text.splitlines(True) if not ln.startswith("tests = "))
        key = 'exclude = ["*.lock"]\n'
        self.write("revali.toml", text.replace(key, key + 'tests = "%s"\n' % mode))
        self.commit_all("tests mode %s" % mode)

    def archived(self, rel):
        return os.path.join(archive_dir(self.rdir()), rel)

    def run_round(self, entry, expect):
        self.claude(entry)
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, expect, out)
        return out

    def fix(self):
        self.write("src/calc.py", self.read("src/calc.py") + "\n# negatives handled\n")
        self.commit_all("fix")


class CommitModeWithAStaleArchive(StaleArchiveCase):
    def test_after_archive_rounds_and_a_reset(self):
        # AC-3: the realistic path: an archive-mode round, `reset`, the key switched
        self.set_mode("archive")
        self.run_round(requesting_changes(BOTH), EXIT_ACTION)
        self.assertEqual(run_cli(["reset"])[0], EXIT_OK)
        self.set_mode("commit")
        out = self.run_round(approving({FILE: TEST_REVIEW_MUL}), EXIT_OK)
        found = notes(out)
        self.assertEqual(len(found), 1, out)
        count, where, line = found[0]
        self.assertEqual(count, 2)
        self.assertEqual(where, ARCHIVE)
        self.assertIn("commit mode does not use them", line)
        self.assertIn("archive_dir", line)
        self.assertIn("merge", line)
        self.assertIn("delete", line)
        self.assertIn(FILE, line)
        self.assertIn(DATA, line)
        # nothing deleted, and the round went the commit-mode way
        self.assertEqual(archived_files(self.rdir()), sorted([DATA, FILE]))
        self.assertEqual(read(self.archived(FILE)), TEST_REVIEW_MUL)
        self.assertEqual(read(self.archived(DATA)), "{}\n")
        self.assertEqual(State.load(self.rdir()).tests_mode, "commit")

    def test_the_note_repeats_on_every_run_and_still_deletes_nothing(self):
        # AC-3: a later commit-mode round finds the archive untouched and says it again, once
        self.set_mode("commit")
        self.write(ARCHIVE + "/" + FILE, "stale\n")
        out = self.run_round(requesting_changes({FILE: TEST_REVIEW_MUL}), EXIT_ACTION)
        self.assertEqual(len(notes(out)), 1, out)
        self.fix()
        out = self.run_round(approving({FILE: TEST_REVIEW_MUL}), EXIT_OK)
        found = notes(out)
        self.assertEqual(len(found), 1, out)
        self.assertEqual(found[0][0], 1)
        self.assertEqual(read(self.archived(FILE)), "stale\n")

    def test_every_file_in_the_directory_counts(self):
        # AC-3: the count is what the directory holds, paths with forward slashes
        self.set_mode("commit")
        self.write(ARCHIVE + "/other/thing.txt", "x\n")
        self.write(ARCHIVE + "/" + FILE, "stale\n")
        out = self.run_round(approving({FILE: TEST_REVIEW_MUL}), EXIT_OK)
        found = notes(out)
        self.assertEqual(len(found), 1, out)
        self.assertEqual(found[0][0], 2)
        self.assertIn("other/thing.txt", found[0][2])
        self.assertEqual(read(self.archived("other/thing.txt")), "x\n")
        self.assertEqual(read(self.archived(FILE)), "stale\n")


class NoNote(StaleArchiveCase):
    def test_no_directory(self):
        # AC-3
        self.set_mode("commit")
        out = self.run_round(approving({FILE: TEST_REVIEW_MUL}), EXIT_OK)
        self.assertEqual(notes(out), [], out)
        self.assertFalse(os.path.isdir(archive_dir(self.rdir())))

    def test_an_empty_directory(self):
        # AC-3: absent or empty are the same to the author
        self.set_mode("commit")
        os.makedirs(archive_dir(self.rdir()))
        out = self.run_round(approving({FILE: TEST_REVIEW_MUL}), EXIT_OK)
        self.assertEqual(notes(out), [], out)

    def test_an_empty_subdirectory(self):
        # AC-3: no file, no note
        self.set_mode("commit")
        os.makedirs(os.path.join(archive_dir(self.rdir()), "tests", "review_data"))
        out = self.run_round(approving({FILE: TEST_REVIEW_MUL}), EXIT_OK)
        self.assertEqual(notes(out), [], out)

    def test_archive_mode_never_says_it(self):
        # AC-3: round 2 in archive mode finds the archive non-empty; that is its own
        self.set_mode("archive")
        out = self.run_round(requesting_changes(BOTH), EXIT_ACTION)
        self.assertEqual(notes(out), [], out)
        self.fix()
        out = self.run_round(approving(BOTH), EXIT_OK)
        self.assertEqual(notes(out), [], out)
        self.assertIn("placed 2 archived test file(s) back", out)


if __name__ == "__main__":
    unittest.main()
