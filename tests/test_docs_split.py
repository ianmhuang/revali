"""feature/docs-split: the README as a front page with the reference material under docs/
(AC-1, AC-2, AC-4, AC-10, AC-11), version 0.2.0 (AC-5), and the PR #24 follow-ups in
`merge` and `stop` (AC-6 to AC-9). AC-3 (moved text is verbatim) is checked against the
previous README by hand; AC-12 is the rest of the suite."""
import os
import re
import subprocess
import sys
import unittest
from unittest import mock

from tests.helpers import RepoCase, git, run_cli
from revali import EXIT_ERROR, EXIT_OK, VERSION
from revali import gitops, merge, pipeline, procs
from revali.state import LockHeld, State, lock_path, tree_lock_path, write_json_atomic

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DOCS = ("workflow.md", "configuration.md", "files.md", "sandbox.md", "side-effects.md")


def read(*parts):
    with open(os.path.join(ROOT, *parts), "r", encoding="utf-8", newline="") as fh:
        return fh.read()


def h2_section(text, title):
    body = text.split("\n## %s\n" % title, 1)[1]
    return body.split("\n## ", 1)[0]


class ReadmeFrontPage(unittest.TestCase):
    """AC-1"""

    def setUp(self):
        self.text = read("README.md")

    def test_length(self):
        self.assertLessEqual(len(self.text.splitlines()), 170)

    def test_parts_in_order(self):
        markers = ["- **Developer**", "- **Reviewer**", "- **Validator**", "**Why separate sessions.**",
                   "```mermaid", "sequenceDiagram", "| Role |", "Three user actions", "Exit codes:",
                   "\nStatus:", "\n## Requirements\n", "\n## Usage\n", "\n## Documentation\n",
                   "\n## Development\n", "\n## License\n"]
        positions = [self.text.index(m) for m in markers]
        self.assertEqual(positions, sorted(positions), markers)

    def test_why_paragraph_is_at_most_four_sentences(self):
        m = re.search(r"\*\*Why separate sessions\.\*\*(.*?)\n\n", self.text, flags=re.S)
        self.assertIsNotNone(m)
        body = " ".join(m.group(1).split())
        sentences = [s for s in re.split(r"(?<=[.!?])\s+(?=[A-Z`(])", body) if s.strip()]
        self.assertLessEqual(len(sentences), 4, sentences)
        self.assertIn("docs/workflow.md", body)

    def test_documentation_index_links_every_docs_file(self):
        index = h2_section(self.text, "Documentation")
        for name in sorted(os.listdir(os.path.join(ROOT, "docs"))):
            self.assertIn("`docs/%s`" % name, index, name)
        self.assertEqual(set(os.listdir(os.path.join(ROOT, "docs"))), set(DOCS))

    def test_moved_sections_are_gone_from_the_readme(self):
        for heading in ("## Configuration", "## Files", "## Workflow", "## Several agents on one repository",
                        "## Sandbox", "## Project setup", "## What revali does to your repository"):
            self.assertNotIn(heading, self.text, heading)


class DocsHoldTheSections(unittest.TestCase):
    """AC-2"""

    def test_workflow(self):
        text = read("docs", "workflow.md")
        self.assertTrue(text.startswith("# Workflow\n"))
        for title in ("Why separate sessions", "Workflow", "Several agents on one repository", "Running",
                      "Project setup"):
            self.assertIn("\n## %s\n" % title, text, title)
        why = " ".join(h2_section(text, "Why separate sessions").split())
        self.assertIn("nobody grades their own work", why)
        self.assertIn("the engine seam is where a second vendor would", why)   # the full paragraph
        self.assertIn("status: draft", h2_section(text, "Workflow"))
        self.assertIn("templates/CLAUDE-snippet.md", h2_section(text, "Project setup"))
        running = h2_section(text, "Running")
        for phrase in ("`repo: <working tree root>  branch: <branch>`", "`.revali/tree.lock`",
                       "After exit code 2", "died at stage", "skips the reviewer and goes to validation"):
            self.assertIn(phrase, running, phrase)

    def test_sandbox(self):
        text = read("docs", "sandbox.md")
        self.assertTrue(text.startswith("# Sandbox\n"))
        for phrase in ('`[validate.linux] runner = "wsl"`', '`runner = "ssh"`', '`runner = "local"`', "BatchMode"):
            self.assertIn(phrase, text, phrase)

    def test_side_effects(self):
        text = read("docs", "side-effects.md")
        self.assertTrue(text.startswith("# What revali does to your repository\n"))
        self.assertIn("Read this before the first run.", text)
        self.assertIn("never runs on a repo you do not own", text)

    def test_files(self):
        text = read("docs", "files.md")
        self.assertTrue(text.startswith("# Files\n"))
        self.assertIn("| Document | Written by | Read by | Default location | Config key |", text)
        self.assertIn("Branch `feature/x` maps to directory `feature__x`.", text)
        self.assertIn("`REVALI_HOME` environment variable", text)

    def test_configuration(self):
        text = read("docs", "configuration.md")
        self.assertTrue(text.startswith("# Configuration\n"))
        self.assertIn("Three layers, the most specific wins", text)
        self.assertIn('`model = "auto"`', text)
        self.assertIn("`REVALI_DISABLE=1`", text)


