import os
import types
import unittest

from revali import changedoc
from revali.changedoc import parse, validate
from revali.review import CHANGES_REQUESTED, ac_gaps, render_review_md, render_tests_md
from tests.fixtures.make_sample_repo import CHANGE_MD
from tests.helpers import ROOT, approve_response  # noqa: F401  (sys.path setup)

FIXTURE_ACS = (
    "- AC-1: mul(a, b) returns the product of a and b for integers\n"
    "- AC-2: mul with zero returns zero\n"
)


def with_acs(block: str) -> str:
    """CHANGE_MD with its two AC lines replaced by `block`."""
    assert FIXTURE_ACS in CHANGE_MD
    return CHANGE_MD.replace(FIXTURE_ACS, block)


def line_no(text: str, line: str) -> int:
    """1-based number of the first line of `text` equal to `line`."""
    return text.splitlines().index(line) + 1


def malformed(text: str, line: str) -> str:
    return 'change.md: line %d looks like an AC but is not "- AC-n: text": %s' % (
        line_no(text, line),
        line.strip(),
    )


class ChangeDocTests(unittest.TestCase):
    def test_parse_fixture(self):
        doc = parse(CHANGE_MD)
        self.assertEqual(doc.title, "Add mul to calc")
        self.assertEqual(doc.kind, "feature")
        self.assertEqual(doc.author_model, "fixture")
        self.assertIn("mul(a, b)", doc.section("request"))
        self.assertEqual(doc.ac_ids, ["AC-1", "AC-2"])
        self.assertEqual(doc.acs[1][1], "mul with zero returns zero")
        self.assertEqual(validate(doc), [])

    def test_heading_title_fallback(self):
        text = CHANGE_MD.replace("title: Add mul to calc\n", "")
        text = text.replace("---\n\n## Request", "---\n\n# Heading title\n\n## Request", 1)
        doc = parse(text)
        self.assertEqual(doc.title, "Heading title")

    def test_missing_request(self):
        doc = parse(
            CHANGE_MD.replace("add a mul(a, b) function to calc that multiplies two numbers", "")
        )
        self.assertTrue(any("Request" in p for p in validate(doc)))

    def test_missing_ac(self):
        text = CHANGE_MD.replace(
            "- AC-1: mul(a, b) returns the product of a and b for integers\n", ""
        )
        text = text.replace("- AC-2: mul with zero returns zero\n", "")
        self.assertTrue(any("at least one" in p for p in validate(parse(text))))

    def test_duplicate_ac(self):
        text = CHANGE_MD.replace("- AC-2:", "- AC-1:")
        self.assertTrue(any("duplicate AC-1" in p for p in validate(parse(text))))

    def test_short_ac(self):
        text = CHANGE_MD.replace("- AC-2: mul with zero returns zero", "- AC-2: ok")
        self.assertTrue(any("too short" in p for p in validate(parse(text))))

    def test_draft_refused(self):
        text = CHANGE_MD.replace(
            "author_model: fixture\n", "author_model: fixture\nstatus: draft\n"
        )
        self.assertTrue(any("draft" in p for p in validate(parse(text))))

    def test_kind_not_in_v1(self):
        problems = validate(parse(CHANGE_MD.replace("kind: feature", "kind: hotfix")))
        self.assertTrue(any("not available in this version" in p for p in problems))

    def test_unknown_kind(self):
        problems = validate(parse(CHANGE_MD.replace("kind: feature", "kind: banana")))
        self.assertTrue(any("unknown kind" in p for p in problems))

    def test_missing_kind_and_title(self):
        text = CHANGE_MD.replace("title: Add mul to calc\n", "").replace("kind: feature\n", "")
        problems = validate(parse(text))
        self.assertTrue(any("missing title" in p for p in problems))
        self.assertTrue(any("missing 'kind:'" in p for p in problems))

    def test_shipped_template(self):
        path = os.path.join(ROOT, "templates", "change.md")
        with open(path, "r", encoding="utf-8", newline="") as fh:
            text = fh.read()
        self.assertIn("status: draft", text)
        problems = validate(parse(text))
        self.assertTrue(any("draft" in p for p in problems), problems)
        # Approval = the status line goes; placeholders replaced by real AC text
        approved = "".join(line for line in text.splitlines(True) if not line.startswith("status:"))
        approved = approved.replace(
            "<another one; every AC maps to at least one test>", "second observable behaviour"
        )
        doc = parse(approved)
        self.assertEqual(doc.ac_ids, ["AC-1", "AC-2"])
        self.assertEqual(validate(doc), [])

    def test_crlf_input(self):
        doc = parse(CHANGE_MD.replace("\n", "\r\n"))
        self.assertEqual(doc.ac_ids, ["AC-1", "AC-2"])
        self.assertEqual(validate(doc), [])


