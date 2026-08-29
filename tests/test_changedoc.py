import unittest

from tests.helpers import ROOT  # noqa: F401  (sys.path setup)
from tests.fixtures.make_sample_repo import CHANGE_MD
from revali.changedoc import parse, validate


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
        doc = parse(CHANGE_MD.replace("add a mul(a, b) function to calc that multiplies two numbers", ""))
        self.assertTrue(any("Request" in p for p in validate(doc)))

    def test_missing_ac(self):
        text = CHANGE_MD.replace("- AC-1: mul(a, b) returns the product of a and b for integers\n", "")
        text = text.replace("- AC-2: mul with zero returns zero\n", "")
        self.assertTrue(any("at least one" in p for p in validate(parse(text))))

    def test_duplicate_ac(self):
        text = CHANGE_MD.replace("- AC-2:", "- AC-1:")
        self.assertTrue(any("duplicate AC-1" in p for p in validate(parse(text))))

    def test_short_ac(self):
        text = CHANGE_MD.replace("- AC-2: mul with zero returns zero", "- AC-2: ok")
        self.assertTrue(any("too short" in p for p in validate(parse(text))))

    def test_draft_refused(self):
        text = CHANGE_MD.replace("author_model: fixture\n", "author_model: fixture\nstatus: draft\n")
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

    def test_crlf_input(self):
        doc = parse(CHANGE_MD.replace("\n", "\r\n"))
        self.assertEqual(doc.ac_ids, ["AC-1", "AC-2"])
        self.assertEqual(validate(doc), [])


if __name__ == "__main__":
    unittest.main()
