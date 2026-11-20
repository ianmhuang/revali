"""make_sample_repo.create() builds each kind of fixture once per process and copies it."""

import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.fixtures import make_sample_repo  # noqa: E402
from tests.helpers import rmtree_force  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "tests", "fixtures", "make_sample_repo.py")
KINDS = [
    (True, True, "local"),
    (False, True, "local"),
    (True, False, "local"),
    (False, False, "wsl"),
]


def git(args, cwd=None, git_dir=None):
    cmd = ["git"] + (["--git-dir", git_dir] if git_dir else []) + args
    res = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=True)
    return res.stdout.strip()


def files(top):
    """Every file under `top` but .git, as {relative path: bytes}."""
    out = {}
    for dirpath, dirnames, filenames in os.walk(top):
        dirnames[:] = [d for d in dirnames if d != ".git"]
        for name in filenames:
            path = os.path.join(dirpath, name)
            with open(path, "rb") as fh:
                out[os.path.relpath(path, top).replace(os.sep, "/")] = fh.read()
    return out


def refs(cwd=None, git_dir=None):
    """Each ref's tree, subject and depth: what a build gives, without its commit dates."""
    lines = git(["for-each-ref", "--format=%(refname)"], cwd, git_dir).splitlines()
    out = {}
    for ref in lines:
        tree = git(["rev-parse", ref + "^{tree}"], cwd, git_dir)
        subject = git(["log", "-1", "--format=%s", ref], cwd, git_dir)
        depth = git(["rev-list", "--count", ref], cwd, git_dir)
        out[ref] = (tree, subject, depth)
    return out


def snapshot(info):
    """What a test can see of a fixture, with its own paths replaced by placeholders."""
    repo = info["repo"]
    config = git(["config", "--local", "--list"], repo)
    if info["remote"]:
        config = config.replace(info["remote"], "<remote>")
    snap = {
        "files": files(repo),
        "refs": refs(repo),
        "head": git(["symbolic-ref", "HEAD"], repo),
        "config": sorted(config.splitlines()),
        "status": git(["status", "--porcelain", "--ignored"], repo),
        "branch": info["branch"],
    }
    if info["remote"]:
        snap["remote_refs"] = refs(git_dir=info["remote"])
        snap["upstream"] = git(["rev-parse", "--abbrev-ref", "main@{upstream}"], repo)
        snap["remote_main_is_main"] = git(["rev-parse", "main"], repo) == git(
            ["rev-parse", "main"], git_dir=info["remote"]
        )
    return snap


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="revali fixcopy "))
        self.addCleanup(rmtree_force, self.tmp)

    def path(self, name):
        return os.path.join(self.tmp, name)


class Copies(Case):
    def test_a_copy_matches_a_fresh_build(self):
        # AC-1: for each kind, a copy (the second call) looks like a fresh build
        for kind in KINDS:
            with self.subTest(kind=kind):
                tag = "%d%d%s" % kind
                fresh = make_sample_repo.build(self.path("fresh " + tag), *kind)
                make_sample_repo.create(self.path("first " + tag), *kind)
                copy = make_sample_repo.create(self.path("copy " + tag), *kind)
                self.assertEqual(snapshot(copy), snapshot(fresh))

    def test_the_returned_dict_names_the_target(self):
        # AC-1: the same keys and values a fresh build at that target returns
        for kind in KINDS:
            with self.subTest(kind=kind):
                target = self.path("t%d%d%s" % kind)
                info = make_sample_repo.create(target, *kind)
                self.assertEqual(
                    info,
                    {
                        "repo": target,
                        "remote": target + ".git" if kind[0] else None,
                        "branch": "feature/mul" if kind[1] else "main",
                    },
                )
                if kind[0]:
                    url = git(["config", "remote.origin.url"], target)
                    self.assertEqual(os.path.normcase(url), os.path.normcase(target + ".git"))

    def test_a_relative_target_is_made_absolute(self):
        # AC-1: as a build does, the dict holds the absolute path
        cwd = os.getcwd()
        os.chdir(self.tmp)
        self.addCleanup(os.chdir, cwd)
        info = make_sample_repo.create("rel", True, True, "local")
        self.assertEqual(info["repo"], self.path("rel"))
        self.assertEqual(info["remote"], self.path("rel") + ".git")
        self.assertEqual(git(["rev-parse", "--abbrev-ref", "HEAD"], info["repo"]), "feature/mul")

    def test_a_later_call_runs_no_git(self):
        # AC-1: built once per process; the copy itself starts no process
        make_sample_repo.create(self.path("warm"), True, True, "local")
        with mock.patch.object(
            make_sample_repo.subprocess, "run", side_effect=AssertionError("git started")
        ):
            info = make_sample_repo.create(self.path("again"), True, True, "local")
        self.assertTrue(os.path.isdir(os.path.join(info["repo"], ".git")))

    def test_copies_of_a_kind_share_their_commits(self):
        # Goal: the commit ids are the same within one process (they were not before)
        a = make_sample_repo.create(self.path("a"), True, True, "local")
        b = make_sample_repo.create(self.path("b"), True, True, "local")
        self.assertEqual(
            git(["rev-parse", "HEAD"], a["repo"]), git(["rev-parse", "HEAD"], b["repo"])
        )


