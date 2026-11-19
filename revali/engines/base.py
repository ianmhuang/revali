"""The seam between revali and the CLI that runs a Reviewer or diagnoser session.

An engine turns an EngineRequest (prompt, schema, model, budget, abstract
permissions) into one headless session and returns an EngineResult. Nothing
outside revali/engines/ knows a CLI flag.
"""

import threading
from dataclasses import dataclass, field
from typing import List, Optional

from revali import EXIT_ERROR, models
from revali.config import EngineCfg
from revali.preflight import Stop
from revali.state import RunLog


class SessionCancelled(Stop):
    """A session stopped through EngineRequest.cancel. It reported nothing, so its cost is
    not known."""

    def __init__(self, role: str):
        super().__init__(EXIT_ERROR, "%s session stopped before it answered" % role)


class ModelUnavailable(Stop):
    """A session stopped because its model could not serve it. `text` is the engine's own
    words. Its own message gives no advice: what the user can do next differs for the
    Reviewer and the diagnosis, so each caller passes it to `line` (PR #99 round 1)."""

    what = "could not run"  # in the line: "<role> model <m> <what>: <text>"
    past = "could not run"  # "... the model <past> on an earlier <item>"
    when = "later"  # when the same model may serve again

    def __init__(self, role: str, model: str, text: str, raw_path: str, cost: float):
        self.role, self.model, self.text, self.raw_path = role, model, text, raw_path
        super().__init__(EXIT_ERROR, self.line(""), cost=cost)

    def with_advice(self, way_on: str) -> "ModelUnavailable":
        """The same stop, its message carrying the caller's advice; still of its class, so a
        caller further up can tell it from other failures."""
        out = type(self)(self.role, self.model, self.text, self.raw_path, self.cost)
        out.message = out.line(way_on)
        out.args = (out.message,)
        return out

    def line(self, way_on: str) -> str:
        """The one line: what happened, `way_on` (the caller's advice, may be empty), where
        the raw output is."""
        return "%s model %s %s: %s; %sraw output saved to %s" % (
            self.role,
            self.model,
            self.what,
            self.text,
            way_on + "; " if way_on else "",
            self.raw_path,
        )


class UsageLimit(ModelUnavailable):
    """The model's usage limit is reached; `text` carries the reset time."""

    what = past = "hit its usage limit"
    when = "after the reset"


class ModelBusy(ModelUnavailable):
    """The model is overloaded (`api_error_status` 529) and no fallback took its place: the
    Reviewer and the auditor have none unless the user names one (PR #99 round 5), the
    diagnosis may have used its own up. Not verified against a real busy result."""

    what = "is overloaded"
    past = "was overloaded"


def run_advice(stop: ModelUnavailable) -> str:
    """What a `revali run` whose Reviewer model could not serve can do next."""
    return (
        "nothing is lost: run `revali run` again %s, or `revali run --reviewer-model <model>` "
        "for this run only" % stop.when
    )


def loop_advice(items: str, stop: ModelUnavailable) -> str:
    """The advice in a tool that runs one session per item (tools/doc_audit.py,
    tools/review_bench.py) and stops at a model that cannot serve; `--resume` keeps the
    recorded model, so another model is a new run."""
    return (
        "the %s left are not run; continue with `--resume <dir>` %s, or start a new run with "
        "`--model <model>`" % (items, stop.when)
    )


def loop_stopped(item: str, stop: ModelUnavailable) -> str:
    """Why an item after the one whose model could not serve is not run."""
    return "not run: the model %s on an earlier %s" % (stop.past, item)


@dataclass
class EngineRequest:
    role: str  # "reviewer" | "diagnoser", for messages
    prompt: str
    schema_text: str  # JSON schema the answer must match
    model: str
    fallback_model: str  # "" = none; comma-separated list otherwise
    effort: str
    budget_usd: float
    timeout_s: int
    cwd: str
    raw_path: str  # where the raw CLI output is saved
    may_write: List[str] = field(
        default_factory=list
    )  # directories the session is expected to edit;
    # an engine may grant broader write access,
    # revali's guard_worktree reverts anything outside
    read_only: bool = False  # no edits, no shell at all
    shell_allow: List[str] = field(
        default_factory=list
    )  # command prefixes it may run, e.g. "git diff"
    cancel: Optional[threading.Event] = None  # set by another thread to stop the session
    # (the parallel baseline failed); the engine then raises SessionCancelled


@dataclass
class EngineResult:
    data: dict  # the structured answer ({} when none)
    payload: dict  # the CLI's whole result object, engine-specific
    raw: str
    model_requested: str
    model_actual: str  # "" when the engine cannot tell
    fallback: bool  # the work was done by a model outside the requested family
    cost: float  # 0.0 when the engine cannot tell
    denials: list  # tool calls the engine refused, engine-specific rows
    duration_ms: int


class Engine:
    name = ""
    supports_schema = True  # False: the prompt asks the model to write JSON to raw_path itself

    def __init__(self, cfg: EngineCfg):
        self.cfg = cfg
        self.tiers = list(cfg.tiers)
        self.helper_prefix = cfg.helper_prefix

    def run(self, request: EngineRequest, log: Optional[RunLog] = None) -> EngineResult:
        raise NotImplementedError

    def model_family(self, model: str) -> str:
        """The tier name inside a model id ("claude-opus-5" -> "opus"), else the id itself."""
        idx = models.tier_index(model, self.tiers)
        return self.tiers[idx].lower() if idx is not None else (model or "").lower()

    def pick_actual_model(self, model_usage: dict, requested: str):
        """The non-helper model that did the work; fallback when it is not the requested family."""
        candidates = {
            k: v
            for k, v in (model_usage or {}).items()
            if not (self.helper_prefix and k.startswith(self.helper_prefix))
        }
        if not candidates:
            return "", False
        wanted = self.model_family(requested)
        for name in candidates:
            if self.model_family(name) == wanted:
                return name, False
        best = max(candidates, key=lambda k: float((candidates[k] or {}).get("costUSD", 0) or 0))
        return best, True
