"""AC-1, AC-2, AC-4 and AC-7 of fix/leftover-lows: the newest commit in base..HEAD that
added a path under test_dir decides whose it is. A file the author deleted and re-created
under the reviewer's file name, in a commit without the `Revali-Round` trailer, is the
author's: the prompt lists it among the files that are not the reviewer's, a reviewer edit is
restored and the reviewer sent back once, the state drops the path and the log names the
path and the commit that re-added it, once. An author edit keeps the owner; a file the
reviewer re-creates after the author deleted it is the reviewer's again. After a rewrite,
the recovery line names the trailer commits none of whose files survive, also when other
trailer commits still have files. `docs/side-effects.md` states the rule.

Round 2: replacing the content in a single commit is a modification to git and keeps the
owner, as the docs now say; a state that kept its files but lost its commits gets the
emptied commits on a line of their own; the newest add is looked up once per path."""

import json
import os
import unittest
from unittest import mock

from revali import EXIT_OK, gitops
from revali.state import State
from tests import test_rebase_ownership as ro
from tests.helpers import TEST_REVIEW_MUL, approve_response, claude_entry, git, run_cli

HERE = os.path.dirname(os.path.abspath(__file__))
FILE = "tests/test_review_mul.py"
SECOND = "tests/test_review_mul2.py"
MINE = (
    "import unittest\n\n"
    "from src.calc import mul\n\n\n"
    "class MineNow(unittest.TestCase):\n"
    "    def test_two(self):\n"
    "        self.assertEqual(mul(2, 2), 4)\n"
)


def second_file_entry():
    """A reviewer answer that writes only tests/test_review_mul2.py."""
    entry = claude_entry(
        approve_response(
            tests=[{"path": SECOND, "purpose": "p", "covers": ["AC-1", "AC-2"], "expected": "e"}]
        ),
        write_tests=False,
    )
    entry["write_files"] = {SECOND: TEST_REVIEW_MUL.replace("MulTests", "MulTests2")}
    return entry


def head(repo):
    return git(["rev-parse", "HEAD"], repo).strip()


def log_lines(case):
    """Every line of the stage log (`revali.log`) under the branch's state directory."""
    lines = []
    for root, _, files in os.walk(case.rdir()):
        for name in files:
            if name == "revali.log":
                with open(os.path.join(root, name), "r", encoding="utf-8", newline="") as fh:
                    lines += fh.read().splitlines()
    return lines


class AuthorTakesOverByRecreating(ro.RewriteCase):
    def take_over(self):
        """Delete the reviewer's file in one commit, re-create the name in another."""
        git(["rm", "-q", FILE], self.repo)
        git(["commit", "-q", "-m", "delete the reviewer's test"], self.repo)
        self.write(FILE, MINE)
        self.commit_all("the same name, my own test")
        return head(self.repo)

    def test_the_recreated_file_is_protected_and_leaves_the_state(self):  # AC-1
        self.first_round()
        recreated_by = self.take_over()
        reviewer_edit = claude_entry(approve_response())
        reviewer_edit["write_files"][FILE] = ro.UPDATED  # the reviewer treats it as its own
        self.claude(reviewer_edit, second_file_entry())
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertNotIn("starts over", out)  # no rewrite: the round-1 commit is still in HEAD
        # AC-1: the prompt lists the file among those that are not the reviewer's
        ps = ro.prompts(self)
        self.assertEqual(len(ps), 2, "the reviewer must be sent back once")
        self.assertIn(FILE, ro.section(ps[0], ro.NOT_YOURS))
        self.assertNotIn(FILE, ro.section(ps[0], ro.EARLIER))
        self.assertIn(FILE, ps[1].split("Corrections required", 1)[1])
        # AC-1: the edit was restored from HEAD, the tree is clean
        self.assertEqual(self.read(FILE), MINE)
        self.assertEqual(git(["status", "--porcelain", "--", "tests"], self.repo).strip(), "")
        # AC-1: the state dropped the path; the log names the path and the commit
        state = State.load(self.rdir())
        self.assertEqual(state.test_files, [SECOND])
        naming = [ln for ln in log_lines(self) if FILE in ln and recreated_by[:10] in ln]
        self.assertEqual(len(naming), 1, log_lines(self))
        on_stdout = [ln for ln in out.splitlines() if FILE in ln and recreated_by[:10] in ln]
        self.assertEqual(len(on_stdout), 1, out)

    def test_the_drop_is_logged_once(self):  # AC-1: a later run stays quiet
        self.first_round()
        recreated_by = self.take_over()
        self.claude(second_file_entry())
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertEqual(State.load(self.rdir()).test_files, [SECOND])
        self.fix_and_commit("another fix")
        self.claude(claude_entry(approve_response(), write_tests=False))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertNotIn(recreated_by[:10], out)
        self.assertNotIn("recovered", out)
        naming = [ln for ln in log_lines(self) if FILE in ln and recreated_by[:10] in ln]
        self.assertEqual(len(naming), 1, log_lines(self))
        self.assertEqual(State.load(self.rdir()).test_files, [SECOND])
        # the author's file is still listed as one the reviewer must not touch
        self.assertIn(FILE, ro.section(ro.prompts(self)[-1], ro.NOT_YOURS))


