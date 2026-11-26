"""Reviewer acceptance tests for fix/diff-size-breakdown.

AC-1: the refusal's first line is unchanged (`diff is <N> lines (limit <L>); split the change
      into smaller PRs`, exit 2); what is new comes below it.
AC-2: below it, the counted files, largest first (equal counts by path), one per line with its
      count, at most five, then `... and <n> more files, <m> lines`; a rename under its new name.
AC-3: then the files the count left out, with their counts and the reason (`reviewer test`,
      `[review] exclude`; a file on both rules is `[review] exclude`), at most five, the rest
      summarised; no such part when nothing was left out.
AC-4: the total and the decision are the ones from before, and a run under the limit logs the
      same `diff: <N> lines in <F> files (+<K> excluded)` line.

The fixture's branch adds 4 lines to src/calc.py; its test pattern is test_review_{topic}.py and
its `[review] exclude` is ["*.lock"]. Changing the limit in revali.toml is one line replaced, so
that file counts 2. The headings of the two lists are not in the AC and are not read here: an
entry is the one line below the first that names the path, and its only number is the count.
"""

import re

from revali import EXIT_ACTION
from revali.preflight import Stop, preflight
from tests.helpers import RepoCase, git, run_cli

FIRST = "diff is %d lines (limit %d); split the change into smaller PRs"
REVIEWER = "reviewer test"
EXCLUDED = "[review] exclude"


def rows(n, tag):
    return "".join("%s_row_%d = %d\n" % (tag, i, i) for i in range(n))


def named(path):
    return re.compile(r"(?<![\w./-])%s(?![\w./-])" % re.escape(path))


class _ListLog:
    def __init__(self, lines):
        self.lines = lines

    def stage(self, stage, msg):
        self.lines.append("%s: %s" % (stage, msg))

    def detail(self, msg):
        pass


