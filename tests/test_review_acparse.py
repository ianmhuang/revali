"""Acceptance tests for fix/ac-parse: noted, wrapped and malformed AC lines in change.md.

Black-box against revali.changedoc.parse / validate and the review renderers; every
expectation is taken from the AC text, not from the implementation.
"""

import types
import unittest

from revali.changedoc import parse, validate
from revali.review import CHANGES_REQUESTED, ac_gaps, render_review_md, render_tests_md
from tests.fixtures.make_sample_repo import CHANGE_MD
from tests.helpers import ROOT, approve_response  # noqa: F401  (sys.path setup)

FIXTURE_ACS = (
    "- AC-1: mul(a, b) returns the product of a and b for integers\n"
    "- AC-2: mul with zero returns zero\n"
)
AC_1 = "- AC-1: mul(a, b) returns the product of a and b for integers\n"
PROBLEM = 'change.md: line %d looks like an AC but is not "- AC-n: text": %s'


def doc_with(block):
    """The fixture change.md with its acceptance block replaced by `block`."""
    assert FIXTURE_ACS in CHANGE_MD
    return CHANGE_MD.replace(FIXTURE_ACS, block)


def problem_for(text, line):
    """The AC-4 problem for `line`, numbered from the top of the file."""
    number = text.splitlines().index(line) + 1
    return PROBLEM % (number, line.strip())


class NoteBeforeTheColon(unittest.TestCase):
    """AC-1."""

    def test_note_is_kept_in_front_of_the_text(self):
        doc = parse(doc_with(AC_1 + "- AC-2 (PR #30 F1): mul with zero returns zero\n"))
        self.assertEqual(doc.ac_ids, ["AC-1", "AC-2"])
        self.assertEqual(doc.acs[1], ("AC-2", "(PR #30 F1) mul with zero returns zero"))
        self.assertEqual(validate(doc), [])

    def test_empty_note_leaves_the_text_alone(self):
        doc = parse(doc_with("- AC-1 (): mul(a, b) returns the product of a and b\n"))
        self.assertEqual(doc.acs, [("AC-1", "mul(a, b) returns the product of a and b")])
        self.assertEqual(validate(doc), [])

    def test_one_level_of_nested_parentheses(self):
        doc = parse(doc_with("- AC-1 (probe, as for (m)): mul returns the product\n"))
        self.assertEqual(doc.acs, [("AC-1", "(probe, as for (m)) mul returns the product")])
        self.assertEqual(validate(doc), [])

    def test_full_width_colon_with_and_without_a_note(self):
        doc = parse(
            doc_with(
                "- AC-1：mul(a, b) returns the product of a and b\n"
                "- AC-2 (round 1 F2)：mul with zero returns zero\n"
            )
        )
        self.assertEqual(
            doc.acs,
            [
                ("AC-1", "mul(a, b) returns the product of a and b"),
                ("AC-2", "(round 1 F2) mul with zero returns zero"),
            ],
        )
        self.assertEqual(validate(doc), [])


