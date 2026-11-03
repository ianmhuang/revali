"""Acceptance tests for fix/ac-prose: a prose paragraph that opens with an AC id.

Black-box against revali.changedoc.parse / validate. Every expectation is taken from
the AC text of the change (AC-1 to AC-4); AC-5 and AC-6 are manual / documentation.
"""

import unittest

from revali.changedoc import parse, validate
from tests.fixtures.make_sample_repo import CHANGE_MD
from tests.helpers import ROOT  # noqa: F401  (sys.path setup)

FIXTURE_ACS = (
    "- AC-1: mul(a, b) returns the product of a and b for integers\n"
    "- AC-2: mul with zero returns zero\n"
)
AC_1 = "- AC-1: mul returns the product"
PROBLEM = 'change.md: line %d looks like an AC but is not "- AC-n: text": %s'


def doc_with(block):
    """The fixture change.md with its acceptance block replaced by `block`."""
    assert FIXTURE_ACS in CHANGE_MD
    return CHANGE_MD.replace(FIXTURE_ACS, block)


def number_of(text, line):
    """1-based number of the first line of `text` equal to `line` (the line must be unique)."""
    return text.splitlines().index(line) + 1


def problem_for(text, line):
    return PROBLEM % (number_of(text, line), line.strip())


class ProseParagraphIsIgnored(unittest.TestCase):
    """AC-1: a bare line opening with an AC id, no list marker, no colon right after the id
    or its note, is neither parsed nor reported when no AC's text is open."""

    PROSE = (
        "AC-9 was added during implementation. The first full run failed",
        "AC-1 and AC-2 as approved named boards that do not exist",
        "**AC-2** was split from AC-1 after the first round",
        "*AC-2* was split from AC-1 after the first round",
        "_AC-2_ was split from AC-1 after the first round",
        "`AC-2` was split from AC-1 after the first round",
        "AC-2 (twenty documents) was measured by hand",
        "AC-2 (as for (m)) was measured by hand",
        "  AC-2 and AC-3 were merged",
        # a colon later in the sentence is not a colon right after the id
        "AC-9 was added during implementation: the first run failed",
    )

    def test_prose_after_a_blank_line_or_a_closing_list_item(self):
        for closer in ("\n", "- a plain bullet\n"):
            for prose in self.PROSE:
                with self.subTest(closer=repr(closer), prose=prose):
                    text = doc_with(AC_1 + "\n" + closer + prose + "\n")
                    doc = parse(text)
                    self.assertEqual(doc.acs, [("AC-1", "mul returns the product")])
                    self.assertEqual(validate(doc), [])

    def test_prose_before_the_first_ac(self):
        for prose in self.PROSE:
            with self.subTest(prose=prose):
                doc = parse(doc_with(prose + "\n\n" + AC_1 + "\n"))
                self.assertEqual(doc.acs, [("AC-1", "mul returns the product")])
                self.assertEqual(validate(doc), [])

    def test_lines_after_the_prose_are_outside_any_ac(self):
        # the sentence's own continuation and a nested bullet are skipped like any line
        # outside an AC; a marked AC-like line after it is reported like any such line
        marked = "- AC-3 no colon here"
        text = doc_with(
            AC_1 + "\n\n"
            "AC-9 was added during implementation. The first\n"
            "full run failed, so a bar was added.\n"
            "  - a nested bullet under the paragraph\n"
            "and more words\n" + marked + "\n"
        )
        doc = parse(text)
        self.assertEqual(doc.acs, [("AC-1", "mul returns the product")])
        self.assertEqual(validate(doc), [problem_for(text, marked)])

    def test_prose_between_two_acs(self):
        text = doc_with(
            AC_1 + "\n\n"
            "AC-9 was added during implementation.\n\n"
            "- AC-2: mul with zero returns zero\n"
        )
        doc = parse(text)
        self.assertEqual(
            doc.acs,
            [("AC-1", "mul returns the product"), ("AC-2", "mul with zero returns zero")],
        )
        self.assertEqual(validate(doc), [])

    def test_a_document_whose_only_ac_looking_line_is_prose_still_needs_an_ac(self):
        # AC-2, last sentence
        problems = validate(parse(doc_with("AC-9 was added during implementation.\n")))
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("needs at least one '- AC-n: ...' line", problems[0])


class RealAcWithoutBulletOrColonReadsAsProse(unittest.TestCase):
    """AC-3: the stated cost; the docstring wording itself is documentation, not pinned."""

    def test_bare_ac_without_colon_outside_an_open_ac_is_ignored(self):
        text = doc_with(AC_1 + "\n\nAC-3 mul returns the product of floats\n")
        doc = parse(text)
        self.assertEqual(doc.ac_ids, ["AC-1"])
        self.assertEqual(validate(doc), [])


