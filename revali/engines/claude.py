"""Engine for Claude Code: `claude -p` with a JSON schema and a tool allowlist."""
import json
from typing import List, Optional

from revali import EXIT_ERROR
from revali.engines.base import Engine, EngineRequest, EngineResult
from revali.preflight import Stop
from revali.procs import ExeNotFound, ProcTimeout, resolve, run
from revali.state import RunLog, write_text

READ_ONLY_TOOLS = "Read,Grep,Glob"
BUDGET_SUBTYPE = "error_max_budget_usd"


def failure_message(role: str, payload: dict, returncode: int, budget_usd: float, raw_path: str) -> str:
    """One line saying why a claude session did not deliver, from its result object."""
    errors = [str(e) for e in (payload.get("errors") or []) if str(e).strip()]
    if payload.get("subtype") == BUDGET_SUBTYPE or payload.get("terminal_reason") == "budget_exhausted":
        return ("%s ran out of budget ($%.2f) after %s turns, spent $%.2f; raise budget_usd for this "
                "project or split the change; raw output saved to %s"
                % (role, budget_usd, payload.get("num_turns", "?"),
                   float(payload.get("total_cost_usd") or 0.0), raw_path))
    detail = "; ".join(errors) or str(payload.get("result", ""))[:300] or "no error text"
    return "%s session failed (exit %d): %s; raw output saved to %s" % (role, returncode, detail, raw_path)


def permission_args(request: EngineRequest) -> List[str]:
    """Abstract permissions -> claude flags."""
    args: List[str] = []
    if request.read_only:
        args += ["--tools", READ_ONLY_TOOLS]
        return args
    if request.may_write:
        args += ["--permission-mode", "acceptEdits"]
    if request.shell_allow:
        args += ["--allowedTools", " ".join("Bash(%s *)" % prefix for prefix in request.shell_allow)]
    return args


class ClaudeEngine(Engine):
    name = "claude"
    supports_schema = True

    def run(self, request: EngineRequest, log: Optional[RunLog] = None) -> EngineResult:
        role = request.role
        try:
            exe = resolve("claude")
        except ExeNotFound as exc:
            raise Stop(EXIT_ERROR, str(exc))
        cmd = exe + ["-p", "--model", request.model, "--effort", request.effort,
                     "--json-schema", request.schema_text, "--output-format", "json",
                     "--max-budget-usd", str(request.budget_usd)] + permission_args(request)
        if request.fallback_model:
            cmd += ["--fallback-model", request.fallback_model]
        try:
            res = run(cmd, cwd=request.cwd, timeout=request.timeout_s, input_text=request.prompt,
                      log=log.detail if log else None)
        except ProcTimeout:
            raise Stop(EXIT_ERROR, "%s session timed out after %d minutes" % (role, request.timeout_s // 60))
        write_text(request.raw_path,
                   res.stdout + ("\n--- stderr ---\n" + res.stderr if res.stderr.strip() else ""))
        try:
            payload = json.loads(res.stdout)
        except ValueError:
            raise Stop(EXIT_ERROR, "%s returned invalid JSON (exit %d); raw output saved to %s"
                       % (role, res.returncode, request.raw_path))
        if not isinstance(payload, dict):
            raise Stop(EXIT_ERROR, "%s output is not a JSON object; saved to %s" % (role, request.raw_path))
        if payload.get("is_error") or res.returncode != 0:
            raise Stop(EXIT_ERROR, failure_message(role, payload, res.returncode, request.budget_usd,
                                               request.raw_path))
        data = payload.get("structured_output")
        if data is None:
            try:
                data = json.loads(payload.get("result", ""))
            except (TypeError, ValueError):
                data = None
        actual, fallback = self.pick_actual_model(payload.get("modelUsage") or {}, request.model)
        return EngineResult(
            data=data if isinstance(data, dict) else {}, payload=payload, raw=res.stdout,
            model_requested=request.model, model_actual=actual or request.model, fallback=fallback,
            cost=float(payload.get("total_cost_usd") or 0.0),
            denials=list(payload.get("permission_denials") or []),
            duration_ms=int(payload.get("duration_ms") or 0),
        )
