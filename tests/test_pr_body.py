"""The PR body is change.md without its `## Request` section, wherever that section sits."""

import unittest

from revali.pr import strip_request

FRONT = "---\ntitle: Add mul\nkind: feature\nauthor_model: m\n---\n\n"
REQUEST = "## Request\nadd a mul(a, b) function that multiplies two numbers\n\n"
GOAL = "## Goal\n`mul` multiplies two integers.\n\n"
ACS = "## Acceptance criteria\n- AC-1: mul(a, b) returns the product of a and b\n\n"
DEPS = "## Dependencies\nnone\n"


class StripRequest(unittest.TestCase):
    def test_first_section_goes_and_the_rest_is_verbatim(self):  # AC-1, AC-2
        self.assertEqual(
            strip_request(FRONT + REQUEST + GOAL + ACS + DEPS), FRONT + GOAL + ACS + DEPS
        )

    def test_middle_section_goes(self):  # AC-2
        self.assertEqual(
            strip_request(FRONT + GOAL + REQUEST + ACS + DEPS), FRONT + GOAL + ACS + DEPS
        )

    def test_last_section_with_no_heading_after_it_goes(self):  # AC-2
        text = FRONT + GOAL + ACS + DEPS + "\n## Request\nadd mul\n"
        self.assertEqual(strip_request(text), FRONT + GOAL + ACS + DEPS + "\n")

    def test_last_section_without_a_trailing_newline_goes(self):  # AC-2
        self.assertEqual(strip_request(FRONT + GOAL + "## Request\nadd mul"), FRONT + GOAL)

    def test_the_words_inside_another_section_stay(self):  # AC-2
        why = "## Why\nsee ## Request above; the user asked for it\n\n"
        self.assertEqual(strip_request(FRONT + REQUEST + why + GOAL), FRONT + why + GOAL)

    def test_a_longer_heading_is_not_the_request(self):  # AC-2
        text = FRONT + "## Requests handled\nnone\n\n" + GOAL
        self.assertEqual(strip_request(text), text)

    def test_a_document_without_the_section_is_unchanged(self):  # AC-2
        self.assertEqual(strip_request(FRONT + GOAL + ACS), FRONT + GOAL + ACS)

    def test_a_multi_paragraph_request_goes_whole(self):  # AC-1
        request = "## Request\nfirst line\n\nsecond paragraph, still the request\n\n"
        self.assertEqual(strip_request(FRONT + request + GOAL), FRONT + GOAL)


if __name__ == "__main__":
    unittest.main()