class ContinuationLines(unittest.TestCase):
    """AC-2 and AC-3."""

    def test_lines_join_stripped_with_one_space(self):
        doc = parse(
            doc_with(
                "- AC-1: mul(a, b) returns   the product\n"
                "   of a and b\t\n"
                "\t\tfor integers   \n"
                "- AC-2: mul with zero returns zero\n"
            )
        )
        self.assertEqual(
            doc.acs,
            [
                ("AC-1", "mul(a, b) returns   the product of a and b for integers"),
                ("AC-2", "mul with zero returns zero"),
            ],
        )
        self.assertEqual(validate(doc), [])

    def test_text_entirely_on_the_following_lines(self):
        doc = parse(doc_with("- AC-1:\n  mul(a, b) returns the\n  product of a and b\n"))
        self.assertEqual(doc.acs, [("AC-1", "mul(a, b) returns the product of a and b")])
        self.assertEqual(validate(doc), [])

    def test_blank_line_ends_the_text(self):
        doc = parse(doc_with("- AC-1: mul returns the product\n\n  not part of the AC\n"))
        self.assertEqual(doc.acs, [("AC-1", "mul returns the product")])

    def test_section_heading_ends_the_text(self):
        # the fixture's "## Out of scope" follows the last AC after a blank line; drop the blank
        tail = "returns zero\n## Out of scope"
        text = CHANGE_MD.replace("returns zero\n\n## Out of scope", tail)
        self.assertNotEqual(text, CHANGE_MD)
        doc = parse(text)
        self.assertEqual(doc.acs[1], ("AC-2", "mul with zero returns zero"))

    def test_list_item_at_or_above_the_ac_indent_ends_the_text(self):
        for item in ("- a dash", "* a star", "+ a plus", "1. a step", "12) a step"):
            with self.subTest(item=item):
                doc = parse(doc_with("  - AC-1: mul returns the product\n  " + item + "\n"))
                self.assertEqual(doc.acs, [("AC-1", "mul returns the product")])
                doc = parse(doc_with("  - AC-1: mul returns the product\n" + item + "\n"))
                self.assertEqual(doc.acs, [("AC-1", "mul returns the product")])
                self.assertEqual(validate(doc), [])

    def test_deeper_list_items_and_unindented_lines_join(self):
        doc = parse(
            doc_with(
                "- AC-1: mul handles\n"
                "  - negative numbers\n"
                "  1. zero\n"
                "and floats too\n"
                "- AC-2: mul with zero returns zero\n"
            )
        )
        self.assertEqual(
            doc.acs[0], ("AC-1", "mul handles - negative numbers 1. zero and floats too")
        )
        self.assertEqual(doc.ac_ids, ["AC-1", "AC-2"])

    def test_an_ac_line_at_any_indent_starts_a_new_ac(self):
        doc = parse(
            doc_with(
                "- AC-1: mul returns the product of a and b\n"
                "    - AC-2: mul with zero\n"
                "      returns zero\n"
                "  * AC-3 (PR #1 F1): mul is commutative\n"
                "    for integers\n"
            )
        )
        self.assertEqual(
            doc.acs,
            [
                ("AC-1", "mul returns the product of a and b"),
                ("AC-2", "mul with zero returns zero"),
                ("AC-3", "(PR #1 F1) mul is commutative for integers"),
            ],
        )
        self.assertEqual(validate(doc), [])

    def test_crlf_input(self):
        text = doc_with("- AC-1: mul returns\n  the product of a and b\n").replace("\n", "\r\n")
        self.assertEqual(parse(text).acs, [("AC-1", "mul returns the product of a and b")])