class OwnershipFollowsTheNewestAdd(ro.RewriteCase):
    def test_an_author_edit_does_not_transfer_the_file(self):  # AC-2 (unchanged behaviour)
        self.first_round()
        self.write(FILE, self.read(FILE) + "\n# the author touched it\n")
        self.commit_all("touch the reviewer's test")
        entry = claude_entry(approve_response())
        entry["write_files"][FILE] = ro.UPDATED
        self.claude(entry)
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertNotIn("re-created", out)
        ps = ro.prompts(self)
        self.assertEqual(len(ps), 1, "no bounce: the file is still the reviewer's")
        self.assertIn(FILE, ro.section(ps[0], ro.EARLIER))
        self.assertNotIn(FILE, ro.section(ps[0], ro.NOT_YOURS))
        self.assertEqual(self.read(FILE), ro.UPDATED)
        self.assertEqual(State.load(self.rdir()).test_files, [FILE])

    def test_replacing_the_content_in_one_commit_keeps_the_owner(self):  # AC-2, round 1 F1
        """Delete and re-create inside one commit is a modification to git: the file stays
        the reviewer's, which is what docs/side-effects.md now says."""
        self.first_round()
        os.remove(os.path.join(self.repo, FILE))
        self.write(FILE, MINE)
        self.commit_all("replace the reviewer's test in place")
        replaced_by = head(self.repo)
        self.assertEqual(gitops.commit_paths(replaced_by, self.repo, diff_filter="A"), [])
        self.assertEqual(gitops.commit_paths(replaced_by, self.repo, diff_filter="M"), [FILE])
        entry = claude_entry(approve_response())
        entry["write_files"][FILE] = ro.UPDATED
        self.claude(entry)
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertNotIn("re-created", out)
        ps = ro.prompts(self)
        self.assertEqual(len(ps), 1, "no bounce: a modification does not transfer the file")
        self.assertIn(FILE, ro.section(ps[0], ro.EARLIER))
        self.assertEqual(self.read(FILE), ro.UPDATED)
        self.assertEqual(State.load(self.rdir()).test_files, [FILE])

    def test_a_file_the_reviewer_recreates_is_the_reviewers_again(self):  # AC-2
        self.first_round()
        git(["rm", "-q", FILE], self.repo)
        git(["commit", "-q", "-m", "delete the reviewer's test"], self.repo)
        self.claude(claude_entry(approve_response()))  # round 2 writes the file anew
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertNotIn("re-created", out)
        self.assertEqual(len(ro.prompts(self)), 1)
        self.assertEqual(self.read(FILE), TEST_REVIEW_MUL)
        self.assertEqual(len(ro.trailer_commits(self.repo)), 2)
        self.assertEqual(State.load(self.rdir()).test_files, [FILE])
        # a third run: the reviewer may update the file it re-created, without a bounce
        if os.path.isfile(self.fake_log):
            os.remove(self.fake_log)
        self.fix_and_commit("polish")
        entry = claude_entry(approve_response())
        entry["write_files"][FILE] = ro.UPDATED
        self.claude(entry)
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertNotIn("re-created", out)
        self.assertEqual(len(ro.prompts(self)), 1)
        self.assertIn(FILE, ro.section(ro.prompts(self)[0], ro.EARLIER))
        self.assertEqual(self.read(FILE), ro.UPDATED)
        self.assertEqual(State.load(self.rdir()).test_files, [FILE])


