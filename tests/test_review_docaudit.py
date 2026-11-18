"""Acceptance tests for feature/doc-audit (M3.5.3.1): AC-1 to AC-7.

`tools/doc_audit.py` is used as a command: a subprocess for the dry run, `main(argv)` for the
rest, always inside a fixture repository whose `[review]` table names its own `doc_roots`,
`effort` and `budget_usd`, so a value read from somewhere else shows. The fake claude answers.

The documentation graph of the fixture, from the one root `index.md`:

    index.md -> docs/guide.md, templates/revali.toml, src/calc.py, schemas/x.schema.json,
                settings.toml, templates/notes.json
    docs/guide.md -> docs/more.md, docs/extra.toml

so the documents are index.md, docs/guide.md, docs/more.md and templates/revali.toml. Each
carries a marker line; the document a session was given is the one whose marker its prompt
holds. The per-document cap is $1.25 and a fake session costs $0.50.

"Interrupted" is a KeyboardInterrupt raised where the review engine would start the session.
"""

import contextlib
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import unittest
from unittest import mock

from revali import engines as revali_engines
from revali.config import load_project_config, load_user_config
from tests.helpers import ROOT, RepoCase, claude_entry

TOOL = os.path.join(ROOT, "tools", "doc_audit.py")
SCHEMA = os.path.join(ROOT, "tools", "doc_audit.schema.json")
CAP = 1.25
COST = 0.5
MARK = {
    "index.md": "MARK-INDEX-PAGE",
    "docs/guide.md": "MARK-GUIDE-PAGE",
    "docs/more.md": "MARK-MORE-PAGE",
    "templates/revali.toml": "MARK-TEMPLATE-FILE",
}
LEFT_OUT = (
    "src/calc.py",
    "x.schema.json",
    "settings.toml",
    "notes.json",
    "extra.toml",
    "orphan.md",
    "README.md",
)
FIELDS = ("severity", "line", "quote", "actual", "evidence", "suggestion")
SEVERITIES = ("high", "medium", "low")

SPEC = importlib.util.spec_from_file_location("doc_audit_under_review", TOOL)
audit = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(audit)


def canon(found):
    return sorted(json.dumps(f, sort_keys=True) for f in found)


def finding(severity="low", line=3, tag="x"):
    return {
        "severity": severity,
        "line": line,
        "quote": "quote-%s" % tag,
        "actual": "actual-%s" % tag,
        "evidence": "src/calc.py:%d" % line,
        "suggestion": "suggestion-%s" % tag,
    }


def answer(*findings):
    return {"summary": "every statement was checked", "findings": list(findings)}


def entry(*findings, **kw):
    return claude_entry(answer(*findings), write_tests=False, **kw)


def raw(data, **kw):
    return claude_entry(data, write_tests=False, **kw)


class Interrupting:
    """The review engine, except that session number `at` is interrupted before it starts."""

    def __init__(self, inner, at):
        self.inner, self.at, self.count = inner, at, 0

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def run(self, request, log=None):
        self.count += 1
        if self.count == self.at:
            raise KeyboardInterrupt
        return self.inner.run(request, log)