class LinesThatOnlyLookLikeAnAc(unittest.TestCase):
    """AC-4 and AC-5."""

    FORMS = (
        "- AC-4 - a dash instead of the colon",
        "1. AC-7: a numbered item",
        "1) AC-7: a numbered item",
        "+ AC-6: a plus bullet",
        "- **AC-8**: a bold id",
        "- *AC-8*: an emphasised id",
        "- _AC-8_: an underscored id",
        "- `AC-8`: a code id",
        "AC-1: no list marker, outside any open AC",
        "- AC-9 no colon at all",
        "- AC-6b: a sub-id",
        "- AC-6b (added later): a sub-id with a note",
        "- AC-10 re-measure (after the fix): a word between id and note",
        "  - AC-5 - indented, dash instead of the colon",
    )

    def test_each_form_stops_with_its_line_number(self):
        for form in self.FORMS:
            with self.subTest(form=form):
                # the blank line closes AC-1 so the bare "AC-1: ..." form is outside an open AC
                text = doc_with(AC_1 + "\n" + form + "\n")
                doc = parse(text)
                self.assertEqual(doc.ac_ids, ["AC-1"])
                self.assertEqual(validate(doc), [problem_for(text, form)])

    def test_line_numbers_count_the_front_matter(self):
        text = doc_with(AC_1 + "- AC-2 two\n")
        self.assertTrue(text.startswith("---\n"))
        number = text.splitlines().index("- AC-2 two") + 1
        self.assertGreater(number, 5)  # below the front matter and the earlier sections
        self.assertEqual(validate(parse(text)), [PROBLEM % (number, "- AC-2 two")])

    def test_one_problem_per_line(self):
        a, b, c = "- AC-2 two", "1. AC-3: three", "- **AC-4**: four"
        text = doc_with(AC_1 + a + "\n" + b + "\n\n" + c + "\n")
        self.assertEqual(
            validate(parse(text)),
            [problem_for(text, a), problem_for(text, b), problem_for(text, c)],
        )

    def test_bare_reference_inside_an_open_ac_joins_the_text(self):
        # AC-4 exception: a wrapped sentence whose next line begins with an AC id
        doc = parse(
            doc_with(
                "- AC-1: mul returns the product, as in\n"
                "  AC-2 (twenty documents), counting\n"
                "AC-8 and AC-9 calls; reported\n"
                "- AC-2: mul with zero returns zero\n"
            )
        )
        self.assertEqual(
            doc.acs[0],
            (
                "AC-1",
                "mul returns the product, as in AC-2 (twenty documents), counting "
                "AC-8 and AC-9 calls; reported",
            ),
        )
        self.assertEqual(doc.ac_ids, ["AC-1", "AC-2"])
        self.assertEqual(validate(doc), [])

    def test_bare_reference_outside_an_open_ac_is_reported(self):
        for closer in ("\n", "- a plain bullet\n"):
            with self.subTest(closer=repr(closer)):
                line = "AC-2 was added after the first round"
                text = doc_with("- AC-1: mul returns the product\n" + closer + line + "\n")
                doc = parse(text)
                self.assertEqual(doc.ac_ids, ["AC-1"])
                self.assertEqual(validate(doc), [problem_for(text, line)])

    def test_bare_definition_inside_an_open_ac_is_reported(self):
        # AC-4 (narrowed in round 2): a colon after the id is a forgotten bullet, not a
        # wrapped sentence, so it is reported even while AC-1's text is open
        for bare in (
            "AC-2: mul with zero returns zero",
            "  AC-2: mul with zero returns zero",
            "AC-2：mul with zero returns zero",
            "**AC-2**: mul with zero returns zero",
            "*AC-2*: mul with zero returns zero",
            "`AC-2`: mul with zero returns zero",
            "AC-2 (PR #3 F1): mul with zero returns zero",
            "AC-2 (probe, as for (m)): mul with zero returns zero",
            "AC-2:",
        ):
            with self.subTest(bare=bare):
                text = doc_with("- AC-1: mul returns the product\n" + bare + "\n")
                doc = parse(text)
                self.assertEqual(doc.acs, [("AC-1", "mul returns the product")])
                self.assertEqual(validate(doc), [problem_for(text, bare)])

    def test_bare_reference_with_a_note_but_no_colon_still_joins(self):
        # the exception keeps a wrapped sentence whose id carries a parenthesis
        doc = parse(
            doc_with(
                "- AC-1: mul returns the product as\n"
                "AC-2 (twenty documents) does, and\n"
                "AC-3 (as for (m)) too\n"
            )
        )
        self.assertEqual(
            doc.acs,
            [
                (
                    "AC-1",
                    "mul returns the product as AC-2 (twenty documents) does, and "
                    "AC-3 (as for (m)) too",
                )
            ],
        )
        self.assertEqual(validate(doc), [])

    def test_marked_ac_like_line_inside_an_open_ac_is_reported(self):
        lines = ("  - AC-2 depends on it", "  1. AC-2: a numbered item", "  - **AC-2**: bold")
        for line in lines:
            with self.subTest(line=line):
                text = doc_with("- AC-1: mul returns the product\n" + line + "\n")
                doc = parse(text)
                self.assertEqual(doc.acs, [("AC-1", "mul returns the product")])
                self.assertEqual(validate(doc), [problem_for(text, line)])

    def test_malformed_lines_are_stops_not_silently_skipped(self):
        # the pasted report: with nine lines the old parser kept four and validate() said nothing
        block = (
            "- AC-1: mul(a, b) returns the product of a and b\n"
            "- AC-2 (PR #30 F1): mul with zero returns zero\n"
            "- AC-3: mul with a negative operand\n"
            "  returns a negative product\n"
            "- **AC-4**: mul of two floats returns a float\n"
            "- AC-5 mul of a float and an int returns a float\n"
            "1. AC-6: mul is commutative\n"
            "- AC-7:\n"
            "  mul of big integers does not overflow\n"
        )
        text = doc_with(block)
        doc = parse(text)
        self.assertEqual(doc.ac_ids, ["AC-1", "AC-2", "AC-3", "AC-7"])
        self.assertEqual(
            validate(doc),
            [
                problem_for(text, "- **AC-4**: mul of two floats returns a float"),
                problem_for(text, "- AC-5 mul of a float and an int returns a float"),
                problem_for(text, "1. AC-6: mul is commutative"),
            ],
        )

    def test_other_sections_are_left_alone(self):
        text = CHANGE_MD.replace(
            "## Out of scope\nDivision.\n",
            "## Out of scope\nAC-3 is not in scope.\n- AC-4 division\n- **AC-5**: rounding\n",
        )
        text = text.replace("## What\n", "## What\n1. AC-6: what\n")
        text = text.replace("## Why\n", "## Why\n- AC-7 (PR #1): why\n")
        self.assertNotEqual(text, CHANGE_MD)
        doc = parse(text)
        self.assertEqual(doc.ac_ids, ["AC-1", "AC-2"])
        self.assertEqual(validate(doc), [])