class SizeRefusal(RepoCase):
    def set_limit(self, limit):
        toml, found = re.subn(
            r"max_diff_lines = \d+", "max_diff_lines = %d" % limit, self.read("revali.toml")
        )
        self.assertEqual(found, 1, toml)
        self.write("revali.toml", toml)

    def refusal(self):
        with self.assertRaises(Stop) as caught:
            preflight(self.repo)
        self.assertEqual(caught.exception.exit_code, EXIT_ACTION, caught.exception.message)
        return caught.exception.message.split("\n")

    def where(self, lines, path):
        """Index, below the first line, of the one line that names `path`."""
        hits = [i for i, line in enumerate(lines) if i and named(path).search(line)]
        self.assertEqual(len(hits), 1, "%s in %r" % (path, lines))
        return hits[0]

    def assert_entry(self, lines, path, count):
        """One line below the first names `path`, and the only number on it is `count`."""
        i = self.where(lines, path)
        rest = named(path).sub("", lines[i])
        self.assertEqual(re.findall(r"\d+", rest), [str(count)], lines[i])
        return i

    def test_largest_counted_files_then_the_left_out_ones(self):
        """AC-1, AC-2, AC-3, AC-4: eight counted files (270 lines in six new ones, 4 in
        src/calc.py, 2 in revali.toml), a 33-line reviewer test and a 12-line lock file. The
        total is the counted 276; the five largest are named in order, the other three are
        `... and 3 more files, 26 lines` (20 + 4 + 2); the two left out follow, larger first."""
        sizes = [
            ("alpha", 70),
            ("bravo", 60),
            ("charlie", 50),
            ("delta", 40),
            ("echo", 30),
            ("foxtrot", 20),
        ]
        for name, n in sizes:
            self.write("src/%s.py" % name, rows(n, name))
        self.write("tests/test_review_extra.py", rows(33, "extra"))
        self.write("deps.lock", rows(12, "lock"))
        self.set_limit(100)
        self.commit_all("a large change")
        lines = self.refusal()
        self.assertEqual(lines[0], FIRST % (276, 100))
        shown = [self.assert_entry(lines, "src/%s.py" % name, n) for name, n in sizes[:5]]
        self.assertEqual(shown, sorted(shown), lines)
        message = "\n".join(lines)
        for path in ("src/foxtrot.py", "src/calc.py", "revali.toml"):
            self.assertNotIn(path, message)
        more = [i for i, line in enumerate(lines) if "more file" in line]
        self.assertEqual(len(more), 1, lines)
        self.assertEqual(lines[more[0]].strip(), "... and 3 more files, 26 lines")
        self.assertGreater(more[0], shown[-1])
        test = self.assert_entry(lines, "tests/test_review_extra.py", 33)
        lock = self.assert_entry(lines, "deps.lock", 12)
        self.assertGreater(test, more[0])
        self.assertGreater(lock, test)
        self.assertIn(REVIEWER, lines[test])
        self.assertNotIn(EXCLUDED, lines[test])
        self.assertIn(EXCLUDED, lines[lock])
        self.assertNotIn(REVIEWER, lines[lock])
        for i in shown:
            self.assertNotIn(REVIEWER, lines[i])
            self.assertNotIn(EXCLUDED, lines[i])

    def test_five_counted_files_equal_counts_by_path_and_nothing_left_out(self):
        """AC-2, AC-3, AC-4: the plain fixture branch counts 4 lines and passes. With three
        more files (9, 9 and 7 lines) and a limit of 3 the refusal names all five counted
        files, the two of 9 lines by path, has no summary line (nothing beyond five) and ends
        with the last counted file: nothing was left out, so nothing follows."""
        plain = preflight(self.repo)
        self.assertEqual(plain.diff_lines, 4)
        self.assertEqual(plain.excluded_files, [])
        self.write("src/zulu.py", rows(9, "zulu"))
        self.write("src/alpha.py", rows(9, "alpha"))
        self.write("src/mike.py", rows(7, "mike"))
        self.set_limit(3)
        self.commit_all("three more files")
        lines = self.refusal()
        self.assertEqual(lines[0], FIRST % (31, 3))
        order = [
            self.assert_entry(lines, path, count)
            for path, count in [
                ("src/alpha.py", 9),
                ("src/zulu.py", 9),
                ("src/mike.py", 7),
                ("src/calc.py", 4),
                ("revali.toml", 2),
            ]
        ]
        self.assertEqual(order, sorted(order), lines)
        self.assertEqual(order[-1], len(lines) - 1, lines)
        below = "\n".join(lines[1:])
        for word in (REVIEWER, EXCLUDED, "more file"):
            self.assertNotIn(word, below)
        self.assertEqual(len([line for line in lines[1:] if re.search(r"\d", line)]), 5, lines)

    def test_a_file_on_both_rules_is_shown_as_excluded(self):
        """AC-3: the branch adds tests/test_review_both.py (11 lines), which the test pattern
        and an exclude pattern both match; it is listed once, as `[review] exclude`. The
        total is src/calc.py (4) plus the two replaced lines of revali.toml (4)."""
        toml = self.read("revali.toml")
        old = 'exclude = ["*.lock"]'
        new = 'exclude = ["*.lock", "tests/test_review_both.py"]'
        self.assertIn(old, toml)
        self.write("revali.toml", toml.replace(old, new))
        self.set_limit(3)
        self.write("tests/test_review_both.py", rows(11, "both"))
        self.commit_all("a file both rules leave out")
        lines = self.refusal()
        self.assertEqual(lines[0], FIRST % (8, 3))
        both = self.assert_entry(lines, "tests/test_review_both.py", 11)
        self.assertIn(EXCLUDED, lines[both])
        self.assertNotIn(REVIEWER, "\n".join(lines))
        self.assertGreater(both, self.assert_entry(lines, "src/calc.py", 4))
        self.assertGreater(both, self.assert_entry(lines, "revali.toml", 4))

    def test_more_than_five_left_out_are_summarised(self):
        """AC-3: seven lock files and one reviewer test are left out. The five largest are
        named, larger first, each with its reason; the other three lock files (9 + 6 + 3) are
        `... and 3 more files, 18 lines`. The two counted files come before them and have no
        summary line of their own."""
        locks = [
            ("kilo", 21),
            ("lima", 18),
            ("november", 15),
            ("oscar", 12),
            ("papa", 9),
            ("quebec", 6),
            ("romeo", 3),
        ]
        for name, n in locks:
            self.write("%s.lock" % name, rows(n, name))
        self.write("tests/test_review_extra.py", rows(14, "extra"))
        self.set_limit(3)
        self.commit_all("many files left out")
        lines = self.refusal()
        self.assertEqual(lines[0], FIRST % (6, 3))
        counted = [
            self.assert_entry(lines, "src/calc.py", 4),
            self.assert_entry(lines, "revali.toml", 2),
        ]
        self.assertEqual(counted, sorted(counted), lines)
        left = [
            self.assert_entry(lines, "kilo.lock", 21),
            self.assert_entry(lines, "lima.lock", 18),
            self.assert_entry(lines, "november.lock", 15),
            self.assert_entry(lines, "tests/test_review_extra.py", 14),
            self.assert_entry(lines, "oscar.lock", 12),
        ]
        self.assertEqual(left, sorted(left), lines)
        self.assertGreater(left[0], counted[-1])
        for i in left:
            reason = REVIEWER if "test_review_extra.py" in lines[i] else EXCLUDED
            self.assertIn(reason, lines[i])
        message = "\n".join(lines)
        for name in ("papa", "quebec", "romeo"):
            self.assertNotIn("%s.lock" % name, message)
        more = [i for i, line in enumerate(lines) if "more file" in line]
        self.assertEqual(len(more), 1, lines)
        self.assertEqual(lines[more[0]].strip(), "... and 3 more files, 18 lines")
        self.assertGreater(more[0], left[-1])

    def test_a_renamed_file_is_listed_under_its_new_name(self):
        """AC-2: src/calc.py, which the branch already changes by 4 added lines, is renamed
        to src/arith.py; the refusal names the new path with those 4 lines and not the old."""
        git(["mv", "src/calc.py", "src/arith.py"], self.repo)
        self.set_limit(3)
        self.commit_all("rename calc")
        lines = self.refusal()
        self.assertEqual(lines[0], FIRST % (6, 3))
        self.assert_entry(lines, "src/arith.py", 4)
        self.assertNotIn("calc.py", "\n".join(lines))

    def test_at_the_limit_it_passes_and_one_below_it_refuses_with_the_same_total(self):
        """AC-1, AC-4: 46 counted lines (40 + 4 + 2) with a lock file and a reviewer test left
        out. At a limit of 46 preflight passes and logs the line it always logged; at 45 the
        same 46 is refused, and the left-out files are named below, not added to the total."""
        self.write("src/alpha.py", rows(40, "alpha"))
        self.write("deps.lock", rows(12, "lock"))
        self.write("tests/test_review_extra.py", rows(8, "extra"))
        self.set_limit(46)
        self.commit_all("at the limit")
        logged = []
        ctx = preflight(self.repo, log=_ListLog(logged))
        self.assertEqual(ctx.diff_lines, 46)
        self.assertIn("preflight: diff: 46 lines in 3 files (+2 excluded)", logged)
        self.assertEqual(sorted(ctx.excluded_files), ["deps.lock", "tests/test_review_extra.py"])
        self.set_limit(45)
        self.commit_all("one below")
        lines = self.refusal()
        self.assertEqual(lines[0], FIRST % (46, 45))
        self.assert_entry(lines, "src/alpha.py", 40)
        self.assertIn(EXCLUDED, lines[self.assert_entry(lines, "deps.lock", 12)])
        self.assertIn(REVIEWER, lines[self.assert_entry(lines, "tests/test_review_extra.py", 8)])

    def test_cli_preflight_prints_the_first_line_then_the_files(self):
        """AC-1, AC-2: `revali preflight` exits 2; its ACTION NEEDED line is the unchanged
        first line, and the counted files follow on the lines after it, larger first."""
        self.set_limit(3)
        self.commit_all("tiny limit")
        code, out = run_cli(["preflight"])
        self.assertEqual(code, EXIT_ACTION, out)
        printed = out.split("\n")
        head = "ACTION NEEDED: " + FIRST % (6, 3)
        self.assertEqual(printed.count(head), 1, out)
        lines = printed[printed.index(head) :]
        calc = self.assert_entry(lines, "src/calc.py", 4)
        self.assertLess(calc, self.assert_entry(lines, "revali.toml", 2))
