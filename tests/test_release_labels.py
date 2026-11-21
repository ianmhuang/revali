"""feature/release-labels (M3.22): every PR revali opens carries one release label from
change.md (`release:`, else `[pr] kind_labels[kind]`), kept in step on each run and before
`revali merge`, so `gh release create --generate-notes` groups the notes by it.

The fixture's change.md is kind `feature`, so with the defaults the wanted label is `minor`.
A new PR is #7 (the gh stub's `pr create`); an open PR handed in by `prs_open` is #12.
"""

import os
from unittest import mock

from revali import EXIT_ERROR, EXIT_OK
from revali.config import ConfigError, parse_project_config, release_label
from revali.preflight import Stop, preflight
from revali.procs import ProcTimeout
from tests.helpers import ROOT, RepoCase, claude_entry, run_cli

REVALI_LOG = ".revali/feature__mul/logs/revali.log"
OK_LINE = "preflight: ok: Add mul to calc (feature, 2 AC: AC-1, AC-2%s)"


class ListLog:
    def __init__(self):
        self.lines = []

    def stage(self, stage, msg):
        self.lines.append("%s: %s" % (stage, msg))

    def detail(self, msg):
        pass


def open_pr(*labels):
    pr = {"number": 12, "url": "https://x/pull/12", "isDraft": True, "title": "Add mul to calc"}
    pr["labels"] = [{"name": n} for n in labels]
    return {"prs_open": [pr]}


class LabelCase(RepoCase):
    def setUp(self):
        super().setUp()
        os.environ["REVALI_POLL_SECONDS"] = "0.01"

    def gh(self):
        return [c["argv"] for c in self.fake_calls("gh")]

    def label_edits(self):
        return [
            a
            for a in self.gh()
            if a[:2] == ["pr", "edit"] and ("--add-label" in a or "--remove-label" in a)
        ]

    def label_lookups(self):
        return [a for a in self.gh() if a[:1] == ["api"] and "/labels/" in a[1]]

    def label_creates(self):
        return [a for a in self.gh() if a[:2] == ["label", "create"]]

    def merges(self):
        return [a for a in self.gh() if a[:2] == ["pr", "merge"]]

    def config(self, extra):
        """Commit `extra` (TOML) at the end of the fixture's revali.toml."""
        self.write("revali.toml", self.read("revali.toml") + "\n" + extra)
        self.commit_all("pr config")

    def front(self, line):
        """Add a front-matter line to the fixture's change.md (not tracked)."""
        text = self.read(".revali/feature__mul/change.md")
        self.write(
            ".revali/feature__mul/change.md",
            text.replace("kind: feature\n", "kind: feature\n" + line + "\n", 1),
        )

    def parse(self, extra):
        return parse_project_config(self.read("revali.toml") + "\n" + extra, repo_root=self.repo)

    def problems(self, extra):
        with self.assertRaises(ConfigError) as cm:
            self.parse(extra)
        return "\n".join(cm.exception.problems)

    def run_ok(self, *extra):
        self.claude(claude_entry())
        code, out = run_cli(["run", "--foreground", *extra])
        self.assertEqual(code, EXIT_OK, out)
        return out

    def ok_line(self):
        log = ListLog()
        preflight(self.repo, log=log)
        return [ln for ln in log.lines if ln.startswith("preflight: ok: ")]


class ConfigKeys(LabelCase):
    """AC-1"""

    def test_the_defaults(self):
        cfg = self.parse("")
        self.assertEqual(
            # M3.38 added `internal`
            cfg.pr.release_labels,
            ["major", "minor", "patch", "docs", "internal", "skip-changelog"],
        )
        self.assertEqual(cfg.pr.kind_labels, {"feature": "minor", "fix": "patch", "docs": "docs"})

    def test_a_key_that_is_not_a_kind_is_named(self):
        text = self.problems('[pr]\nkind_labels = { feature = "minor", chore = "patch" }\n')
        self.assertIn("pr.kind_labels.chore", text)

    def test_a_value_outside_release_labels_is_named(self):
        text = self.problems('[pr]\nkind_labels = { fix = "hotpatch" }\n')
        self.assertIn("pr.kind_labels.fix", text)
        self.assertIn("hotpatch", text)

    def test_kind_labels_must_be_a_table(self):
        self.assertIn(
            "pr.kind_labels must be a table", self.problems('[pr]\nkind_labels = "minor"\n')
        )

    def test_a_label_gh_cannot_take_is_refused(self):
        self.assertIn("pr.release_labels", self.problems('[pr]\nrelease_labels = ["a,b"]\n'))
        self.assertIn("pr.release_labels", self.problems('[pr]\nrelease_labels = [" "]\n'))

    def test_an_empty_list_turns_the_feature_off(self):
        cfg = self.parse("[pr]\nrelease_labels = []\n")
        self.assertEqual(cfg.pr.release_labels, [])

        class Doc:
            kind = "feature"
            release = ""

        self.assertEqual(release_label(cfg, Doc()), "")