class AuditCase(RepoCase):
    def setUp(self):
        super().setUp()
        self.out = os.path.join(self.tmp, "out")
        toml = self.read("revali.toml")
        self.assertIn("[review]\n", toml)
        self.assertIn("budget_usd = 1.0\n", toml)
        toml = toml.replace("budget_usd = 1.0\n", "budget_usd = %s\n" % CAP, 1)
        toml = toml.replace(
            "[review]\n", '[review]\ndoc_roots = ["index.md"]\neffort = "medium"\n', 1
        )
        self.write("revali.toml", toml)
        self.write(
            "index.md",
            "# index\n\n%s\n\n"
            "[guide](docs/guide.md), [template](templates/revali.toml), [code](src/calc.py),\n"
            "[schema](schemas/x.schema.json), [settings](settings.toml),\n"
            "[notes](templates/notes.json)\n" % MARK["index.md"],
        )
        self.write(
            "docs/guide.md",
            "# guide\n\n%s Run `calc --fast`.\n\n[more](more.md) and [extra](extra.toml)\n"
            % MARK["docs/guide.md"],
        )
        self.write("docs/more.md", "# more\n\n%s\n" % MARK["docs/more.md"])
        self.write("docs/extra.toml", "y = 2\n")
        self.write("docs/orphan.md", "# orphan\n\nNo page links here.\n")
        self.write("templates/revali.toml", "# %s\n[review]\n" % MARK["templates/revali.toml"])
        self.write("templates/notes.json", "{}\n")
        self.write("schemas/x.schema.json", "{}\n")
        self.write("settings.toml", "x = 1\n")
        self.cfg = load_project_config(self.repo, load_user_config())
        self.assertEqual(self.cfg.review.budget_usd, CAP)

    def run_audit(self, *argv):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            code = audit.main(list(argv))
        return code, buf.getvalue()

    def results(self, out=None):
        with open(os.path.join(out or self.out, "results.json"), encoding="utf-8") as fh:
            data = json.load(fh)
        return data, {r["doc"]: r for r in data["results"]}

    def statuses(self, out=None):
        return {doc: rec["status"] for doc, rec in self.results(out)[1].items()}

    def out_text(self, name, out=None):
        with open(os.path.join(out or self.out, name), encoding="utf-8") as fh:
            return fh.read()

    def listed(self):
        """The documents in the order a dry run lists them."""
        out = os.path.join(self.tmp, "listing")
        code, text = self.run_audit("--dry-run", "--out", out)
        self.assertEqual(code, 0, text)
        order = list(self.results(out)[1])
        self.assertEqual(sorted(order), sorted(MARK))
        return order

    def calls(self):
        return self.fake_calls("claude")

    def sessions(self):
        """The document each session so far was given, in the order they ran."""
        found = []
        for call in self.calls():
            docs = [doc for doc, mark in MARK.items() if mark in call["prompt"]]
            self.assertEqual(len(docs), 1, call["prompt"][:400])
            found.append(docs[0])
        return found


