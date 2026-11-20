"""Run the test suite across worker processes, sharded by test class.

    python tests/run_parallel.py [-j N] [names ...]

Collects the same tests `python -m unittest discover -s tests -t .` collects (or the
`names`, resolved like `python -m unittest <names>`), distributes whole test classes over
N worker processes and prints one unittest-style summary:

    Ran 1008 tests in 149.1s
    OK (skipped=1)

Each worker runs its tests with `unittest.TextTestRunner` and writes its counts to a JSON
file; its printed output is kept only for reprinting. Workers whose tests all pass
contribute one line; a worker with a failure or error has its full output (tracebacks,
summary) reprinted. A worker that ends without writing its result (crash, kill) is
reported as one error carrying its exit code, and its tests still count in `Ran N`.
Exit 0 when everything passed, 1 otherwise. Standard library only.

N is `-j`, else the environment variable RUN_PARALLEL_JOBS (a machine whose disk, not
its CPUs, bounds the suite sets it once instead of editing the committed `test` line),
else the CPU count. `--list` prints the collected test ids and exits; `-s DIR -t DIR`
change the discovery start and top-level directories (the defaults are this repository's
`tests/` and root).

`--shard K/N`, else the environment variable RUN_PARALLEL_SHARD, runs only shard K of N:
the collected classes are split into N shards the way they are split over workers, so N
runs with K = 1..N on the same tree run every test exactly once (CI runs every leg this
way: two jobs per Ubuntu Python version, six on Windows, three on macOS). A module that
does not import is reported by shard 1 only. The variable is not passed on to the workers,
so a test that starts this runner itself is not sharded by it.

Classes are placed by weight, heaviest first onto the least loaded worker or shard. The
weight is a class's seconds in `durations.json` in the start directory. A class the file
does not list weighs its test count times the median seconds per test of the listed classes.
Without a usable file, every class weighs its test count. `--durations-out FILE` writes the
seconds of each class this run ran, and only those: a shard or a run given names writes a
smaller file, and so does a run where a module does not import or a setUpClass fails. When
a worker leaves no result the file is not written, so a lost worker does not drop its
classes from a refresh. The same file also spreads classes over the workers of a
Linux run, whose relative class times were not measured. To refresh the committed file
after the suite has changed, run the whole suite, unsharded and with every test passing, on
the Windows host (its seconds balance the shard jobs of every CI leg, the Ubuntu and macOS
ones included):

    python tests/run_parallel.py --durations-out tests/durations.json
"""

import argparse
import io
import json
import math
import os
import statistics
import subprocess
import sys
import tempfile
import time
import unittest
from typing import Dict, List, Optional, Sequence, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

JOBS_ENV = "RUN_PARALLEL_JOBS"
SHARD_ENV = "RUN_PARALLEL_SHARD"
DURATIONS_FILE = "durations.json"
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


