"""fix/audit-lows-0.4.0 (M3.5.3.3.5): the 27 low findings of the 0.4.0 documentation
re-audit (audit-0.4.0-recheck) are fixed, each in the file the audit named and in the other
places that repeated the claim. Every check reads the file with its whitespace folded, so a
rewrap does not matter but dropping a corrected statement does."""

import os
import tomllib
import unittest

from revali import PROMPT_VERSION
from revali.changedoc import parse
from revali.config import EngineCfg, PrCfg
from tests.helpers import ROOT


def flat(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8", newline="") as fh:
        return " ".join(fh.read().split())


def raw(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8", newline="") as fh:
        return fh.read()


class Readme(unittest.TestCase):
    """AC-1"""

    def test_public_repositories_get_summaries(self):
        text = flat("README.md")
        self.assertIn(
            "with each round's review as a comment (a summary when the repository is not private)",
            text,
        )
        self.assertIn("and a PR comment (a summary when the repository is not private)", text)
        self.assertIn("as PR comments (summaries when the repository is not private)", text)

    def test_the_sequence_line_too(self):  # round 1 F1
        self.assertIn(
            "R->>G: commit tests, PR comment (a summary unless the repo is private)",
            raw("README.md"),
        )

    def test_several_platforms_name_their_diagnosis_file(self):
        text = flat("README.md")
        self.assertIn("`diagnose-n-<platform>.json` for each failing platform", text)
        self.assertIn("`diagnose-<n>-<platform>.json` per failing platform", text)

    def test_within_its_cap(self):
        self.assertLessEqual(raw("README.md").count("\n"), 200)


class Conventions(unittest.TestCase):
    """AC-2"""

    def test_resolve_is_scoped_to_the_host(self):
        text = flat("CONVENTIONS.md")
        self.assertIn("every external executable revali starts on the host", text)
        self.assertIn(
            "Programs run inside a wsl or ssh sandbox (`bash`, `git`, `timeout`, `wslpath`) "
            "are named as written",
            text,
        )

    def test_newline_exceptions_name_the_line_reads(self):
        text = flat("CONVENTIONS.md")
        self.assertIn("reads of the wsl retries record", text)
        # round 1 F3: the step log is read whole, so the reason is text mode, not stripping
        self.assertIn("which run in text mode, so a CRLF the sandbox wrote reads as LF", text)
        self.assertIn("`<label>.results`, `<label>-script.log`", text)


class Workflow(unittest.TestCase):
    """AC-3"""

    def test_merge_from_a_stage_a_later_run_stopped_in(self):
        text = flat("docs", "workflow.md")
        self.assertIn("`revali merge` also merges from the stage a later run stopped in", text)
        self.assertIn("while HEAD is still the commit the last validation passed on", text)

    def test_which_cycles_count(self):
        text = flat("docs", "workflow.md")
        self.assertIn(
            "a cycle after a CHANGES_REQUESTED round or a failed validation counts against "
            "`review.max_fixes`, and answering a NEEDS_INFO round or fixing a preflight "
            "refusal does not",
            text,
        )

    def test_status_line_as_printed(self):
        self.assertIn(
            "`running: no; the last run stopped at stage '<stage>' without a result (see <path to "
            "logs/run.log>)`",  # round 1 F6: the line as pipeline.py prints it
            flat("docs", "workflow.md"),
        )

    def test_pull_in_the_tree_that_holds_the_base(self):
        self.assertIn(
            "`git pull` in the tree that holds the base (the merge output's `worktree:` line "
            "names it)",
            flat("docs", "workflow.md"),
        )

    def test_ac_continuation_and_section(self):
        text = flat("docs", "workflow.md")
        self.assertIn("indented continuation lines or unindented ones", text)
        self.assertIn("Only the Acceptance criteria section is checked", text)


class Configuration(unittest.TestCase):
    """AC-4"""

    def test_labels_revali_creates_and_does_not(self):
        text = flat("docs", "configuration.md")
        self.assertIn("(revali does not create `bug`)", text)
        self.assertNotIn("revali creates no label", text)
        self.assertIn("(creating it first when the repository lacks it)", text)

    def test_kind_label_keys(self):
        text = flat("docs", "configuration.md")
        self.assertIn("outside `feature`, `fix`, `docs`, `refactor`, `hotfix` and `small`", text)
        self.assertIn("so those keys are accepted and never apply", text)

    def test_closing_keywords(self):
        self.assertIn("`Fixes #n` or another closing keyword", flat("docs", "configuration.md"))


class SideEffects(unittest.TestCase):
    """AC-5"""

    def test_closing_keywords_as_matched(self):
        self.assertIn(
            "`Resolves #n`, or another form of those keywords (`fix`, `fixed`, `close`, `closed`, "
            "`resolve`, `resolved`, in any case) followed by `#n`",
            flat("docs", "side-effects.md"),
        )

    def test_ci_wait_is_conditional(self):
        text = flat("docs", "side-effects.md")
        self.assertIn("with `[merge] wait_for_checks` (the default), waits for CI checks", text)
        self.assertIn("up to `[merge] checks_timeout_min` (30)", text)
        self.assertIn("failed or cancelled checks end the merge with exit 2", text)


class Releasing(unittest.TestCase):
    """AC-6"""

    def test_dry_run_exit(self):
        self.assertIn("or a `--dry-run` that wrote its prompts", flat("docs", "releasing.md"))

    def test_the_default_labels(self):
        # M3.38 added `internal` to the default list
        text = flat("docs", "releasing.md")
        self.assertIn(
            "(`major`, `minor`, `patch`, `docs`, `internal` or `skip-changelog` by default)", text
        )
        self.assertIn("`major`, `internal` and `skip-changelog` come from `release:` only", text)


class Files(unittest.TestCase):
    """AC-7"""

    def test_review_json_reader(self):
        self.assertIn(
            "the `.json` also the next round's Reviewer prompt (prior findings)",
            flat("docs", "files.md"),
        )

    def test_state_json_readers(self):
        text = flat("docs", "files.md")
        self.assertIn(
            "`wait`, `status`, `preflight`, `stop`, `reset`, the next `run`, `merge`, `archive`",
            text,
        )
        self.assertIn(
            "a read (`wait`, `status` and every other command under Read by) waits for a writer",
            text,
        )

    def test_platform_entry_fields(self):
        self.assertIn(
            "its own name, result, failed step, cause, `introduced_by`", flat("docs", "files.md")
        )
        # round 1 F7: every value validate.INTRODUCED_BY allows, and the empty one
        self.assertIn(
            "`introduced_by` (branch, base or unknown, from the diagnosis; empty without one)",
            flat("docs", "files.md"),
        )

    def test_history_row_fields(self):
        self.assertIn(
            "every row carries `at`, `revali_version`, `prompt_version`", flat("docs", "files.md")
        )


class SkillAndTemplate(unittest.TestCase):
    """AC-8"""

    def test_ac_rules(self):
        for rel in (("skill", "SKILL.md"), ("templates", "change.md")):
            with self.subTest(path="/".join(rel)):
                text = flat(*rel)
                self.assertIn("indented or not", text)
                self.assertIn("other sections are not checked", text)
                self.assertIn("any other line that starts like an AC stops preflight", text)

    def test_only_a_section_heading_ends_an_ac(self):  # round 1 F2
        for rel in (("skill", "SKILL.md"), ("templates", "change.md"), ("docs", "workflow.md")):
            with self.subTest(path="/".join(rel)):
                self.assertIn("section heading", flat(*rel))
        head = "---\ntitle: t\nkind: fix\n---\n\n## Acceptance criteria\n- AC-1: first text\n"
        joined = dict(parse(head + "### not a section\n").acs)
        self.assertIn("not a section", joined["AC-1"])  # a deeper heading joins the AC
        ended = dict(parse(head + "## Out of scope\nnothing\n").acs)
        self.assertEqual(ended["AC-1"], "first text")

    def test_worktree_detach_condition(self):
        text = flat("skill", "SKILL.md")
        self.assertIn("while another tree (normally the primary one) has the base branch", text)
        self.assertIn("in the tree the merge output's `worktree:` line names", text)
        self.assertIn("the worktree ends on the updated base branch", text)

    def test_placeholder_states_the_coverage_rule(self):
        text = flat("templates", "change.md")
        self.assertIn(
            "the reviewer maps every AC to a test, or records why it cannot be tested", text
        )
        self.assertNotIn("every AC maps to at least one test", text)


class TomlTemplate(unittest.TestCase):
    """AC-9"""

    def test_header(self):
        text = flat("templates", "revali.toml").replace("# ", "")
        self.assertIn('Every key below is optional except the one marked "required"', text)
        self.assertIn("defaults.toml lists every key with its default, except config_version", text)
        self.assertIn("a temporary git worktree run by the host shell for the local runner", text)

    def test_sandbox_dir_serves_wsl_and_ssh(self):
        self.assertIn(
            "# wsl and ssh: in the distro / on the ssh host,", raw("templates", "revali.toml")
        )

    def test_files_in_the_smoke_run(self):
        for rel in (("templates", "revali.toml"), ("docs", "sandbox.md")):
            with self.subTest(path="/".join(rel)):
                text = flat(*rel).replace("# ", "")
                self.assertIn("the reviewer's files under", text)
                self.assertIn("plus the earlier rounds'", text)
                self.assertNotIn("this round's files in the smoke run", text)
        self.assertIn("no file to name skips the step", flat("templates", "revali.toml"))

    def test_host_is_required_for_ssh(self):  # round 1 F5
        text = flat("templates", "revali.toml").replace("# ", "")
        self.assertIn("and host for the ssh runner", text)
        self.assertIn("ssh only, and required there", text)

    def test_no_other_place_says_this_rounds_files(self):  # round 1 F4
        self.assertNotIn("this round's files", raw("revali", "review.py"))

    def test_defaults_toml_has_every_key_but_config_version(self):  # round 1 follow-up
        # test_config_layers covers project, review, validate, merge, paths and the platform
        # table; these are the two sections it leaves out
        with open(os.path.join(ROOT, "defaults.toml"), "rb") as fh:
            defaults = tomllib.load(fh)
        for name in PrCfg.__dataclass_fields__:
            self.assertIn(name, defaults["pr"], "pr.%s has no default" % name)
        for engine, table in defaults["engines"].items():
            for name in EngineCfg.__dataclass_fields__:
                if name != "name":  # the table's own key
                    self.assertIn(name, table, "engines.%s.%s has no default" % (engine, name))

    def test_write_retry_comment_names_every_reader(self):  # round 1 follow-up
        line = next(
            ln for ln in raw("defaults.toml").splitlines() if ln.startswith("write_retry_s")
        )
        self.assertIn("any command that loads it", line)

    def test_config_docstring(self):
        self.assertIn("except `config_version`", flat("revali", "config.py"))


class Prompt(unittest.TestCase):
    """AC-10"""

    def test_renames_are_named(self):
        text = raw("prompts", "review.md")
        self.assertIn(
            "Existing test files this diff modifies, deletes or renames (a rename under its old "
            "name): $modified_tests",
            text,
        )
        self.assertIn("existing test file the diff modifies, deletes or renames, add a", text)
        self.assertNotIn("modifies or deletes", text)
        self.assertIn("modifies, deletes or renames.", raw("schemas", "review.schema.json"))

    def test_prompt_version(self):
        self.assertGreaterEqual(int(PROMPT_VERSION), 15)


if __name__ == "__main__":
    unittest.main()
