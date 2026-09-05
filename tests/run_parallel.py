"""Run the test suite across worker processes, sharded by test class.

    python tests/run_parallel.py [-j N] [names ...]

Collects the same tests `python -m unittest discover -s tests -t .` collects (or the
`names`, resolved like `python -m unittest <names>`), distributes whole test classes over
N worker processes (default: the CPU count) and prints one unittest-style summary:

    Ran 997 tests in 118.3s
    OK (skipped=1)

Workers whose tests all pass contribute one line; a worker with a failure or error has its
full output (tracebacks, summary) reprinted. A worker that ends without a unittest summary
(crash, kill) is reported as an error with its exit code. Exit 0 when everything passed,
1 otherwise. Standard library only.

`--list` prints the collected test ids and exits; `-s DIR -t DIR` change the discovery
start and top-level directories (the defaults are this repository's `tests/` and root).
"""

import argparse
import io
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from typing import Dict, List, Sequence, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

SUMMARY_RE = re.compile(r"^Ran (\d+) tests? in ([\d.]+)s$", re.M)
VERDICT_RE = re.compile(r"^(OK|FAILED)(?: \((.*)\))?\s*$", re.M)
COUNT_KEYS = ("failures", "errors", "skipped", "expected failures", "unexpected successes")


def _flatten(suite) -> List[unittest.TestCase]:
    out = []
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            out.extend(_flatten(item))
        else:
            out.append(item)
    return out


def collect(start: str, top: str, names: Sequence[str]) -> List[unittest.TestCase]:
    """The leaf tests, as `unittest discover -s start -t top` or `unittest <names>` sees them.
    Modules that fail to import arrive as unittest's `_FailedTest` instances."""
    if top not in sys.path:
        sys.path.insert(0, top)
    loader = unittest.TestLoader()
    if names:
        suite = loader.loadTestsFromNames(list(names))
    else:
        suite = loader.discover(start_dir=start, top_level_dir=top)
    return _flatten(suite)


def is_failed_import(test: unittest.TestCase) -> bool:
    return type(test).__name__ == "_FailedTest"


def class_of(test_id: str) -> str:
    return test_id.rsplit(".", 1)[0]


def shard(ids: Sequence[str], workers: int) -> List[List[str]]:
    """Whole classes per worker, largest classes first onto the least loaded worker."""
    groups: Dict[str, List[str]] = {}
    for tid in ids:
        groups.setdefault(class_of(tid), []).append(tid)
    workers = max(1, min(workers, len(groups)))
    buckets: List[List[str]] = [[] for _ in range(workers)]
    for members in sorted(groups.values(), key=len, reverse=True):
        min(buckets, key=len).extend(members)
    return [b for b in buckets if b]


def run_worker_mode(ids_file: str, top: str) -> int:
    """`--worker`: run the ids listed in the file the way `python -m unittest <ids>` would."""
    if top not in sys.path:
        sys.path.insert(0, top)
    with open(ids_file, "r", encoding="utf-8") as fh:
        ids = [line.strip() for line in fh if line.strip()]
    suite = unittest.TestLoader().loadTestsFromNames(ids)
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    return 0 if result.wasSuccessful() else 1


def parse_summary(text: str):
    """(tests run, counts by key) from a worker's output, or None when it has no summary."""
    summary = SUMMARY_RE.findall(text)
    verdict = VERDICT_RE.findall(text)
    if not summary or not verdict:
        return None
    ran = int(summary[-1][0])
    counts = {key: 0 for key in COUNT_KEYS}
    detail = verdict[-1][1]
    for part in [p.strip() for p in detail.split(",") if p.strip()]:
        key, _, value = part.rpartition("=")
        if key in counts and value.isdigit():
            counts[key] = int(value)
    return ran, counts


def format_verdict(counts: Dict[str, int]) -> str:
    failed = counts["failures"] or counts["errors"] or counts["unexpected successes"]
    infos = [
        "%s=%d" % (key, counts[key])
        for key in COUNT_KEYS
        if counts[key] and (failed or key not in ("failures", "errors", "unexpected successes"))
    ]
    head = "FAILED" if failed else "OK"
    return head + (" (%s)" % ", ".join(infos) if infos else "")