def load_durations(path: str) -> Dict[str, float]:
    """`{class id: seconds}` from a durations file; {} when the file is missing or is not a
    JSON object, and entries whose value is not a finite number of 0 or more are dropped. A
    bad file only costs the balance, so it is never an error."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):  # UnicodeDecodeError is a ValueError
        return {}
    if not isinstance(data, dict):
        return {}
    out = {}
    for name, value in data.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        try:
            seconds = float(value)
        except OverflowError:  # an int too large for a float
            continue
        if math.isfinite(seconds) and seconds >= 0:
            out[str(name)] = seconds
    return out


def class_weights(ids: Sequence[str], durations: Dict[str, float]) -> Optional[Dict[str, float]]:
    """The weight of each collected class: its recorded seconds, or for a class the file does
    not list, its test count times the median seconds per test of the listed ones. None when
    the file lists none of the collected classes (the split then goes by test count)."""
    counts: Dict[str, int] = {}
    for tid in ids:
        name = class_of(tid)
        counts[name] = counts.get(name, 0) + 1
    known = {name: durations[name] for name in counts if name in durations}
    if not known:
        return None
    per_test = statistics.median(known[name] / counts[name] for name in known)
    return {name: known.get(name, counts[name] * per_test) for name in counts}


def shard(
    ids: Sequence[str], workers: int, weights: Optional[Dict[str, float]] = None
) -> List[List[str]]:
    """Whole classes per worker, heaviest first onto the least loaded worker. A class weighs
    its entry in `weights` (seconds), else its test count. Ties go to the bucket with fewer
    tests, then to the first one."""
    groups: Dict[str, List[str]] = {}
    for tid in ids:
        groups.setdefault(class_of(tid), []).append(tid)
    workers = max(1, min(workers, len(groups)))

    def weight(name: str) -> float:
        return weights[name] if weights is not None else float(len(groups[name]))

    buckets: List[List[str]] = [[] for _ in range(workers)]
    loads = [0.0] * workers
    for name in sorted(groups, key=weight, reverse=True):
        i = min(range(workers), key=lambda j: (loads[j], len(buckets[j])))
        buckets[i].extend(groups[name])
        loads[i] += weight(name)
    return [b for b in buckets if b]


def select_shard(
    ids: Sequence[str], k: int, n: int, weights: Optional[Dict[str, float]] = None
) -> List[str]:
    """Shard k (1-based) of n: the k-th bucket `shard` makes for n workers, or nothing when
    there are fewer classes than shards. Deterministic for the same ids, in the same order,
    and the same weights."""
    buckets = shard(ids, n, weights)
    return buckets[k - 1] if k <= len(buckets) else []


def parse_shard(raw: str, source: str) -> Tuple[int, int]:
    """`K/N` in plain decimal digits with 1 <= K <= N; anything else is refused, naming the
    value and where it came from."""
    text = raw.strip()
    k, sep, n = text.partition("/")
    plain = all(p.isascii() and p.isdigit() for p in (k, n))
    if not sep or not plain or not 1 <= int(k) <= int(n):
        raise SystemExit("%s%r: expected K/N with 1 <= K <= N, e.g. 2/4" % (source, raw))
    return int(k), int(n)


def default_shard(environ=os.environ) -> Optional[Tuple[int, int]]:
    """RUN_PARALLEL_SHARD when set and not blank, else None (the whole suite)."""
    raw = environ.get(SHARD_ENV)
    if raw is None or not raw.strip():
        return None
    return parse_shard(raw, "%s=" % SHARD_ENV)


def counts_of(result: unittest.TestResult) -> Dict[str, int]:
    return {
        "failures": len(result.failures),
        "errors": len(result.errors),
        "skipped": len(result.skipped),
        "expected failures": len(result.expectedFailures),
        "unexpected successes": len(result.unexpectedSuccesses),
    }


class TimedResult(unittest.TextTestResult):
    """A text result that also keeps each class's seconds: from the start of its first test
    to the end of its last one, in this worker."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.spans: Dict[str, List[float]] = {}

    def startTest(self, test):
        self.spans.setdefault(class_of(test.id()), [time.monotonic(), 0.0])
        super().startTest(test)

    def stopTest(self, test):
        super().stopTest(test)
        span = self.spans.get(class_of(test.id()))
        if span is not None:
            span[1] = time.monotonic()

    def class_seconds(self) -> Dict[str, float]:
        return {name: round(max(0.0, end - start), 2) for name, (start, end) in self.spans.items()}


def run_worker_mode(ids_file: str, top: str) -> int:
    """`--worker`: run the ids listed in the file the way `python -m unittest <ids>` would,
    then write the counts and each class's seconds to `<ids_file>.json` for the parent."""
    if top not in sys.path:
        sys.path.insert(0, top)
    with open(ids_file, "r", encoding="utf-8") as fh:
        ids = [line.strip() for line in fh if line.strip()]
    suite = unittest.TestLoader().loadTestsFromNames(ids)
    result = unittest.TextTestRunner(verbosity=1, resultclass=TimedResult).run(suite)
    record = counts_of(result)
    record["ran"] = result.testsRun
    record["successful"] = result.wasSuccessful()
    record["classes"] = result.class_seconds()
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