class NotedAcs(unittest.TestCase):
    """AC-1: a note in parentheses before the colon."""

    def test_note_goes_into_the_text(self):
        doc = parse(
            with_acs(
                "- AC-1: mul(a, b) returns the product of a and b for integers\n"
                "- AC-2 (PR #30 F1): mul with zero returns zero\n"
            )
        )
        self.assertEqual(doc.ac_ids, ["AC-1", "AC-2"])
        self.assertEqual(doc.acs[1], ("AC-2", "(PR #30 F1) mul with zero returns zero"))
        self.assertEqual(validate(doc), [])

    def test_empty_note(self):
        doc = parse(with_acs("- AC-1 (): mul(a, b) returns the product of a and b\n"))
        self.assertEqual(doc.acs, [("AC-1", "mul(a, b) returns the product of a and b")])
        self.assertEqual(validate(doc), [])

    def test_full_width_colon_after_note(self):
        doc = parse(with_acs("- AC-1 (round 1 F2)：mul with zero returns zero\n"))
        self.assertEqual(doc.acs, [("AC-1", "(round 1 F2) mul with zero returns zero")])

    def test_nested_parentheses_in_the_note(self):
        doc = parse(with_acs("- AC-4 (probe, rebuilt as for (m)): mul returns the product\n"))
        self.assertEqual(doc.acs, [("AC-4", "(probe, rebuilt as for (m)) mul returns the product")])

    def test_wrapped_note_with_nested_parentheses(self):
        doc = parse(
            with_acs(
                "- AC-7 (probe, rebuilt as for (m), 10 calls,\n  main and branch): mul works\n"
            )
        )
        self.assertEqual(
            doc.acs, [("AC-7", "(probe, rebuilt as for (m), 10 calls, main and branch) mul works")]
        )

    def test_note_without_text_is_too_short(self):
        doc = parse(with_acs("- AC-1: mul returns the product of a and b\n- AC-2 (PR #30 F1):\n"))
        self.assertEqual(doc.ac_ids, ["AC-1", "AC-2"])
        self.assertIn("change.md: AC-2 is too short to be testable", validate(doc))


class WrappedAcs(unittest.TestCase):
    """AC-2, AC-3: continuation lines join the AC's text."""

    def text_of(self, block, ac_id="AC-1"):
        return dict(parse(with_acs(block)).acs)[ac_id]

    def test_indented_continuation(self):
        self.assertEqual(
            self.text_of(
                "- AC-1: mul(a, b) returns the product\n"
                "  of a and b\n"
                "    for integers   \n"
                "- AC-2: mul with zero returns zero\n"
            ),
            "mul(a, b) returns the product of a and b for integers",
        )

    def test_text_on_the_next_line(self):
        doc = parse(with_acs("- AC-1:\n  mul(a, b) returns the product of a and b\n"))
        self.assertEqual(doc.acs, [("AC-1", "mul(a, b) returns the product of a and b")])
        self.assertEqual(validate(doc), [])

    def test_blank_line_ends_the_ac(self):
        doc = parse(with_acs("- AC-1: mul returns the product\n\n  prose after a blank line\n"))
        self.assertEqual(doc.acs, [("AC-1", "mul returns the product")])

    def test_deeper_list_items_join(self):
        self.assertEqual(
            self.text_of(
                "- AC-1: mul handles\n"
                "  - negative numbers\n"
                "  1. zero\n"
                "- AC-2: mul with zero returns zero\n"
            ),
            "mul handles - negative numbers 1. zero",
        )

    def test_list_item_at_the_same_indent_ends_the_ac(self):
        for item in ("- a plain bullet", "* a star bullet", "+ a plus bullet", "1. a step"):
            with self.subTest(item=item):
                doc = parse(with_acs("- AC-1: mul returns the product\n" + item + "\n"))
                self.assertEqual(doc.acs, [("AC-1", "mul returns the product")])
                self.assertEqual(validate(doc), [])

    def test_shallower_list_item_ends_an_indented_ac(self):
        doc = parse(with_acs("  - AC-1: mul returns the product\n- a plain bullet\n"))
        self.assertEqual(doc.acs, [("AC-1", "mul returns the product")])

    def test_unindented_plain_line_joins(self):
        self.assertEqual(
            self.text_of("- AC-1: mul returns the\nproduct of a and b\n"),
            "mul returns the product of a and b",
        )

    def test_a_heading_ends_the_ac(self):
        doc = parse(CHANGE_MD)
        self.assertEqual(doc.acs[1], ("AC-2", "mul with zero returns zero"))

    def test_crlf(self):
        text = with_acs("- AC-1: mul returns\n  the product of a and b\n").replace("\n", "\r\n")
        self.assertEqual(parse(text).acs, [("AC-1", "mul returns the product of a and b")])

    def test_indented_ac_line_starts_a_new_ac(self):
        doc = parse(
            with_acs(
                "- AC-1: mul returns the product of a and b\n"
                "  - AC-2: mul with zero returns zero\n"
                "    even for negative a\n"
            )
        )
        self.assertEqual(
            doc.acs,
            [
                ("AC-1", "mul returns the product of a and b"),
                ("AC-2", "mul with zero returns zero even for negative a"),
            ],
        )