class EmptiedTrailerCommitIsNamed(ro.RewriteCase):
    def two_trailer_commits(self):
        """Round 1 commits FILE, round 2 commits SECOND; returns their SHAs, oldest first."""
        self.first_round()
        self.fix_and_commit()
        self.claude(second_file_entry())
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        shas = ro.trailer_commits(self.repo)
        self.assertEqual(len(shas), 2)
        return shas

    def test_the_recovery_line_names_the_commit_with_no_file_left(self):  # AC-4
        self.two_trailer_commits()
        git(["rm", "-q", SECOND], self.repo)
        git(["commit", "-q", "-m", "drop the second test"], self.repo)
        self.move_main()
        git(["rebase", "-q", "main"], self.repo)
        kept, emptied = ro.trailer_commits(self.repo)
        self.claude(claude_entry(approve_response(), write_tests=False))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("starts over", out)
        recovered = [ln for ln in out.splitlines() if "recovered" in ln]
        self.assertEqual(len(recovered), 1, out)
        line = recovered[0]
        self.assertIn(FILE, line)  # the first commit's file was recovered
        self.assertIn("none of", line)  # AC-4: and the emptied commit is named on the same line
        tail = line.split("none of", 1)[1]
        self.assertIn(emptied[:10], tail)
        self.assertNotIn(kept[:10], tail)
        state = State.load(self.rdir())
        self.assertEqual(state.test_files, [FILE])
        self.assertEqual(state.test_commits[:2], [kept, emptied])

    def test_a_run_where_nothing_changed_stays_quiet(self):  # AC-4: written once
        self.two_trailer_commits()
        git(["rm", "-q", SECOND], self.repo)
        git(["commit", "-q", "-m", "drop the second test"], self.repo)
        self.move_main()
        git(["rebase", "-q", "main"], self.repo)
        self.claude(claude_entry(approve_response(), write_tests=False))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("none of", out)
        self.fix_and_commit("once more")
        self.claude(claude_entry(approve_response(), write_tests=False))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertNotIn("none of", out)
        self.assertNotIn("recovered", out)
        self.assertEqual(State.load(self.rdir()).test_files, [FILE])

    def test_a_state_that_lost_its_commits_names_the_emptied_one(self):  # AC-4, round 1 F4
        """No file to recover (the state kept them), no rewrite, yet both trailer commits are
        new to the state: the one with no file left is named once, on a line of its own."""
        first, second = self.two_trailer_commits()
        git(["rm", "-q", SECOND], self.repo)
        git(["commit", "-q", "-m", "drop the second test"], self.repo)
        path = State.path(self.rdir())
        with open(path, "r", encoding="utf-8", newline="") as fh:
            data = json.load(fh)
        data["test_commits"] = []
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(data, fh)
        self.claude(claude_entry(approve_response(), write_tests=False))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertNotIn("starts over", out)
        self.assertNotIn("recovered", out)
        naming = [ln for ln in out.splitlines() if "none of" in ln]
        self.assertEqual(len(naming), 1, out)
        self.assertIn(second[:10], naming[0])
        self.assertNotIn(first[:10], naming[0])
        state = State.load(self.rdir())
        self.assertEqual(state.test_commits[:2], [first, second])
        self.assertEqual(state.test_files, [FILE, SECOND])  # kept: the state is not weakened
        # the next run has nothing new and stays quiet
        self.fix_and_commit("once more")
        self.claude(claude_entry(approve_response(), write_tests=False))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertNotIn("none of", out)
        self.assertNotIn("recovered", out)


class NewestAddLookedUpOncePerPath(ro.RewriteCase):
    def test_a_path_in_two_trailer_commits_is_looked_up_once(self):  # round 1 F3
        self.first_round()
        self.fix_and_commit()
        entry = claude_entry(approve_response(verdict="CHANGES_REQUESTED", findings=[ro.HIGH]))
        entry["write_files"][FILE] = ro.UPDATED  # round 2 modifies the round-1 file
        self.claude(entry)
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(len(ro.trailer_commits(self.repo)), 2, out)
        self.fix_and_commit("again")
        self.claude(claude_entry(approve_response(), write_tests=False))
        with mock.patch("revali.gitops.last_add_commit", wraps=gitops.last_add_commit) as spy:
            code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        looked_up = [c.args[2] for c in spy.call_args_list]
        self.assertEqual(looked_up.count(FILE), 1, looked_up)
        self.assertEqual(State.load(self.rdir()).test_files, [FILE])


class SideEffectsStateTheRule(unittest.TestCase):
    def test_the_ownership_sentence_covers_delete_and_recreate(self):  # AC-7
        path = os.path.join(os.path.dirname(HERE), "docs", "side-effects.md")
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read()
        bullets = [b for b in text.split("\n- ") if "Revali-Round" in b and "test_dir" in b]
        self.assertTrue(bullets, "the trailer bullet is missing from docs/side-effects.md")
        bullet = bullets[0]
        self.assertIn("added a path", bullet)
        self.assertIn("re-create", bullet)
        self.assertIn("delete", bullet)
        # round 1 F1: the rule as git sees it, and how to hand a file back
        self.assertIn("single commit", bullet)
        self.assertIn("modification", bullet)
        self.assertIn("hand a file back", bullet)


if __name__ == "__main__":
    unittest.main()
