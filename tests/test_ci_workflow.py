"""feature/ci AC-6: the GitHub Actions workflow, pinned against its text (no YAML library)."""

import os
import re
import tomllib
import unittest

from tests.helpers import ROOT

WORKFLOW = os.path.join(ROOT, ".github", "workflows", "test.yml")


def read(*parts: str) -> str:
    with open(os.path.join(ROOT, *parts), "r", encoding="utf-8", newline="") as fh:
        return fh.read()


def block(text: str, key: str) -> str:
    """The lines under a top-level `key:` up to the next top-level key."""
    m = re.search(r"^%s:[^\n]*\n((?:[ \t]+[^\n]*\n|\n)*)" % re.escape(key), text, re.M)
    if not m:
        raise AssertionError("no top-level %r in the workflow" % key)
    return m.group(1)


def legs(text: str):
    """The matrix `include` entries as dicts of their string values."""
    include = text.split("include:", 1)[1].split("steps:", 1)[0]
    out = []
    for item in re.split(r"^\s*- ", include, flags=re.M)[1:]:
        leg = {}
        for key, value in re.findall(r"^\s*(\w+):\s*(.+?)\s*$", "  " + item, re.M):
            leg[key] = value.strip("\"'")
        out.append(leg)
    return out


class Workflow(unittest.TestCase):
    def setUp(self):
        self.text = read(".github", "workflows", "test.yml")

    def test_push_is_the_only_trigger(self):
        # AC-1
        on = block(self.text, "on")
        self.assertEqual(re.findall(r"^  (\w+):", on, re.M), ["push"])
        code = "\n".join(ln for ln in self.text.splitlines() if not ln.lstrip().startswith("#"))
        self.assertNotIn("pull_request", code)

    def test_read_only_token_and_superseded_runs_cancelled(self):
        # AC-1
        self.assertEqual(block(self.text, "permissions").split(), ["contents:", "read"])
        concurrency = block(self.text, "concurrency")
        self.assertIn("github.ref", concurrency)
        self.assertRegex(concurrency, r"cancel-in-progress:\s*true")

    def test_matrix_legs(self):
        # AC-2; ci/windows-job AC-1 added the Windows leg; ci-windows-shards AC-3 runs it as
        # four shards
        target = tomllib.loads(read("pyproject.toml"))["tool"]["black"]["target-version"]
        self.assertEqual(target, ["py311"])
        self.assertEqual(
            [(leg["os"], leg["python"]) for leg in legs(self.text)],
            [
                ("ubuntu-latest", "3.11"),
                ("ubuntu-latest", "3.x"),
            ]
            + [("windows-latest", "3.x")] * 4,
        )
        self.assertRegex(self.text, r"fail-fast:\s*false")

    def test_the_windows_shards_cover_the_suite_once(self):
        # ci-windows-shards AC-3, AC-4: shards 1..N of one N, each once, only on Windows;
        # the suite step hands the leg's shard to the runner, and the job name shows it
        windows = [leg for leg in legs(self.text) if leg["os"].startswith("windows")]
        self.assertEqual({leg.get("shards") for leg in windows}, {"4"})
        self.assertEqual(sorted(int(leg["shard"]) for leg in windows), [1, 2, 3, 4])
        others = [leg for leg in legs(self.text) if not leg["os"].startswith("windows")]
        self.assertFalse([leg for leg in others if "shard" in leg or "shards" in leg])
        job = block(block(self.text, "jobs"), "  test")
        self.assertRegex(
            job.split("steps:", 1)[0],
            r"RUN_PARALLEL_SHARD: \$\{\{ matrix\.shard && "
            r"format\('\{0\}/\{1\}', matrix\.shard, matrix\.shards\) \|\| '' \}\}",
        )
        self.assertIn(
            "format(' / shard {0} of {1}', matrix.shard, matrix.shards)", job.splitlines()[0]
        )

    def test_a_broken_shard_list_fails_the_coverage_check(self):
        # ci-windows-shards AC-4: the check above, fed a workflow whose shards are wrong
        good = self.text
        broken = {
            "a shard missing": good.replace("shard: 4\n", "shard: 3\n"),
            "two Ns": good.replace("shards: 4\n", "shards: 5\n", 1),
        }
        for what, text in broken.items():
            with self.subTest(what=what):
                self.assertNotEqual(text, good)
                self.text = text
                with self.assertRaises(AssertionError):
                    self.test_the_windows_shards_cover_the_suite_once()
        self.text = good

    def test_a_hung_leg_ends_before_merge_stops_waiting(self):
        # ci/windows-job AC-2: the job's timeout-minutes is below revali's own
        # checks_timeout_min (75); the defaults other projects get stay at 30
        job = block(block(self.text, "jobs"), "  test")
        timeouts = re.findall(r"^    timeout-minutes:\s*(\d+)\s*$", job, re.M)
        self.assertEqual(len(timeouts), 1, "one timeout-minutes on the test job")
        wait = tomllib.loads(read("revali.toml"))["merge"]["checks_timeout_min"]
        self.assertEqual(wait, 75)
        self.assertLess(int(timeouts[0]), wait)
        for rel in (("defaults.toml",), ("templates", "revali.toml")):
            with self.subTest(path="/".join(rel)):
                self.assertEqual(tomllib.loads(read(*rel))["merge"]["checks_timeout_min"], 30)

    def test_one_leg_lints_with_the_projects_lint_line(self):
        # AC-3
        lint = tomllib.loads(read("revali.toml"))["project"]["lint"]
        self.assertEqual(
            [(leg["os"], leg["python"]) for leg in legs(self.text) if leg.get("lint") == "true"],
            [("ubuntu-latest", "3.x")],
        )
        steps = self.text.split("steps:", 1)[1]
        lint_step = steps.split("- name: lint\n", 1)[1].split("- ", 1)[0]
        self.assertEqual(re.findall(r"^\s*run: (.*)$", lint_step, re.M), [lint])
        for name in ("install lint tools", "lint"):
            with self.subTest(step=name):
                step = steps.split("- name: %s\n" % name, 1)[1].split("- ", 1)[0]
                self.assertIn("if: matrix.lint", step)
        self.assertEqual(self.text.count("pip install"), 1)

    def test_every_leg_runs_the_suite_as_readme_says(self):
        # AC-4
        command = "python tests/run_parallel.py"
        self.assertIn(command + "  ", read("README.md"))
        suite = self.text.split("- name: suite\n", 1)[1]
        self.assertNotIn("if:", suite)
        self.assertRegex(suite, r"^\s*run: %s\s*$" % re.escape(command))

    def test_no_git_identity_is_configured(self):
        # AC-5: the suite sets its own identity; the workflow does not
        self.assertNotIn("user.email", self.text)
        self.assertNotIn("user.name", self.text)


class Docs(unittest.TestCase):
    def test_conventions_and_workflow_doc_name_the_ci(self):
        # AC-7
        for rel in (("CONVENTIONS.md",), ("docs", "workflow.md")):
            with self.subTest(path="/".join(rel)):
                text = " ".join(read(*rel).split())
                self.assertIn("the suite on Linux and Windows on every push", text)
                self.assertIn("`revali merge` waits for it", text)
                self.assertIn("test.yml", text)
                self.assertNotIn("Windows and macOS on every push", text)

    def test_readme_within_its_cap(self):
        # AC-7
        self.assertLessEqual(len(read("README.md").splitlines()), 170)


if __name__ == "__main__":
    unittest.main()
