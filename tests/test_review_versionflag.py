"""Acceptance tests for `revali --version` (feature/version-flag).

AC-1: `revali --version` prints the `revali <VERSION>` line to stdout and exits 0,
      in-process through cli.main() and as a subprocess.
AC-2: `revali version` still prints the same line and exits 0.
AC-3: `revali run --version` is rejected as before; only the top-level flag exists.
AC-4: the README usage block lists `--version`.

Black-box: the tests go through cli.main() / revali.py and do not use the changed
run_cli helper, so they do not depend on how it maps SystemExit.
"""

import io
import os
import subprocess
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from revali import EXIT_OK, NAME, VERSION  # noqa: E402
from revali.cli import main  # noqa: E402

EXPECTED_LINE = "%s %s\n" % (NAME, VERSION)


def _main(argv):
    """Run cli.main in-process; return (exit code, stdout, stderr). argparse ends
    --version and usage errors with SystemExit, whose code is the exit code."""
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        try:
            code = main(argv)
        except SystemExit as raised:
            code = raised.code if raised.code is not None else 0
    return code, out.getvalue(), err.getvalue()


def _subprocess(argv):
    return subprocess.run(
        [sys.executable, os.path.join(ROOT, "revali.py")] + argv,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=ROOT,
    )


class VersionFlagInProcess(unittest.TestCase):
    def test_flag_prints_the_version_line_and_exits_0(self):
        """AC-1: the flag alone, no subcommand."""
        code, out, err = _main(["--version"])
        self.assertEqual(code, EXIT_OK, err)
        self.assertEqual(out, EXPECTED_LINE)
        self.assertEqual(err, "")

    def test_flag_and_subcommand_print_the_same_line(self):
        """AC-1 + AC-2: byte-for-byte the same stdout."""
        flag_code, flag_out, _ = _main(["--version"])
        sub_code, sub_out, sub_err = _main(["version"])
        self.assertEqual(sub_code, EXIT_OK, sub_err)
        self.assertEqual(sub_out, EXPECTED_LINE)
        self.assertEqual(flag_code, sub_code)
        self.assertEqual(flag_out, sub_out)

    def test_flag_after_verbose_still_works(self):
        """AC-1: the other top-level flag does not get in the way."""
        code, out, err = _main(["--verbose", "--version"])
        self.assertEqual(code, EXIT_OK, err)
        self.assertEqual(out, EXPECTED_LINE)

    def test_flag_is_listed_in_top_level_help(self):
        """AC-1: the flag is a real top-level option, so `-h` advertises it."""
        code, out, _ = _main(["-h"])
        self.assertEqual(code, EXIT_OK)
        self.assertIn("--version", out)

    def test_flag_after_a_subcommand_is_rejected(self):
        """AC-3: `revali run --version` is a usage error, and no version is printed."""
        for argv in (["run", "--version"], ["version", "--version"], ["status", "--version"]):
            with self.subTest(argv=argv):
                code, out, err = _main(argv)
                self.assertNotEqual(code, EXIT_OK)
                self.assertEqual(code, 2)  # argparse usage error, as before
                self.assertNotIn(VERSION, out)
                self.assertIn("--version", err)


class VersionFlagSubprocess(unittest.TestCase):
    def test_flag_through_the_entry_point(self):
        """AC-1: `python revali.py --version` exits 0 with the line on stdout only."""
        res = _subprocess(["--version"])
        self.assertEqual(res.returncode, EXIT_OK, res.stderr)
        self.assertEqual(res.stdout.strip(), EXPECTED_LINE.strip())
        self.assertEqual(res.stderr, "")

    def test_subcommand_through_the_entry_point_matches(self):
        """AC-2: `python revali.py version` still works and prints the same line."""
        flag = _subprocess(["--version"])
        sub = _subprocess(["version"])
        self.assertEqual(sub.returncode, EXIT_OK, sub.stderr)
        self.assertEqual(sub.stdout.strip(), EXPECTED_LINE.strip())
        self.assertEqual(flag.stdout, sub.stdout)

    def test_flag_after_a_subcommand_is_rejected_through_the_entry_point(self):
        """AC-3: the subprocess form of the rejection, exit 2 and nothing on stdout."""
        res = _subprocess(["run", "--version"])
        self.assertEqual(res.returncode, 2, res.stderr)
        self.assertNotIn(VERSION, res.stdout)
        self.assertIn("--version", res.stderr)


class ReadmeListsTheFlag(unittest.TestCase):
    def test_usage_block_mentions_the_flag(self):
        """AC-4: the fenced usage block under `## Usage` contains a `--version` line."""
        with open(os.path.join(ROOT, "README.md"), "r", encoding="utf-8", newline="") as fh:
            text = fh.read()
        self.assertIn("\n## Usage\n", text)
        usage = text.split("\n## Usage\n", 1)[1]
        self.assertIn("```", usage)
        block = usage.split("```", 2)[1]
        self.assertIn("--version", block)
        # the subcommand stays documented next to it
        self.assertIn("version", block.replace("--version", ""))


if __name__ == "__main__":
    unittest.main()