class MalformedAcs(unittest.TestCase):
    """AC-4, AC-5: a line that starts like an AC but is not one is a problem."""

    FORMS = (
        "- AC-4 - dash instead of a colon",
        "1. AC-7: a numbered item",
        "1) AC-7: a numbered item",
        "+ AC-6: a plus bullet",
        "- **AC-8**: a bold id",
        "- `AC-8`: an id in backticks",
        "- _AC-8_: an emphasised id",
        "AC-1: no bullet at all",
        "- AC-9 no colon at all",
        "  - AC-3 (unclosed note: text",
        "- AC-6b: a sub-id",
        "- AC-6b (added during implementation): a sub-id with a note",
    )

    def test_each_form_is_reported(self):
        for form in self.FORMS:
            with self.subTest(form=form):
                # the blank line closes AC-1, so a bare "AC-1: ..." is not a continuation
                text = with_acs("- AC-1: mul returns the product of a and b\n\n" + form + "\n")
                doc = parse(text)
                self.assertEqual(doc.ac_ids, ["AC-1"])
                self.assertEqual(validate(doc), [malformed(text, form)])

    def test_one_problem_per_line_with_its_number(self):
        first, second = "- AC-2 two", "- **AC-3**: three"
        text = with_acs(
            "- AC-1: mul returns the product of a and b\n" + first + "\n\n" + second + "\n"
        )
        problems = validate(parse(text))
        self.assertEqual(problems, [malformed(text, first), malformed(text, second)])
        self.assertNotEqual(line_no(text, first), line_no(text, second))

    def test_bare_ac_reference_in_a_continuation_joins(self):
        # AC-4 amendment: a wrapped sentence may start its next line with an AC reference
        doc = parse(
            with_acs(
                "- AC-1: mul returns the product as in\n"
                "  AC-2 (twenty documents), each one\n"
                "AC-2 and AC-3 calls; reported\n"
                "- AC-2: mul with zero returns zero\n"
            )
        )
        self.assertEqual(
            doc.acs[0],
            (
                "AC-1",
                "mul returns the product as in AC-2 (twenty documents), each one "
                "AC-2 and AC-3 calls; reported",
            ),
        )
        self.assertEqual(doc.ac_ids, ["AC-1", "AC-2"])
        self.assertEqual(validate(doc), [])

    def test_marked_ac_like_line_in_a_continuation_is_reported(self):
        text = with_acs("- AC-1: mul returns the product\n  - AC-2 depends on it\n")
        doc = parse(text)
        self.assertEqual(doc.acs, [("AC-1", "mul returns the product")])
        self.assertEqual(validate(doc), [malformed(text, "  - AC-2 depends on it")])

    def test_bare_ac_line_after_a_closed_ac_is_reported(self):
        text = with_acs("- AC-1: mul returns the product\n- a plain bullet\nAC-2: mul by zero\n")
        self.assertEqual(validate(parse(text)), [malformed(text, "AC-2: mul by zero")])

    def test_bare_ac_definition_inside_an_open_ac_is_reported(self):
        # PR #46 follow-up 1: a forgotten bullet is not a wrapped sentence
        for bare in (
            "AC-2: mul with zero returns zero",
            "  AC-2：mul with zero returns zero",
            "**AC-2**: mul with zero returns zero",
            "AC-2 (PR #3 F1): mul with zero returns zero",
            "AC-2 (as for (m)): mul with zero returns zero",
        ):
            with self.subTest(bare=bare):
                text = with_acs("- AC-1: mul returns the product\n" + bare + "\n")
                doc = parse(text)
                self.assertEqual(doc.acs, [("AC-1", "mul returns the product")])
                self.assertEqual(validate(doc), [malformed(text, bare)])

    def test_line_numbers_count_front_matter_with_crlf(self):
        text = with_acs("- AC-1: mul returns the product of a and b\n- AC-2 two\n")
        problems = validate(parse(text.replace("\n", "\r\n")))
        self.assertEqual(problems, [malformed(text, "- AC-2 two")])

    def test_malformed_only_ac_also_reports_no_ac(self):
        # A document whose only AC-looking line is malformed has no AC at all: both problems.
        text = with_acs("- AC-1 mul returns the product of a and b\n")
        problems = validate(parse(text))
        self.assertIn(malformed(text, "- AC-1 mul returns the product of a and b"), problems)
        self.assertTrue(any("at least one" in p for p in problems), problems)

    def test_other_sections_are_ignored(self):
        text = CHANGE_MD.replace(
            "## Out of scope\nDivision.\n",
            "## Out of scope\nAC-3 is not in scope.\n- AC-4 division\n- **AC-5**: rounding\n",
        )
        self.assertNotEqual(text, CHANGE_MD)
        text = text.replace("## Why\n", "## Why\n1. AC-6: why\n")
        doc = parse(text)
        self.assertEqual(doc.ac_ids, ["AC-1", "AC-2"])
        self.assertEqual(validate(doc), [])


