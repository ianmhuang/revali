"""fix/review-modified-tests (M3.5.3.3.2): a test file already on the base is reviewed like any
other changed file, even when its name matches `test_file_pattern` (an earlier change's
reviewer test); only the pattern files this branch adds stay out of the review.

The fixture's pattern is test_review_{topic}.py and its `[review] exclude` is ["*.lock"]. main
(pushed) carries `tests/test_review_old.py`, as if an earlier merged change's reviewer wrote
it, and a README that links to it; feature/mul is rebased onto it.
"""

import re

from revali import PROMPT_VERSION, gitops
from revali.preflight import Stop, preflight
from revali.review import OMITTED_TEST_MARK, build_prompt
from revali.state import State
from tests.helpers import RepoCase, git

OLD = "tests/test_review_old.py"
BODY = "".join("line_%d = %d\n" % (i, i) for i in range(20))
README = "# sample\n\nThe [old test](tests/test_review_old.py).\n"


def diff_block(prompt):
    return re.search(r"```diff\n(.*?)\n```", prompt, re.S).group(1)


def prompt_line(prompt, head):
    return re.search(r"^%s: (.*)$" % re.escape(head), prompt, re.M).group(1)


def omitted_line(prompt):
    return prompt_line(prompt, "Files changed but omitted from the diff above")


def modified_line(prompt):
    return prompt_line(
        prompt,
        "Existing test files this diff modifies, deletes or renames (a rename under its old name)",
    )


class EarlierReviewerTest(RepoCase):
    def setUp(self):
        super().setUp()
        git(["checkout", "-q", "main"], self.repo)
        self.write(OLD, BODY)
        self.write("README.md", README)
        self.commit_all("an earlier change's reviewer test")
        git(["push", "-q", "origin", "main"], self.repo)
        git(["checkout", "-q", "feature/mul"], self.repo)
        git(["rebase", "-q", "main"], self.repo)

    def build(self):
        ctx = preflight(self.repo)
        ctx.log = None
        return ctx, build_prompt(ctx, State(), self.rdir(), 1)

    def edit_old(self):
        self.write(OLD, BODY.replace("line_3 = 3\n", "weakened_marker = 3\n"))

    def test_a_modified_one_is_listed_shown_and_counted(self):
        """AC-1, AC-2, AC-3"""
        before = preflight(self.repo).diff_lines
        self.edit_old()
        self.commit_all("weaken the earlier test")
        ctx, prompt = self.build()
        self.assertEqual(modified_line(prompt), OLD)
        diff = diff_block(prompt)
        self.assertIn("weakened_marker = 3", diff)
        self.assertIn("-line_3 = 3", diff)
        self.assertIn("def mul", diff)
        self.assertNotIn(OLD, omitted_line(prompt))
        self.assertEqual(omitted_line(prompt), "(none)")
        self.assertIn(OLD, ctx.changed_files)
        self.assertEqual(ctx.excluded_files, [])
        self.assertEqual(ctx.diff_lines, before + 2)

    def test_a_deleted_one_is_listed_and_shown(self):
        """AC-1, AC-2, AC-3"""
        git(["rm", "-q", OLD], self.repo)
        self.commit_all("drop the earlier test")
        ctx, prompt = self.build()
        self.assertEqual(modified_line(prompt), OLD)
        diff = diff_block(prompt)
        self.assertIn("deleted file mode", diff)
        self.assertIn("-line_7 = 7", diff)
        self.assertEqual(omitted_line(prompt), "(none)")

    def test_a_renamed_one_is_listed_under_its_old_name_and_shown(self):
        """AC-1, AC-2: renamed to another name on the pattern, with one line changed"""
        git(["mv", OLD, "tests/test_review_new.py"], self.repo)
        self.write("tests/test_review_new.py", BODY.replace("line_3 = 3\n", "moved_edit = 3\n"))
        self.commit_all("rename the earlier test")
        ctx, prompt = self.build()
        self.assertEqual(modified_line(prompt), OLD)
        diff = diff_block(prompt)
        self.assertIn("rename from %s" % OLD, diff)
        self.assertIn("rename to tests/test_review_new.py", diff)
        self.assertIn("moved_edit = 3", diff)
        self.assertEqual(omitted_line(prompt), "(none)")
        self.assertIn("tests/test_review_new.py", ctx.changed_files)

    def test_a_renamed_plain_test_is_listed_under_its_old_name(self):
        """AC-1: a file off the pattern, which the old list also left out once renamed"""
        git(["mv", "tests/test_calc.py", "tests/test_arith.py"], self.repo)
        self.commit_all("rename test_calc")
        ctx, prompt = self.build()
        self.assertEqual(modified_line(prompt), "tests/test_calc.py")

    def test_a_plain_test_is_still_listed(self):
        """AC-1: the case the list always covered"""
        self.write("tests/test_calc.py", self.read("tests/test_calc.py").replace("5)", "6)"))
        self.edit_old()
        self.commit_all("weaken both")
        _, prompt = self.build()
        self.assertEqual(modified_line(prompt), "tests/test_calc.py, %s" % OLD)

    def test_it_counts_toward_max_diff_lines(self):
        """AC-2: 20 deleted lines take the diff over a limit the rest stays under"""
        toml = self.read("revali.toml").replace("max_diff_lines = 800", "max_diff_lines = 15")
        self.write("revali.toml", toml)
        self.commit_all("a small limit")
        self.assertLessEqual(preflight(self.repo).diff_lines, 15)
        git(["rm", "-q", OLD], self.repo)
        self.commit_all("drop the earlier test")
        with self.assertRaises(Stop) as cm:
            preflight(self.repo)
        self.assertEqual(cm.exception.exit_code, 2)
        self.assertIn("limit 15", str(cm.exception))

    def test_review_exclude_still_leaves_it_out(self):
        """AC-2: `[review] exclude` wins whatever the file is; it is named as omitted"""
        toml = self.read("revali.toml").replace(
            'exclude = ["*.lock"]', 'exclude = ["*.lock", "test_review_old.py"]'
        )
        self.write("revali.toml", toml)
        self.edit_old()
        self.commit_all("exclude the earlier test")
        ctx, prompt = self.build()
        self.assertNotIn("weakened_marker", diff_block(prompt))
        self.assertEqual(omitted_line(prompt), OLD)
        # AC-1 lists it all the same; since M4.7 it is marked as omitted from the diff
        self.assertEqual(modified_line(prompt), "%s %s" % (OLD, OMITTED_TEST_MARK))
        self.assertEqual(ctx.excluded_files, [OLD])

    def test_the_related_documents_see_it(self):
        """AC-2: the related-documents section leaves out the same files as the diff"""
        self.edit_old()
        self.commit_all("weaken the earlier test")
        _, prompt = self.build()
        self.assertRegex(prompt, r"(?m)^- `README\.md`.*links to.*test_review_old\.py")


