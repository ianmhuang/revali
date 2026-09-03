"""Command line interface. Exit codes: 0 ok / ready, 1 pipeline error,
2 author must act, 3 human must decide, 4 (wait only) still running."""
import argparse
import sys

from revali import NAME, VERSION
from revali import pipeline


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=NAME,
        description="Review, validate and merge a feature branch with headless reviewer sessions.")
    parser.add_argument("--verbose", action="store_true", help="echo every command to the terminal")
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="start the pipeline on the current branch (detached by default)")
    p_run.add_argument("--foreground", action="store_true", help="run in this process instead of detaching")
    p_run.add_argument("--dry-run", action="store_true", help="print what would happen, spawn nothing")
    p_run.add_argument("--base", default="", help="base branch for this run (overrides revali.toml)")
    p_run.set_defaults(func=pipeline.cmd_run)

    p_pre = sub.add_parser("preflight", help="run the preflight checks only")
    p_pre.add_argument("--base", default="")
    p_pre.set_defaults(func=pipeline.cmd_preflight)

    p_wait = sub.add_parser("wait", help="wait for the running pipeline (returns its exit code, or 4 if still running)")
    p_wait.add_argument("--timeout", default="9m", help="e.g. 30s, 9m, 1h (default 9m)")
    p_wait.set_defaults(func=pipeline.cmd_wait)

    p_status = sub.add_parser("status", help="show the state of the current (or given) branch")
    p_status.add_argument("--branch", default="")
    p_status.set_defaults(func=pipeline.cmd_status)

    p_reset = sub.add_parser("reset", help="drop state.json for the current branch, keep the files")
    p_reset.set_defaults(func=pipeline.cmd_reset)

    p_clean = sub.add_parser("clean", help="delete the state directory of a branch")
    p_clean.add_argument("branch")
    p_clean.set_defaults(func=pipeline.cmd_clean)

    stop_help = ("kill the running pipeline for the current branch, or record a run that died without a "
                 "result as stopped")
    p_stop = sub.add_parser("stop", help=stop_help, description=stop_help)  # description: `stop -h`
    p_stop.set_defaults(func=pipeline.cmd_stop)

    p_merge = sub.add_parser("merge", help="merge the PR of the current branch (only after READY TO MERGE)")
    p_merge.set_defaults(func=pipeline.cmd_merge)

    p_stats = sub.add_parser("stats", help="summarise the run history")
    p_stats.set_defaults(func=_cmd_stats)

    p_ver = sub.add_parser("version", help="print the version")
    p_ver.set_defaults(func=pipeline.cmd_version)
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
