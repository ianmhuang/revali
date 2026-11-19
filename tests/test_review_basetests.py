"""Reviewer acceptance tests for fix/review-modified-tests.

AC-1: every file under test_dir that is on the base and that the branch modifies, deletes or
      renames is on the prompt's "Existing test files this diff modifies or deletes" line (a
      rename under its old name), whether or not its name matches test_file_pattern.
AC-2: the prompt's diff shows the change to such a file and preflight counts its lines; a
      pattern file the branch adds stays out, and `[review] exclude` still wins.
AC-3: such a file is not named on the "omitted from the diff" line.
AC-4: the test-writing rule no longer claims the sandbox has no network or credentials.
AC-5: PROMPT_VERSION is 14 or later.
AC-6: the branch's own new pattern file is left out and named as omitted, as before.

The fixture's pattern is test_review_{topic}.py, its test_dir is tests and its `[review]
exclude` is ["*.lock"]. main (pushed) carries two files an earlier change's reviewer could have
written, one of them in a subdirectory, and a README linking to the first; feature/mul is
rebased onto it. Every assertion on a line of the prompt reads that line alone, and every
"in the diff" reads a marker from the file's content.

Round 2 (round 1 F1): the prompt tools/review_bench.py builds for a case follows the same rule,
read from the prompt.md a `--dry-run` writes (no session is run).
"""

import contextlib
import importlib.util
import io
import json
import os
import re

from revali import PROMPT_VERSION
from revali.preflight import Stop, preflight
from revali.review import build_prompt
from revali.state import State
from tests.helpers import ROOT, RepoCase, git

SPEC = importlib.util.spec_from_file_location(
    "review_bench", os.path.join(ROOT, "tools", "review_bench.py")
)
bench = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bench)

EARLIER = "tests/test_review_earlier.py"
NESTED = "tests/unit/test_review_deep.py"
BODY = "".join("row_%d = %d\n" % (i, i) for i in range(20))
README = "# sample\n\nSee the [earlier test](tests/test_review_earlier.py).\n"


def diff_of(prompt):
    return re.search(r"```diff\n(.*?)\n```", prompt, re.S).group(1)


def line_after(prompt, head):
    return re.search(r"^%s: (.*)$" % re.escape(head), prompt, re.M).group(1)


def omitted(prompt):
    return line_after(prompt, "Files changed but omitted from the diff above")


def modified(prompt):
    return line_after(prompt, "Existing test files this diff modifies or deletes")


def names(line):
    return [] if line == "(none)" else [n.strip() for n in line.split(",")]


