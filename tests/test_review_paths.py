"""AC-1 / AC-2: [paths] state_dir and logs_dir drive every command; sandbox_dir,
prompt, schema and checklist_builtin replace the shipped files."""
import os
import sys
import unittest

from tests.helpers import RepoCase, _quote, claude_entry, git, run_cli
from revali import EXIT_ACTION, EXIT_ERROR, EXIT_OK
from revali.config import PlatformCfg
from revali.runners import RunnerError, WslRunner
from revali.state import RunLog, State, lock_path, write_json_atomic

CHANGE = ".revali/feature__mul/change.md"
DEAD_PID = 2 ** 31 - 1   # above any pid range on Windows and Linux: never alive

REVIEW_PROMPT = "REVIEW PROMPT $branch round $round\n\n$checklist\n\n$diff\n"
REVIEW_SCHEMA = '{"type": "object", "x-custom": "review"}\n'
BUILTIN = "PROJECT BUILT-IN LIST\n"
DIAG_PROMPT = "DIAG PROMPT $failed_step\n$log_tail\n"
DIAG_SCHEMA = '{"type": "object", "x-custom": "diagnose"}\n'

# Answers `wslpath`, refuses to run bash: the generated script stays on disk unexecuted.
WSLPATH_ONLY = '''import sys
a = sys.argv[1:]
cmd = a[3:] if len(a) >= 4 and a[0] == "-d" else a
if cmd and cmd[0] == "wslpath":
    print(cmd[-1].replace("\\\\", "/"))
    sys.exit(0)
sys.exit(1)
'''


def argv_after(argv, flag):
    return argv[argv.index(flag) + 1]