class CrossReferences(unittest.TestCase):
    """AC-4"""

    def test_conventions_point_at_docs(self):
        text = read("CONVENTIONS.md")
        self.assertIn("`docs/side-effects.md`", text)
        self.assertNotIn('"What revali does to your repository"', text)
        self.assertIn("`docs/`", text)

    def test_template_conventions_mention_docs(self):
        self.assertIn("`docs/`", read("templates", "CONVENTIONS.md"))

    def test_defaults_comment(self):
        text = read("defaults.toml")
        self.assertIn("docs/configuration.md", text)
        self.assertNotIn('README "Configuration"', text)

    def test_nothing_points_at_a_moved_readme_section(self):
        moved = ("Configuration", "Files", "Workflow", "Sandbox", "Project setup",
                 "What revali does to your repository", "Several agents")
        for rel in ("skill/SKILL.md", "CONVENTIONS.md", "templates/CONVENTIONS.md", "templates/CLAUDE-snippet.md",
                    "CLAUDE.md", "defaults.toml", "checklists/default.md"):
            text = read(*rel.split("/"))
            for line in text.splitlines():
                if "README" in line:
                    for title in moved:
                        self.assertNotIn('"%s"' % title, line, "%s: %s" % (rel, line))


class Version(unittest.TestCase):
    """AC-5"""

    def test_version_constant(self):
        self.assertEqual(VERSION, "0.2.0")

    def test_version_command(self):
        code, out = run_cli(["version"])
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(out.strip(), "revali 0.2.0")

    def test_status_line(self):
        text = read("README.md")
        line = next(l for l in text.splitlines() if l.startswith("Status:"))
        self.assertIn("0.2.0", line)
        self.assertNotIn("0.1.0", text)


class ReadyRepo(RepoCase):
    def setUp(self):
        super().setUp()
        os.environ["REVALI_POLL_SECONDS"] = "0.01"

    def ready(self, root):
        rdir = os.path.join(root, ".revali", "feature__mul")
        State(repo="owner/repo", branch="feature/mul", base="main", stage="ready_to_merge",
              message="validation 1 passed", last_exit=EXIT_OK, pr_number=7,
              head_sha=gitops.rev_parse("HEAD", root), test_files=["tests/test_review_mul.py"]).save(rdir)
        git(["push", "-q", "-u", "origin", "feature/mul"], root)
        return rdir

    def add_worktree(self, branch):
        path = os.path.join(self.tmp, "wt-" + branch.replace("/", "__"))
        git(["worktree", "add", "--quiet", path, branch], self.repo)

        def drop():
            os.chdir(self.repo)
            subprocess.run(["git", "worktree", "remove", "--force", path], cwd=self.repo, capture_output=True)

        self.addCleanup(drop)
        return path

    @staticmethod
    def failing_git(*prefix):
        """A `merge.run` replacement that fails the git call starting with `prefix` and passes
        every other call through."""
        real = merge.run
        wanted = " ".join(prefix)

        def fake(argv, **kw):
            if wanted in " ".join(argv):
                return procs.Result(argv, 1, "", "error: fake refusal for %s\n" % wanted, 0.0)
            return real(argv, **kw)
        return fake


