"""One place that knows how to call `claude -p` for structured output."""
import json
import os
from dataclasses import dataclass, field
from typing import List, Optional

from revali import EXIT_ERROR
from revali.preflight import Stop
from revali.procs import ExeNotFound, ProcTimeout, resolve, run
from revali.state import RunLog, write_text

HELPER_MODEL_PREFIX = "claude-haiku"


@dataclass
class ClaudeResult:
    data: dict
    payload: dict
    raw: str
    model_requested: str
    model_actual: str
    fallback: bool
    cost: float
    denials: list
    duration_ms: int


def model_family(model: str) -> str:
    m = model.lower()
    for alias in ("fable", "opus", "sonnet", "haiku"):
        if m == alias or m.startswith("claude-" + alias):
            return alias
    return m


def pick_actual_model(model_usage: dict, requested: str):
    """The non-helper model that did the work; fallback when it is not the requested family."""
    candidates = {k: v for k, v in (model_usage or {}).items() if not k.startswith(HELPER_MODEL_PREFIX)}
    if not candidates:
        return "", False
    wanted = model_family(requested)
    for name in candidates:
        if model_family(name) == wanted:
            return name, False
    best = max(candidates, key=lambda k: float((candidates[k] or {}).get("costUSD", 0) or 0))
    return best, True


def invoke(role: str, model: str, fallback_model: str, effort: str, schema_text: str, budget_usd: float,
           extra_args: List[str], prompt: str, cwd: str, timeout_s: int, raw_path: str,
           log: Optional[RunLog] = None) -> ClaudeResult:
    """Run one headless session and return its structured output. Raises Stop on failure."""
    try:
        exe = resolve("claude")
    except ExeNotFound as exc:
        raise Stop(EXIT_ERROR, str(exc))
    cmd = exe + ["-p", "--model", model, "--effort", effort,
                 "--json-schema", schema_text, "--output-format", "json",
                 "--max-budget-usd", str(budget_usd)] + list(extra_args)
    if fallback_model:
        cmd += ["--fallback-model", fallback_model]
    try:
        res = run(cmd, cwd=cwd, timeout=timeout_s, input_text=prompt, log=log.detail if log else None)
    except ProcTimeout:
        raise Stop(EXIT_ERROR, "%s session timed out after %d minutes" % (role, timeout_s // 60))
    write_text(raw_path, res.stdout + ("\n--- stderr ---\n" + res.stderr if res.stderr.strip() else ""))
    try:
        payload = json.loads(res.stdout)
    except ValueError:
        raise Stop(EXIT_ERROR, "%s returned invalid JSON (exit %d); raw output saved to %s"
                   % (role, res.returncode, raw_path))
    if not isinstance(payload, dict):
        raise Stop(EXIT_ERROR, "%s output is not a JSON object; saved to %s" % (role, raw_path))
    if payload.get("is_error") or res.returncode != 0:
        raise Stop(EXIT_ERROR, "%s session failed (exit %d): %s; raw output saved to %s"
                   % (role, res.returncode, str(payload.get("result", ""))[:300], raw_path))
    data = payload.get("structured_output")
    if data is None:
        try:
            data = json.loads(payload.get("result", ""))
        except (TypeError, ValueError):
            data = None
    actual, fallback = pick_actual_model(payload.get("modelUsage") or {}, model)
    return ClaudeResult(
        data=data if isinstance(data, dict) else {}, payload=payload, raw=res.stdout,
        model_requested=model, model_actual=actual or model, fallback=fallback,
        cost=float(payload.get("total_cost_usd") or 0.0),
        denials=list(payload.get("permission_denials") or []),
        duration_ms=int(payload.get("duration_ms") or 0),
    )