class BaseCarriesReviewerTests(RepoCase):
    def setUp(self):
        super().setUp()
        git(["checkout", "-q", "main"], self.repo)
        self.write(EARLIER, BODY)
        self.write(NESTED, BODY)
        self.write("README.md", README)
        self.commit_all("tests an earlier change's reviewer wrote")
        git(["push", "-q", "origin", "main"], self.repo)
        git(["checkout", "-q", "feature/mul"], self.repo)
        git(["rebase", "-q", "main"], self.repo)

    def build(self):
        ctx = preflight(self.repo)
        ctx.log = None
        return ctx, build_prompt(ctx, State(), self.rdir(), 1)

    def weaken(self, path=EARLIER, marker="loosened_row"):
        self.write(path, BODY.replace("row_4 = 4\n", "%s = 4\n" % marker))

    def test_an_edited_one_is_listed_shown_counted_and_not_omitted(self):
        """AC-1, AC-2, AC-3: one line of the earlier reviewer test is replaced"""
        before = preflight(self.repo).diff_lines
        self.weaken()
        self.commit_all("loosen the earlier reviewer test")
        ctx, prompt = self.build()
        self.assertEqual(names(modified(prompt)), [EARLIER])
        diff = diff_of(prompt)
        self.assertIn("+loosened_row = 4", diff)
        self.assertIn("-row_4 = 4", diff)
        self.assertIn("def mul", diff)  # the rest of the change is still there
        self.assertEqual(omitted(prompt), "(none)")
        self.assertNotIn(EARLIER, ctx.excluded_files)
        self.assertIn(EARLIER, ctx.changed_files)
        self.assertEqual(ctx.diff_lines, before + 2)  # one line removed, one added

    def test_one_in_a_subdirectory_of_test_dir_is_treated_the_same(self):
        """AC-1, AC-2, AC-3: "under test_dir" is at any depth"""
        self.weaken(NESTED, "deep_row")
        self.commit_all("loosen the nested one")
        _, prompt = self.build()
        self.assertEqual(names(modified(prompt)), [NESTED])
        self.assertIn("+deep_row = 4", diff_of(prompt))
        self.assertEqual(omitted(prompt), "(none)")

    def test_a_deleted_one_is_listed_and_its_deletion_is_shown(self):
        """AC-1, AC-2, AC-3"""
        git(["rm", "-q", EARLIER], self.repo)
        self.commit_all("delete the earlier reviewer test")
        ctx, prompt = self.build()
        self.assertEqual(names(modified(prompt)), [EARLIER])
        diff = diff_of(prompt)
        self.assertIn("deleted file mode", diff)
        self.assertIn("-row_11 = 11", diff)
        self.assertEqual(omitted(prompt), "(none)")
        self.assertEqual(ctx.excluded_files, [])

    def test_a_renamed_one_is_listed_under_its_old_name_and_shown_as_a_rename(self):
        """AC-1, AC-2, AC-3: renamed to another name on the pattern, one line changed, so the
        new name alone would look like a reviewer test of this branch"""
        new = "tests/test_review_later.py"
        git(["mv", EARLIER, new], self.repo)
        self.write(new, BODY.replace("row_4 = 4\n", "renamed_row = 4\n"))
        self.commit_all("rename the earlier reviewer test")
        ctx, prompt = self.build()
        self.assertEqual(names(modified(prompt)), [EARLIER])
        diff = diff_of(prompt)
        self.assertIn("rename from %s" % EARLIER, diff)
        self.assertIn("rename to %s" % new, diff)
        self.assertIn("+renamed_row = 4", diff)
        self.assertNotIn("deleted file mode", diff)
        self.assertEqual(omitted(prompt), "(none)")
        self.assertIn(new, ctx.changed_files)

    def test_a_renamed_test_off_the_pattern_is_listed_under_its_old_name(self):
        """AC-1: "whether or not it matches test_file_pattern" covers the rename as well"""
        git(["mv", "tests/test_calc.py", "tests/test_sums.py"], self.repo)
        self.commit_all("rename the plain test")
        _, prompt = self.build()
        self.assertEqual(names(modified(prompt)), ["tests/test_calc.py"])

    def test_plain_and_reviewer_tests_are_listed_together(self):
        """AC-1: one modified, one deleted, one renamed, on and off the pattern"""
        calc = self.read("tests/test_calc.py")
        self.assertIn("5)", calc)
        self.write("tests/test_calc.py", calc.replace("5)", "6)"))
        git(["rm", "-q", EARLIER], self.repo)
        git(["mv", NESTED, "tests/unit/test_review_moved.py"], self.repo)
        self.commit_all("touch three existing tests")
        _, prompt = self.build()
        self.assertEqual(
            sorted(names(modified(prompt))), sorted(["tests/test_calc.py", EARLIER, NESTED])
        )
        self.assertEqual(omitted(prompt), "(none)")

    def test_its_lines_count_toward_max_diff_lines(self):
        """AC-2: deleting the 20-line file takes the diff over a limit the rest stays under"""
        limit = preflight(self.repo).diff_lines + 10
        toml = self.read("revali.toml")
        self.assertIn("max_diff_lines = 800", toml)
        small = "max_diff_lines = %d" % limit
        self.write("revali.toml", toml.replace("max_diff_lines = 800", small))
        self.commit_all("a small limit")
        self.assertLessEqual(preflight(self.repo).diff_lines, limit)
        git(["rm", "-q", EARLIER], self.repo)
        self.commit_all("delete the earlier reviewer test")
        with self.assertRaises(Stop) as caught:
            preflight(self.repo)
        self.assertEqual(caught.exception.exit_code, 2)
        self.assertIn("limit %d" % limit, str(caught.exception))

    def test_a_new_pattern_file_stays_out_next_to_an_edited_one(self):
        """AC-2, AC-3, AC-6: the branch adds its own reviewer test and edits the earlier one;
        only the added file is left out and named as omitted"""
        fresh = "tests/test_review_fresh.py"
        self.write(fresh, "fresh_reviewer_row = 1\n")
        self.weaken()
        self.commit_all("a reviewer test of this branch, and the earlier one loosened")
        ctx, prompt = self.build()
        diff = diff_of(prompt)
        self.assertNotIn("fresh_reviewer_row", diff)
        self.assertNotIn("test_review_fresh.py", diff)
        self.assertIn("+loosened_row = 4", diff)
        self.assertEqual(names(omitted(prompt)), [fresh])
        self.assertEqual(ctx.excluded_files, [fresh])
        self.assertEqual(names(modified(prompt)), [EARLIER])

    def test_review_exclude_still_takes_it_out_of_the_diff(self):
        """AC-2: a file matching `[review] exclude` stays out whatever it is, and is named
        as omitted. AC-1: it is still on the list of modified existing tests."""
        toml = self.read("revali.toml")
        self.assertIn('exclude = ["*.lock"]', toml)
        self.write(
            "revali.toml",
            toml.replace('exclude = ["*.lock"]', 'exclude = ["*.lock", "test_review_earlier.py"]'),
        )
        self.weaken()
        self.weaken(NESTED, "deep_row")
        self.commit_all("exclude the earlier reviewer test and edit both")
        ctx, prompt = self.build()
        diff = diff_of(prompt)
        self.assertNotIn("loosened_row", diff)
        self.assertIn("+deep_row = 4", diff)  # the pattern file the exclude does not name
        self.assertEqual(names(omitted(prompt)), [EARLIER])
        self.assertEqual(ctx.excluded_files, [EARLIER])
        self.assertEqual(sorted(names(modified(prompt))), sorted([EARLIER, NESTED]))

    def test_a_document_linking_to_it_is_listed_as_related(self):
        """AC-2: the file is part of the diff, so the README that links to it is among the
        documents related to the change"""
        self.weaken()
        self.commit_all("loosen the earlier reviewer test")
        _, prompt = self.build()
        entries = [ln for ln in prompt.splitlines() if ln.startswith("- `README.md`")]
        self.assertTrue([ln for ln in entries if "tests/test_review_earlier.py" in ln], entries)


