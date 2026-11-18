"""feature/doc-audit (M3.5.3.1): tools/doc_audit.py runs one read-only session per document of
the documentation graph and reports where a document disagrees with the code. Offline: the
fixture repository is the audited repository and the fake claude answers."""

import contextlib
import importlib.util
import io
import json
import os
import shutil
import unittest
from unittest import mock

from revali.config import load_project_config, load_user_config
from tests.helpers import ROOT, RepoCase, claude_entry

SPEC = importlib.util.spec_from_file_location(
    "doc_audit", os.path.join(ROOT, "tools", "doc_audit.py")
)
audit = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(audit)

DOCS = ["README.md", "CONVENTIONS.md", "docs/guide.md", "templates/revali.toml"]


def finding(severity="low", line=3, quote="Run `calc`."):
    return {
        "severity": severity,
        "line": line,
        "quote": quote,
        "actual": "there is no calc command",
        "evidence": "src/calc.py:1",
        "suggestion": "name the function instead",
    }


def answer(*findings):
    return {"summary": "checked every statement", "findings": list(findings)}


def ce(data, **kw):
    return claude_entry(data, write_tests=False, **kw)


def entry(*findings, cost=0.5):
    return ce(answer(*findings), cost=cost)


class AuditCase(RepoCase):
    def setUp(self):
        super().setUp()
        self.out = os.path.join(self.tmp, "out")
        self.write(
            "README.md",
            "# sample\n\n"
            "[guide](docs/guide.md), [template](templates/revali.toml), [code](src/calc.py),\n"
            "[schema](schemas/a.schema.json), [settings](settings.toml),\n"
            "[notes](templates/notes.json)\n",
        )
        self.write("docs/guide.md", "# guide\n\nRun `calc`.\n")
        self.write("templates/revali.toml", "# a template\n[review]\n")
        self.write("schemas/a.schema.json", "{}\n")
        self.write("settings.toml", "x = 1\n")
        self.write("templates/notes.json", "{}\n")
        self.cfg = load_project_config(self.repo, load_user_config())

    def run_audit(self, *argv, out=None):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = audit.main(list(argv) + (["--out", out or self.out] if out is not False else []))
        return code, buf.getvalue()

    def results(self, out=None):
        with open(os.path.join(out or self.out, "results.json"), encoding="utf-8") as fh:
            data = json.load(fh)
        return data, {r["doc"]: r for r in data["results"]}

    def out_file(self, name, out=None):
        with open(os.path.join(out or self.out, name), encoding="utf-8") as fh:
            return fh.read()

    def calls(self):
        return self.fake_calls("claude")


class DryRun(AuditCase):
    def test_lists_markdown_and_template_toml_only(self):  # AC-1
        code, out = self.run_audit("--dry-run")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.calls(), [])
        data, by_doc = self.results()
        self.assertTrue(data["dry_run"])
        self.assertEqual(data["documents"], DOCS)
        self.assertEqual(list(by_doc), DOCS)
        for doc in DOCS:
            self.assertIn(doc, out)
            prompt = self.out_file(audit.slug(doc) + ".prompt.txt")
            self.assertIn(doc, prompt)
        for left_out in ("src/calc.py", "schemas/a.schema.json", "settings.toml", "notes.json"):
            self.assertNotIn(left_out, data["documents"])
        # the prompt carries the document with line numbers
        self.assertIn("   3| Run `calc`.", self.out_file("docs__guide.md.prompt.txt"))

    def test_a_linked_prompt_page_is_a_document(self):  # AC-1 (prompts are audited)
        self.write("README.md", self.read("README.md") + "\n[prompt](prompts/review.md)\n")
        self.write("prompts/review.md", "You are the reviewer.\n")
        code, out = self.run_audit("--dry-run")
        self.assertEqual(code, 0, out)
        self.assertIn("prompts/review.md", self.results()[0]["documents"])

    def test_roots_come_from_the_config(self):  # AC-1
        toml = self.read("revali.toml")
        self.assertIn("[review]\n", toml)
        toml = toml.replace("[review]\n", '[review]\ndoc_roots = ["docs/guide.md"]\n', 1)
        self.write("revali.toml", toml)
        code, out = self.run_audit("--dry-run")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.results()[0]["documents"], ["docs/guide.md"])


