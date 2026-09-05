"""Command line interface. Exit codes: 0 ok / ready, 1 pipeline error,
2 author must act, 3 human must decide, 4 (wait only) still running."""

import argparse
import sys

from revali import NAME, pipeline


def _command(sub, name: str, sentence: str, func):
    """Register a subcommand whose one sentence serves both the `revali --help` listing
    (`help=`) and its own `revali <cmd> -h` (`description=`); argparse feeds only the
    listing from `help=`."""
    p = sub.add_parser(name, help=sentence, description=sentence)
    p.set_defaults(func=func)
    return p


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=NAME,
        description="Review, validate and merge a feature branch with headless reviewer sessions.",
    )
    parser.add_argument("--verbose", action="store_true", help="echo every command to the terminal")
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = _command(
        sub,
        "run",
        "start the pipeline on the current branch (detached by default)",
        pipeline.cmd_run,
    )
    p_run.add_argument(
        "--foreground", action="store_true", help="run in this process instead of detaching"
    )
    p_run.add_argument(
        "--dry-run", action="store_true", help="print what would happen, spawn nothing"
    )
    p_run.add_argument(
        "--base", default="", help="base branch for this run (overrides revali.toml)"
    )

    p_pre = _command(sub, "preflight", "run the preflight checks only", pipeline.cmd_preflight)
    p_pre.add_argument(
        "--base", default="", help="base branch to check against (overrides revali.toml)"
    )

    p_wait = _command(
        sub,
        "wait",
        "wait for the running pipeline (returns its exit code, or 4 if still running)",
        pipeline.cmd_wait,
    )
    p_wait.add_argument("--timeout", default="9m", help="e.g. 30s, 9m, 1h (default 9m)")
    p_wait.add_argument(
        "--branch", default="", help="wait for that branch's run instead of the checked-out one"
    )

    p_status = _command(
        sub, "status", "show the state of the current (or given) branch", pipeline.cmd_status
    )
    p_status.add_argument(
        "--branch", default="", help="branch to show instead of the checked-out one"
    )

    _command(
        sub, "reset", "drop state.json for the current branch, keep the files", pipeline.cmd_reset
    )

    p_clean = _command(sub, "clean", "delete the state directory of a branch", pipeline.cmd_clean)
    p_clean.add_argument(
        "branch", help="the branch whose state directory goes (its git branch may be gone)"
    )

    _command(
        sub,
        "stop",
        "kill the running pipeline in this working tree, whatever branch is checked out, "
        "or record a run that died without a result as stopped",
        pipeline.cmd_stop,
    )
    _command(
        sub,
        "merge",
        "merge the PR of the current branch (only after READY TO MERGE)",
        pipeline.cmd_merge,
    )
    _command(sub, "stats", "summarise the run history", _cmd_stats)
    _command(sub, "version", "print the version", pipeline.cmd_version)
    return parser


def _cmd_stats(args) -> int:
    from revali.stats import cmd_stats

    return cmd_stats(args)


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        print("interrupted")
        return 1


if __name__ == "__main__":
    sys.exit(main())