class StateAndLogsDir(RepoCase):
    def setUp(self):
        super().setUp()
        self.write("revali.toml", self.read("revali.toml") + '\n[paths]\nstate_dir = ".box"\nlogs_dir = "trace"\n')
        self.commit_all("custom paths")
        self.write(".box/feature__mul/change.md", self.read(CHANGE))

    def test_every_command_uses_the_configured_directories(self):
        # AC-1: the untracked state directory is not a dirty tree
        code, out = run_cli(["preflight"])
        self.assertEqual(code, EXIT_OK, out)

        self.claude(claude_entry())
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_OK, out)
        for rel in ("review-1.md", "review-1.json", "tests.md", "state.json",
                    "trace/revali.log", "trace/review-r1-1.raw.json", "trace/prompt-r1-1.md",
                    "trace/pr-body.md", "trace/comment-review-1.md"):
            self.assertTrue(self.exists(".box/feature__mul/" + rel), rel)
        self.assertFalse(self.exists(".box/feature__mul/logs"))
        self.assertFalse(self.exists(".revali/feature__mul/review-1.md"))
        self.assertFalse(self.exists(".revali/feature__mul/state.json"))
        # the state directory name is what lands in .gitignore
        self.assertIn(".box/\n", self.read(".gitignore"))
        self.assertIn("chore: ignore .box/", git(["log", "--format=%s"], self.repo))

        code, out = run_cli(["status"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("stage: ready_to_merge", out)

        code, out = run_cli(["wait", "--timeout", "1s"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("ready_to_merge", out)

        code, out = run_cli(["reset"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertFalse(self.exists(".box/feature__mul/state.json"))
        self.assertTrue(self.exists(".box/feature__mul/review-1.md"))

        code, out = run_cli(["clean", "feature/mul"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertFalse(self.exists(".box/feature__mul"))

    def test_stop_without_a_run_looks_in_the_configured_directory(self):
        code, out = run_cli(["stop"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("no run in progress", out)
        self.assertFalse(self.exists(".revali/feature__mul/lock"))

    def test_wait_hint_names_the_configured_logs_dir(self):
        # AC-1 / AC-5 (round-1 F3): a run that died leaves the user a hint pointing at
        # <state_dir>/<branch>/<logs_dir>/run.log, not at a literal logs/ directory
        rdir = os.path.join(self.repo, ".box", "feature__mul")
        State(stage="review", message="reviewer round 1").save(rdir)
        write_json_atomic(lock_path(rdir), {"pid": DEAD_PID, "since": "2026-01-01T00:00:00+0000"})
        code, out = run_cli(["wait", "--timeout", "1s"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertIn("died at stage 'review'", out)
        self.assertIn(os.path.join(".box", "feature__mul", "trace", "run.log"), out)
        self.assertNotIn(os.path.join("logs", "run.log"), out)
        self.assertFalse(self.exists(".box/feature__mul/lock"))

    def test_broken_config_still_uses_the_configured_state_dir(self):
        # AC-6 (round-1 F5): with a config that does not validate, the change.md lookup
        # follows the project's own [paths] state_dir instead of the tool default
        self.write("revali.toml", self.read("revali.toml").replace("budget_usd = 1.0", 'budget_usd = "lots"'))
        self.commit_all("broken config")
        code, out = run_cli(["preflight"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertIn("review.budget_usd must be a number", out)
        self.assertNotIn("change.md", out)
        # and when change.md really is missing, the message names the configured directory
        os.remove(os.path.join(self.repo, ".box", "feature__mul", "change.md"))
        code, out = run_cli(["preflight"])
        self.assertEqual(code, EXIT_ERROR, out)
        self.assertIn("change.md not found", out)
        self.assertIn(".box", out)
        self.assertNotIn(".revali", out)

    def test_runlog_has_no_logs_dir_default(self):
        # AC-5 / conventions (round-1 F4): the logs dir is not defaulted in code
        rdir = os.path.join(self.repo, ".box", "feature__mul")
        with self.assertRaises(TypeError):
            RunLog(rdir)
        with self.assertRaises(TypeError):
            RunLog(rdir, logs_dir="")
        RunLog(None)   # no branch directory, no file: allowed
        log = RunLog(rdir, logs_dir="trace", quiet=True)
        log.stage("test", "hello")
        self.assertTrue(self.exists(".box/feature__mul/trace/revali.log"))
        self.assertFalse(self.exists(".box/feature__mul/logs"))


class UserLayerStateDir(RepoCase):
    def test_state_dir_from_the_user_config(self):
        with open(os.path.join(self.home, "config.toml"), "w", encoding="utf-8") as fh:
            fh.write('[paths]\nstate_dir = ".ubox"\n')
        self.write(".ubox/feature__mul/change.md", self.read(CHANGE))
        code, out = run_cli(["run", "--dry-run"])
        self.assertEqual(code, EXIT_OK, out)
        self.assertTrue(self.exists(".ubox/feature__mul/state.json"))
        self.assertTrue(self.exists(".ubox/feature__mul/logs/revali.log"))
        self.assertFalse(self.exists(".revali/feature__mul/state.json"))


class SandboxDir(RepoCase):
    runner = "wsl"

    def setUp(self):
        super().setUp()
        self.use_real_local_runner()
        stub = os.path.join(self.tmp, "wslpath_only.py")
        with open(stub, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(WSLPATH_ONLY)
        os.environ["REVALI_WSL_CMD"] = "%s %s" % (_quote(sys.executable), _quote(stub))

    def script_for(self, sandbox_dir, label):
        r = WslRunner(PlatformCfg(runner="wsl", distro="Ubuntu", command_timeout_min=1, sandbox_dir=sandbox_dir))
        logs = os.path.join(self.tmp, "logs-" + label)
        head = git(["rev-parse", "HEAD"], self.repo).strip()
        try:
            r.run(self.repo, head, [("test", "true")], {}, logs, label)
        except RunnerError:
            pass
        with open(os.path.join(logs, label + ".sh"), "r", encoding="utf-8") as fh:
            return fh.read()

    def test_sandbox_root_comes_from_the_platform_table(self):
        text = self.script_for("~/boxes", "validate-r7")
        self.assertIn('SB="$HOME/boxes/sample/validate-r7"', text)
        self.assertNotIn(".revali/sandbox", text)
        text = self.script_for("/srv/sb/", "validate-r8")
        self.assertIn('SB="/srv/sb/sample/validate-r8"', text)


class FileOverrides(RepoCase):
    def test_project_files_replace_the_shipped_ones(self):
        self.write("docs/rp.md", REVIEW_PROMPT)
        self.write("docs/rs.json", REVIEW_SCHEMA)
        self.write("docs/bl.md", BUILTIN)
        self.write("docs/dp.md", DIAG_PROMPT)
        self.write("docs/ds.json", DIAG_SCHEMA)
        cfg = self.read("revali.toml")
        cfg = cfg.replace("[review]\n", '[review]\nprompt = "docs/rp.md"\nschema = "docs/rs.json"\n'
                                        'checklist_builtin = "docs/bl.md"\n')
        cfg = cfg.replace("[validate]\n", '[validate]\nprompt = "docs/dp.md"\nschema = "docs/ds.json"\n')
        self.write("revali.toml", cfg)
        self.commit_all("overrides")
        self.runner_scenario({"default": 0, "results": {"validate-r1": {"new_test": 1}},
                              "outputs": {"validate-r1": {"new_test": "AssertionError: 12 != 7"}}})
        diag = {"summary": "product wrong", "cause": "code", "failures": [], "recommendation": "return a * b"}
        self.claude(claude_entry(), claude_entry(diag, write_tests=False))
        code, out = run_cli(["run", "--foreground"])
        self.assertEqual(code, EXIT_ACTION, out)
        reviewer, diagnoser = self.fake_calls("claude")
        self.assertTrue(reviewer["prompt"].startswith("REVIEW PROMPT feature/mul round 1"), reviewer["prompt"][:80])
        self.assertIn("PROJECT BUILT-IN LIST", reviewer["prompt"])
        self.assertNotIn("Behaviour changes have tests", reviewer["prompt"])
        self.assertIn("### Project", reviewer["prompt"])  # CONVENTIONS.md layer still there
        self.assertEqual(argv_after(reviewer["argv"], "--json-schema"), REVIEW_SCHEMA)
        self.assertTrue(diagnoser["prompt"].startswith("DIAG PROMPT new_test"), diagnoser["prompt"][:80])
        self.assertIn("AssertionError: 12 != 7", diagnoser["prompt"])
        self.assertEqual(argv_after(diagnoser["argv"], "--json-schema"), DIAG_SCHEMA)


if __name__ == "__main__":
    unittest.main()