class DryRun(AuditCase):
    def test_lists_the_documents_and_writes_their_prompts(self):  # AC-1
        res = subprocess.run(
            [sys.executable, TOOL, "--dry-run", "--out", self.out],
            cwd=self.repo,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        text = res.stdout + res.stderr
        self.assertEqual(res.returncode, 0, text)
        for doc in MARK:
            self.assertIn(doc, text)
        for name in LEFT_OUT:
            self.assertNotIn(name, text)
        self.assertEqual(self.calls(), [])
        self.assertEqual(sorted(self.statuses()), sorted(MARK))
        self.assertNotIn("done", self.statuses().values())
        written = {
            name: self.out_text(name) for name in os.listdir(self.out) if name != "results.json"
        }
        for doc, mark in MARK.items():
            self.assertTrue(any(mark in body for body in written.values()), doc)

    def test_the_roots_are_the_configured_ones(self):  # AC-1
        toml = self.read("revali.toml")
        old, new = '["index.md"]', '["docs/guide.md"]'
        self.assertIn("doc_roots = " + old, toml)
        self.write("revali.toml", toml.replace("doc_roots = " + old, "doc_roots = " + new))
        code, text = self.run_audit("--dry-run", "--out", self.out)
        self.assertEqual(code, 0, text)
        self.assertEqual(sorted(self.statuses()), ["docs/guide.md", "docs/more.md"])
        self.assertEqual(self.calls(), [])

    def test_a_dry_run_needs_no_budget_and_takes_only(self):  # AC-1, AC-6
        code, text = self.run_audit("--dry-run", "--only", "docs/more.md", "--out", self.out)
        self.assertEqual(code, 0, text)
        self.assertEqual(list(self.statuses()), ["docs/more.md"])
        self.assertEqual(self.calls(), [])


class Sessions(AuditCase):
    def test_the_budget_is_required(self):  # AC-2
        code, text = self.run_audit("--out", self.out)
        self.assertNotIn(code, (0, 2), text)
        self.assertIn("--budget-usd", text)
        self.assertEqual(self.calls(), [])

    def test_one_read_only_session_per_document_in_order(self):  # AC-2, AC-3, AC-4, AC-5, AC-7
        order = self.listed()
        self.claude(entry())
        code, text = self.run_audit("--budget-usd", "10", "--out", self.out)
        self.assertEqual(code, 0, text)
        self.assertEqual(self.sessions(), order)
        with open(SCHEMA, encoding="utf-8") as fh:
            shipped = json.load(fh)
        top_tier = self.cfg.engine_for("review").tiers[-1]
        for call in self.calls():
            argv = call["argv"]
            self.assertEqual(
                os.path.normcase(os.path.realpath(call["cwd"])),
                os.path.normcase(os.path.realpath(self.repo)),
            )
            self.assertEqual(argv[argv.index("--model") + 1], top_tier)  # auto, no author
            self.assertEqual(argv[argv.index("--effort") + 1], "medium")
            self.assertEqual(float(argv[argv.index("--max-budget-usd") + 1]), CAP)
            # no edits, no shell
            self.assertNotIn("--permission-mode", argv)
            self.assertNotIn("--allowedTools", argv)
            tools = argv[argv.index("--tools") + 1]
            for tool in ("Bash", "Edit", "Write"):
                self.assertNotIn(tool, tools)
            # the schema the tool ships under tools/ is the one the session answers in
            schema = json.loads(argv[argv.index("--json-schema") + 1])
            self.assertEqual(schema, shipped)
            item = schema["properties"]["findings"]["items"]
            self.assertEqual(sorted(item["properties"]["severity"]["enum"]), sorted(SEVERITIES))
            for key in FIELDS:
                self.assertIn(key, item["required"])
            # the prompt defines the three severities
            prompt = call["prompt"].lower()
            for word in SEVERITIES + ("public interface",):
                self.assertIn(word, prompt)
        data, by_doc = self.results()
        for doc in MARK:
            rec = by_doc[doc]
            self.assertEqual(rec["status"], "done")
            self.assertTrue(rec["model"], rec)
            self.assertEqual(rec["cost"], COST)
            self.assertEqual(rec["findings"], [])
            self.assertTrue(any(key.startswith("duration") for key in rec), rec)
        self.assertTrue(os.path.isfile(os.path.join(self.out, "summary.md")))

    def test_model_and_cap_overrides(self):  # AC-2
        self.claude(entry())
        code, text = self.run_audit(
            "--budget-usd",
            "10",
            "--model",
            "claude-test-x",
            "--doc-usd",
            "0.75",
            "--only",
            "docs/more.md",
            "--out",
            self.out,
        )
        self.assertEqual(code, 0, text)
        (call,) = self.calls()
        argv = call["argv"]
        self.assertEqual(argv[argv.index("--model") + 1], "claude-test-x")
        self.assertEqual(float(argv[argv.index("--max-budget-usd") + 1]), 0.75)

    def test_the_override_cap_decides_what_starts(self):  # AC-2
        # $0.50 a session, cap $0.50: 0.5 + 0.5 = 1.0 is not over $1.00, 1.0 + 0.5 is
        order = self.listed()
        self.claude(entry())
        code, text = self.run_audit("--budget-usd", "1.0", "--doc-usd", "0.5", "--out", self.out)
        self.assertEqual(code, 1, text)
        self.assertEqual(self.sessions(), order[:2])

    def test_a_cap_that_would_pass_the_budget_is_not_started(self):  # AC-2, AC-4, AC-5
        order = self.listed()
        self.claude(entry())
        # 0 + 1.25 and 0.5 + 1.25 = 1.75 are not over $1.75; 1.0 + 1.25 is
        code, text = self.run_audit("--budget-usd", "1.75", "--out", self.out)
        self.assertEqual(code, 1, text)
        self.assertEqual(self.sessions(), order[:2])
        statuses = self.statuses()
        self.assertEqual([statuses[d] for d in order], ["done", "done", "not-run", "not-run"])
        summary = self.out_text("summary.md")
        for doc in order[2:]:
            self.assertIn(doc, summary)

    def test_a_budget_below_one_cap_starts_nothing(self):  # AC-2, AC-5
        code, text = self.run_audit("--budget-usd", "1.0", "--out", self.out)
        self.assertEqual(code, 1, text)
        self.assertEqual(self.calls(), [])
        self.assertEqual(set(self.statuses().values()), {"not-run"})

    def test_an_amount_that_is_not_above_zero_is_refused(self):  # AC-2
        for argv in (
            ("--budget-usd", "0"),
            ("--budget-usd", "-1"),
            ("--budget-usd", "10", "--doc-usd", "0"),
            ("--budget-usd", "10", "--doc-usd", "-0.5"),
        ):
            code, text = self.run_audit(*argv, "--out", self.out)
            self.assertEqual(code, 1, argv)
        self.assertEqual(self.calls(), [])

    def test_a_directory_that_holds_a_run_is_not_written_over(self):  # AC-4
        self.claude(entry(finding("low")))
        argv = ("--budget-usd", "10", "--only", "docs/more.md", "--out", self.out)
        code, text = self.run_audit(*argv)
        self.assertEqual(code, 0, text)
        before = self.out_text("results.json")
        code, text = self.run_audit(*argv)
        self.assertEqual(code, 1, text)
        self.assertEqual(len(self.calls()), 1)  # no second session
        self.assertEqual(self.out_text("results.json"), before)

    def test_a_dry_run_does_not_write_over_a_run(self):  # AC-4
        self.claude(entry(finding("low")))
        code, text = self.run_audit(
            "--budget-usd", "10", "--only", "docs/more.md", "--out", self.out
        )
        self.assertEqual(code, 0, text)
        before = self.out_text("results.json")
        code, text = self.run_audit("--dry-run", "--out", self.out)
        self.assertEqual(code, 1, text)
        self.assertEqual(self.out_text("results.json"), before)
        self.assertEqual(len(self.calls()), 1)

    def test_a_run_may_start_in_a_dry_run_directory(self):  # AC-1, AC-4
        code, text = self.run_audit("--dry-run", "--out", self.out)
        self.assertEqual(code, 0, text)
        self.assertEqual(sorted(self.statuses()), sorted(MARK))
        self.claude(entry())
        code, text = self.run_audit(
            "--budget-usd", "10", "--only", "docs/more.md", "--out", self.out
        )
        self.assertEqual(code, 0, text)
        self.assertEqual(self.sessions(), ["docs/more.md"])
        # the directory now holds the run, not the dry run's listing
        self.assertEqual(self.statuses(), {"docs/more.md": "done"})
        self.assertTrue(os.path.isfile(os.path.join(self.out, "summary.md")))


class Answers(AuditCase):
    def one(self, *entries, out=None):
        """Audit docs/guide.md alone with these answers; (exit code, output directory)."""
        out = out or self.out
        self.claude(*entries)
        code, _text = self.run_audit("--budget-usd", "10", "--only", "docs/guide.md", "--out", out)
        return code, out

    def test_findings_reach_the_results_the_report_and_the_summary(self):  # AC-3, AC-4, AC-5
        found = [
            finding("high", 1, "ha"),
            finding("high", 2, "hb"),
            finding("medium", 3, "ma"),
            finding("low", 4, "la"),
            finding("low", 5, "lb"),
            finding("low", 5, "lc"),
        ]
        code, out = self.one(entry(*found))
        self.assertEqual(code, 2)
        rec = self.results()[1]["docs/guide.md"]
        self.assertEqual(rec["status"], "done")
        self.assertEqual(canon(rec["findings"]), canon(found))
        reports = [n for n in os.listdir(out) if n.endswith(".md") and n != "summary.md"]
        self.assertEqual(len(reports), 1, reports)
        report = self.out_text(reports[0])
        for f in found:
            for field in ("severity", "quote", "actual", "evidence", "suggestion"):
                self.assertIn(f[field], report)
        summary = self.out_text("summary.md")
        for f in found[:3]:  # every high and medium finding, with where the code shows it
            for field in ("quote", "actual", "evidence"):
                self.assertIn(f[field], summary)
        rows = [ln.replace("docs/guide.md", "") for ln in summary.splitlines() if "guide.md" in ln]
        # 2 high, 1 medium, 3 low
        self.assertTrue(any(re.search(r"\b2\b.*\b1\b.*\b3\b", ln) for ln in rows), summary)

    def test_exit_codes_by_severity(self):  # AC-5
        for n, (severity, expected) in enumerate((("high", 2), ("medium", 2), ("low", 0))):
            out = os.path.join(self.tmp, "sev%d" % n)
            code, _out = self.one(entry(finding(severity)), out=out)
            self.assertEqual(code, expected, severity)

    def test_an_answer_off_the_schema_is_an_error(self):  # AC-3, AC-5
        missing = finding()
        del missing["evidence"]
        bad_answers = (
            answer(missing),
            answer(dict(finding(), severity="critical")),
            answer(dict(finding(), line="3")),
            answer(dict(finding(), line=0)),
            {"summary": "checked", "findings": "none"},
            {"findings": []},
        )
        for n, bad in enumerate(bad_answers):
            out = os.path.join(self.tmp, "bad%d" % n)
            code, _out = self.one(raw(bad, cost=0.3), out=out)
            self.assertEqual(code, 1, bad)
            rec = self.results(out)[1]["docs/guide.md"]
            self.assertEqual(rec["status"], "error", bad)
            self.assertEqual(rec["cost"], 0.3, bad)
            self.assertEqual(rec["findings"], [], bad)
            reason = " ".join(v for v in rec.values() if isinstance(v, str)).lower()
            self.assertIn("schema", reason, bad)

    def test_the_run_goes_on_after_an_error(self):  # AC-3, AC-4, AC-5
        order = self.listed()
        self.claude(
            raw({"findings": []}, cost=0.3),  # off the schema
            raw(answer(), is_error=True, cost=0.2),  # a failed session that reports a cost
            {"exit": 1, "raw_stdout": "boom"},  # a failed session that reports nothing
            entry(finding("low")),
        )
        code, text = self.run_audit("--budget-usd", "10", "--out", self.out)
        self.assertEqual(code, 1, text)
        self.assertEqual(self.sessions(), order)
        data, by_doc = self.results()
        self.assertEqual([by_doc[d]["status"] for d in order], ["error", "error", "error", "done"])
        self.assertEqual(by_doc[order[0]]["cost"], 0.3)
        self.assertEqual(by_doc[order[1]]["cost"], 0.2)
        for doc in order[:3]:  # each error carries its reason
            reason = " ".join(v for v in by_doc[doc].values() if isinstance(v, str))
            self.assertNotEqual(reason.replace(doc, "").replace("error", "").strip(), "", doc)
        summary = self.out_text("summary.md")
        for doc in order[:3]:  # the documents that were not audited
            self.assertIn(doc, summary)

    def test_a_finding_beats_an_error_and_a_document_not_run(self):  # AC-5
        self.claude(entry(finding("medium")), raw({"findings": []}))
        # the third and fourth documents are not started for the budget
        code, text = self.run_audit("--budget-usd", "1.75", "--out", self.out)
        self.assertEqual(code, 2, text)
        self.assertEqual(sorted(self.statuses().values()), ["done", "error", "not-run", "not-run"])

    def test_low_findings_with_an_error_are_exit_1(self):  # AC-5
        self.claude(entry(finding("low")), raw(answer(), is_error=True))
        code, text = self.run_audit(
            "--budget-usd", "10", "--only", "index.md", "docs/guide.md", "--out", self.out
        )
        self.assertEqual(code, 1, text)


class Selection(AuditCase):
    def test_only_audits_the_named_documents(self):  # AC-6
        self.claude(entry())
        code, text = self.run_audit(
            "--budget-usd", "10", "--only", "index.md", "docs/more.md", "--out", self.out
        )
        self.assertEqual(code, 0, text)
        self.assertEqual(sorted(self.sessions()), ["docs/more.md", "index.md"])
        self.assertEqual(sorted(self.statuses()), ["docs/more.md", "index.md"])

    def test_a_name_given_twice_is_audited_once_in_list_order(self):  # AC-2, AC-6
        order = self.listed()
        last, first = order[-1], order[0]
        self.claude(entry())
        code, text = self.run_audit(
            "--budget-usd", "10", "--only", last, first, last, "--out", self.out
        )
        self.assertEqual(code, 0, text)
        self.assertEqual(self.sessions(), [first, last])
        self.assertEqual(list(self.statuses()), [first, last])

    def test_an_unknown_name_stops_before_any_session(self):  # AC-6
        for name in ("docs/nope.md", "src/calc.py", "docs/orphan.md"):
            code, text = self.run_audit(
                "--budget-usd", "10", "--only", "docs/guide.md", name, "--out", self.out
            )
            self.assertEqual(code, 1, text)
            self.assertIn(name, text)
            for doc in MARK:  # the list is printed
                self.assertIn(doc, text)
        self.assertEqual(self.calls(), [])


class Resume(AuditCase):
    def resume(self, budget):
        return self.run_audit("--resume", self.out, "--budget-usd", str(budget))

    def test_resume_runs_the_rest_and_counts_the_earlier_cost(self):  # AC-6
        order = self.listed()
        self.claude(entry())
        code, text = self.run_audit("--budget-usd", "1.75", "--out", self.out)
        self.assertEqual(code, 1, text)
        self.assertEqual(self.sessions(), order[:2])
        # $1.00 is spent: 1.0 + 1.25 is not over $2.25, then 1.5 + 1.25 is. Were the earlier
        # cost not counted, both remaining documents would run.
        code, text = self.resume(2.25)
        self.assertEqual(code, 1, text)
        self.assertEqual(self.sessions(), order[:3])
        statuses = self.statuses()
        self.assertEqual([statuses[d] for d in order], ["done", "done", "done", "not-run"])
        code, text = self.resume(10)
        self.assertEqual(code, 0, text)
        self.assertEqual(self.sessions(), order)  # one session each over the three runs
        self.assertEqual(set(self.statuses().values()), {"done"})
        summary = self.out_text("summary.md")
        for doc in order:
            self.assertIn(doc, summary)

    def test_resume_reruns_an_error_and_keeps_what_is_done(self):  # AC-6, AC-5
        kept = finding("high", 1, "kept")
        self.claude(entry(kept), raw(answer(), is_error=True, cost=0.2))
        code, text = self.run_audit(
            "--budget-usd", "10", "--only", "index.md", "docs/guide.md", "--out", self.out
        )
        self.assertEqual(code, 2, text)
        self.assertEqual(self.statuses(), {"index.md": "done", "docs/guide.md": "error"})
        self.claude(entry())
        code, text = self.resume(10)
        # the kept document's finding still counts; only the failed one ran again, and the
        # recorded selection (two documents, not four) is what is continued
        self.assertEqual(code, 2, text)
        self.assertEqual(self.sessions(), ["index.md", "docs/guide.md", "docs/guide.md"])
        data, by_doc = self.results()
        self.assertEqual(self.statuses(), {"index.md": "done", "docs/guide.md": "done"})
        self.assertEqual(by_doc["index.md"]["findings"], [kept])
        self.assertIn("quote-kept", self.out_text("summary.md"))

    def test_resume_runs_an_interrupted_document_again(self):  # AC-6
        order = self.listed()
        real = revali_engines.for_role

        def second_is_interrupted(cfg, role):
            return Interrupting(real(cfg, role), at=2)

        self.claude(entry())
        with mock.patch.object(revali_engines, "for_role", side_effect=second_is_interrupted):
            with contextlib.suppress(KeyboardInterrupt):
                self.run_audit("--budget-usd", "10", "--out", self.out)
        self.assertEqual(self.sessions(), order[:1])
        statuses = self.statuses()
        self.assertEqual(statuses[order[0]], "done")
        self.assertNotEqual(statuses.get(order[1]), "done")
        code, text = self.resume(10)
        self.assertEqual(code, 0, text)
        self.assertEqual(self.sessions(), order)  # the first one was kept, not run again
        self.assertEqual(self.statuses(), dict.fromkeys(order, "done"))

    def test_an_interrupted_resume_keeps_the_done_documents_after_it(self):  # AC-6
        order = self.listed()
        # the first document fails, the three after it are done
        self.claude(raw(answer(), is_error=True, cost=0.2), entry(), entry(), entry())
        code, text = self.run_audit("--budget-usd", "10", "--out", self.out)
        self.assertEqual(code, 1, text)
        self.assertEqual(self.sessions(), order)
        statuses = self.statuses()
        self.assertEqual([statuses[d] for d in order], ["error", "done", "done", "done"])
        real = revali_engines.for_role

        def first_is_interrupted(cfg, role):
            return Interrupting(real(cfg, role), at=1)

        # the resume ends inside the first document, before the three done ones
        with mock.patch.object(revali_engines, "for_role", side_effect=first_is_interrupted):
            with contextlib.suppress(KeyboardInterrupt):
                self.resume(10)
        self.assertEqual(len(self.calls()), 4)
        statuses = self.statuses()
        self.assertNotEqual(statuses.get(order[0]), "done")
        for doc in order[1:]:  # what was done is still on record
            self.assertEqual(statuses.get(doc), "done", doc)
        self.claude(entry())
        code, text = self.resume(10)
        self.assertEqual(code, 0, text)
        # one more session, for the first document only: the done ones were kept
        self.assertEqual(self.sessions(), order + order[:1])
        self.assertEqual(self.statuses(), dict.fromkeys(order, "done"))
        self.assertEqual(list(self.statuses()), order)

    def test_resume_takes_a_new_cap_and_refuses_another_model(self):  # AC-2, AC-6
        self.claude(entry())
        # 0 + 1.25 is not over $1.25, so one document runs; 0.5 + 1.25 is, so the other waits
        code, text = self.run_audit(
            "--budget-usd", "1.25", "--only", "index.md", "docs/guide.md", "--out", self.out
        )
        self.assertEqual(code, 1, text)
        self.assertEqual(sorted(self.statuses().values()), ["done", "not-run"])
        self.assertEqual(len(self.calls()), 1)
        code, text = self.run_audit(
            "--resume", self.out, "--budget-usd", "10", "--model", "claude-some-other-model"
        )
        self.assertNotIn(code, (0, 2), text)
        self.assertEqual(len(self.calls()), 1)  # the recorded model is the run's model
        code, text = self.run_audit("--resume", self.out, "--budget-usd", "10", "--doc-usd", "0.4")
        self.assertEqual(code, 0, text)
        self.assertEqual(len(self.calls()), 2)
        first, second = (call["argv"] for call in self.calls())
        self.assertEqual(float(second[second.index("--max-budget-usd") + 1]), 0.4)
        self.assertEqual(second[second.index("--model") + 1], first[first.index("--model") + 1])
        self.assertEqual(set(self.statuses().values()), {"done"})

    def test_resume_refuses_a_dry_run(self):  # AC-6
        code, text = self.run_audit("--dry-run", "--out", self.out)
        self.assertEqual(code, 0, text)
        code, text = self.resume(10)
        self.assertNotIn(code, (0, 2), text)
        self.assertEqual(self.calls(), [])

    def test_resume_refuses_a_changed_document_list(self):  # AC-6
        order = self.listed()
        self.claude(entry())
        code, text = self.run_audit("--budget-usd", "1.75", "--out", self.out)
        self.assertEqual(code, 1, text)
        before = self.out_text("results.json")
        self.write("index.md", self.read("index.md") + "\n[new](docs/new.md)\n")
        self.write("docs/new.md", "# new\n")
        code, text = self.resume(10)
        self.assertNotIn(code, (0, 2), text)
        self.assertEqual(self.sessions(), order[:2])  # nothing more ran
        self.assertEqual(self.out_text("results.json"), before)

    def test_resume_needs_an_earlier_run(self):  # AC-6
        os.makedirs(self.out)
        code, text = self.resume(10)
        self.assertNotIn(code, (0, 2), text)
        self.assertEqual(self.calls(), [])


class Shipped(unittest.TestCase):
    def test_the_prompt_and_schema_are_under_tools(self):  # AC-7
        names = [n.lower() for n in os.listdir(os.path.join(ROOT, "tools")) if "audit" in n]
        self.assertTrue(any("schema" in n for n in names), names)
        self.assertTrue(any("prompt" in n for n in names), names)
        for folder in ("prompts", "schemas"):
            for name in os.listdir(os.path.join(ROOT, folder)):
                self.assertNotIn("audit", name.lower())
