"""Acceptance tests for feature/ci-windows-shards, the CI side (AC-3, AC-4).

The workflow is read from its text with the standard library (no YAML library). What a leg
gets is not read off the text: the job name and RUN_PARALLEL_SHARD are rendered per matrix
leg by a small evaluator of GitHub's `${{ }}` expressions (literals, `matrix.<key>`, `!`,
`==`, `!=`, `&&`, `||`, parentheses, `format()`), and the rendered values are handed to
`tests/run_parallel.py` on a throwaway tree whose tests each leave a file named by their id.

AC-4 is checked on the repository's own workflow tests: they pass on the workflow as it is
and stop passing on a copy whose Windows shards no longer cover 1..N of one N once, the six
legs unchanged.
"""

import builtins
import io
import os
import re
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from tests.helpers import ROOT, rmtree_force

WORKFLOW = os.path.join(ROOT, ".github", "workflows", "test.yml")
RUNNER = os.path.join(ROOT, "tests", "run_parallel.py")
VAR = "RUN_PARALLEL_SHARD"
RAN_DIR = "CISHARDS_RAN"
SUITE = "python tests/run_parallel.py"  # README's Development line, AC-3
PINNING = (  # the modules that pin the workflow's legs
    "tests.test_ci_workflow",
    "tests.test_review_ciworkflow",
    "tests.test_review_windowsleg",
)

HEAD = """import os
import unittest


def ran(test):
    with open(os.path.join(os.environ["%s"], test.id()), "w"):
        pass
""" % RAN_DIR
CLASSES = (("A", 3), ("B", 2), ("C", 2), ("D", 1), ("E", 1), ("F", 1))
EVERYTHING = sorted(
    "tests.test_parts.%s.test_%d" % (name, i) for name, size in CLASSES for i in range(size)
)

TOKEN = re.compile(
    r"\s*(?:(?P<end>\}\})|(?P<str>'(?:[^']|'')*')|(?P<num>\d+(?:\.\d+)?)"
    r"|(?P<op>&&|\|\||==|!=|[!(),])|(?P<name>[A-Za-z_][\w.-]*))"
)


def read(*parts):
    with open(os.path.join(ROOT, *parts), "r", encoding="utf-8", newline="") as fh:
        return fh.read()


# --- the workflow's text ---------------------------------------------------------------


def indent(line):
    return len(line) - len(line.lstrip())


def code_lines(text):
    return [ln for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]


def pair(line):
    """`key: value` of a line (a leading `- ` dropped), or None."""
    m = re.match(r"(?:-\s+)?([\w-]+):\s*(.*)$", line.strip())
    return (m.group(1), m.group(2).strip()) if m else None


def unquote(raw):
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
        return raw[1:-1]
    return raw


def scalar(raw):
    """A matrix value with the type GitHub's expressions see."""
    raw = re.sub(r"\s+#.*$", "", raw).strip()
    if unquote(raw) != raw:
        return unquote(raw)
    if raw in ("true", "false"):
        return raw == "true"
    if raw in ("", "null", "~"):
        return None
    return int(raw) if re.fullmatch(r"\d+", raw) else raw


def under(lines, key):
    """The lines indented deeper than the first `key:` line."""
    for i, ln in enumerate(lines):
        found = pair(ln)
        if found and found[0] == key and not ln.lstrip().startswith("- "):
            out = []
            for nxt in lines[i + 1 :]:
                if indent(nxt) <= indent(ln):
                    break
                out.append(nxt)
            return out
    raise AssertionError("no %r in the workflow" % key)


def items(lines):
    """A sequence block as a list of dicts of each item's `key: value` lines."""
    out = []
    first = None
    for ln in lines:
        starts = ln.lstrip().startswith("- ")
        if first is None and starts:
            first = indent(ln)
        if starts and indent(ln) == first:
            out.append({})
        found = pair(ln)
        if out and found:
            out[-1][found[0]] = found[1]
    return out


