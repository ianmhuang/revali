import unittest

from tests.helpers import ROOT  # noqa: F401
from revali.secretscan import ALLOW_MARKER, scan_diff, scan_text


def diff(*added, removed=(), path="src/app.py"):
    lines = ["diff --git a/%s b/%s" % (path, path), "--- a/%s" % path, "+++ b/%s" % path, "@@ -1,2 +1,3 @@", " unchanged"]
    lines += ["-" + r for r in removed]
    lines += ["+" + a for a in added]
    return "\n".join(lines) + "\n"


class SecretScanTests(unittest.TestCase):
    def test_aws_key(self):
        hits = scan_diff(diff('KEY = "AKIAIOSFODNN7EXAMPLE"'))
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].pattern, "aws-access-key")
        self.assertEqual(hits[0].file, "src/app.py")
        self.assertEqual(hits[0].line, 2)
        self.assertNotIn("AKIAIOSFODNN7EXAMPLE", hits[0].excerpt)

    def test_github_token(self):
        hits = scan_diff(diff("token = 'ghp_" + "a" * 36 + "'"))
        self.assertEqual(hits[0].pattern, "github-token")

    def test_private_key_header(self):
        hits = scan_diff(diff("-----BEGIN RSA PRIVATE KEY-----"))
        self.assertEqual(hits[0].pattern, "private-key")

    def test_generic_assignment(self):
        hits = scan_diff(diff('password = "hunter2hunter2"'))
        self.assertEqual(hits[0].pattern, "generic-assignment")

    def test_placeholder_not_flagged(self):
        self.assertEqual(scan_diff(diff('password = "<your-password>"')), [])
        self.assertEqual(scan_diff(diff('api_key = os.environ["API_KEY"]')), [])

    def test_removed_lines_ignored(self):
        self.assertEqual(scan_diff(diff("x = 1", removed=['KEY = "AKIAIOSFODNN7EXAMPLE"'])), [])

    def test_allow_marker(self):
        self.assertEqual(scan_diff(diff('KEY = "AKIAIOSFODNN7EXAMPLE"  # %s' % ALLOW_MARKER)), [])

    def test_clean_diff(self):
        self.assertEqual(scan_diff(diff("def mul(a, b):", "    return a * b")), [])

    def test_scan_text(self):
        hits = scan_text("summary\nsk-ant-" + "b" * 24 + "\n", label="review")
        self.assertEqual(hits[0].file, "review")
        self.assertEqual(hits[0].line, 2)


if __name__ == "__main__":
    unittest.main()
