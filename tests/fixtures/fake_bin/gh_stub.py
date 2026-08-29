"""Stand-in for the GitHub CLI, driven by a scenario JSON file.

Env:
  REVALI_FAKE_SCENARIO  path to a JSON file (see DEFAULT below)
  REVALI_FAKE_LOG       path; every invocation's argv is appended as one JSON line
Use via REVALI_GH_CMD="<python> <this file>".
"""
import json
import os
import sys

DEFAULT = {
    "auth_exit": 0,
    "login": "me",
    "owner": "me",
    "name": "sample",
    "visibility": "PRIVATE",
    "default_branch": "main",
    "url": "https://github.example/me/sample",
    "prs_open": [],
    "prs_all": [],
    "pr_create": {"number": 7, "url": "https://github.example/me/sample/pull/7"},
    "checks": [],
    "merge_exit": 0,
    "comment_exit": 0,
}


def load_scenario():
    data = dict(DEFAULT)
    path = os.environ.get("REVALI_FAKE_SCENARIO")
    if path and os.path.isfile(path):
        with open(path, "r", encoding="utf-8") as fh:
            data.update(json.load(fh))
    return data


def log(argv):
    path = os.environ.get("REVALI_FAKE_LOG")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"exe": "gh", "argv": argv}) + "\n")


def main(argv):
    log(argv)
    sc = load_scenario()
    if not argv:
        return 1
    if argv[:2] == ["auth", "status"]:
        return int(sc["auth_exit"])
    if argv[:2] == ["api", "user"]:
        print(sc["login"])
        return 0
    if argv[:2] == ["repo", "view"]:
        print(json.dumps({
            "owner": {"login": sc["owner"]},
            "name": sc["name"],
            "visibility": sc["visibility"],
            "defaultBranchRef": {"name": sc["default_branch"]},
            "url": sc["url"],
        }))
        return 0
    if argv[:2] == ["pr", "list"]:
        state = argv[argv.index("--state") + 1] if "--state" in argv else "open"
        print(json.dumps(sc["prs_open"] if state == "open" else sc["prs_all"]))
        return 0
    if argv[:2] == ["pr", "create"]:
        print(sc["pr_create"]["url"])
        return 0
    if argv[:2] == ["pr", "view"]:
        print(json.dumps(sc["pr_create"]))
        return 0
    if argv[:2] == ["pr", "comment"]:
        return int(sc["comment_exit"])
    if argv[:2] == ["pr", "checks"]:
        print(json.dumps(sc["checks"]))
        return 0
    if argv[:2] == ["pr", "ready"]:
        return 0
    if argv[:2] == ["pr", "merge"]:
        return int(sc["merge_exit"])
    print("gh_stub: unhandled: %s" % " ".join(argv), file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
