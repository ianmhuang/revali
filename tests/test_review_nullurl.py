"""Reviewer acceptance test for fix/history-append-lock, AC-6 (the code half):
`gh_open_issues` returns "" for a listed issue whose `url` is null or missing, not "None".

Through the fake gh: scenario key `issue_list_raw` is what `gh issue list` prints.
"""

import json

from revali import gitops
from tests.helpers import RepoCase

URL = "https://github.example/me/sample/issues/3"


class OpenIssuesUrl(RepoCase):
    def listing(self, items):
        self.scenario({"issue_list_raw": json.dumps(items)})
        return gitops.gh_open_issues(self.repo)

    def test_a_null_url_is_an_empty_string(self):
        got = self.listing([{"number": 1, "title": "a", "url": None}])
        self.assertEqual(got, [{"number": 1, "title": "a", "url": ""}])

    def test_a_missing_url_is_an_empty_string(self):
        got = self.listing([{"number": 2, "title": "b"}])
        self.assertEqual(got, [{"number": 2, "title": "b", "url": ""}])

    def test_a_url_is_kept_and_the_order_with_it(self):
        got = self.listing(
            [
                {"number": 1, "title": "a", "url": None},
                {"number": 3, "title": "c", "url": URL},
                {"number": 2, "title": "b"},
            ]
        )
        self.assertEqual([i["number"] for i in got], [1, 3, 2])
        self.assertEqual([i["url"] for i in got], ["", URL, ""])
        self.assertNotIn("None", [i["url"] for i in got])