class Parsed:
    def __init__(self, text):
        self.lines = code_lines(text)
        jobs = under(self.lines, "jobs")
        ids = [ln.strip() for ln in jobs if indent(ln) == indent(jobs[0])]
        if len(ids) != 1:
            raise AssertionError("one job is expected under jobs: %r" % ids)
        self.job = jobs[1:]
        base = indent(self.job[0])
        self.scalars = dict(pair(ln) for ln in self.job if indent(ln) == base and pair(ln))
        include = items(under(self.job, "include"))
        self.legs = [{key: scalar(raw) for key, raw in leg.items()} for leg in include]
        self.steps = items(under(self.job, "steps"))

    def on(self, system):
        return [leg for leg in self.legs if str(leg.get("os", "")).startswith(system)]

    def suite_steps(self):
        return [step for step in self.steps if unquote(step.get("run", "")) == SUITE]

    def shard_template(self):
        """The one value RUN_PARALLEL_SHARD is given, on the job or on the suite step."""
        found = [pair(ln)[1] for ln in self.lines if pair(ln) and pair(ln)[0] == VAR]
        if len(found) != 1:
            raise AssertionError("%s is set %d times in the workflow" % (VAR, len(found)))
        holders = [step for step in self.steps if VAR in step]
        if holders and holders != self.suite_steps():
            raise AssertionError("%s is set on a step that is not the suite step" % VAR)
        return unquote(found[0])


# --- GitHub's expressions, the subset a matrix needs ------------------------------------


def truthy(value):
    return not (value is None or value is False or value == 0 or value == "")


def text_of(value):
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value == int(value):
        return str(int(value))
    return str(value)


def number(value):
    if value is None:
        return 0
    if isinstance(value, str):
        try:
            return float(value) if value.strip() else 0
        except ValueError:
            return float("nan")
    return value


def same(left, right):
    if isinstance(left, str) and isinstance(right, str):
        return left.lower() == right.lower()
    return number(left) == number(right)


def fill(args):
    """format('{0} of {1}', a, b)"""

    def put(m):
        return m.group(0)[0] if m.group(1) is None else text_of(args[1 + int(m.group(1))])

    return re.sub(r"\{\{|\}\}|\{(\d+)\}", put, text_of(args[0]))


def tokenize(src, pos):
    """The tokens of an expression that starts at `pos`, and the index after its `}}`."""
    tokens = []
    while True:
        m = TOKEN.match(src, pos)
        if not m:
            raise AssertionError("cannot read the expression at %r" % src[pos : pos + 40])
        pos = m.end()
        if m.lastgroup == "end":
            return tokens, pos
        tokens.append((m.lastgroup, m.group(m.lastgroup)))


class Expression:
    def __init__(self, tokens, matrix):
        self.tokens = tokens + [("end", "")]
        self.at = 0
        self.matrix = matrix

    def peek(self):
        return self.tokens[self.at]

    def take(self):
        token = self.tokens[self.at]
        self.at += 1
        return token

    def value(self):
        out = self.either()
        if self.peek()[0] != "end":
            raise AssertionError("unexpected %r in an expression" % self.peek()[1])
        return out

    def either(self):
        left = self.both()
        while self.peek() == ("op", "||"):
            self.take()
            right = self.both()
            left = left if truthy(left) else right
        return left

    def both(self):
        left = self.compare()
        while self.peek() == ("op", "&&"):
            self.take()
            right = self.compare()
            left = right if truthy(left) else left
        return left

    def compare(self):
        left = self.unary()
        if self.peek() in (("op", "=="), ("op", "!=")):
            op = self.take()[1]
            return same(left, self.unary()) == (op == "==")
        return left

    def unary(self):
        if self.peek() == ("op", "!"):
            self.take()
            return not truthy(self.unary())
        return self.primary()

    def primary(self):
        kind, token = self.take()
        if kind == "str":
            return token[1:-1].replace("''", "'")
        if kind == "num":
            return float(token) if "." in token else int(token)
        if (kind, token) == ("op", "("):
            out = self.either()
            if self.take() != ("op", ")"):
                raise AssertionError("an unclosed parenthesis in an expression")
            return out
        if kind == "name" and self.peek() == ("op", "("):
            self.take()
            args = []
            while self.peek() != ("op", ")"):
                args.append(self.either())
                if self.peek() == ("op", ","):
                    self.take()
            self.take()
            if token != "format" or not args:
                raise AssertionError("%s() is not a call this test can evaluate" % token)
            return fill(args)
        if kind == "name" and token in ("true", "false"):
            return token == "true"
        if kind == "name" and token == "null":
            return None
        if kind == "name" and token.startswith("matrix."):
            return self.matrix.get(token[len("matrix.") :])
        raise AssertionError("cannot evaluate %r in an expression" % token)


def render(template, matrix):
    """The template with every `${{ expression }}` replaced by its value for this leg."""
    out = []
    pos = 0
    while True:
        start = template.find("${{", pos)
        if start < 0:
            return "".join(out) + template[pos:]
        out.append(template[pos:start])
        tokens, pos = tokenize(template, start + 3)
        out.append(text_of(Expression(tokens, matrix).value()))


