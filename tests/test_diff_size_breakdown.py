"""fix/diff-size-breakdown (M3.44): a refusal over `[review] max_diff_lines` names the largest
counted files and the files the count left out, with the reason.

The fixture's branch changes src/calc.py (4 added lines); its test pattern is
test_review_{topic}.py and its `[review] exclude` is ["*.lock"].
"""

import subprocess
import unittest

from revali import EXIT_ACTION
from revali.preflight import Stop, preflight, size_breakdown
from tests.helpers import RepoCase, git


def numbered(n, tag):
    return "".join("%s_%d = %d\n" % (tag, i, i) for i in range(n))


def git_numstat_total(repo):
    """Lines of `git diff --numstat` against origin/main, read here without revali's code."""
    out = subprocess.run(
        ["git", "diff", "-M", "--numstat", "origin/main...HEAD"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    total = {}
    for line in out.splitlines():
        added, deleted, path = line.split("\t", 2)
        total[path] = int(added) + int(deleted)
    return total


class _ListLog:
    def __init__(self, lines):
        self.lines = lines

    def stage(self, stage, msg):
        self.lines.append("%s: %s" % (stage, msg))

    def detail(self, msg):
        pass


class Breakdown(RepoCase):
    def set_config(self, limit, exclude='["*.lock"]'):
        toml = self.read("revali.toml").replace(
            "max_diff_lines = 800", "max_diff_lines = %d" % limit
        )
        self.write("revali.toml", toml.replace('exclude = ["*.lock"]', "exclude = %s" % exclude))

    def add_many(self):
        """Six counted files of their own besides src/calc.py and revali.toml, one reviewer
        test, two excluded files (one of them also on the test pattern)."""
        for name, n in [("a", 60), ("b", 50), ("c", 40), ("e", 30), ("d", 30), ("f", 20)]:
            self.write("src/%s.py" % name, numbered(n, name))
        self.write("tests/test_review_size.py", numbered(25, "size"))
        self.write("poetry.lock", numbered(15, "lock"))
        self.write("tests/test_review_both.py", numbered(10, "both"))

    def refusal(self):
        with self.assertRaises(Stop) as cm:
            preflight(self.repo)
        self.assertEqual(cm.exception.exit_code, EXIT_ACTION)
        return cm.exception.message

    def test_over_the_limit_names_counted_and_left_out_files(self):
        """AC-1, AC-2, AC-3: revali.toml changes two lines (+2 -2), src/calc.py adds 4"""
        self.set_config(3, '["*.lock", "tests/test_review_both.py"]')
        self.add_many()
        self.commit_all("many files")
        self.assertEqual(
            self.refusal().split("\n"),
            [
                "diff is 238 lines (limit 3); split the change into smaller PRs",
                "counted, largest first:",
                "  60  src/a.py",
                "  50  src/b.py",
                "  40  src/c.py",
                "  30  src/d.py",
                "  30  src/e.py",
                "  ... and 3 more files, 28 lines",
                "left out of the count:",
                "  25  tests/test_review_size.py (reviewer test)",
                "  15  poetry.lock ([review] exclude)",
                "  10  tests/test_review_both.py ([review] exclude)",
            ],
        )

    def test_the_total_is_git_numstat_without_the_left_out_files(self):
        """AC-4: the number that decides is the same sum as before"""
        self.set_config(3, '["*.lock", "tests/test_review_both.py"]')
        self.add_many()
        self.commit_all("many files")
        stat = git_numstat_total(self.repo)
        for path in ["tests/test_review_size.py", "poetry.lock", "tests/test_review_both.py"]:
            del stat[path]
        first = self.refusal().split("\n")[0]
        self.assertEqual(
            first,
            "diff is %d lines (limit 3); split the change into smaller PRs" % sum(stat.values()),
        )

    def test_nothing_left_out_has_no_left_out_part(self):
        """AC-2, AC-3: revali.toml (+1 -1) and src/calc.py (+4)"""
        self.set_config(3)
        self.commit_all("tiny limit")
        self.assertEqual(
            self.refusal().split("\n"),
            [
                "diff is 6 lines (limit 3); split the change into smaller PRs",
                "counted, largest first:",
                "  4  src/calc.py",
                "  2  revali.toml",
            ],
        )

    def test_more_than_five_left_out_are_summarised(self):
        """AC-3"""
        self.set_config(3)
        for i, name in enumerate("abcdefg"):
            self.write("%s.lock" % name, numbered(7 - i, name))
        self.commit_all("locks")
        message = self.refusal()
        left = message.split("left out of the count:\n", 1)[1].split("\n")
        self.assertEqual(
            left,
            [
                "  7  a.lock ([review] exclude)",
                "  6  b.lock ([review] exclude)",
                "  5  c.lock ([review] exclude)",
                "  4  d.lock ([review] exclude)",
                "  3  e.lock ([review] exclude)",
                "  ... and 2 more files, 3 lines",
            ],
        )

    def test_a_rename_is_listed_under_its_new_name(self):
        """AC-2"""
        self.set_config(3)
        git(["mv", "tests/test_calc.py", "tests/test_arith.py"], self.repo)
        self.write(
            "tests/test_arith.py",
            self.read("tests/test_arith.py").replace("(2, 3), 5", "(2, 2), 4"),
        )
        self.commit_all("rename")
        message = self.refusal()
        self.assertIn("\n  2  tests/test_arith.py", message)
        self.assertNotIn("test_calc.py", message)

    def test_under_the_limit_logs_the_same_line_as_before(self):
        """AC-4: the limit stays, so revali.toml changes one line (+1 -1)"""
        self.set_config(800, '["*.lock", "tests/test_review_both.py"]')
        self.add_many()
        self.commit_all("many files")
        lines = []
        ctx = preflight(self.repo, log=_ListLog(lines))
        self.assertEqual(ctx.diff_lines, 236)
        self.assertIn("preflight: diff: 236 lines in 8 files (+3 excluded)", lines)
        self.assertEqual(
            sorted(ctx.excluded_files),
            ["poetry.lock", "tests/test_review_both.py", "tests/test_review_size.py"],
        )

    def test_plain_branch_counts_as_git_does(self):
        """AC-4: no reviewer test, no excluded file"""
        ctx = preflight(self.repo)
        self.assertEqual(ctx.diff_lines, sum(git_numstat_total(self.repo).values()))
        self.assertEqual(ctx.diff_lines, 4)
        self.assertEqual(ctx.excluded_files, [])


class BreakdownText(unittest.TestCase):
    def test_one_more_file_of_one_line_is_singular(self):
        counted = [(10 - i, "f%d" % i, "") for i in range(5)] + [(1, "z", "")]
        self.assertEqual(size_breakdown(counted, [])[-1], "  ... and 1 more file, 1 line")

    def test_equal_counts_go_by_path(self):
        out = size_breakdown([(3, "b", ""), (3, "a", ""), (5, "c", "")], [])
        self.assertEqual(out, ["counted, largest first:", "  5  c", "  3  a", "  3  b"])
