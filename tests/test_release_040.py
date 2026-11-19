"""release/0.4.0: the version, the 0.4.0 changelog section (AC-1 to AC-5), the audit lows
fixed in docs/sandbox.md, docs/releasing.md, bench/README.md and checklists/default.md
(AC-6 to AC-9), the archived test files' paths in the diagnosis prompt (AC-10) and
PROMPT_VERSION (AC-11). Documents are read with their whitespace folded, so a rewrap does
not matter but dropping a corrected statement does."""

import os
import re
import tempfile
import unittest
from types import SimpleNamespace

from revali import EXIT_ACTION, PROMPT_VERSION
from revali.state import State
from revali.validate import _test_files_for_prompt
from tests.helpers import ROOT, TEST_REVIEW_MUL, claude_entry, run_cli
from tests.test_every_platform import LOGS, diagnosis
from tests.test_tests_archive import BOTH, FILE, HELPER, ArchiveCase, approving

# every PR merged between v0.3.0 and this change (`git log --oneline v0.3.0..main`); #79 is
# an issue
RELEASE_PRS = [n for n in range(44, 100) if n != 79]


def flat(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8", newline="") as fh:
        return " ".join(fh.read().split())


def raw(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8", newline="") as fh:
        return fh.read()


def section_040():
    text = raw("CHANGELOG.md")
    return text.split("\n## 0.4.0 - 2026-11-19\n", 1)[1].split("\n## ", 1)[0]


class Changelog(unittest.TestCase):
    """AC-2 to AC-5"""

    def test_the_new_section_sits_above_0_3_0_and_the_rest_is_kept(self):
        text = raw("CHANGELOG.md")
        headings = [line for line in text.split("\n") if line.startswith("## ")]
        self.assertEqual(headings[:2], ["## 0.4.0 - 2026-11-19", "## 0.3.0 - 2026-09-07"])
        # the preamble ends where the new section starts
        preamble = text.split("\n## 0.4.0 - ", 1)[0]
        self.assertTrue(preamble.startswith("# Changelog\n"))
        self.assertIn(
            "changes merged since the newest section are not listed", " ".join(preamble.split())
        )

    def test_every_pr_of_the_release_and_no_other(self):
        named = {int(n) for n in re.findall(r"#(\d+)\b", section_040())}
        self.assertEqual(sorted(set(RELEASE_PRS) - named), [], "PRs left out")
        self.assertEqual(sorted(named - set(RELEASE_PRS)), [], "PRs not of this release")

    def test_the_history_row_change_is_not_a_ci_change(self):
        # PR #100 F1: #45 changed what revali writes to the history file
        body = section_040()
        ci = body.split("CI and the suite:", 1)[1].split("Tools and documents:", 1)[0]
        runs = body.split("Runs, state and merge:", 1)[1].split("CI and the suite:", 1)[0]
        self.assertNotIn("#45", ci)
        self.assertIn("#45", runs)

    def test_it_opens_with_the_lead_sentence(self):
        body = section_040().strip()
        first = body.split("\n\n", 1)[0]
        self.assertTrue(first.startswith("Validation on Windows as well as Linux"), first)
        self.assertNotIn("\n- ", first)

    def test_upgrading(self):
        body = " ".join(section_040().split())
        upgrading = body.split("Upgrading from 0.3.0:", 1)[1].split("Validation and the sandbox:")[
            0
        ]
        for needle in (
            'fallback_model = "auto"` now means no fallback',
            "usage limit or an overloaded model stops the run",
            "--reviewer-model <model>",
            "PROMPT_VERSION goes from 8 to 16",
            "own `[review] prompt`",
            "earlier reviewer tests among them (#95)",
            "modified reviewer tests changed (#95, #98)",
            "`revali archive`",
            "`[project] format`",
            "`[review] doc_roots` and `doc_map_max`",
            "`[validate] parallel_baseline`",
            "`wsl_retries`",
            "`max_procs`",
            "`windows` as a platform",
            "the `[pr]` section",
            "`state.json` written by 0.3.0 is still read",
        ):
            with self.subTest(needle=needle):
                self.assertIn(needle, upgrading)

    def test_known_issues(self):
        body = " ".join(section_040().split())
        known = body.split("Known issues:", 1)[1]
        self.assertIn("No audit finding is open except one in the released 0.2.0 section", known)
        self.assertIn("`version` and `stats`", known)

    def test_prompt_version_as_released(self):
        self.assertIn("from 8 to 16", " ".join(section_040().split()))


class Sandbox(unittest.TestCase):
    """AC-6"""

    def setUp(self):
        self.text = flat("docs", "sandbox.md")

    def test_wsl_writes_the_logs_in_place(self):
        self.assertNotIn("copies the logs back", self.text)
        self.assertIn(
            "The script writes the step logs and the results file straight into the host's "
            "logs directory, through its `/mnt` path; nothing is copied back",
            self.text,
        )

    def test_ssh_sends_a_bundle_of_all_refs(self):
        self.assertNotIn("the branch travels as a git bundle", self.text)
        self.assertIn("the repository travels as a git bundle of all its refs", self.text)
        self.assertIn("`git bundle create --all`", self.text)

    def test_the_probe_needs_git_bash_and_timeout(self):
        self.assertIn("reachable, git, bash and coreutils `timeout` present", self.text)
        self.assertIn("The distro needs git, bash, coreutils `timeout` and whatever", self.text)


class Releasing(unittest.TestCase):
    """AC-7"""

    def setUp(self):
        self.text = flat("docs", "releasing.md")

    def test_which_documents_review_covers(self):
        self.assertNotIn("the documents a change touches, links to or names", self.text)
        self.assertIn(
            "covers the documents a change touches, those that link to a file it changes, and "
            "those whose code spans name a file it changes or a word on a line it adds or removes",
            self.text,
        )

    def test_resume_takes_no_dry_run_out_or_only(self):
        self.assertIn("cannot be combined with `--dry-run`, `--out` or `--only`", self.text)

    def test_a_dry_run_writes_no_summary(self):
        self.assertIn("and, except after a `--dry-run`, `summary.md`:", self.text)


class Bench(unittest.TestCase):
    """AC-8"""

    def setUp(self):
        self.text = flat("bench", "README.md")

    def test_malformed_beside_ready_and_error(self):
        self.assertIn("each case is `ready`, `error` or `malformed`", self.text)
        self.assertIn(
            "reported as `error` (or `malformed` when its directory does not load)", self.text
        )

    def test_isolation_steps_in_the_tool_order(self):
        steps = (
            "check out the head commit",
            "write the case's own `revali.toml`",
            "read the configuration",
            "point the base branch",
            "delete every other ref, `FETCH_HEAD`, `ORIG_HEAD` and the reflogs",
            "build the round-1 prompt",
        )
        where = [self.text.find(s) for s in steps]
        self.assertNotIn(-1, where, dict(zip(steps, where, strict=True)))
        self.assertEqual(where, sorted(where))

    def test_a_case_may_set_its_own_prompt(self):
        self.assertIn(
            "unless the case's `revali.toml` sets its own `[review] prompt`, `schema` or "
            "`checklist_builtin`; `~/.revali/config.toml` applies as in a normal run",
            self.text,
        )

    def test_other_findings_live_in_results_json(self):
        self.assertNotIn("Other findings are listed under the case", self.text)
        self.assertIn("recorded under the case in `results.json` (`other_findings`)", self.text)

    def test_bench_case_does_not_expand_tilde(self):
        self.assertIn("`tools/bench_case.py` does not expand `~` itself", self.text)


class Checklist(unittest.TestCase):
    """AC-9"""

    def test_security_kind(self):
        text = flat("checklists", "default.md")
        self.assertIn("in which case they are `correctness`", text)
        self.assertIn(
            "or expose secrets or confidential material as item 3 describes, in which case "
            "they are `security`",
            text,
        )


class Version(unittest.TestCase):
    """AC-1, AC-11"""

    def test_prompt_version(self):
        self.assertGreaterEqual(int(PROMPT_VERSION), 16)

    def test_version_command(self):
        code, out = run_cli(["--version"])
        self.assertEqual(code, 0, out)
        self.assertRegex(out.strip(), r"^revali \d+\.\d+\.\d+$")


class ArchivedPaths(unittest.TestCase):
    """AC-10, on the helper the prompt is filled from."""

    def ctx(self, root, mode):
        review = SimpleNamespace(tests=mode)
        return SimpleNamespace(repo_root=root, cfg=SimpleNamespace(review=review))

    def test_commit_mode_lists_the_paths_as_before(self):
        state = State(test_files=[FILE, HELPER])
        out = _test_files_for_prompt(self.ctx("/repo", "commit"), state, "/repo/.revali/b")
        self.assertEqual(out, "- %s\n- %s" % (FILE, HELPER))

    def test_no_file(self):
        out = _test_files_for_prompt(self.ctx("/repo", "archive"), State(), "/repo/.revali/b")
        self.assertEqual(out, "(none)")

    def test_a_state_dir_outside_the_repository_gives_absolute_paths(self):
        with tempfile.TemporaryDirectory() as repo, tempfile.TemporaryDirectory() as away:
            rdir = os.path.join(away, "feature__mul")
            out = _test_files_for_prompt(self.ctx(repo, "archive"), State(test_files=[FILE]), rdir)
            where = os.path.abspath(os.path.join(rdir, "tests", *FILE.split("/")))
            self.assertEqual(out, "- %s (not in the working tree; read it at `%s`)" % (FILE, where))

    def test_a_linked_state_dir_keeps_its_path_under_the_repository(self):
        # PR #100 F2: the path goes through the link, which the session can read from its
        # working directory; the resolved target outside the repository it may not
        with tempfile.TemporaryDirectory() as repo, tempfile.TemporaryDirectory() as away:
            try:
                os.symlink(away, os.path.join(repo, ".revali"), target_is_directory=True)
            except (OSError, NotImplementedError) as exc:
                self.skipTest("cannot create a directory symlink here: %s" % exc)
            rdir = os.path.join(repo, ".revali", "feature__mul")
            out = _test_files_for_prompt(self.ctx(repo, "archive"), State(test_files=[FILE]), rdir)
            self.assertEqual(
                out,
                "- %s (not in the working tree; read it at `.revali/feature__mul/tests/%s`)"
                % (FILE, FILE),
            )
            self.assertNotIn(os.path.realpath(away), out)


class DiagnosisPrompt(ArchiveCase):
    """AC-10, through a run whose validation fails."""

    def failing_run(self, *review_entries):
        self.runner_scenario({"default": 0, "results": {"validate-r1": {"new_test": 1}}})
        self.claude(*review_entries, claude_entry(diagnosis(), write_tests=False))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ACTION, out)
        return self.read(os.path.join(LOGS, "prompt-diagnose-1.md"))

    def test_archive_mode_gives_the_archived_paths(self):
        prompt = self.failing_run(approving(**{FILE: TEST_REVIEW_MUL, HELPER: "{}\n"}))
        for rel in BOTH:
            line = "- %s (not in the working tree; read it at `.revali/feature__mul/tests/%s`)" % (
                rel,
                rel,
            )
            with self.subTest(rel=rel):
                self.assertIn(line, prompt)
                self.assertTrue(self.exists(".revali/feature__mul/tests/" + rel))
                self.assertFalse(self.exists(rel))

    def test_files_md_says_so(self):
        self.assertIn("named to the diagnosis session by that path", flat("docs", "files.md"))

    def test_releasing_names_the_released_list(self):
        # PR #100 follow-up: the release updates test_changelog's RELEASED too
        self.assertIn(
            "adds the new version and its date to `RELEASED` in", flat("docs", "releasing.md")
        )

    def test_commit_mode_lists_the_paths_as_before(self):
        self.switch_mode("commit")
        prompt = self.failing_run(claude_entry())
        self.assertIn("## Test files written by the reviewer\n\n- %s\n" % FILE, prompt)
        self.assertNotIn("not in the working tree", prompt)


if __name__ == "__main__":
    unittest.main()
