"""fix/archive-followups AC-2: `revali status` prints `tests: commit` or `tests: archive`
from the mode the state recorded once the branch has a review round; before the first round
the line is absent. A state that has rounds but no recorded mode (written before the key
existed) runs as commit, so status says `tests: commit` for it. The `round:` counter on the
line above counts the recorded rounds. Black-box through the CLI on the fixture repo."""

import os
import re
import unittest

from revali import EXIT_ACTION, EXIT_OK
from revali.state import State
from tests.helpers import TEST_REVIEW_MUL, RepoCase, approve_response, claude_entry, run_cli

FILE = "tests/test_review_mul.py"
LINE = re.compile(r"^tests: (\S+)$", re.MULTILINE)
COUNTERS = re.compile(r"^round: (\d+), fixes: (\d+), cost: \$[\d.]+$", re.MULTILINE)


def approving():
    tests = [
        {"path": FILE, "purpose": "acceptance", "covers": ["AC-1", "AC-2"], "expected": "per AC"}
    ]
    entry = claude_entry(approve_response(tests=tests), write_tests=False)
    entry["write_files"] = {FILE: TEST_REVIEW_MUL}
    return entry


def requesting_changes():
    entry = approving()
    entry["structured_output"].update(
        verdict="CHANGES_REQUESTED",
        findings=[
            {
                "id": "F1",
                "file": "src/calc.py",
                "line": 3,
                "severity": "high",
                "kind": "correctness",
                "text": "mul ignores negative numbers",
                "suggestion": "handle them",
            }
        ],
    )
    return entry


class StatusModeCase(RepoCase):
    def setUp(self):
        super().setUp()
        os.environ["REVALI_POLL_SECONDS"] = "0.01"

    def set_mode(self, mode):
        text = self.read("revali.toml").replace(
            'exclude = ["*.lock"]\n', 'exclude = ["*.lock"]\ntests = "%s"\n' % mode
        )
        self.write("revali.toml", text)
        self.commit_all("tests mode %s" % mode)

    def status_modes(self, *argv):
        code, out = run_cli(["status"] + list(argv))
        self.assertEqual(code, EXIT_OK, out)
        return LINE.findall(out), out

    def counters(self, out):
        found = COUNTERS.findall(out)
        self.assertEqual(len(found), 1, out)
        return int(found[0][0]), int(found[0][1])

    def fix(self):
        self.write("src/calc.py", self.read("src/calc.py") + "\n# negatives handled\n")
        self.commit_all("fix")


class ModeLine(StatusModeCase):
    def test_no_line_before_the_first_round(self):
        # AC-2: no state, then a state whose run stopped before a round
        modes, out = self.status_modes()
        self.assertEqual(modes, [], out)
        self.set_mode("archive")
        code, out = run_cli(["run", "--foreground", "--dry-run"])
        self.assertEqual(code, EXIT_OK, out)
        modes, out = self.status_modes()
        self.assertIn("stage:", out)  # a state exists now
        self.assertEqual(modes, [], out)  # but no round has fixed the mode
        self.assertEqual(self.counters(out), (0, 0))

    def test_archive_after_the_first_round(self):
        # AC-2
        self.set_mode("archive")
        self.claude(requesting_changes())
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ACTION, out)
        modes, out = self.status_modes()
        self.assertEqual(modes, ["archive"], out)
        # the line sits with the branch's counters, before the trailing lines
        self.assertLess(out.index("round: 1"), out.index("tests: archive"))
        self.assertLess(out.index("tests: archive"), out.index("updated:"))

    def test_commit_after_the_first_round(self):
        # AC-2: the default mode, explicit and implicit
        self.claude(requesting_changes())
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ACTION, out)
        modes, out = self.status_modes()
        self.assertEqual(modes, ["commit"], out)

    def test_status_by_branch_from_the_base_checkout(self):
        # AC-2: `status --branch` reads the same state
        self.set_mode("archive")
        self.claude(requesting_changes())
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ACTION, out)
        modes, out = self.status_modes("--branch", "feature/mul")
        self.assertEqual(modes, ["archive"], out)


class StateFromBeforeTheKey(StatusModeCase):
    def test_rounds_without_a_recorded_mode_read_as_commit(self):
        # AC-2 (round-1 F1): the run treats an empty tests_mode with rounds as commit, and
        # status shows the mode the run will enforce rather than staying silent
        self.claude(requesting_changes())
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ACTION, out)
        state = State.load(self.rdir())
        self.assertEqual(state.tests_mode, "commit")
        state.tests_mode = ""
        state.save(self.rdir())
        modes, out = self.status_modes()
        self.assertEqual(modes, ["commit"], out)
        self.assertLess(out.index("round: 1"), out.index("tests: commit"))

    def test_the_fallback_needs_a_round(self):
        # AC-2: an empty mode and no rounds is simply "before the first round": no line
        self.set_mode("archive")
        code, out = run_cli(["run", "--foreground", "--dry-run"])
        self.assertEqual(code, EXIT_OK, out)
        state = State.load(self.rdir())
        self.assertEqual(state.rounds, [])
        state.tests_mode = ""
        state.save(self.rdir())
        modes, out = self.status_modes()
        self.assertEqual(modes, [], out)


class RoundCounter(StatusModeCase):
    def test_the_counter_follows_the_recorded_rounds(self):
        # AC-2's line sits next to the counters; the round count is the number of recorded
        # rounds (the old field nothing wrote always read 0)
        self.set_mode("archive")
        self.claude(requesting_changes())
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ACTION, out)
        modes, out = self.status_modes()
        self.assertEqual(self.counters(out), (1, 0))
        self.assertEqual(modes, ["archive"], out)
        self.fix()
        self.claude(approving())
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        modes, out = self.status_modes()
        self.assertEqual(self.counters(out), (2, 1))
        self.assertEqual(modes, ["archive"], out)  # still the mode round 1 fixed


if __name__ == "__main__":
    unittest.main()