class NothingOnTheBase(RepoCase):
    def test_a_new_reviewer_test_is_left_out_as_before(self):
        """AC-6: the branch only adds a pattern file; the diff, the omitted line and the
        modified line are what the old rule gave"""
        self.write("tests/test_review_x.py", "new_reviewer_marker = 1\n")
        self.write("pkg/deps.lock", "lock_marker\n")
        self.commit_all("a reviewer test and a lock file")
        ctx = preflight(self.repo)
        ctx.log = None
        prompt = build_prompt(ctx, State(), self.rdir(), 1)
        old_rule = gitops.diff_text(
            ctx.base_ref, "HEAD", self.repo, exclude=["*.lock", "test_review_*.py"]
        )
        self.assertEqual(diff_block(prompt), old_rule.strip())
        self.assertNotIn("new_reviewer_marker", diff_block(prompt))
        self.assertEqual(omitted_line(prompt), "pkg/deps.lock, tests/test_review_x.py")
        self.assertEqual(modified_line(prompt), "(none)")
        self.assertEqual(sorted(ctx.excluded_files), ["pkg/deps.lock", "tests/test_review_x.py"])


class PromptText(RepoCase):
    def test_the_sandbox_wording(self):
        """AC-4"""
        prompt = build_prompt(preflight(self.repo), State(), self.rdir(), 1)
        self.assertNotIn("the sandbox has none", prompt)
        self.assertIn("neither provides nor blocks", prompt)
        self.assertIn("must not depend on network access or credentials", prompt)

    def test_prompt_version(self):
        """AC-5"""
        self.assertGreaterEqual(int(PROMPT_VERSION), 14)