def default_jobs(environ=os.environ) -> int:
    """RUN_PARALLEL_JOBS when set, else the CPU count. Only plain decimal digits are read
    (`int()` would also take `1_6` or `+8`); anything else, or 0, is refused: silently falling
    back would hide a typo behind a full run."""
    raw = environ.get(JOBS_ENV)
    if raw is None or not raw.strip():
        return os.cpu_count() or 1
    digits = raw.strip()
    if not (digits.isascii() and digits.isdigit()) or int(digits) < 1:
        raise SystemExit("%s=%r: expected a whole number of 1 or more" % (JOBS_ENV, raw))
    return int(digits)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[1])
    parser.add_argument(
        "-j",
        "--jobs",
        type=int,
        default=None,
        help="worker processes (default: %s, else the CPU count)" % JOBS_ENV,
    )
    parser.add_argument("-s", "--start-dir", default=HERE)
    parser.add_argument("-t", "--top-level-dir", default=ROOT)
    parser.add_argument("--list", action="store_true", help="print the test ids and exit")
    parser.add_argument(
        "--shard",
        metavar="K/N",
        help="run only shard K of N (default: %s, else the whole suite)" % SHARD_ENV,
    )
    parser.add_argument(
        "--durations-out",
        metavar="FILE",
        help="write the seconds of each test class this run ran to FILE, as JSON",
    )
    parser.add_argument("--worker", metavar="IDS_FILE", help=argparse.SUPPRESS)
    parser.add_argument("names", nargs="*", help="test names, as for `python -m unittest`")
    args = parser.parse_args(argv)
    top = os.path.abspath(args.top_level_dir)
    if args.worker:
        return run_worker_mode(args.worker, top)
    if args.jobs is None:
        args.jobs = default_jobs()
    part = parse_shard(args.shard, "--shard ") if args.shard is not None else default_shard()

    started = time.monotonic()
    start_dir = os.path.abspath(args.start_dir)
    tests = collect(start_dir, top, args.names)
    broken = [t for t in tests if is_failed_import(t)]
    ids = [t.id() for t in tests if not is_failed_import(t)]
    collected = len(ids)
    weights = class_weights(ids, load_durations(os.path.join(start_dir, DURATIONS_FILE)))
    if part:
        ids = select_shard(ids, *part, weights)
        if part[0] != 1:
            broken = []
    if args.list:
        for tid in [t.id() for t in broken] + ids:
            print(tid)
        return 0
    out = _utf8_stdout()
    counts = {key: 0 for key in COUNT_KEYS}
    total_ran = 0
    if broken:
        for key, value in run_failed_imports(broken, out).items():
            counts[key] += value
        total_ran += len(broken)

    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"  # the logs are read back as UTF-8
    env["PYTHONUTF8"] = "1"
    env.pop(SHARD_ENV, None)  # this run's shard, not the shard of a runner a test starts
    buckets = shard(ids, args.jobs, weights)
    seconds: Dict[str, float] = {}
    silent = 0  # workers that ended without writing their result
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
        if part:
            out.write(
                "%d of %d tests in %d worker(s) (shard %d of %d)\n"
                % (len(ids), collected, len(procs), part[0], part[1])
            )
        else:
            out.write("%d tests in %d worker(s)\n" % (len(ids), len(procs)))
        out.flush()
        for index, proc, result_path, log_path, size in procs:
            code = proc.wait()
            with open(log_path, "r", encoding="utf-8", errors="replace") as fh:
                text = fh.read()
            record = read_result(result_path)
            if record is None:
                counts["errors"] += 1
                silent += 1
                total_ran += size  # collected, assigned, never reported
                out.write(
                    "worker %d: exit %d without a result (%d tests not accounted for, counted "
                    "as one error); its output:\n%s\n" % (index, code, size, text.rstrip())
                )
                out.flush()
                continue
            total_ran += int(record["ran"])
            if isinstance(record.get("classes"), dict):
                seconds.update(record["classes"])
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

    tail = " (shard %d of %d)" % part if part else ""
    out.write("\nRan %d tests in %.1fs%s\n\n" % (total_ran, time.monotonic() - started, tail))
    verdict = format_verdict(counts)
    out.write(verdict + "\n")
    result = 0 if verdict.startswith("OK") else 1
    if args.durations_out:
        # after the summary, so a file that cannot be written does not cost the verdict
        if silent:
            out.write(
                "durations not written to %s: %d worker(s) left no result, so their classes "
                "would be missing\n" % (args.durations_out, silent)
            )
            return 1
        try:
            with open(args.durations_out, "w", encoding="utf-8", newline="\n") as fh:
                json.dump(dict(sorted(seconds.items())), fh, indent=0)
                fh.write("\n")
        except OSError as exc:
            out.write("ERROR: cannot write durations to %s (%s)\n" % (args.durations_out, exc))
            return 1
    return result


if __name__ == "__main__":
    sys.exit(main())