def run_failed_imports(tests: List[unittest.TestCase], out) -> Dict[str, int]:
    """Modules that did not import: run their `_FailedTest`s here so the traceback is printed."""
    stream = io.StringIO()
    result = unittest.TextTestRunner(stream=stream, verbosity=0).run(unittest.TestSuite(tests))
    out.write(stream.getvalue())
    return {"errors": len(result.errors), "failures": len(result.failures)}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[1])
    parser.add_argument("-j", "--jobs", type=int, default=os.cpu_count() or 1)
    parser.add_argument("-s", "--start-dir", default=HERE)
    parser.add_argument("-t", "--top-level-dir", default=ROOT)
    parser.add_argument("--list", action="store_true", help="print the test ids and exit")
    parser.add_argument("--worker", metavar="IDS_FILE", help=argparse.SUPPRESS)
    parser.add_argument("names", nargs="*", help="test names, as for `python -m unittest`")
    args = parser.parse_args(argv)
    top = os.path.abspath(args.top_level_dir)
    if args.worker:
        return run_worker_mode(args.worker, top)

    started = time.monotonic()
    tests = collect(os.path.abspath(args.start_dir), top, args.names)
    if args.list:
        for test in tests:
            print(test.id())
        return 0
    broken = [t for t in tests if is_failed_import(t)]
    ids = [t.id() for t in tests if not is_failed_import(t)]
    out = sys.stdout
    counts = {key: 0 for key in COUNT_KEYS}
    total_ran = 0
    if broken:
        broken_counts = run_failed_imports(broken, out)
        counts["errors"] += broken_counts["errors"]
        counts["failures"] += broken_counts["failures"]
        total_ran += len(broken)

    buckets = shard(ids, args.jobs)
    procs: List[Tuple[int, subprocess.Popen, str, int]] = []
    tmp = tempfile.mkdtemp(prefix="run_parallel-")
    try:
        for index, bucket in enumerate(buckets):
            ids_path = os.path.join(tmp, "worker-%d.ids" % index)
            with open(ids_path, "w", encoding="utf-8") as fh:
                fh.write("\n".join(bucket) + "\n")
            log_path = os.path.join(tmp, "worker-%d.log" % index)
            log = open(log_path, "wb")
            proc = subprocess.Popen(
                [sys.executable, os.path.abspath(__file__), "--worker", ids_path, "-t", top],
                cwd=top,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
            log.close()
            procs.append((index, proc, log_path, len(bucket)))
        out.write("%d tests in %d worker(s)\n" % (len(ids), len(procs)))
        out.flush()
        for index, proc, log_path, size in procs:
            code = proc.wait()
            with open(log_path, "r", encoding="utf-8", errors="replace") as fh:
                text = fh.read()
            parsed = parse_summary(text)
            if parsed is None:
                counts["errors"] += 1
                out.write(
                    "worker %d: exit %d without a unittest summary (%d tests not accounted "
                    "for); its output:\n%s\n" % (index, code, size, text.rstrip())
                )
                continue
            ran, wc = parsed
            total_ran += ran
            for key in COUNT_KEYS:
                counts[key] += wc[key]
            if code == 0 and not (wc["failures"] or wc["errors"] or wc["unexpected successes"]):
                out.write(
                    "worker %d: %s\n" % (index, SUMMARY_RE.findall(text)[-1][0] + " tests OK")
                )
            else:
                out.write("worker %d (exit %d):\n%s\n" % (index, code, text.rstrip()))
            out.flush()
    finally:
        for name in os.listdir(tmp):
            try:
                os.remove(os.path.join(tmp, name))
            except OSError:
                pass
        try:
            os.rmdir(tmp)
        except OSError:
            pass

    out.write("\nRan %d tests in %.1fs\n\n" % (total_ran, time.monotonic() - started))
    verdict = format_verdict(counts)
    out.write(verdict + "\n")
    return 0 if verdict.startswith("OK") else 1


if __name__ == "__main__":
    sys.exit(main())