class ChecksRunOnTheJoinedText(unittest.TestCase):
    """AC-6."""

    def test_short_first_line_with_a_long_continuation_passes(self):
        doc = parse(doc_with("- AC-1: mul\n  returns the product of a and b\n"))
        self.assertEqual(validate(doc), [])

    def test_empty_ac_is_too_short_and_still_listed(self):
        for second in ("- AC-2:\n\n", "- AC-2 (note):\n"):
            with self.subTest(second=second):
                doc = parse(doc_with(AC_1 + second))
                self.assertEqual(doc.ac_ids, ["AC-1", "AC-2"])
                self.assertEqual(validate(doc), ["change.md: AC-2 is too short to be testable"])

    def test_duplicate_id_in_a_noted_wrapped_ac(self):
        doc = parse(
            doc_with(
                "- AC-1: mul returns the product of a and b\n"
                "- AC-1 (PR #3 F1):\n"
                "  mul with zero returns zero\n"
            )
        )
        self.assertEqual(validate(doc), ["change.md: duplicate AC-1"])


class WrappedNotes(unittest.TestCase):
    """AC-10."""

    def test_note_over_two_lines_then_text(self):
        doc = parse(
            doc_with(
                "- AC-1 (manual, measured outside the\n"
                "  sandbox; bars set by the owner): mul handles\n"
                "  big integers\n"
                "- AC-2: mul with zero returns zero\n"
            )
        )
        self.assertEqual(
            doc.acs,
            [
                (
                    "AC-1",
                    "(manual, measured outside the sandbox; bars set by the owner) "
                    "mul handles big integers",
                ),
                ("AC-2", "mul with zero returns zero"),
            ],
        )
        self.assertEqual(validate(doc), [])

    def test_note_over_three_lines_full_width_colon_text_below(self):
        doc = parse(
            doc_with(
                "  - AC-1 (go/no-go,\n"
                "    measured before revali;\n"
                "    AC-8 too)：\n"
                "    mul handles big integers\n"
            )
        )
        self.assertEqual(
            doc.acs,
            [("AC-1", "(go/no-go, measured before revali; AC-8 too) mul handles big integers")],
        )
        self.assertEqual(validate(doc), [])

    def test_nested_parentheses_across_the_wrap(self):
        doc = parse(doc_with("- AC-1 (probe, as for (m),\n  10 calls): mul works for ints\n"))
        self.assertEqual(doc.acs, [("AC-1", "(probe, as for (m), 10 calls) mul works for ints")])

    def test_note_that_never_closes_is_reported_on_its_first_line(self):
        first = "- AC-1 (manual, measured outside the"
        endings = {
            "blank line": "\n\nmul handles big integers\n",
            "end of section": "\n  sandbox and more\n",
            "list item at the same indent": "\n  sandbox and more\n- a plain bullet\n",
            "closed without a colon": "\n  sandbox) mul handles big integers\n",
        }
        for name, rest in endings.items():
            with self.subTest(ending=name):
                text = doc_with(first + rest)
                doc = parse(text)
                self.assertEqual(doc.ac_ids, [])
                self.assertIn(problem_for(text, first), validate(doc))

    def test_next_ac_line_ends_an_open_note(self):
        first = "- AC-1 (manual, measured outside the"
        text = doc_with(first + "\n- AC-2: mul with zero returns zero\n")
        doc = parse(text)
        self.assertEqual(doc.acs, [("AC-2", "mul with zero returns zero")])
        self.assertEqual(validate(doc), [problem_for(text, first)])

    def test_deeper_ac_line_ends_an_open_note(self):
        # round 1 F1: an AC line indented deeper than the open note is its own AC, with
        # its own wrapped note, and the unclosed one is reported on its first line
        first = "- AC-1 (manual, measured outside the"
        text = doc_with(
            first + "\n    - AC-2 (PR #3\n      F1): mul with zero returns zero\n      for ints\n"
        )
        doc = parse(text)
        self.assertEqual(doc.acs, [("AC-2", "(PR #3 F1) mul with zero returns zero for ints")])
        self.assertEqual(validate(doc), [problem_for(text, first)])

    def test_deeper_malformed_ac_line_ends_an_open_note(self):
        first, second = "- AC-1 (manual, measured outside the", "    1. AC-2: a numbered item"
        text = doc_with(first + "\n" + second + "\n")
        doc = parse(text)
        self.assertEqual(doc.ac_ids, [])
        problems = validate(doc)
        self.assertIn(problem_for(text, first), problems)
        self.assertIn(problem_for(text, second), problems)