class ChangeMdField(LabelCase):
    """AC-2"""

    def test_the_ok_line_names_the_kinds_label(self):
        self.assertEqual(self.ok_line(), [OK_LINE % "; label minor"])

    def test_release_overrides_the_kind(self):
        self.front("release: major")
        self.assertEqual(self.ok_line(), [OK_LINE % "; label major"])

    def test_a_value_outside_release_labels_stops_preflight(self):
        self.front("release: minr")
        with self.assertRaises(Stop) as cm:
            preflight(self.repo)
        self.assertEqual(cm.exception.exit_code, EXIT_ERROR)
        self.assertIn(
            "change.md: release 'minr' is not one of [pr] release_labels", cm.exception.message
        )

    def test_turned_off_the_ok_line_has_no_label(self):
        self.config("[pr]\nrelease_labels = []\n")
        self.assertEqual(self.ok_line(), [OK_LINE % ""])

    def test_turned_off_a_release_field_stops_preflight(self):
        self.config("[pr]\nrelease_labels = []\n")
        self.front("release: minor")
        with self.assertRaises(Stop) as cm:
            preflight(self.repo)
        self.assertIn("change.md: release 'minor' is not one of", cm.exception.message)

    def test_a_kind_without_an_entry_has_no_label(self):
        self.config('[pr]\nkind_labels = { fix = "patch" }\n')
        self.assertEqual(self.ok_line(), [OK_LINE % ""])


class NewPr(LabelCase):
    """AC-3, AC-5"""

    def test_the_new_pr_gets_the_label_created_first(self):
        self.run_ok()
        gh = self.gh()
        create = [
            "label",
            "create",
            "minor",
            "--description",
            "Release notes group (set by revali)",
        ]
        self.assertEqual(self.label_creates(), [create])
        self.assertEqual(self.label_edits(), [["pr", "edit", "7", "--add-label", "minor"]])
        self.assertLess(gh.index(create), gh.index(["pr", "edit", "7", "--add-label", "minor"]))
        self.assertLess(
            [i for i, a in enumerate(gh) if a[:2] == ["pr", "create"]][0],
            gh.index(create),
        )
        self.assertIn("PR #7 release label: +minor", self.read(REVALI_LOG))

    def test_a_label_the_repository_has_is_not_created(self):
        self.scenario({"labels": ["bug", "Minor"]})  # GitHub matches label names in any case
        self.run_ok()
        self.assertEqual(self.label_creates(), [])
        self.assertEqual(self.label_edits(), [["pr", "edit", "7", "--add-label", "minor"]])

    def test_the_release_field_wins(self):
        self.front("release: patch")
        self.run_ok()
        self.assertEqual(self.label_edits(), [["pr", "edit", "7", "--add-label", "patch"]])

    def test_turned_off_nothing_is_labelled(self):
        self.config("[pr]\nrelease_labels = []\n")
        self.run_ok()
        self.assertEqual(self.label_edits(), [])
        self.assertEqual(self.label_creates(), [])
        self.assertEqual(self.label_lookups(), [])

    def test_a_failed_create_is_a_warning(self):
        self.scenario({"label_create_exit": 1})
        self.run_ok()
        log = self.read(REVALI_LOG)
        self.assertIn("warning: could not create the label 'minor'", log)
        self.assertEqual(self.label_edits(), [])  # nothing to add it with

    def test_a_failed_lookup_is_a_warning_and_the_label_is_still_tried(self):
        self.scenario({"label_view_exit": 1})
        self.run_ok()
        self.assertIn("warning: could not check the label 'minor'", self.read(REVALI_LOG))
        self.assertEqual(self.label_creates(), [])
        self.assertEqual(self.label_edits(), [["pr", "edit", "7", "--add-label", "minor"]])

    def test_the_lookup_is_one_exact_name(self):
        self.run_ok()
        self.assertEqual(self.label_lookups(), [["api", "repos/{owner}/{repo}/labels/minor"]])
        self.assertEqual([a for a in self.gh() if a[:2] == ["label", "list"]], [])

    def test_a_failed_edit_is_a_warning(self):
        self.scenario({"label_edit_exit": 1})
        out = self.run_ok()
        self.assertIn("READY TO MERGE", out)
        self.assertIn("warning: could not update the release label of PR #7", self.read(REVALI_LOG))

    def test_a_hung_edit_is_a_warning(self):
        from revali import pr

        real = pr.run_retry

        def fake(cmd, *args, **kwargs):
            if "--add-label" in cmd:
                raise ProcTimeout("timed out after 120s: gh")
            return real(cmd, *args, **kwargs)

        self.claude(claude_entry())
        with mock.patch.object(pr, "run_retry", side_effect=fake):
            code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn(
            "warning: could not update the release label of PR #7: timed out after 120s",
            self.read(REVALI_LOG),
        )