def evaluate(condition, matrix):
    """An `if:` value, written with or without `${{ }}`."""
    condition = condition.strip()
    source = condition[3:] if condition.startswith("${{") else condition + "}}"
    return Expression(tokenize(source, 0)[0], matrix).value()


# --- a throwaway tree the rendered values are tried on -----------------------------------


class Tree:
    def __init__(self):
        self.root = tempfile.mkdtemp(prefix="cishards ")
        self.tests = os.path.join(self.root, "tests")
        os.makedirs(self.tests)
        source = [HEAD]
        for name, size in CLASSES:
            source.append("\n\nclass %s(unittest.TestCase):\n" % name)
            for i in range(size):
                source.append("    def test_%d(self):\n        ran(self)\n\n" % i)
        for name, body in (("__init__.py", ""), ("test_parts.py", "".join(source))):
            with open(os.path.join(self.tests, name), "w", encoding="utf-8", newline="\n") as fh:
                fh.write(body)

    def run(self, shard_value):
        """The suite step's command with the leg's RUN_PARALLEL_SHARD: exit code, output and
        the ids of the tests that ran."""
        ran_dir = tempfile.mkdtemp(prefix="ran-", dir=self.root)
        env = {k: v for k, v in os.environ.items() if k not in (VAR, "RUN_PARALLEL_JOBS")}
        env[RAN_DIR] = ran_dir
        env[VAR] = shard_value
        res = subprocess.run(
            [sys.executable, RUNNER, "-s", self.tests, "-t", self.root, "-j", "2"],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            cwd=self.root,
            env=env,
        )
        return res.returncode, res.stdout + res.stderr, sorted(os.listdir(ran_dir))


class Wiring(unittest.TestCase):
    """AC-3: the Windows leg as four shard jobs, the rest as it was."""

    def setUp(self):
        self.wf = Parsed(read(".github", "workflows", "test.yml"))
        self.windows = self.wf.on("windows")
        self.ubuntu = self.wf.on("ubuntu")

    def shard_values(self, legs):
        template = self.wf.shard_template()
        return [render(template, leg).strip() for leg in legs]

    def test_the_windows_leg_is_four_jobs_one_shard_each(self):
        self.assertEqual(len(self.windows), 4)
        self.assertEqual(sorted(self.shard_values(self.windows)), ["1/4", "2/4", "3/4", "4/4"])
        self.assertEqual(
            {(leg["os"], leg["python"]) for leg in self.windows}, {("windows-latest", "3.x")}
        )
        # what stays: the two Ubuntu legs on the whole suite, six legs in all
        self.assertEqual(
            sorted((leg["os"], leg["python"]) for leg in self.ubuntu),
            [("ubuntu-latest", "3.11"), ("ubuntu-latest", "3.x")],
        )
        self.assertEqual(len(self.wf.legs), 6)
        self.assertEqual(self.shard_values(self.ubuntu), ["", ""])
        # the suite step is README's command, on every leg; a failing shard fails the run
        suite = self.wf.suite_steps()
        self.assertEqual(len(suite), 1, "one suite step")
        self.assertNotIn("if", suite[0])
        self.assertNotIn("if", self.wf.scalars)
        self.assertRegex(read("README.md"), r"(?m)^%s\s+#" % re.escape(SUITE))
        code = "\n".join(self.wf.lines)
        self.assertNotIn("continue-on-error", code)
        self.assertNotIn("exclude:", code)
        strategy = dict(pair(ln) for ln in under(self.wf.job, "strategy") if pair(ln))
        self.assertEqual(strategy.get("fail-fast"), "false")

    def test_lint_stays_on_the_one_ubuntu_leg_among_six(self):
        self.assertEqual(len(self.wf.legs), 6)
        lint_steps = [
            step for step in self.wf.steps if re.search(r"\b(ruff|black)\b", step.get("run", ""))
        ]
        self.assertGreaterEqual(len(lint_steps), 1)
        for step in lint_steps:
            with self.subTest(step=step.get("name")):
                self.assertIn("if", step)
                linted = [leg for leg in self.wf.legs if truthy(evaluate(step["if"], leg))]
                self.assertEqual(
                    [(leg["os"], leg["python"]) for leg in linted], [("ubuntu-latest", "3.x")]
                )

    def test_the_job_names_show_the_shard(self):
        template = unquote(self.wf.scalars.get("name", ""))
        names = [render(template, leg) for leg in self.windows]
        self.assertEqual(
            sorted(names), ["windows-latest / python 3.x / shard %d of 4" % k for k in (1, 2, 3, 4)]
        )
        others = [render(template, leg) for leg in self.ubuntu]
        self.assertEqual([name for name in others if "shard" in name], [])
        self.assertEqual(len(set(names + others)), 6, "six jobs, six names")

    def test_the_rendered_values_run_every_test_once_on_windows_and_all_on_ubuntu(self):
        windows = sorted(self.shard_values(self.windows))
        ubuntu = self.shard_values(self.ubuntu)
        self.assertEqual((len(windows), len(ubuntu)), (4, 2))
        tree = Tree()
        self.addCleanup(rmtree_force, tree.root)
        seen = []
        for value in windows:
            code, out, ran = tree.run(value)
            with self.subTest(shard=value):
                self.assertEqual(code, 0, out)
                self.assertTrue(ran, "a Windows job with nothing to run")
                self.assertIn("(shard %s of %s)" % tuple(value.split("/")), out)
            seen.extend(ran)
        self.assertEqual(sorted(seen), EVERYTHING)  # the four jobs: all, none twice
        for value in sorted(set(ubuntu)):
            code, out, ran = tree.run(value)
            with self.subTest(ubuntu=value):
                self.assertEqual(code, 0, out)
                self.assertEqual(ran, EVERYTHING)