class NewlyParsedAcsAreHeldToCoverage(unittest.TestCase):
    """AC-8."""

    TEXT = doc_with(
        FIXTURE_ACS
        + "- AC-3 (PR #30 F1): mul with a negative operand\n"
        + "  returns a negative product\n"
        + "- AC-4:\n"
        + "  mul of two floats returns a float\n"
    )

    def test_ac_gaps_lists_them(self):
        doc = parse(self.TEXT)
        self.assertEqual(ac_gaps(approve_response(), doc.ac_ids), ["AC-3", "AC-4"])

    def test_review_md_marks_them_uncovered(self):
        doc = parse(self.TEXT)
        meta = {"round": 1, "model_actual": "m"}
        md = render_review_md(approve_response(), CHANGES_REQUESTED, [], meta, doc.ac_ids)
        self.assertIn("- AC-3: **uncovered**", md)
        self.assertIn("- AC-4: **uncovered**", md)

    def test_tests_md_shows_the_joined_text(self):
        doc = parse(self.TEXT)
        ctx = types.SimpleNamespace(doc=doc, branch="feature/mul")
        md = render_tests_md(ctx, None, [{"data": approve_response()}])
        self.assertIn(
            "- AC-3: (PR #30 F1) mul with a negative operand returns a negative product"
            " -> **uncovered**",
            md,
        )
        self.assertIn("- AC-4: mul of two floats returns a float -> **uncovered**", md)


if __name__ == "__main__":
    unittest.main()