class ProseOutsideAnAc(unittest.TestCase):
    """fix/ac-prose AC-1 to AC-3: a bare line opening with an AC id and no colon is prose."""

    AC_1 = "- AC-1: mul returns the product of a and b"
    PROSE = (
        "AC-9 was added during implementation. The first full run failed",
        "**AC-2** was split from AC-1 after the first round",
        "`AC-2` was split from AC-1 after the first round",
        "AC-2 (twenty documents) was measured by hand",
        "  AC-2 and AC-3 were merged",
    )

    def test_prose_after_a_closed_ac_is_ignored(self):
        for closer in ("\n", "- a plain bullet\n"):
            for prose in self.PROSE:
                with self.subTest(closer=repr(closer), prose=prose):
                    text = with_acs(self.AC_1 + "\n" + closer + prose + "\nsecond line of it\n")
                    doc = parse(text)
                    self.assertEqual(doc.acs, [("AC-1", "mul returns the product of a and b")])
                    self.assertEqual(validate(doc), [])

    def test_prose_before_the_first_ac_is_ignored(self):
        doc = parse(with_acs(self.PROSE[0] + "\n\n" + self.AC_1 + "\n"))
        self.assertEqual(doc.acs, [("AC-1", "mul returns the product of a and b")])
        self.assertEqual(validate(doc), [])

    def test_prose_only_still_needs_an_ac(self):
        problems = validate(parse(with_acs(self.PROSE[0] + "\n")))
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("needs at least one", problems[0])

    def test_marked_or_defined_lines_after_a_closed_ac_are_still_reported(self):
        for line in (
            "AC-2: mul with zero returns zero",
            "**AC-2**: mul with zero returns zero",
            "AC-2 (PR #3 F1): mul with zero returns zero",
            "AC-2 (as for (m))：mul with zero returns zero",
            "- AC-2 mul with zero returns zero",
            "1. AC-2 mul with zero returns zero",
        ):
            with self.subTest(line=line):
                text = with_acs(self.AC_1 + "\n\n" + line + "\n")
                doc = parse(text)
                self.assertEqual(doc.ac_ids, ["AC-1"])
                self.assertEqual(validate(doc), [malformed(text, line)])

    def test_known_cost_is_documented(self):
        # AC-3: a real AC with neither bullet nor colon reads as prose and is ignored
        doc = parse(with_acs(self.AC_1 + "\n\nAC-3 mul returns the product of floats\n"))
        self.assertEqual(doc.ac_ids, ["AC-1"])
        self.assertEqual(validate(doc), [])
        self.assertIn("without a bullet and without a colon", " ".join(changedoc.__doc__.split()))