class Sessions(AuditCase):
    def test_one_read_only_session_per_document(self):  # AC-2, AC-4, AC-5
        self.claude(*[entry() for _ in DOCS])
        code, out = self.run_audit("--budget-usd", "10")
        self.assertEqual(code, 0, out)
        calls = self.calls()
        self.assertEqual(len(calls), len(DOCS))
        tiers = self.cfg.engine_for("review").tiers
        for call, doc in zip(calls, DOCS, strict=True):
            argv = call["argv"]
            self.assertEqual(os.path.normcase(call["cwd"]), os.path.normcase(self.repo))
            self.assertEqual(argv[argv.index("--model") + 1], tiers[-1])  # auto = top tier
            self.assertEqual(argv[argv.index("--effort") + 1], self.cfg.review.effort)
            self.assertEqual(
                float(argv[argv.index("--max-budget-usd") + 1]), self.cfg.review.budget_usd
            )
            self.assertIn("--tools", argv)  # read-only
            self.assertNotIn("--permission-mode", argv)
            self.assertIn(doc, call["prompt"])
        data, by_doc = self.results()
        self.assertEqual([by_doc[d]["status"] for d in DOCS], ["done"] * len(DOCS))
        self.assertEqual(by_doc["docs/guide.md"]["cost"], 0.5)
        self.assertEqual(by_doc["docs/guide.md"]["model"], "claude-fable-5")
        for doc in DOCS:
            self.assertTrue(os.path.isfile(os.path.join(self.out, audit.slug(doc) + ".md")))
        self.assertIn("PASS", self.out_file("summary.md"))

    def test_overrides(self):  # AC-2
        self.claude(entry())
        code, out = self.run_audit(
            "--budget-usd",
            "10",
            "--model",
            "claude-test-x",
            "--doc-usd",
            "0.75",
            "--only",
            "README.md",
        )
        self.assertEqual(code, 0, out)
        argv = self.calls()[0]["argv"]
        self.assertEqual(argv[argv.index("--model") + 1], "claude-test-x")
        self.assertEqual(float(argv[argv.index("--max-budget-usd") + 1]), 0.75)

    def test_budget_is_required(self):  # AC-2
        code, out = self.run_audit()
        self.assertEqual(code, 1, out)
        self.assertIn("--budget-usd", out)
        self.assertEqual(self.calls(), [])

    def test_amounts_must_be_above_zero(self):  # AC-2 (round 1 F5)
        for argv in (
            ["--budget-usd", "0"],
            ["--budget-usd", "-1"],
            ["--budget-usd", "nan"],
            ["--budget-usd", "10", "--doc-usd", "0"],
            ["--budget-usd", "10", "--doc-usd", "-0.5"],
        ):
            code, out = self.run_audit(*argv)
            self.assertEqual(code, 1, argv)
            self.assertIn("above 0", out)
        self.assertEqual(self.calls(), [])

    def test_a_missing_executable_stops_before_any_charge(self):  # AC-2 (round 1 F2)
        os.environ.pop("REVALI_CLAUDE_CMD")
        real = shutil.which
        with mock.patch(
            "revali.procs.shutil.which", side_effect=lambda n: None if n == "claude" else real(n)
        ):
            code, out = self.run_audit("--budget-usd", "10")
        self.assertEqual(code, 1, out)
        self.assertIn("not found", out)
        self.assertFalse(os.path.exists(os.path.join(self.out, "results.json")))

    def test_a_used_output_directory_is_refused(self):  # AC-4 (round 1 follow-up)
        self.claude(entry())
        code, out = self.run_audit("--budget-usd", "10", "--only", "README.md")
        self.assertEqual(code, 0, out)
        code, out = self.run_audit("--budget-usd", "10", "--only", "README.md")
        self.assertEqual(code, 1, out)
        self.assertIn("--resume", out)
        self.assertEqual(len(self.calls()), 1)

    def test_a_missing_executable_makes_no_output_directory(self):  # round 2 follow-up
        os.environ.pop("REVALI_CLAUDE_CMD")
        real = shutil.which
        with (
            mock.patch(
                "revali.procs.shutil.which",
                side_effect=lambda n: None if n == "claude" else real(n),
            ),
            mock.patch.object(audit.tempfile, "mkdtemp") as mkdtemp,
        ):
            code, out = self.run_audit("--budget-usd", "10", out=False)
        self.assertEqual(code, 1, out)
        mkdtemp.assert_not_called()

    def test_a_dry_run_directory_takes_the_run(self):  # AC-4 (round 2 F2)
        self.run_audit("--dry-run")
        self.claude(entry())
        code, out = self.run_audit("--budget-usd", "10", "--only", "README.md")
        self.assertEqual(code, 0, out)
        data, by_doc = self.results()
        self.assertFalse(data["dry_run"])
        self.assertEqual(by_doc["README.md"]["status"], "done")
        # and a dry run does not overwrite a paid run
        code, out = self.run_audit("--dry-run")
        self.assertEqual(code, 1, out)
        self.assertFalse(self.results()[0]["dry_run"])

    def test_a_cap_over_the_budget_is_not_started(self):  # AC-2, AC-5
        cap = self.cfg.review.budget_usd
        self.claude(entry(cost=0.25))
        code, out = self.run_audit("--budget-usd", str(cap + 0.2))
        self.assertEqual(code, 1, out)
        self.assertEqual(len(self.calls()), 1)
        data, by_doc = self.results()
        self.assertEqual(by_doc["README.md"]["status"], "done")
        # $0.25 spent + the next cap would pass the budget: that one and every later one
        self.assertEqual([by_doc[d]["status"] for d in DOCS[1:]], ["not-run"] * 3)
        summary = self.out_file("summary.md")
        for doc in DOCS[1:]:
            self.assertIn(doc, summary.split("## Not audited", 1)[1])