# --- AC-4 --------------------------------------------------------------------------------


def broken_shards(text):
    """Copies of the workflow whose Windows shards are wrong, the legs otherwise untouched."""
    lines = text.splitlines(keepends=True)
    legs = []
    current = None
    for i, ln in enumerate(lines):
        stripped = ln.strip()
        if stripped.startswith("- ") or stripped.startswith("steps:"):
            current = None
            if re.match(r"-\s+os:\s*[\"']?windows", stripped):
                current = {}
                legs.append(current)
            continue
        m = re.match(r"(shards?):\s*(\d+)$", stripped)
        if current is not None and m:
            current[m.group(1)] = (i, int(m.group(2)))
    if len(legs) < 2 or any(set(leg) != {"shard", "shards"} for leg in legs):
        raise AssertionError("the Windows legs carry no `shard: K` / `shards: N` to break")

    def with_values(changes):
        out = list(lines)
        for index, value in changes:
            out[index] = re.sub(r"\d+(?=\s*$)", str(value), out[index], count=1)
        return "".join(out)

    n = legs[0]["shards"][1]
    return {
        "one shard twice, another missing": with_values(
            [(legs[-1]["shard"][0], legs[0]["shard"][1])]
        ),
        "two different N": with_values([(legs[0]["shards"][0], n + 1)]),
        "shards 1..N of N + 1": with_values([(leg["shards"][0], n + 1) for leg in legs]),
    }


class Coverage(unittest.TestCase):
    """AC-4: a test of the repository fails when the shards do not cover 1..N of one N once."""

    def run_pinning(self, text):
        """The repository's workflow tests, the workflow file reading as `text`."""
        target = os.path.normcase(os.path.abspath(WORKFLOW))
        real = builtins.open
        hits = []

        def fake(file, mode="r", *args, **kwargs):
            if isinstance(file, str) and os.path.normcase(os.path.abspath(file)) == target:
                hits.append(mode)
                return io.BytesIO(text.encode("utf-8")) if "b" in mode else io.StringIO(text)
            return real(file, mode, *args, **kwargs)

        suite = unittest.TestLoader().loadTestsFromNames(list(PINNING))
        result = unittest.TestResult()
        with mock.patch("builtins.open", fake):
            suite.run(result)
        return result, len(hits)

    def test_the_repositorys_tests_fail_on_shards_that_do_not_cover_the_suite(self):
        text = read(".github", "workflows", "test.yml")
        result, hits = self.run_pinning(text)
        self.assertGreater(hits, 0, "the workflow tests read the workflow through open()")
        problems = [str(test) for test, _ in result.failures + result.errors]
        self.assertEqual(problems, [], "they pass on the workflow as it is")
        self.assertGreater(result.testsRun, 0)
        legs = [(leg.get("os"), leg.get("python")) for leg in Parsed(text).legs]
        for what, broken in broken_shards(text).items():
            with self.subTest(what=what):
                self.assertNotEqual(broken, text)
                # the legs are the same six: only a check of the shards can tell
                self.assertEqual(
                    [(leg.get("os"), leg.get("python")) for leg in Parsed(broken).legs], legs
                )
                result, hits = self.run_pinning(broken)
                self.assertGreater(len(result.failures), 0, "no test noticed: %s" % what)


if __name__ == "__main__":
    unittest.main()