class LineClassesAgree(unittest.TestCase):
    """fix/ac-prose AC-4: an open AC text and an unclosed note end on the same lines."""

    # line shape -> (ends the open text or note, the line itself is reported)
    SHAPES = {
        "blank": ("", True, False),
        "well-formed AC": ("- AC-2: mul by zero returns zero", True, False),
        "marked AC-like": ("- AC-2 no colon here", True, True),
        "bare definition": ("AC-2: mul by zero returns zero", True, True),
        "bare prose": ("AC-2 was split from AC-1", False, False),
        "shallow list item": ("- a plain bullet", True, False),
        "deeper list item": ("  - a nested bullet", False, False),
        "plain text": ("plain words that wrap", False, False),
    }
    TAIL = "  zzz tail"

    def outcome(self, head: str, line: str, tail: str):
        text = with_acs(head + "\n" + line + "\n" + tail + "\n")
        number = line_no(text, head) + 1
        doc = parse(text)
        reported = any(p.startswith("change.md: line %d " % number) for p in validate(doc))
        ac_1 = dict(doc.acs).get("AC-1")
        ended = ac_1 is None or "zzz" not in ac_1
        return ended, reported

    def test_each_shape_ends_both_or_neither(self):
        for name, (line, ends, reported) in self.SHAPES.items():
            with self.subTest(shape=name):
                in_text = self.outcome("- AC-1: mul returns the product", line, self.TAIL)
                in_note = self.outcome("- AC-1 (note", line, "  zzz): mul returns the product")
                self.assertEqual(in_text, (ends, reported))
                self.assertEqual(in_note, (ends, reported))


class WrappedNotes(unittest.TestCase):
    """AC-10: a note may wrap onto the following lines."""

    def test_note_on_two_lines(self):
        doc = parse(
            with_acs(
                "- AC-9 (manual, measured, outside the\n"
                "  sandbox; bars set by the owner): mul handles\n"
                "  big integers\n"
                "- AC-2: mul with zero returns zero\n"
            )
        )
        self.assertEqual(
            doc.acs,
            [
                (
                    "AC-9",
                    "(manual, measured, outside the sandbox; bars set by the owner) "
                    "mul handles big integers",
                ),
                ("AC-2", "mul with zero returns zero"),
            ],
        )
        self.assertEqual(validate(doc), [])

    def test_note_on_three_lines_text_on_the_next(self):
        doc = parse(
            with_acs(
                "  - AC-9 (go/no-go,\n"
                "    measured before revali;\n"
                "    AC-8 too)：\n"
                "    mul handles big integers\n"
            )
        )
        self.assertEqual(
            doc.acs,
            [("AC-9", "(go/no-go, measured before revali; AC-8 too) mul handles big integers")],
        )
        self.assertEqual(validate(doc), [])

    def test_unclosed_note_is_reported(self):
        first = "- AC-9 (manual, measured, outside the"
        endings = {
            "blank line": "\n\nmul handles big integers\n",
            "end of section": "\n  sandbox and more\n",
            "list item": "\n  sandbox and more\n- a plain bullet\n",
        }
        for name, rest in endings.items():
            with self.subTest(ending=name):
                text = with_acs(first + rest)
                doc = parse(text)
                self.assertEqual(doc.ac_ids, [])
                self.assertIn(malformed(text, first), validate(doc))

    def test_next_ac_line_ends_an_unclosed_note(self):
        first = "- AC-9 (manual, measured, outside the"
        text = with_acs(first + "\n- AC-2: mul with zero returns zero\n")
        doc = parse(text)
        self.assertEqual(doc.acs, [("AC-2", "mul with zero returns zero")])
        self.assertEqual(validate(doc), [malformed(text, first)])

    def test_deeper_ac_line_ends_an_unclosed_note(self):
        # PR #46 F1: a nested AC line must not vanish into the open note
        first = "- AC-9 (unclosed"
        text = with_acs(first + "\n    - AC-10 (x\n      y): mul handles big integers\n")
        doc = parse(text)
        self.assertEqual(doc.acs, [("AC-10", "(x y) mul handles big integers")])
        self.assertEqual(validate(doc), [malformed(text, first)])

    def test_bare_ac_definition_ends_an_unclosed_note(self):
        # PR #46 round 2 F1: a forgotten bullet inside an open note must not vanish into it
        first, bare = "- AC-9 (manual", "AC-2: mul by zero"
        text = with_acs(first + "\n" + bare + "\n  by the owner): mul handles big integers\n")
        doc = parse(text)
        self.assertEqual(doc.ac_ids, [])
        problems = validate(doc)
        self.assertIn(malformed(text, first), problems)
        self.assertIn(malformed(text, bare), problems)

    def test_deeper_malformed_ac_line_ends_an_unclosed_note(self):
        first, second = "- AC-9 (unclosed", "    - **AC-10**: bold"
        text = with_acs(first + "\n" + second + "\n")
        problems = validate(parse(text))
        self.assertIn(malformed(text, first), problems)
        self.assertIn(malformed(text, second), problems)
        self.assertEqual(len(problems), 3, problems)  # the two, and "needs at least one"

    def test_note_closed_without_a_colon_is_reported(self):
        first = "- AC-9 (manual, measured"
        text = with_acs(first + "\n  by the owner) mul handles big integers\n")
        doc = parse(text)
        self.assertEqual(doc.ac_ids, [])
        self.assertIn(malformed(text, first), validate(doc))


