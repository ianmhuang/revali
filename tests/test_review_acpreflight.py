"""Acceptance tests for fix/ac-parse at the preflight stage: a malformed AC line stops
preflight with the other change.md problems (AC-4), the ok line lists the parsed ids (AC-7).
"""

import unittest

from revali import EXIT_ERROR
from revali.preflight import Stop, preflight
from tests.helpers import RepoCase

CHANGE_MD_REL = ".revali/feature__mul/change.md"
AC_2 = "- AC-2: mul with zero returns zero\n"
PROBLEM = 'change.md: line %d looks like an AC but is not "- AC-n: text": %s'


class _StageLog:
    def __init__(self):
        self.lines = []

    def stage(self, stage, msg):
        self.lines.append("%s: %s" % (stage, msg))

    def detail(self, msg):
        pass


class MalformedAcStopsPreflight(RepoCase):
    """AC-4."""

    def replace_ac_2(self, block):
        text = self.read(CHANGE_MD_REL)
        self.assertIn(AC_2, text)
        text = text.replace(AC_2, block)
        self.write(CHANGE_MD_REL, text)
        return text

    def test_stops_with_exit_1_and_the_numbered_line(self):
        bad = "- **AC-3**: mul is commutative"
        text = self.replace_ac_2(AC_2 + bad + "\n")
        number = text.splitlines().index(bad) + 1
        with self.assertRaises(Stop) as cm:
            preflight(self.repo)
        self.assertEqual(cm.exception.exit_code, EXIT_ERROR, cm.exception.message)
        self.assertIn("before revali can run", cm.exception.message)
        self.assertIn("  - " + PROBLEM % (number, bad), cm.exception.message)

    def test_every_malformed_line_is_listed(self):
        first, second = "- AC-3 mul is commutative", "1. AC-4: mul of floats"
        text = self.replace_ac_2(AC_2 + first + "\n" + second + "\n")
        with self.assertRaises(Stop) as cm:
            preflight(self.repo)
        for line in (first, second):
            number = text.splitlines().index(line) + 1
            self.assertIn(PROBLEM % (number, line), cm.exception.message)


class OkLineListsTheIds(RepoCase):
    """AC-7."""

    def test_ok_line_with_noted_and_wrapped_acs(self):
        block = (
            "- AC-2 (PR #3 F1):\n"
            "  mul with zero returns zero\n"
            "- AC-3: mul is commutative\n"
            "  for integers\n"
        )
        text = self.read(CHANGE_MD_REL)
        self.assertIn(AC_2, text)
        self.write(CHANGE_MD_REL, text.replace(AC_2, block))
        log = _StageLog()
        preflight(self.repo, log=log)
        self.assertIn("preflight: ok: Add mul to calc (feature, 3 AC: AC-1, AC-2, AC-3)", log.lines)

    def test_ok_line_on_the_plain_fixture(self):
        log = _StageLog()
        preflight(self.repo, log=log)
        self.assertIn("preflight: ok: Add mul to calc (feature, 2 AC: AC-1, AC-2)", log.lines)


if __name__ == "__main__":
    unittest.main()