class MarkedAndDefinedLinesAreStillReported(unittest.TestCase):
    """AC-2: what PR #46's AC-4 reports is still reported, same message and line number."""

    FORMS = (
        "- AC-9 no colon",
        "1. AC-7: text of seven",
        "1) AC-7: text of seven",
        "+ AC-6: a plus bullet",
        "- **AC-8**: text of eight",
        "- AC-6b: text of six b",
        "- AC-6b (added later): a sub-id with a note",
        "AC-2: mul with zero returns zero",
        "  AC-2: mul with zero returns zero",
        "**AC-2**: mul with zero returns zero",
        "`AC-2`: mul with zero returns zero",
        "AC-2 (note): mul with zero returns zero",
        "AC-2 (as for (m))：mul with zero returns zero",
        "AC-2：mul with zero returns zero",
        "AC-2:",
    )

    def test_after_a_closed_ac(self):
        for closer in ("\n", "- a plain bullet\n"):
            for form in self.FORMS:
                with self.subTest(closer=repr(closer), form=form):
                    text = doc_with(AC_1 + "\n" + closer + form + "\n")
                    doc = parse(text)
                    self.assertEqual(doc.acs, [("AC-1", "mul returns the product")])
                    self.assertEqual(validate(doc), [problem_for(text, form)])

    def test_before_the_first_ac(self):
        for form in self.FORMS:
            with self.subTest(form=form):
                text = doc_with(form + "\n\n" + AC_1 + "\n")
                doc = parse(text)
                self.assertEqual(doc.acs, [("AC-1", "mul returns the product")])
                self.assertEqual(validate(doc), [problem_for(text, form)])

    def test_inside_an_open_ac(self):
        # a list marker or a colon after the id is never a wrapped sentence
        for form in self.FORMS:
            with self.subTest(form=form):
                text = doc_with(AC_1 + "\n" + form + "\n")
                doc = parse(text)
                self.assertEqual(doc.acs, [("AC-1", "mul returns the product")])
                self.assertEqual(validate(doc), [problem_for(text, form)])

    def test_line_number_counts_from_the_top_of_the_file(self):
        form = "- AC-9 no colon"
        text = doc_with(AC_1 + "\n\n" + form + "\n")
        number = number_of(text, form)
        self.assertGreater(number, 10)  # below the front matter and the earlier sections
        self.assertEqual(validate(parse(text)), [PROBLEM % (number, form)])

    def test_prose_and_a_forgotten_bullet_side_by_side(self):
        # only the forgotten bullet is a problem; the prose paragraph is not
        bare = "AC-3: mul with a negative operand returns a negative product"
        text = doc_with(
            AC_1 + "\n\n"
            "AC-9 was added during implementation.\n\n" + bare + "\n\n"
            "**AC-2** was split from AC-1 after the first round\n"
        )
        doc = parse(text)
        self.assertEqual(doc.ac_ids, ["AC-1"])
        self.assertEqual(validate(doc), [problem_for(text, bare)])


class OpenTextAndOpenNoteEndOnTheSameLines(unittest.TestCase):
    """AC-4: each line shape ends an open AC text exactly when it ends an unclosed note, and
    is reported in both cases or in neither."""

    # shape -> (line, ends the open text / note, the line itself is reported)
    SHAPES = {
        "blank line": ("", True, False),
        "well-formed AC line": ("- AC-2: mul by zero returns zero", True, False),
        "AC-like line with a list marker": ("- AC-2 no colon here", True, True),
        "bare definition": ("AC-2: mul by zero returns zero", True, True),
        "bare definition with a note": ("AC-2 (PR #3 F1): mul by zero", True, True),
        "bare prose line starting with an AC id": ("AC-2 was split from AC-1", False, False),
        "list item no deeper than the AC line": ("- a plain bullet", True, False),
        "deeper list item": ("  - a nested bullet", False, False),
        "plain text": ("plain words that wrap", False, False),
    }
    TEXT_HEAD = "- AC-1: mul returns the product"
    NOTE_HEAD = "- AC-1 (manual, measured outside the"
    TEXT_TAIL = "  zzz tail of the text"
    NOTE_TAIL = "  zzz sandbox): mul returns the product"

    def outcome(self, head, line, tail):
        """(the line ended AC-1's open text or note, the line itself is reported)."""
        text = doc_with(head + "\n" + line + "\n" + tail + "\n")
        number = number_of(text, head) + 1
        doc = parse(text)
        reported = any(p.startswith("change.md: line %d " % number) for p in validate(doc))
        ac_1 = dict(doc.acs).get("AC-1")
        ended = ac_1 is None or "zzz" not in ac_1
        return ended, reported

    def test_each_shape_in_an_open_text(self):
        for name, (line, ends, reported) in self.SHAPES.items():
            with self.subTest(shape=name):
                self.assertEqual(
                    self.outcome(self.TEXT_HEAD, line, self.TEXT_TAIL), (ends, reported)
                )

    def test_each_shape_in_an_open_note(self):
        for name, (line, ends, reported) in self.SHAPES.items():
            with self.subTest(shape=name):
                self.assertEqual(
                    self.outcome(self.NOTE_HEAD, line, self.NOTE_TAIL), (ends, reported)
                )

    def test_the_tail_is_parsed_when_the_line_joins(self):
        # sanity check of the harness: a joining line keeps the tail inside AC-1
        text = doc_with(self.NOTE_HEAD + "\nplain words that wrap\n" + self.NOTE_TAIL + "\n")
        doc = parse(text)
        self.assertEqual(
            doc.acs,
            [
                (
                    "AC-1",
                    "(manual, measured outside the plain words that wrap zzz sandbox) "
                    "mul returns the product",
                )
            ],
        )
        self.assertEqual(validate(doc), [])

    def test_glued_dash_note_start_ends_both(self):
        # the description's Why: "-AC-3 (x" (no space after the dash) used to end an open
        # text but join an open note; now it ends both
        line = "-AC-3 (x"
        in_text = self.outcome(self.TEXT_HEAD, line, self.TEXT_TAIL)
        in_note = self.outcome(self.NOTE_HEAD, line, self.NOTE_TAIL)
        self.assertTrue(in_text[0])
        self.assertTrue(in_note[0])


if __name__ == "__main__":
    unittest.main()
