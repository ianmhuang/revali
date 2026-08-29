"""Shared test scaffolding: temp repos, fake gh, isolated REVALI_HOME."""
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


def git(args, cwd):
    return subprocess.run(["git"] + args, cwd=cwd, check=True, capture_output=True, text=True,
                          encoding="utf-8").stdout


class RepoCase(unittest.TestCase):
    """A fixture repo in a temp dir (with a space in the path), fake gh, private REVALI_HOME."""

    with_remote = True
    with_branch = True

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="revali test ")
        self.addCleanup(self._cleanup)
        self.info = make_sample_repo.create(os.path.join(self.tmp, "sample"),
                                            with_remote=self.with_remote,
                                            with_branch=self.with_branch)
        self.repo = self.info["repo"]
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(self.home)
        self.scenario_path = os.path.join(self.tmp, "scenario.json")
        self.fake_log = os.path.join(self.tmp, "fake.log")
        self.scenario({})
        self._env_backup = dict(os.environ)
        os.environ["REVALI_HOME"] = self.home
        os.environ["REVALI_GH_CMD"] = "%s %s" % (_quote(sys.executable), _quote(GH_STUB))
        os.environ["REVALI_FAKE_SCENARIO"] = self.scenario_path
        os.environ["REVALI_FAKE_LOG"] = self.fake_log
        os.environ.pop("REVALI_DISABLE", None)
        self._cwd_backup = os.getcwd()
        os.chdir(self.repo)

    def _cleanup(self):
        os.chdir(self._cwd_backup)
        os.environ.clear()
        os.environ.update(self._env_backup)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def scenario(self, data):
        with open(self.scenario_path, "w", encoding="utf-8") as fh:
            json.dump(data, fh)

    def fake_calls(self):
        if not os.path.isfile(self.fake_log):
            return []
        with open(self.fake_log, "r", encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def write(self, relpath, text):
        path = os.path.join(self.repo, relpath)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        return path

    def read(self, relpath):
        with open(os.path.join(self.repo, relpath), "r", encoding="utf-8", newline="") as fh:
            return fh.read()

    def commit_all(self, message="wip"):
        git(["add", "-A"], self.repo)
        git(["commit", "-q", "-m", message], self.repo)

    def change_md(self):
        return os.path.join(self.repo, ".revali", "feature__mul", "change.md")


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
