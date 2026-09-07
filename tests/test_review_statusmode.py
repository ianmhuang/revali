"""fix/archive-followups AC-2: `revali status` prints `tests: commit` or `tests: archive`
from the mode the state recorded once the branch has a review round; before the first round
the line is absent. Black-box through the CLI on the fixture repo."""

import os
import re
import unittest

from revali import EXIT_ACTION, EXIT_OK
from tests.helpers import TEST_REVIEW_MUL, RepoCase, approve_response, claude_entry, run_cli

FILE = "tests/test_review_mul.py"
LINE = re.compile(r"^tests: (\S+)$", re.MULTILINE)


def requesting_changes():
    data = approve_response(
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
    entry = claude_entry(data, write_tests=False)
    entry["write_files"] = {FILE: TEST_REVIEW_MUL}
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


if __name__ == "__main__":
    unittest.main()