class RefusalBeforeTheCiWait(ReadyRepo):
    """AC-6"""

    def test_primary_tree_is_refused_before_wait_for_checks(self):
        linked = self.add_worktree("main")
        rdir = self.ready(self.repo)
        with mock.patch.object(merge, "wait_for_checks", side_effect=AssertionError("CI wait must not run")):
            code, out = run_cli(["merge"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertIn("ERROR: main is checked out in", out)
        self.assertIn(os.path.normpath(linked), os.path.normpath(out))
        self.assertIn("remove or switch that worktree, then merge again", out)
        line = next(l for l in out.splitlines() if "ERROR: main is checked out in" in l)
        self.assertIn('docs/workflow.md, "Several agents on one repository"', line)
        self.assertNotIn("git worktree add", line)      # cannot run while this tree holds the branch
        self.assertNotIn("git checkout", line)
        self.assertFalse(any(c["argv"][:2] == ["pr", "checks"] for c in self.fake_calls("gh")))
        self.assertFalse(any(c["argv"][:2] == ["pr", "merge"] for c in self.fake_calls("gh")))
        self.assertEqual(State.load(rdir).stage, "ready_to_merge")


class BranchDeleteFailure(ReadyRepo):
    """AC-7"""

    def test_primary_tree_reports_a_kept_branch(self):
        self.ready(self.repo)
        # the fake gh does not delete the local branch, so revali's own `git branch -D` runs
        with mock.patch.object(merge, "run", self.failing_git("branch", "-D")):
            code, out = run_cli(["merge"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("MERGED: PR #7 into main", out)
        self.assertIn("branch feature/mul kept (git branch -D failed: error: fake refusal", out)
        self.assertNotIn("branch feature/mul removed", out)
        self.assertEqual(gitops.current_branch(self.repo), "main")
        self.assertIsNotNone(gitops.rev_parse("feature/mul", self.repo))

    def test_worktree_mode_reports_a_kept_branch(self):
        git(["checkout", "-q", "main"], self.repo)
        wt = self.add_worktree("feature/mul")
        os.chdir(wt)
        self.ready(wt)
        with mock.patch.object(merge, "run", self.failing_git("branch", "-D")):
            code, out = run_cli(["merge"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("detached at the merged main, local branch feature/mul kept (git branch -D failed: "
                      "error: fake refusal", out)
        self.assertIn("git worktree remove", out)
        self.assertEqual(gitops.current_branch(wt), "HEAD")
        self.assertIsNotNone(gitops.rev_parse("feature/mul", wt))

    def test_success_still_says_removed(self):
        self.ready(self.repo)
        code, out = run_cli(["merge"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("local: on main, branch feature/mul removed", out)
        self.assertNotIn("kept", out)


class BranchLockRace(ReadyRepo):
    """AC-8"""

    def test_lock_held_between_check_and_acquire_is_an_error_line(self):
        rdir = self.ready(self.repo)

        def held(rdir, pid=None):
            raise LockHeld(4242, "2026-09-05T00:00:00")

        with mock.patch.object(pipeline, "acquire_lock", held), \
             mock.patch.object(merge, "do_merge", side_effect=AssertionError("must not run")):
            code, out = run_cli(["merge"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertIn("ERROR: another revali run holds the lock (pid 4242", out)
        self.assertNotIn("Traceback", out)
        self.assertFalse(os.path.isfile(tree_lock_path(self.repo, ".revali")))
        self.assertFalse(os.path.isfile(lock_path(rdir)))
        self.assertEqual(State.load(rdir).stage, "ready_to_merge")


def dead_pid():
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


class DetachedStopOnADeadRun(RepoCase):
    """AC-9"""

    def tree_lock(self):
        return tree_lock_path(self.repo, ".revali")

    def test_stale_tree_lock_names_the_dead_runs_branch(self):
        State(repo="owner/repo", branch="feature/mul", base="main", stage="review",
              message="reviewer round 1", last_exit=-1, pr_number=7).save(self.rdir())
        write_json_atomic(lock_path(self.rdir()), {"pid": dead_pid(), "since": "x"})
        write_json_atomic(self.tree_lock(), {"pid": dead_pid(), "branch": "feature/mul", "since": "x"})
        git(["checkout", "-q", "--detach"], self.repo)
        code, out = run_cli(["stop"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertEqual(out.splitlines()[0], "repo: %s  branch: feature/mul" % gitops.repo_root(self.repo))
        self.assertIn("found dead at stage 'review' is now recorded as stopped", out)
        state = State.load(self.rdir())
        self.assertEqual((state.stage, state.last_exit), ("stopped", EXIT_ERROR))
        self.assertIn("found dead at stage 'review'", state.message)
        self.assertFalse(os.path.isfile(self.tree_lock()))
        self.assertFalse(os.path.isfile(lock_path(self.rdir())))
        self.assertEqual(gitops.current_branch(self.repo), "HEAD")
        self.assertFalse(os.path.exists(os.path.join(self.repo, ".revali", "HEAD")))

    def test_without_a_tree_lock_the_dead_run_is_left_and_no_run_is_reported(self):
        State(repo="owner/repo", branch="feature/mul", base="main", stage="review",
              message="reviewer round 1", last_exit=-1).save(self.rdir())
        git(["checkout", "-q", "--detach"], self.repo)
        code, out = run_cli(["stop"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertEqual(out.splitlines()[0], "repo: %s  branch: HEAD" % gitops.repo_root(self.repo))
        self.assertIn("no run in progress", out)
        self.assertEqual(State.load(self.rdir()).stage, "review")

    def test_on_a_branch_the_stale_tree_lock_changes_nothing(self):
        # on the branch itself the branch lock and state are found directly; the stale record
        # names the same branch and is cleared as before
        State(repo="owner/repo", branch="feature/mul", base="main", stage="validate",
              message="validation 1", last_exit=-1).save(self.rdir())
        write_json_atomic(self.tree_lock(), {"pid": dead_pid(), "branch": "feature/mul", "since": "x"})
        code, out = run_cli(["stop"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("found dead at stage 'validate'", out)
        self.assertEqual(State.load(self.rdir()).stage, "stopped")
        self.assertFalse(os.path.isfile(self.tree_lock()))

    def test_on_a_branch_a_stale_record_for_another_branch_is_still_ignored(self):
        # PR #22 behaviour kept: only a detached HEAD follows a stale record
        other = os.path.join(self.repo, ".revali", "feature__other")
        State(repo="owner/repo", branch="feature/other", base="main", stage="review",
              message="reviewer round 1", last_exit=-1).save(other)
        write_json_atomic(self.tree_lock(), {"pid": dead_pid(), "branch": "feature/other", "since": "x"})
        code, out = run_cli(["stop"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertEqual(out.splitlines()[0], "repo: %s  branch: feature/mul" % gitops.repo_root(self.repo))
        self.assertIn("no run in progress", out)
        self.assertEqual(State.load(other).stage, "review")
        self.assertFalse(os.path.isfile(self.tree_lock()))


class WorktreeModeCondition(unittest.TestCase):
    """AC-10"""

    def test_several_agents_states_when_worktree_mode_applies(self):
        body = " ".join(h2_section(read("docs", "workflow.md"), "Several agents on one repository").split())
        self.assertIn("only when the base branch is checked out in another worktree", body)
        self.assertIn("merges like the primary tree, with `--delete-branch`", body)


class VerificationRecord(unittest.TestCase):
    """AC-11"""

    def test_sandbox_doc_records_the_ssh_verification(self):
        record = h2_section(read("docs", "sandbox.md"), "Verification record")
        self.assertRegex(record, r"\b2026-\d\d-\d\d\b")
        for phrase in ("sshd", "key-only login", "private repository", "APPROVE", "PASS", "`revali merge`",
                       "`sandbox_dir`"):
            self.assertIn(phrase, record, phrase)
        status = next(l for l in read("README.md").splitlines() if l.startswith("Status:"))
        text = read("README.md")
        para = text[text.index(status):].split("\n\n", 1)[0]
        self.assertIn("`docs/sandbox.md`", para)


if __name__ == "__main__":
    unittest.main()
