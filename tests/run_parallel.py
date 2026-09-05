"""Run the test suite across worker processes, sharded by test class.

    python tests/run_parallel.py [-j N] [names ...]

Collects the same tests `python -m unittest discover -s tests -t .` collects (or the
`names`, resolved like `python -m unittest <names>`), distributes whole test classes over
N worker processes (default: the CPU count) and prints one unittest-style summary:

    Ran 1008 tests in 149.1s
    OK (skipped=1)

Each worker runs its tests with `unittest.TextTestRunner` and writes its counts to a JSON
file; its printed output is kept only for reprinting. Workers whose tests all pass
contribute one line; a worker with a failure or error has its full output (tracebacks,
summary) reprinted. A worker that ends without writing its result (crash, kill) is
reported as one error carrying its exit code, and its tests still count in `Ran N`.
Exit 0 when everything passed, 1 otherwise. Standard library only.

`--list` prints the collected test ids and exits; `-s DIR -t DIR` change the discovery
start and top-level directories (the defaults are this repository's `tests/` and root).
"""

import argparse
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from typing import Dict, List, Optional, Sequence, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

COUNT_KEYS = ("failures", "errors", "skipped", "expected failures", "unexpected successes")
FAILING_KEYS = ("failures", "errors", "unexpected successes")


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


def counts_of(result: unittest.TestResult) -> Dict[str, int]:
    return {
        "failures": len(result.failures),
        "errors": len(result.errors),
        "skipped": len(result.skipped),
        "expected failures": len(result.expectedFailures),
        "unexpected successes": len(result.unexpectedSuccesses),
    }


def run_worker_mode(ids_file: str, top: str) -> int:
    """`--worker`: run the ids listed in the file the way `python -m unittest <ids>` would,
    then write the counts to `<ids_file>.json` for the parent."""
    if top not in sys.path:
        sys.path.insert(0, top)
    with open(ids_file, "r", encoding="utf-8") as fh:
        ids = [line.strip() for line in fh if line.strip()]
    suite = unittest.TestLoader().loadTestsFromNames(ids)
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    record = counts_of(result)
    record["ran"] = result.testsRun
    record["successful"] = result.wasSuccessful()
    with open(ids_file + ".json", "w", encoding="utf-8") as fh:
        json.dump(record, fh)
    return 0 if result.wasSuccessful() else 1


def read_result(path: str) -> Optional[dict]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or "ran" not in data:
        return None
    return data


def format_verdict(counts: Dict[str, int]) -> str:
    failed = any(counts[key] for key in FAILING_KEYS)
    infos = [
        "%s=%d" % (key, counts[key])
        for key in COUNT_KEYS
        if counts[key] and (failed or key not in FAILING_KEYS)
    ]
    head = "FAILED" if failed else "OK"
    return head + (" (%s)" % ", ".join(infos) if infos else "")


def run_failed_imports(tests: List[unittest.TestCase], out) -> Dict[str, int]:
    """Modules that did not import: run their `_FailedTest`s here so the traceback is printed."""
    stream = io.StringIO()
    result = unittest.TextTestRunner(stream=stream, verbosity=0).run(unittest.TestSuite(tests))
    out.write(stream.getvalue())
    return counts_of(result)


def _utf8_stdout():
    """The parent reprints worker output that may hold any text; never let that raise."""
    stream = sys.stdout
    if hasattr(stream, "reconfigure"):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, io.UnsupportedOperation):
            pass
    return stream


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
    out = _utf8_stdout()
    broken = [t for t in tests if is_failed_import(t)]
    ids = [t.id() for t in tests if not is_failed_import(t)]
    counts = {key: 0 for key in COUNT_KEYS}
    total_ran = 0
    if broken:
        for key, value in run_failed_imports(broken, out).items():
            counts[key] += value
        total_ran += len(broken)

    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"  # the logs are read back as UTF-8
    env["PYTHONUTF8"] = "1"
    buckets = shard(ids, args.jobs)
    procs: List[Tuple[int, subprocess.Popen, str, str, int]] = []
    tmp = tempfile.mkdtemp(prefix="run_parallel-")
    try:
        for index, bucket in enumerate(buckets):
            ids_path = os.path.join(tmp, "worker-%d.ids" % index)
            with open(ids_path, "w", encoding="utf-8") as fh:
                fh.write("\n".join(bucket) + "\n")
            log_path = os.path.join(tmp, "worker-%d.log" % index)
            with open(log_path, "wb") as log:
                proc = subprocess.Popen(
                    [
                        sys.executable,
                        "-u",
                        os.path.abspath(__file__),
                        "--worker",
                        ids_path,
                        "-t",
                        top,
                    ],
                    cwd=top,
                    env=env,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    start_new_session=(os.name != "nt"),
                )
            procs.append((index, proc, ids_path + ".json", log_path, len(bucket)))
        out.write("%d tests in %d worker(s)\n" % (len(ids), len(procs)))
        out.flush()
        for index, proc, result_path, log_path, size in procs:
            code = proc.wait()
            with open(log_path, "r", encoding="utf-8", errors="replace") as fh:
                text = fh.read()
            record = read_result(result_path)
            if record is None:
                counts["errors"] += 1
                total_ran += size  # collected, assigned, never reported
                out.write(
                    "worker %d: exit %d without a result (%d tests not accounted for, counted "
                    "as one error); its output:\n%s\n" % (index, code, size, text.rstrip())
                )
                out.flush()
                continue
            total_ran += int(record["ran"])
            for key in COUNT_KEYS:
                counts[key] += int(record.get(key, 0))
            passed = (
                code == 0
                and record.get("successful")
                and not any(record.get(key, 0) for key in FAILING_KEYS)
            )
            if passed:
                out.write("worker %d: %d tests OK\n" % (index, int(record["ran"])))
            else:
                out.write("worker %d (exit %d):\n%s\n" % (index, code, text.rstrip()))
            if int(record["ran"]) != size:
                out.write(
                    "worker %d: ran %d tests but was given %d\n" % (index, int(record["ran"]), size)
                )
            out.flush()
    except BaseException:
        for _, proc, _, _, _ in procs:
            if proc.poll() is None:
                proc.kill()
        raise
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