class ReusedPr(LabelCase):
    """AC-4"""

    def test_the_labels_are_made_to_match(self):
        self.scenario(open_pr("patch", "bug", "docs"))
        self.run_ok()
        self.assertEqual(
            self.label_edits(),
            [["pr", "edit", "12", "--add-label", "minor", "--remove-label", "patch,docs"]],
        )
        self.assertIn("PR #12 release label: +minor -patch -docs", self.read(REVALI_LOG))

    def test_a_matching_pr_is_left_alone(self):
        self.scenario(open_pr("bug", "minor"))
        self.run_ok()
        self.assertEqual(self.label_edits(), [])
        self.assertEqual(self.label_lookups(), [])

    def test_a_kind_without_an_entry_takes_the_release_labels_off(self):
        self.config('[pr]\nkind_labels = { fix = "patch" }\n')
        self.scenario(open_pr("minor", "bug"))
        self.run_ok()
        self.assertEqual(self.label_edits(), [["pr", "edit", "12", "--remove-label", "minor"]])

    def test_turned_off_the_pr_is_left_alone(self):
        self.config("[pr]\nrelease_labels = []\n")
        self.scenario(open_pr("patch"))
        self.run_ok()
        self.assertEqual(self.label_edits(), [])


class DryRun(LabelCase):
    """AC-6"""

    def test_it_names_the_label_and_touches_nothing(self):
        code, out = run_cli(["run", "--foreground", "--dry-run"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("dry run: would give the PR the release label 'minor'", self.read(REVALI_LOG))
        self.assertEqual(self.label_edits(), [])
        self.assertEqual(self.label_creates(), [])


class AtMerge(LabelCase):
    """AC-4: `revali merge` makes the labels match before it merges; a failure warns"""

    def test_a_stale_label_is_fixed_before_the_merge(self):
        self.run_ok()
        self.scenario({"pr_labels": ["patch", "bug"], "labels": ["bug", "minor", "patch"]})
        code, out = run_cli(["merge"])
        self.assertEqual(code, EXIT_OK, out)
        gh = self.gh()
        view = ["pr", "view", "7", "--json", "labels"]
        edit = ["pr", "edit", "7", "--add-label", "minor", "--remove-label", "patch"]
        merge_at = [i for i, a in enumerate(gh) if a[:2] == ["pr", "merge"]]
        self.assertEqual(len(merge_at), 1)
        self.assertLess(gh.index(view), gh.index(edit))
        self.assertLess(gh.index(edit), merge_at[0])
        self.assertIn("PR #7 release label: +minor -patch", out)

    def test_a_matching_pr_is_not_edited(self):
        self.run_ok()
        before = len(self.label_edits())
        self.scenario({"pr_labels": ["minor"]})
        code, out = run_cli(["merge"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertEqual(len(self.label_edits()), before)

    def test_a_failed_edit_warns_and_merges(self):
        self.run_ok()
        self.scenario({"pr_labels": ["patch"], "labels": ["minor", "patch"], "label_edit_exit": 1})
        code, out = run_cli(["merge"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("warning: could not update the release label of PR #7", out)
        self.assertEqual(len(self.merges()), 1)

    def test_unreadable_labels_warn_and_merge(self):
        self.run_ok()
        self.scenario({"pr_view_exit": 1})
        code, out = run_cli(["merge"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("warning: could not read the PR's labels", out)
        self.assertEqual(len(self.merges()), 1)


class ReviewRound1(LabelCase):
    """PR round 1: F1 a `release:` edited after the run is checked again at merge; F2 the
    error for a list changed alone says what to do; F3 label names compare in any case;
    follow-up: a label another run created since the lookup is not a failure."""

    def test_a_release_edited_after_the_run_changes_no_label_at_merge(self):  # F1
        self.run_ok()
        creates, edits = len(self.label_creates()), len(self.label_edits())
        self.scenario({"pr_labels": ["minor"]})
        self.front("release: majr")
        code, out = run_cli(["merge"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("warning: change.md: release 'majr' is not one of [pr] release_labels", out)
        self.assertIn("the PR's labels are left as they are", out)
        self.assertEqual(len(self.label_creates()), creates)  # no `gh label create majr`
        self.assertEqual(len(self.label_edits()), edits)  # `minor` stays on the PR
        self.assertEqual(len(self.merges()), 1)

    def test_a_release_with_a_comma_is_refused_too(self):  # F1: would be two labels to gh
        self.front("release: major,minor")
        with self.assertRaises(Stop) as cm:
            preflight(self.repo)
        self.assertIn("change.md: release 'major,minor' is not one of", cm.exception.message)

    def test_a_list_changed_alone_says_to_set_kind_labels(self):  # F2
        text = self.problems('[pr]\nrelease_labels = ["feat", "bugfix"]\n')
        self.assertIn("pr.kind_labels.feature", text)
        # round 2 follow-up: the hint names the case exactly (dropping a mapped label)
        self.assertIn("a layer that drops a label kind_labels maps to sets kind_labels too", text)
        self.assertIn("sets `kind_labels` too; adding a label needs no more", self.flat_doc())

    def test_a_list_that_only_adds_a_label_needs_no_kind_labels(self):  # round 2 follow-up
        cfg = self.parse(
            '[pr]\nrelease_labels = ["major", "minor", "patch", "docs", "skip-changelog", '
            '"security"]\n'
        )
        self.assertEqual(cfg.pr.release_labels[-1], "security")
        self.assertEqual(cfg.pr.kind_labels["feature"], "minor")

    def test_a_label_listed_twice_in_any_case_is_refused(self):  # round 2 follow-up
        text = self.problems('[pr]\nrelease_labels = ["minor", "patch", "Minor", "docs"]\n')
        self.assertIn(
            "pr.release_labels lists 'Minor' more than once (label names compare in any case)",
            text,
        )

    def flat_doc(self):
        with open(os.path.join(ROOT, "docs", "configuration.md"), encoding="utf-8") as fh:
            return " ".join(fh.read().split())

    def test_release_in_another_case_gets_the_lists_spelling(self):  # F3
        self.front("release: Major")
        self.assertEqual(self.ok_line(), [OK_LINE % "; label major"])
        self.run_ok()
        self.assertEqual(self.label_edits(), [["pr", "edit", "7", "--add-label", "major"]])

    def test_a_kind_label_in_another_case_is_accepted(self):  # F3
        self.config('[pr]\nkind_labels = { feature = "MINOR" }\n')
        self.assertEqual(self.ok_line(), [OK_LINE % "; label minor"])

    def test_a_label_created_since_the_lookup_is_applied(self):  # follow-up
        self.scenario({"label_create_exists": ["minor"]})
        self.run_ok()
        self.assertEqual(len(self.label_creates()), 1)
        self.assertNotIn("could not create the label", self.read(REVALI_LOG))
        self.assertEqual(self.label_edits(), [["pr", "edit", "7", "--add-label", "minor"]])


class Files(LabelCase):
    """AC-7, AC-8"""

    def text(self, *parts):
        with open(os.path.join(ROOT, *parts), encoding="utf-8") as fh:
            return fh.read()

    def flat(self, *parts):
        return " ".join(self.text(*parts).split())

    def test_release_yml_groups_by_the_labels(self):
        text = self.text("templates", "release.yml")
        order = [
            text.index("- title: %s" % t)
            for t in ("Major", "Minor", "Patch", "Docs", "Other", "Internal")  # M3.38
        ]
        self.assertEqual(order, sorted(order))
        for label in ("major", "minor", "patch", "docs", "internal"):
            self.assertIn("- %s\n" % label, text)
        exclude = text[text.index("exclude:") : text.index("categories:")]
        self.assertIn("skip-changelog", exclude)
        self.assertIn('- "*"', text)

    def test_revalis_own_release_yml_is_the_template(self):
        self.assertEqual(self.text(".github", "release.yml"), self.text("templates", "release.yml"))

    def test_the_documents(self):
        config = self.flat("docs", "configuration.md")
        self.assertIn("release_labels", config)
        self.assertIn("kind_labels", config)
        template = self.text("templates", "revali.toml")
        self.assertIn("[pr]", template)
        self.assertIn("release_labels", template)
        self.assertIn("kind_labels", template)
        side = self.flat("docs", "side-effects.md")
        self.assertIn("gh label create", side)
        self.assertIn("--add-label", side)
        self.assertIn("--remove-label", side)
        self.assertIn("release:", self.text("templates", "change.md"))
        self.assertIn("release", self.flat("skill", "SKILL.md"))
        releasing = self.flat("docs", "releasing.md")
        self.assertIn(
            'gh release create v<version> --verify-tag --generate-notes --notes "<lead line>"',
            releasing,
        )
        self.assertIn("templates/release.yml", releasing)