class Findings(AuditCase):
    def test_a_high_finding_fails(self):  # AC-3, AC-4, AC-5
        self.claude(entry(), entry(), entry(finding("high"), finding("low")), entry())
        code, out = self.run_audit("--budget-usd", "10")
        self.assertEqual(code, 2, out)
        data, by_doc = self.results()
        found = by_doc["docs/guide.md"]["findings"]
        self.assertEqual([f["severity"] for f in found], ["high", "low"])
        self.assertEqual(found[0]["evidence"], "src/calc.py:1")
        summary = self.out_file("summary.md")
        self.assertIn("FAIL", summary)
        self.assertIn("docs/guide.md:3", summary)
        self.assertIn("there is no calc command", summary)
        report = self.out_file("docs__guide.md.md")
        for part in ("high", "Run `calc`.", "src/calc.py:1", "name the function instead"):
            self.assertIn(part, report)

    def test_a_medium_finding_fails_and_a_low_one_passes(self):  # AC-5
        self.claude(entry(finding("medium")))
        code, out = self.run_audit("--budget-usd", "10", "--only", "docs/guide.md")
        self.assertEqual(code, 2, out)
        self.claude(entry(finding("low")))
        code, out = self.run_audit(
            "--budget-usd", "10", "--only", "docs/guide.md", out=os.path.join(self.tmp, "o2")
        )
        self.assertEqual(code, 0, out)

    def test_a_finding_beats_an_error(self):  # AC-5
        self.claude(entry(finding("high")), ce({"findings": "none"}))
        code, out = self.run_audit("--budget-usd", "10", "--only", "README.md", "CONVENTIONS.md")
        self.assertEqual(code, 2, out)

    def test_an_answer_off_the_schema_is_an_error(self):  # AC-3, AC-5
        bad = finding()
        del bad["severity"]
        self.claude(ce(answer(bad), cost=0.3), entry())
        code, out = self.run_audit("--budget-usd", "10", "--only", "README.md", "CONVENTIONS.md")
        self.assertEqual(code, 1, out)
        data, by_doc = self.results()
        self.assertEqual(by_doc["README.md"]["status"], "error")
        self.assertIn("schema", by_doc["README.md"]["message"])
        self.assertEqual(by_doc["README.md"]["cost"], 0.3)
        self.assertEqual(by_doc["CONVENTIONS.md"]["status"], "done")  # the run went on

    def test_a_failed_session_is_an_error(self):  # AC-3
        self.claude(ce(answer(), is_error=True, cost=0.2), entry())
        code, out = self.run_audit("--budget-usd", "10", "--only", "README.md", "CONVENTIONS.md")
        self.assertEqual(code, 1, out)
        data, by_doc = self.results()
        self.assertEqual(by_doc["README.md"]["status"], "error")
        self.assertEqual(by_doc["README.md"]["cost"], 0.2)
        self.assertEqual(by_doc["CONVENTIONS.md"]["status"], "done")

    def test_wrong_types_are_off_the_schema(self):  # AC-3
        for bad in (
            dict(finding(), severity="critical"),
            dict(finding(), line=0),
            dict(finding(), line="3"),
            dict(finding(), evidence=""),
        ):
            self.assertTrue(audit.schema_problem(answer(bad)), bad)
        self.assertTrue(audit.schema_problem({"findings": []}))  # no summary
        self.assertEqual(audit.schema_problem(answer(finding())), "")