class Independence(Case):
    def test_no_file_names_the_build_directory(self):
        # AC-2: neither the copy nor its remote refers to where the fixture was built
        info = make_sample_repo.create(self.path("own"), True, True, "local")
        built = make_sample_repo.build_dir()
        self.assertTrue(built and os.path.isdir(built), built)
        needles = {built, built.replace("\\", "/"), built.replace("\\", "\\\\")}
        for top in (info["repo"], info["remote"]):
            for dirpath, _dirs, names in os.walk(top):
                for name in names:
                    path = os.path.join(dirpath, name)
                    with open(path, "rb") as fh:
                        data = fh.read().decode("utf-8", "replace")
                    for needle in needles:
                        self.assertNotIn(needle, data, path)
        self.assertEqual(git(["fsck", "--no-progress"], info["repo"]), "")

    def test_a_change_in_one_copy_shows_nowhere_else(self):
        # AC-2: commit, push, branch and file in one copy; a later copy and its remote are clean
        a = make_sample_repo.create(self.path("a"), True, True, "local")
        before = snapshot(make_sample_repo.create(self.path("ref"), True, True, "local"))
        repo = a["repo"]
        git(["checkout", "-q", "main"], repo)
        with open(os.path.join(repo, "extra.txt"), "w", encoding="utf-8") as fh:
            fh.write("extra\n")
        git(["add", "extra.txt"], repo)
        git(["commit", "-q", "-m", "extra"], repo)
        git(["push", "-q", "origin", "main"], repo)
        git(["branch", "side"], repo)
        with open(os.path.join(repo, "loose.txt"), "w", encoding="utf-8") as fh:
            fh.write("loose\n")
        self.assertEqual(git(["log", "-1", "--format=%s", "main"], git_dir=a["remote"]), "extra")

        b = make_sample_repo.create(self.path("b"), True, True, "local")
        self.assertEqual(snapshot(b), before)
        ref = make_sample_repo.create(self.path("ref2"), True, True, "local")
        self.assertEqual(
            git(["log", "-1", "--format=%s", "main"], git_dir=ref["remote"]),
            "Initial sample project",
        )


class Lifetime(Case):
    def test_the_build_directory_goes_when_the_process_exits(self):
        # AC-3
        code = (
            "import sys; sys.path.insert(0, %r)\n"
            "from tests.fixtures import make_sample_repo as m\n"
            "m.create(%r, True, True, 'local')\n"
            "print(m.build_dir())\n" % (ROOT, self.path("child"))
        )
        res = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, res.stderr)
        built = res.stdout.strip()
        self.assertTrue(built)
        self.assertFalse(os.path.exists(built), built)
        self.assertTrue(os.path.isdir(os.path.join(self.path("child"), ".git")))

    def test_a_failed_build_raises_and_is_not_kept(self):
        # AC-3: a kind this suite uses nowhere else, so the first call is a build
        kind = (True, True, "failing build")
        real = subprocess.run
        calls = []

        def fail_on_push(cmd, *a, **kw):
            calls.append(cmd)
            if "push" in cmd:
                raise subprocess.CalledProcessError(1, cmd)
            return real(cmd, *a, **kw)

        def entries():
            built = make_sample_repo.build_dir()
            return set(os.listdir(built)) if built and os.path.isdir(built) else set()

        before = entries()
        with mock.patch.object(make_sample_repo.subprocess, "run", side_effect=fail_on_push):
            with self.assertRaises(subprocess.CalledProcessError):
                make_sample_repo.create(self.path("x"), *kind)
        self.assertTrue(any("push" in cmd for cmd in calls), calls)
        # nothing of the failed build stayed beside the kinds built before it
        self.assertEqual(entries(), before)
        info = make_sample_repo.create(self.path("y"), *kind)
        self.assertEqual(snapshot(info)["remote_refs"].keys(), {"refs/heads/main"})
        self.assertEqual(git(["rev-parse", "--abbrev-ref", "HEAD"], info["repo"]), "feature/mul")
        self.assertEqual(len(entries()), len(before) + 1)


class CommandLine(Case):
    def test_the_script_builds_the_same_repository(self):
        # AC-4
        for flags, kind in (
            ([], (True, True, "wsl")),
            (["--no-remote", "--no-branch", "--local"], (False, False, "local")),
        ):
            with self.subTest(flags=flags):
                target = self.path("cli%d" % len(flags))
                res = subprocess.run(
                    [sys.executable, SCRIPT, target] + flags, capture_output=True, text=True
                )
                self.assertEqual(res.returncode, 0, res.stderr)
                self.assertIn("repo: %s" % target, res.stdout)
                info = {
                    "repo": target,
                    "remote": target + ".git" if kind[0] else None,
                    "branch": "feature/mul" if kind[1] else "main",
                }
                fresh = make_sample_repo.build(self.path("fresh%d" % len(flags)), *kind)
                self.assertEqual(snapshot(info), snapshot(fresh))


if __name__ == "__main__":
    unittest.main()
