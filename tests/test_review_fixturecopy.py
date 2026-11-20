"""Review of feature/fixture-copy: make_sample_repo.create() copies a fixture built once.

AC-1: a later call with the same options copies the fixture instead of building it, and the
copy matches a fresh build (files, branches and commits, checked-out branch, local config,
the remote and the upstream) and returns the dict a build at that target returns.
AC-2: a copy stands alone: it works after the build directory is gone, no file in it names
that directory, and a change in one copy reaches no other copy and no other copy's remote.
AC-3: the build directory is removed when the process exits; a failed build raises, keeps
nothing, and the next call builds again.
AC-4: the command line makes the same repository as before.

The fresh build every copy is compared with is the one the command line makes in its own
process. A call is known to be a copy by its commit ids: the second call runs with other
commit dates in the environment, which a build would take and a copy cannot.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from tests.fixtures import make_sample_repo
from tests.helpers import ROOT, rmtree_force

SCRIPT = os.path.join(ROOT, "tests", "fixtures", "make_sample_repo.py")
# (with_remote, with_branch, runner) and the command line flags that ask for the same kind
KINDS = [
    ((True, True, "local"), ["--local"]),
    ((False, True, "local"), ["--no-remote", "--local"]),
    ((True, False, "local"), ["--no-branch", "--local"]),
    ((False, False, "wsl"), ["--no-remote", "--no-branch"]),
]
DEFAULT = (True, True, "local")  # what RepoCase asks for
OTHER_DATE = "1112911993 +0000"  # 2005: no build of today has it
CHILD_HEAD = (
    "import json, os, sys\n"
    "sys.path.insert(0, %r)\n"
    "from tests.fixtures import make_sample_repo as m\n"
)
TWO_COPIES = (
    "a = m.create(%r, True, True, 'local')\n"
    "b = m.create(%r, True, True, 'local')\n"
    "built = m.build_dir()\n"
    "print(json.dumps({'built': built, 'real': os.path.realpath(built),\n"
    "                  'there': os.path.isdir(built), 'a': a, 'b': b}))\n"
)
FAILED_THEN_BUILT = (
    "os.environ['GIT_AUTHOR_DATE'] = 'xx'\n"  # git refuses the commit: invalid date format
    "try:\n"
    "    m.create(%r, True, True, 'local')\n"
    "    raised = ''\n"
    "except Exception as exc:\n"
    "    raised = type(exc).__name__\n"
    "del os.environ['GIT_AUTHOR_DATE']\n"
    "built = m.build_dir()\n"
    "left = [n for _d, _s, names in os.walk(built or '') for n in names]\n"
    "again = m.create(%r, True, True, 'local')\n"
    "later = m.create(%r, True, True, 'local')\n"
    "print(json.dumps({'raised': raised, 'left': left, 'built': m.build_dir(),\n"
    "                  'again': again, 'later': later}))\n"
)


def git(args, cwd):
    res = subprocess.run(
        ["git"] + args,
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    )
    return res.stdout.strip()


def tips(repo):
    return git(["for-each-ref", "--format=%(refname) %(objectname)"], repo)


def files(top):
    """Every file under `top` outside .git, as {relative path: bytes}."""
    out = {}
    for dirpath, dirnames, filenames in os.walk(top):
        dirnames[:] = [d for d in dirnames if d != ".git"]
        for name in filenames:
            path = os.path.join(dirpath, name)
            with open(path, "rb") as fh:
                out[os.path.relpath(path, top).replace(os.sep, "/")] = fh.read()
    return out


def refs(cwd, bare=False):
    """Each ref with whether HEAD is on it, its upstream, and its history as one line of
    tree, identity and subject per commit: what a build gives, whatever its commit dates.
    The second value is {ref: commit id}. HEAD is left out for a bare repository: the
    fixture never sets it there, so it follows the `init.defaultBranch` of whoever built it."""
    listed = git(["for-each-ref", "--format=%(refname)|%(objectname)|%(HEAD)|%(upstream)"], cwd)
    out, ids = {}, {}
    for line in listed.splitlines():
        name, sha, head, upstream = line.split("|")
        history = git(["log", "--format=%T|%an|%ae|%cn|%ce|%s", name, "--"], cwd).splitlines()
        on_head = None if bare else head == "*"
        out[name] = {"head": on_head, "upstream": upstream, "history": history}
        ids[name] = sha
    return out, ids


def config(cwd, remote):
    """The repository-local config, with the path of its own remote as a placeholder."""
    lines = git(["config", "--local", "--list"], cwd).splitlines()
    if remote:
        lines = [line.replace(remote, "<remote>") for line in lines]
    return sorted(lines)


def snapshot(info):
    """What a test can see of a fixture, without its own paths and commit dates."""
    repo, remote = info["repo"], info["remote"]
    own, ids = refs(repo)
    snap = {
        "files": files(repo),
        "refs": own,
        "config": config(repo, remote),
        "status": git(["status", "--porcelain", "--ignored"], repo),
        "branch": info["branch"],
        "remote": None,
    }
    if remote:
        theirs, their_ids = refs(remote, bare=True)
        snap["remote"] = {
            "refs": theirs,
            "config": config(remote, remote),
            "holds_main": their_ids.get("refs/heads/main") == ids["refs/heads/main"],
            "tracked": ids.get("refs/remotes/origin/main") == ids["refs/heads/main"],
        }
    return snap


def subjects(ref):
    return [line.rsplit("|", 1)[1] for line in ref["history"]]


def spellings(path):
    """The ways a file could hold `path`: as it is, with slashes, with escaped backslashes."""
    return {s.lower() for s in (path, path.replace("\\", "/"), path.replace("\\", "\\\\"))}


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="revali review fixcopy "))
        self.addCleanup(rmtree_force, self.tmp)

    def path(self, name):
        return os.path.join(self.tmp, name)

    def a_copy(self, name, kind):
        """The result of a later call for `kind`, shown to be a copy: it has the commits of
        the call before it, although a build at this moment would date them 2005."""
        first = make_sample_repo.create(self.path(name + " first"), *kind)
        dates = {"GIT_AUTHOR_DATE": OTHER_DATE, "GIT_COMMITTER_DATE": OTHER_DATE}
        with mock.patch.dict(os.environ, dates):
            copy = make_sample_repo.create(self.path(name), *kind)
        self.assertEqual(
            tips(copy["repo"]),
            tips(first["repo"]),
            "the later call built the fixture again instead of copying the first build",
        )
        return copy

    def assert_fixture(self, snap, kind):
        """The fixture the module has always promised for `kind`, read from a snapshot."""
        with_remote, with_branch, runner = kind
        held = snap["files"]
        for name in (
            "src/calc.py",
            "tests/test_calc.py",
            "revali.toml",
            "CONVENTIONS.md",
            ".gitignore",
            "requirements.txt",
        ):
            self.assertIn(name, held)
        self.assertEqual(b"def mul" in held["src/calc.py"], with_branch)
        platform = 'runner = "%s"' % ("local" if runner == "local" else "wsl")
        self.assertIn(platform.encode("utf-8"), held["revali.toml"])
        for line in (
            "user.email=fixture@example.invalid",
            "user.name=Fixture",
            "commit.gpgsign=false",
            "core.autocrlf=false",
        ):
            self.assertIn(line, snap["config"])
        main = snap["refs"]["refs/heads/main"]
        self.assertEqual(subjects(main), ["Initial sample project"])
        if with_branch:
            feature = snap["refs"]["refs/heads/feature/mul"]
            self.assertEqual(subjects(feature), ["Add mul", "Initial sample project"])
            self.assertTrue(feature["head"])
            self.assertFalse(main["head"])
            self.assertEqual(
                held[".revali/feature__mul/change.md"], make_sample_repo.CHANGE_MD.encode("utf-8")
            )
            self.assertEqual(snap["status"], "!! .revali/")
            self.assertEqual(snap["branch"], "feature/mul")
        else:
            self.assertNotIn("refs/heads/feature/mul", snap["refs"])
            self.assertTrue(main["head"])
            self.assertEqual([name for name in held if name.startswith(".revali/")], [])
            self.assertEqual(snap["status"], "")
            self.assertEqual(snap["branch"], "main")
        if with_remote:
            self.assertIn("remote.origin.url=<remote>", snap["config"])
            self.assertEqual(main["upstream"], "refs/remotes/origin/main")
            remote = snap["remote"]
            self.assertEqual(sorted(remote["refs"]), ["refs/heads/main"])
            self.assertEqual(subjects(remote["refs"]["refs/heads/main"]), subjects(main))
            self.assertIn("core.bare=true", remote["config"])
            self.assertTrue(remote["holds_main"])
            self.assertTrue(remote["tracked"])
        else:
            self.assertIsNone(snap["remote"])
            self.assertEqual([line for line in snap["config"] if line.startswith("remote.")], [])


class Copies(Case):
    def test_a_copy_matches_what_the_command_line_builds(self):
        # AC-1 and AC-4, for four kinds: the command line makes the repository it always
        # made, and a copy cannot be told from it
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        for kind, flags in KINDS:
            with self.subTest(kind=kind):
                tag = "%d%d%s" % kind
                branch = "feature/mul" if kind[1] else "main"
                target = self.path("cli " + tag)
                res = subprocess.run(
                    [sys.executable, SCRIPT, target] + flags,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    env=env,
                )
                self.assertEqual(res.returncode, 0, res.stderr)
                printed = ["repo: %s" % target]
                if kind[0]:
                    printed.append("remote: %s.git" % target)
                printed.append("branch: %s" % branch)
                self.assertEqual(res.stdout.splitlines(), printed)
                built = {
                    "repo": target,
                    "remote": target + ".git" if kind[0] else None,
                    "branch": branch,
                }
                fresh = snapshot(built)
                self.assert_fixture(fresh, kind)

                copy = self.a_copy("copy " + tag, kind)
                place = self.path("copy " + tag)
                self.assertEqual(
                    copy,
                    {
                        "repo": place,
                        "remote": place + ".git" if kind[0] else None,
                        "branch": branch,
                    },
                )
                self.assertEqual(os.path.isdir(place + ".git"), kind[0])
                self.assertEqual(snapshot(copy), fresh)

    def test_a_relative_target_comes_back_absolute(self):
        # AC-1: the dict of a later call holds what a build at that target returns
        self.a_copy("warm", DEFAULT)
        cwd = os.getcwd()
        os.chdir(self.tmp)
        self.addCleanup(os.chdir, cwd)
        info = make_sample_repo.create("rel", *DEFAULT)
        self.assertEqual(
            info,
            {
                "repo": self.path("rel"),
                "remote": self.path("rel") + ".git",
                "branch": "feature/mul",
            },
        )
        self.assert_fixture(snapshot(info), DEFAULT)


class Independence(Case):
    def test_a_change_in_one_copy_reaches_no_other(self):
        # AC-2: commits, a push, a branch, config and files changed in place in one copy;
        # an earlier copy, a later copy and their remotes stay what they were
        earlier = self.a_copy("earlier", DEFAULT)
        before = snapshot(earlier)
        changed = make_sample_repo.create(self.path("changed"), *DEFAULT)
        repo = changed["repo"]
        git(["checkout", "-q", "main"], repo)
        with open(os.path.join(repo, "extra.txt"), "w", encoding="utf-8", newline="\n") as fh:
            fh.write("extra\n")
        git(["add", "extra.txt"], repo)
        git(["commit", "-q", "-m", "extra"], repo)
        git(["push", "-q", "origin", "main"], repo)
        git(["branch", "side"], repo)
        git(["checkout", "-q", "feature/mul"], repo)
        git(["config", "user.name", "Somebody Else"], repo)
        # written in place, which a copy sharing its files with the build would pass on
        for name in (
            os.path.join("src", "calc.py"),
            os.path.join(".revali", "feature__mul", "change.md"),
            "README.md",
        ):
            with open(os.path.join(repo, name), "a", encoding="utf-8", newline="\n") as fh:
                fh.write("# changed in one copy\n")
        with open(os.path.join(repo, "loose.txt"), "w", encoding="utf-8", newline="\n") as fh:
            fh.write("loose\n")
        # the premise: the push went to this copy's own remote
        pushed, _ids = refs(changed["remote"])
        self.assertEqual(subjects(pushed["refs/heads/main"])[0], "extra")

        self.assertEqual(snapshot(earlier), before)
        later = make_sample_repo.create(self.path("later"), *DEFAULT)
        after = snapshot(later)
        self.assertEqual(after, before)
        self.assert_fixture(after, DEFAULT)
        self.assertNotIn("extra.txt", after["files"])
        self.assertNotIn("loose.txt", after["files"])
        self.assertNotIn("refs/heads/side", after["refs"])


class OwnProcess(Case):
    """A process of its own, with a temporary directory of its own, so that what it leaves
    behind when it exits can be seen."""

    def child(self, body):
        private = self.path("private temp")
        os.makedirs(private)
        env = dict(os.environ, TMPDIR=private, TEMP=private, TMP=private, PYTHONIOENCODING="utf-8")
        res = subprocess.run(
            [sys.executable, "-c", CHILD_HEAD % ROOT + body],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            cwd=self.tmp,
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        return json.loads(res.stdout.strip().splitlines()[-1]), private

    def assert_no_file_names(self, info, needles):
        for top in (info["repo"], info["remote"]):
            for dirpath, _dirs, names in os.walk(top):
                for name in names:
                    path = os.path.join(dirpath, name)
                    with open(path, "rb") as fh:
                        data = fh.read().decode("utf-8", "replace").lower()
                    for needle in needles:
                        self.assertFalse(needle in data, "%s names %s" % (path, needle))

    def test_copies_outlive_the_build_directory(self):
        # AC-3: the build directory is there while the process runs and gone when it exits.
        # AC-2: the copies it made need nothing of it and name it nowhere.
        out, private = self.child(TWO_COPIES % (self.path("a"), self.path("b")))
        self.assertTrue(out["built"], out)
        self.assertTrue(out["there"], out)
        self.assertFalse(os.path.exists(out["built"]), out["built"])
        self.assertEqual(os.listdir(private), [])
        a, b = out["a"], out["b"]
        self.assertEqual(a["repo"], self.path("a"))
        self.assertEqual(b["remote"], self.path("b") + ".git")

        needles = spellings(out["built"]) | spellings(out["real"])
        for info in (a, b):
            self.assert_no_file_names(info, needles)
            git(["fsck", "--no-progress"], info["repo"])
            git(["fsck", "--no-progress"], info["remote"])
        self.assert_fixture(snapshot(b), DEFAULT)
        # one copy still commits and pushes to its own remote; the other one's stays as built
        git(["commit", "-q", "--allow-empty", "-m", "after the builder exited"], a["repo"])
        git(["push", "-q", "origin", "feature/mul"], a["repo"])
        self.assertEqual(
            sorted(refs(a["remote"])[0]), ["refs/heads/feature/mul", "refs/heads/main"]
        )
        self.assertEqual(sorted(refs(b["remote"])[0]), ["refs/heads/main"])

    def test_a_failed_build_is_not_kept_and_the_next_call_builds(self):
        # AC-3: git refuses the commit's date, so the build fails after its init and add
        body = FAILED_THEN_BUILT % (self.path("failed"), self.path("again"), self.path("later"))
        out, private = self.child(body)
        self.assertEqual(out["raised"], "CalledProcessError")  # as a failing build always did
        self.assertEqual(out["left"], [])
        # the next call built the whole fixture, and the one after it copied that
        self.assert_fixture(snapshot(out["again"]), DEFAULT)
        self.assertEqual(tips(out["later"]["repo"]), tips(out["again"]["repo"]))
        self.assertEqual(sorted(files(out["later"]["repo"])), sorted(files(out["again"]["repo"])))
        git(["fsck", "--no-progress"], out["later"]["repo"])
        self.assertTrue(out["built"], out)
        self.assertFalse(os.path.exists(out["built"]), out["built"])
        self.assertEqual(os.listdir(private), [])


if __name__ == "__main__":
    unittest.main()