class JoinedTextChecks(unittest.TestCase):
    """AC-6: duplicate and length checks see the joined text."""

    def test_wrapped_short_first_line_is_not_too_short(self):
        doc = parse(with_acs("- AC-1: mul\n  returns the product of a and b\n"))
        self.assertEqual(validate(doc), [])

    def test_empty_ac_is_too_short_not_dropped(self):
        doc = parse(with_acs("- AC-1: mul returns the product of a and b\n- AC-2:\n\n"))
        self.assertEqual(doc.ac_ids, ["AC-1", "AC-2"])
        self.assertEqual(validate(doc), ["change.md: AC-2 is too short to be testable"])

    def test_duplicate_across_forms(self):
        doc = parse(
            with_acs(
                "- AC-1: mul returns the product of a and b\n"
                "- AC-1 (PR #3 F1):\n"
                "  mul with zero returns zero\n"
            )
        )
        self.assertEqual(validate(doc), ["change.md: duplicate AC-1"])


class NewlyParsedAcsReachCoverage(unittest.TestCase):
    """AC-8: an AC only the new rules parse is held to the coverage check."""

    TEXT = with_acs(
        "- AC-1: mul(a, b) returns the product of a and b for integers\n"
        "- AC-2: mul with zero returns zero\n"
        "- AC-3 (PR #30 F1): mul with a negative operand\n"
        "  returns a negative product\n"
        "- AC-4:\n"
        "  mul of two floats returns a float\n"
    )

    def test_ac_gaps(self):
        doc = parse(self.TEXT)
        self.assertEqual(ac_gaps(approve_response(), doc.ac_ids), ["AC-3", "AC-4"])

    def test_review_md(self):
        doc = parse(self.TEXT)
        md = render_review_md(
            approve_response(), CHANGES_REQUESTED, [], {"round": 1, "model_actual": "m"}, doc.ac_ids
        )
        self.assertIn("- AC-3: **uncovered**", md)
        self.assertIn("- AC-4: **uncovered**", md)

    def test_tests_md(self):
        doc = parse(self.TEXT)
        ctx = types.SimpleNamespace(doc=doc, branch="feature/mul")
        md = render_tests_md(ctx, None, [{"data": approve_response()}])
        self.assertIn(
            "- AC-3: (PR #30 F1) mul with a negative operand returns a negative product"
            " -> **uncovered**",
            md,
        )
        self.assertIn("- AC-4: mul of two floats returns a float -> **uncovered**", md)


class FormatIsDocumented(unittest.TestCase):
    """AC-9: the author-facing docs state the accepted form and what stops preflight."""

    def test_skill_and_template(self):
        for rel in (("skill", "SKILL.md"), ("templates", "change.md")):
            with self.subTest(path="/".join(rel)):
                with open(os.path.join(ROOT, *rel), "r", encoding="utf-8", newline="") as fh:
                    text = " ".join(fh.read().split())
                self.assertIn("- AC-2 (PR #30 F1): text", text)
                self.assertIn("continuation lines", text)
                self.assertIn("any other line that starts like an AC stops preflight", text)
                # fix/ac-prose AC-6
                self.assertIn("without a bullet or a colon, is prose", text)


if __name__ == "__main__":
    unittest.main()
