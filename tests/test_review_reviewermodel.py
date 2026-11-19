"""feature/usage-limit AC-5 and AC-6: `revali run --reviewer-model <model>` puts that model
in place of `[review] model` for one run, and the run's records say where it came from and
where it stands against the author's tier. Black box through the CLI with the fake gh,
claude and runner; the ladder is the default one (haiku, sonnet, opus, fable)."""

import re
from unittest import mock

from revali import EXIT_ERROR, EXIT_OK
from tests.helpers import RepoCase, claude_entry, git, run_cli

CHANGE = ".revali/feature__mul/change.md"
REVIEW_1 = ".revali/feature__mul/review-1.md"
COMMENT_1 = ".revali/feature__mul/logs/comment-review-1.md"
AUTHOR = "claude-opus-5"
DEAD_PID = 999999999


def value_after(argv, flag):
    return argv[argv.index(flag) + 1]


class Case(RepoCase):
    def setUp(self):
        super().setUp()
        # .revali/ is ignored in the fixture, so this leaves the tree clean
        text = self.read(CHANGE)
        self.write(CHANGE, re.sub(r"author_model: .*", "author_model: " + AUTHOR, text, count=1))

    def reviewer_argv(self, index=0):
        return self.fake_calls("claude")[index]["argv"]

    def assert_reason(self, text, relation):
        """`text` says the model came from the option and how it stands against the author."""
        self.assertIn("--reviewer-model", text)
        self.assertIn(relation, text)
        self.assertIn(AUTHOR, text)


class TheRunUsesTheNamedModel(Case):
    """AC-5"""

    def test_model_and_fallback_follow_the_option(self):
        self.claude(claude_entry(model="claude-sonnet-5"))
        code, out = run_cli(["run", "--foreground", "--reviewer-model", "sonnet"])
        self.assertEqual(code, EXIT_OK, out)
        argv = self.reviewer_argv()
        self.assertEqual(value_after(argv, "--model"), "sonnet")
        # as for an explicit `[review] model`, which since AC-8 has no auto fallback
        self.assertNotIn("--fallback-model", argv)

    def test_the_bottom_tier_has_no_fallback(self):
        self.claude(claude_entry())
        code, out = run_cli(["run", "--foreground", "--reviewer-model", "haiku"])
        self.assertEqual(code, EXIT_OK, out)
        argv = self.reviewer_argv()
        self.assertEqual(value_after(argv, "--model"), "haiku")
        self.assertNotIn("--fallback-model", argv)

    def test_the_next_run_without_it_goes_back_to_the_configuration(self):
        self.claude(claude_entry(model="claude-sonnet-5"), claude_entry())
        code, out = run_cli(["run", "--foreground", "--reviewer-model", "sonnet"])
        self.assertEqual(code, EXIT_OK, out)
        self.write("src/extra.py", "X = 1\n")
        self.commit_all("another change")
        code, out = run_cli(["run", "--foreground"])
        calls = self.fake_calls("claude")
        self.assertGreaterEqual(len(calls), 2, out)
        self.assertEqual(value_after(calls[0]["argv"], "--model"), "sonnet")
        # `[review] model = "auto"`: one tier above the author
        self.assertEqual(value_after(calls[1]["argv"], "--model"), "fable")
        # AC-8: a Reviewer has no auto fallback
        self.assertNotIn("--fallback-model", calls[1]["argv"])

    def test_the_detached_run_reviews_on_it(self):
        with mock.patch("revali.pipeline.spawn_detached", return_value=DEAD_PID) as spawn:
            code, out = run_cli(["run", "--reviewer-model", "opus"])
        self.assertEqual(code, EXIT_OK, out)
        spawn.assert_called_once()
        child = spawn.call_args[0][0]
        self.assertEqual(value_after(child, "--reviewer-model"), "opus")


class ABlankValueIsRefused(Case):
    """AC-5: exit 1 before anything is pushed."""

    def assert_nothing_left_the_machine(self):
        self.assertEqual(self.fake_calls("gh"), [])
        self.assertEqual(self.fake_calls("claude"), [])
        self.assertNotIn("feature/mul", git(["ls-remote", "--heads", "origin"], self.repo))

    def test_in_the_foreground(self):
        for value in ("", "   "):
            with self.subTest(value=value):
                code, out = run_cli(["run", "--foreground", "--reviewer-model", value])
                self.assertEqual(code, EXIT_ERROR, out)
                self.assertIn("--reviewer-model", out)
                self.assert_nothing_left_the_machine()

    def test_before_the_detached_run_is_started(self):
        for value in ("", "\t "):
            with self.subTest(value=value):
                with mock.patch("revali.pipeline.spawn_detached", return_value=DEAD_PID) as spawn:
                    code, out = run_cli(["run", "--reviewer-model", value])
                self.assertEqual(code, EXIT_ERROR, out)
                self.assertIn("--reviewer-model", out)
                spawn.assert_not_called()
                self.assert_nothing_left_the_machine()


class TheRecordsSayWhereTheModelCameFrom(Case):
    """AC-6"""

    def test_log_line_review_header_and_public_comment(self):
        self.scenario({"visibility": "PUBLIC"})
        self.claude(claude_entry(model="claude-sonnet-5"))
        code, out = run_cli(["run", "--foreground", "--reviewer-model", "sonnet"])
        self.assertEqual(code, EXIT_OK, out)
        spawned = [ln for ln in out.splitlines() if "reviewer sonnet" in ln]
        self.assertTrue(spawned, out)
        self.assert_reason(spawned[0], "below")
        header = self.read(REVIEW_1).split("# Review round")[0]
        reasons = [ln for ln in header.splitlines() if ln.startswith("model_reason:")]
        self.assertEqual(len(reasons), 1, header)
        self.assert_reason(reasons[0], "below")
        self.assert_reason(self.read(COMMENT_1), "below")

    def test_private_comment_carries_the_reason(self):
        self.claude(claude_entry())
        code, out = run_cli(["run", "--foreground", "--reviewer-model", "fable"])
        self.assertEqual(code, EXIT_OK, out)
        self.assert_reason(self.read(COMMENT_1), "above")

    def test_dry_run_prints_the_reviewer_and_each_relation(self):
        for model, relation in (("fable", "above"), ("opus", "same tier"), ("sonnet", "below")):
            with self.subTest(model=model):
                code, out = run_cli(["run", "--dry-run", "--reviewer-model", model])
                self.assertEqual(code, EXIT_OK, out)
                would = [ln for ln in out.splitlines() if ln.startswith("DRY RUN OK")]
                self.assertEqual(len(would), 1, out)
                self.assertIn("reviewer %s" % model, would[0])
                self.assert_reason(would[0], relation)
                for other in {"above", "same tier", "below"} - {relation}:
                    self.assertNotIn(other, would[0])
        self.assertEqual(self.fake_calls("claude"), [])
