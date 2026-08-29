"""Shared test scaffolding: temp repos, fake gh/claude/runner, isolated REVALI_HOME."""
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from tests.fixtures import make_sample_repo  # noqa: E402

FAKE_BIN = os.path.join(HERE, "fixtures", "fake_bin")
GH_STUB = os.path.join(FAKE_BIN, "gh_stub.py")
CLAUDE_STUB = os.path.join(FAKE_BIN, "claude_stub.py")

TEST_REVIEW_MUL = '''import unittest

from src.calc import mul


class MulTests(unittest.TestCase):
    def test_product(self):
        self.assertEqual(mul(3, 4), 12)

    def test_zero(self):
        self.assertEqual(mul(9, 0), 0)
'''


def git(args, cwd):
    return subprocess.run(["git"] + args, cwd=cwd, check=True, capture_output=True, text=True,
                          encoding="utf-8").stdout


def approve_response(**overrides):
    """A complete, schema-shaped reviewer answer that approves and covers both fixture ACs."""
    data = {
        "verdict": "APPROVE",
        "summary": "mul is implemented correctly and both acceptance criteria are met.",
        "questions": [],
        "findings": [],
        "previous_findings": [],
        "scope_mismatch": [],
        "dependencies_changed": [],
        "test_changes": [],
        "tests": [{"path": "tests/test_review_mul.py", "purpose": "product and zero",
                   "covers": ["AC-1", "AC-2"], "expected": "mul(3,4)=12; mul(9,0)=0 per AC-1/AC-2"}],
        "not_testable": [],
        "suggestions": [],
    }
    data.update(overrides)
    return data


def claude_entry(data=None, write_tests=True, **kw):
    entry = {"exit": 0, "structured_output": data if data is not None else approve_response(),
             "model": "claude-fable-5", "cost": 0.5}
    if write_tests:
        entry["write_files"] = {"tests/test_review_mul.py": TEST_REVIEW_MUL}
    entry.update(kw)
    return entry


class RepoCase(unittest.TestCase):
    """A fixture repo in a temp dir (with a space in the path), fake gh/claude/runner,
    private REVALI_HOME. Subclasses set `runner`, `with_remote`, `with_branch`."""

    with_remote = True
    with_branch = True
    runner = "local"

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="revali test ")
        self.addCleanup(self._cleanup)
        self.info = make_sample_repo.create(os.path.join(self.tmp, "sample"),
                                            with_remote=self.with_remote,
                                            with_branch=self.with_branch,
                                            runner=self.runner)
        self.repo = self.info["repo"]
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(self.home)
        self.scenario_path = os.path.join(self.tmp, "scenario.json")
        self.runner_path = os.path.join(self.tmp, "runner.json")
        self.fake_log = os.path.join(self.tmp, "fake.log")
        self._scenario = {}
        self.scenario({})
        self.runner_scenario({"default": 0})
        self._env_backup = dict(os.environ)
        os.environ["REVALI_HOME"] = self.home
        os.environ["REVALI_GH_CMD"] = "%s %s" % (_quote(sys.executable), _quote(GH_STUB))
        os.environ["REVALI_CLAUDE_CMD"] = "%s %s" % (_quote(sys.executable), _quote(CLAUDE_STUB))
        os.environ["REVALI_FAKE_SCENARIO"] = self.scenario_path
        os.environ["REVALI_FAKE_LOG"] = self.fake_log
        os.environ.pop("REVALI_DISABLE", None)
        self._cwd_backup = os.getcwd()
        os.chdir(self.repo)

    def _cleanup(self):
        os.chdir(self._cwd_backup)
        os.environ.clear()
        os.environ.update(self._env_backup)
        rmtree_force(self.tmp)

    def scenario(self, data):
        """Merge into the gh/claude scenario file; resets the claude answer cursor."""
        self._scenario.update(data)
        with open(self.scenario_path, "w", encoding="utf-8") as fh:
            json.dump(self._scenario, fh)
        idx = self.scenario_path + ".claude_idx"
        if os.path.isfile(idx):
            os.remove(idx)

    def claude(self, *entries):
        self.scenario({"claude": list(entries)})

    def runner_scenario(self, data):
        with open(self.runner_path, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        os.environ["REVALI_FAKE_RUNNER"] = self.runner_path

    def use_real_local_runner(self):
        os.environ.pop("REVALI_FAKE_RUNNER", None)

    def fake_calls(self, exe=None):
        if not os.path.isfile(self.fake_log):
            return []
        with open(self.fake_log, "r", encoding="utf-8") as fh:
            calls = [json.loads(line) for line in fh if line.strip()]
        return [c for c in calls if exe is None or c.get("exe") == exe]

    def write(self, relpath, text):
        path = os.path.join(self.repo, relpath)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        return path

    def read(self, relpath):
        with open(os.path.join(self.repo, relpath), "r", encoding="utf-8", newline="") as fh:
            return fh.read()

    def exists(self, relpath):
        return os.path.exists(os.path.join(self.repo, relpath))

    def commit_all(self, message="wip"):
        git(["add", "-A"], self.repo)
        git(["commit", "-q", "-m", message], self.repo)

    def change_md(self):
        return os.path.join(self.repo, ".revali", "feature__mul", "change.md")

    def rdir(self):
        return os.path.join(self.repo, ".revali", "feature__mul")


def _quote(path):
    if os.name == "nt":
        return '"%s"' % path if " " in path else path
    return "'%s'" % path if " " in path else path


@contextlib.contextmanager
def captured():
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        yield out


def run_cli(argv):
    """Run revali.cli.main in-process, returning (exit_code, stdout)."""
    from revali.cli import main
    with captured() as out:
        code = main(argv)
    return code, out.getvalue()


def rmtree_force(path):
    """rmtree that copes with read-only files (git objects on Windows)."""
    import stat

    def _onexc(func, p, exc):
        try:
            os.chmod(p, stat.S_IWRITE)
            func(p)
        except OSError:
            pass

    import time
    for attempt in range(4):  # a detached child may still hold the directory briefly
        if not os.path.isdir(path):
            return
        try:
            shutil.rmtree(path, onexc=_onexc)
        except TypeError:  # Python < 3.12
            shutil.rmtree(path, onerror=lambda f, p, e: _onexc(f, p, e[1]))
        except OSError:
            pass
        if os.path.isdir(path):
            time.sleep(0.5 * (attempt + 1))