class Selection(AuditCase):
    def test_only_names_documents(self):  # AC-6
        self.claude(entry())
        code, out = self.run_audit("--budget-usd", "10", "--only", "docs/guide.md")
        self.assertEqual(code, 0, out)
        self.assertEqual(len(self.calls()), 1)
        self.assertEqual(self.results()[0]["selected"], ["docs/guide.md"])

    def test_only_takes_each_document_once_in_list_order(self):  # AC-6 (round 1 F6)
        self.claude(entry(), entry())
        code, out = self.run_audit(
            "--budget-usd", "10", "--only", "docs/guide.md", "README.md", "docs/guide.md"
        )
        self.assertEqual(code, 0, out)
        self.assertEqual(len(self.calls()), 2)
        data, _ = self.results()
        self.assertEqual(data["selected"], ["README.md", "docs/guide.md"])
        self.assertEqual([r["doc"] for r in data["results"]], ["README.md", "docs/guide.md"])

    def test_an_unknown_name_stops_before_any_session(self):  # AC-6
        code, out = self.run_audit("--budget-usd", "10", "--only", "docs/nope.md")
        self.assertEqual(code, 1, out)
        self.assertEqual(self.calls(), [])
        self.assertIn("docs/nope.md", out)
        for doc in DOCS:
            self.assertIn(doc, out)

    def test_resume_reruns_what_is_not_done(self):  # AC-6
        cap = self.cfg.review.budget_usd
        self.claude(entry(cost=0.25), entry(cost=0.25))
        # room for two documents ($0.25 each) but not for a third cap after them
        code, out = self.run_audit("--budget-usd", str(0.49 + cap))
        self.assertEqual(code, 1, out)
        data, by_doc = self.results()
        self.assertEqual(
            [by_doc[d]["status"] for d in DOCS], ["done", "done", "not-run", "not-run"]
        )
        # $0.50 earlier: a budget that leaves room for one more cap runs one more document
        self.claude(entry(cost=0.25))
        before = len(self.calls())
        code, out = self.run_audit("--resume", self.out, "--budget-usd", str(0.5 + cap), out=False)
        self.assertEqual(code, 1, out)
        self.assertEqual(len(self.calls()) - before, 1)
        self.assertIn("docs/guide.md", self.calls()[-1]["prompt"])
        data, by_doc = self.results()
        self.assertEqual([by_doc[d]["status"] for d in DOCS], ["done", "done", "done", "not-run"])
        self.assertAlmostEqual(data["spent_usd"], 0.75)
        # the last one with room for it
        self.claude(entry(cost=0.25))
        code, out = self.run_audit("--resume", self.out, "--budget-usd", "10", out=False)
        self.assertEqual(code, 0, out)
        data, by_doc = self.results()
        self.assertEqual([by_doc[d]["status"] for d in DOCS], ["done"] * 4)
        self.assertEqual(len(self.calls()), 4)

    def test_resume_reruns_an_error(self):  # AC-6
        self.claude(ce(answer(), is_error=True, cost=0.2), entry())
        code, out = self.run_audit("--budget-usd", "10", "--only", "README.md", "CONVENTIONS.md")
        self.assertEqual(code, 1, out)
        self.claude(entry())
        before = len(self.calls())
        code, out = self.run_audit("--resume", self.out, "--budget-usd", "10", out=False)
        self.assertEqual(code, 0, out)
        self.assertEqual(len(self.calls()) - before, 1)
        self.assertIn("README.md", self.calls()[-1]["prompt"])
        data, by_doc = self.results()
        self.assertEqual(data["selected"], ["README.md", "CONVENTIONS.md"])
        self.assertAlmostEqual(data["spent_usd"], 0.2 + 0.5 + 0.5)

    def test_resume_refuses_a_dry_run(self):  # AC-6
        self.run_audit("--dry-run")
        code, out = self.run_audit("--resume", self.out, "--budget-usd", "10", out=False)
        self.assertEqual(code, 1, out)
        self.assertIn("dry run", out)
        self.assertEqual(self.calls(), [])

    def test_resume_refuses_another_document_list(self):  # AC-6
        self.claude(entry())
        self.run_audit("--budget-usd", "10", "--only", "README.md")
        self.write("README.md", self.read("README.md") + "\n[more](docs/more.md)\n")
        self.write("docs/more.md", "# more\n")
        before = len(self.calls())
        code, out = self.run_audit("--resume", self.out, "--budget-usd", "10", out=False)
        self.assertEqual(code, 1, out)
        self.assertIn("docs/more.md", out)
        self.assertEqual(len(self.calls()), before)

    def test_an_interrupted_resume_keeps_the_later_done_documents(self):  # AC-6 (round 2 F1)
        three = ["README.md", "CONVENTIONS.md", "docs/guide.md"]
        self.claude(ce(answer(), is_error=True, cost=0.2), entry(), entry())
        code, out = self.run_audit("--budget-usd", "10", "--only", *three)
        self.assertEqual(code, 1, out)
        with mock.patch.object(audit, "audit_one", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.run_audit("--resume", self.out, "--budget-usd", "10", out=False)
        data, by_doc = self.results()
        self.assertEqual([by_doc[d]["status"] for d in three], ["started", "done", "done"])
        self.claude(entry())
        before = len(self.calls())
        code, out = self.run_audit("--resume", self.out, "--budget-usd", "10", out=False)
        self.assertEqual(code, 0, out)
        self.assertEqual(len(self.calls()) - before, 1)  # README.md only
        data, by_doc = self.results()
        self.assertEqual([r["doc"] for r in data["results"]], three)

    def test_resume_keeps_how_the_model_was_chosen(self):  # round 2 follow-up
        cap = self.cfg.review.budget_usd
        self.claude(entry(cost=0.25))
        self.run_audit("--budget-usd", str(cap + 0.1), "--only", "README.md", "CONVENTIONS.md")
        reason = self.results()[0]["model_reason"]
        self.assertTrue(reason.startswith("auto:"), reason)
        model = self.results()[0]["model"]
        # the recorded model named again keeps the reason too (round 3 F1)
        code, out = self.run_audit(
            "--resume", self.out, "--budget-usd", "10", "--model", model, out=False
        )
        self.assertEqual(code, 0, out)
        self.assertEqual(self.results()[0]["model_reason"], reason)
        code, out = self.run_audit("--resume", self.out, "--budget-usd", "10", out=False)
        self.assertEqual(code, 0, out)
        self.assertEqual(self.results()[0]["model_reason"], reason)

    def test_resume_refuses_another_repository(self):  # AC-6 (round 1 follow-up)
        self.claude(entry())
        self.run_audit("--budget-usd", "10", "--only", "README.md")
        path = os.path.join(self.out, "results.json")
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        data["repo"] = os.path.join(self.tmp, "elsewhere")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        code, out = self.run_audit("--resume", self.out, "--budget-usd", "10", out=False)
        self.assertEqual(code, 1, out)
        self.assertIn("elsewhere", out)
        self.assertEqual(len(self.calls()), 1)

    def test_resume_takes_a_new_cap_and_refuses_another_model(self):  # AC-6 (round 1 F7)
        cap = self.cfg.review.budget_usd
        self.claude(entry(cost=0.25))
        self.run_audit("--budget-usd", str(cap + 0.1), "--only", "README.md", "CONVENTIONS.md")
        code, out = self.run_audit(
            "--resume", self.out, "--budget-usd", "10", "--model", "claude-other", out=False
        )
        self.assertEqual(code, 1, out)
        self.assertEqual(len(self.calls()), 1)
        self.claude(entry())
        code, out = self.run_audit(
            "--resume", self.out, "--budget-usd", "10", "--doc-usd", "0.4", out=False
        )
        self.assertEqual(code, 0, out)
        argv = self.calls()[-1]["argv"]
        self.assertEqual(float(argv[argv.index("--max-budget-usd") + 1]), 0.4)
        self.assertEqual(self.results()[0]["doc_usd"], 0.4)

    def test_resume_needs_a_results_file(self):  # AC-6
        os.makedirs(self.out)
        code, out = self.run_audit("--resume", self.out, "--budget-usd", "10", out=False)
        self.assertEqual(code, 1, out)
        self.assertEqual(self.calls(), [])


class Files(unittest.TestCase):
    def test_prompt_and_schema_live_under_tools(self):  # AC-7
        self.assertEqual(os.path.dirname(audit.PROMPT), os.path.join(ROOT, "tools"))
        self.assertEqual(os.path.dirname(audit.SCHEMA), os.path.join(ROOT, "tools"))
        with open(audit.SCHEMA, encoding="utf-8") as fh:
            schema = json.load(fh)
        item = schema["properties"]["findings"]["items"]
        self.assertEqual(item["properties"]["severity"]["enum"], ["high", "medium", "low"])
        with open(audit.PROMPT, encoding="utf-8") as fh:
            prompt = fh.read()
        for word in ("high:", "medium:", "low:", "public interface", "stale but harmless"):
            self.assertIn(word, prompt)

    def test_the_check_follows_the_schema(self):  # AC-3 (round 1 F3)
        with open(audit.SCHEMA, encoding="utf-8") as fh:
            schema = json.load(fh)
        item = schema["properties"]["findings"]["items"]
        self.assertEqual(audit.SEVERITIES, tuple(item["properties"]["severity"]["enum"]))
        self.assertEqual(audit.FINDING_KEYS, tuple(item["required"]))
        # allowed keys are the schema's properties, not its required list (round 2 F3)
        self.assertEqual(audit.ANSWER_KEYS, tuple(schema["properties"]))
        self.assertEqual(audit.FINDING_ALLOWED, tuple(item["properties"]))
        for key in audit.FINDING_TEXT:  # the session is held to what the check accepts
            self.assertEqual(item["properties"][key]["minLength"], 1, key)
        self.assertIn("unknown", audit.schema_problem(dict(answer(), extra=1)))
        self.assertIn("unknown", audit.schema_problem(answer(dict(finding(), extra=1))))

    def test_lines_are_numbered_as_an_editor_does(self):  # round 1 follow-up
        self.assertEqual(audit.numbered("a\fb\nc\n"), "   1| a\fb\n   2| c")
        self.assertEqual(audit.flat("two\n  lines"), "two lines")

    def test_releasing_page(self):  # AC-8
        with open(os.path.join(ROOT, "README.md"), encoding="utf-8") as fh:
            self.assertIn("(docs/releasing.md)", fh.read())
        with open(os.path.join(ROOT, "docs", "releasing.md"), encoding="utf-8") as fh:
            page = fh.read()
        for name in (
            "tools/doc_audit.py",
            "--budget-usd",
            "--doc-usd",
            "--model",
            "--only",
            "--resume",
            "--dry-run",
            "--out",
            "results.json",
            "summary.md",
            "doc_roots",
        ):
            self.assertIn(name, page)
        for code in ("`0`", "`1`", "`2`"):
            self.assertIn(code, page)


if __name__ == "__main__":
    unittest.main()