class BenchPrompt(RepoCase):
    """Round 1 F1: a benchmark case whose branch edits a reviewer test of its base gets the
    prompt a real run would build: the file in the diff, listed, and not named as omitted."""

    def setUp(self):
        super().setUp()
        git(["checkout", "-q", "main"], self.repo)
        self.write(EARLIER, BODY)
        self.commit_all("a test an earlier change's reviewer wrote")
        git(["checkout", "-q", "feature/mul"], self.repo)
        git(["rebase", "-q", "main"], self.repo)

    def dry_run(self, name):
        """The prompt.md of a one-case `--dry-run` over main...feature/mul of the fixture."""
        cases = os.path.join(self.tmp, "bench-cases")
        out = os.path.join(self.tmp, "bench-out")
        path = os.path.join(cases, name)
        os.makedirs(path)
        toml = [
            "[case]",
            "repo = %s" % json.dumps(self.repo),
            "fetch = []",
            'base = "%s"' % git(["rev-parse", "main"], self.repo).strip(),
            'head = "%s"' % git(["rev-parse", "feature/mul"], self.repo).strip(),
            'branch = "bench/basetests"',
            "[expected]",
            'file = "src/calc.py"',
            "lines = [13, 14]",
            'description = "mul is wrong"',
            "[source]",
            "pr = 1",
            "round = 1",
            'finding = "F1"',
        ]
        with open(os.path.join(path, "case.toml"), "w", encoding="utf-8", newline="\n") as fh:
            fh.write("\n".join(toml) + "\n")
        with open(self.change_md(), encoding="utf-8", newline="") as fh:
            change = fh.read()
        with open(os.path.join(path, "change.md"), "w", encoding="utf-8", newline="\n") as fh:
            fh.write(change)
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = bench.main(["--dry-run", "--cases", cases, "--out", out])
        self.assertEqual(code, 0, stdout.getvalue())
        self.assertEqual(self.fake_calls("claude"), [])
        with open(os.path.join(out, name, "prompt.md"), encoding="utf-8", newline="") as fh:
            return fh.read().replace("\r\n", "\n")

    def test_an_edited_base_reviewer_test_is_in_the_bench_prompt_and_not_omitted(self):
        """AC-1, AC-2, AC-3 for the bench: the branch edits the earlier reviewer test and adds
        one of its own; only the added one is left out and named as omitted"""
        fresh = "tests/test_review_fresh.py"
        self.write(EARLIER, BODY.replace("row_4 = 4\n", "bench_row = 4\n"))
        self.write(fresh, "fresh_bench_row = 1\n")
        self.commit_all("loosen the earlier reviewer test, add a new one")
        prompt = self.dry_run("a-base-test")
        diff = diff_of(prompt)
        self.assertIn("+bench_row = 4", diff)
        self.assertIn("-row_4 = 4", diff)
        self.assertNotIn("fresh_bench_row", diff)
        self.assertEqual(names(omitted(prompt)), [fresh])
        self.assertEqual(names(modified(prompt)), [EARLIER])

    def test_a_deleted_base_reviewer_test_is_not_named_as_omitted(self):
        """AC-2, AC-3 for the bench: the deletion is in the diff, and nothing is omitted"""
        git(["rm", "-q", EARLIER], self.repo)
        self.commit_all("delete the earlier reviewer test")
        prompt = self.dry_run("b-deleted")
        diff = diff_of(prompt)
        self.assertIn("deleted file mode", diff)
        self.assertIn("-row_11 = 11", diff)
        self.assertEqual(omitted(prompt), "(none)")
        self.assertEqual(names(modified(prompt)), [EARLIER])


class PromptRule(RepoCase):
    def test_the_sandbox_is_not_said_to_have_no_network_or_credentials(self):
        """AC-4: the false claim is gone; the rule still asks for tests that depend on
        neither"""
        prompt = build_prompt(preflight(self.repo), State(), self.rdir(), 1)
        self.assertNotIn("the sandbox has none", prompt)
        rule = re.search(r"(?s)A test must not depend on (.*?)\.", prompt)
        self.assertIsNotNone(rule)
        self.assertIn("network", rule.group(1))
        self.assertIn("credentials", rule.group(1))
        self.assertRegex(prompt, r"(?s)sandbox\s+neither\s+provides\s+nor\s+blocks")

    def test_prompt_version_moved(self):
        """AC-5"""
        self.assertGreaterEqual(int(PROMPT_VERSION), 14)
