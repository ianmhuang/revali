"""The seam between revali and the CLI that runs a Reviewer or diagnoser session.

An engine turns an EngineRequest (prompt, schema, model, budget, abstract
permissions) into one headless session and returns an EngineResult. Nothing
outside revali/engines/ knows a CLI flag.
"""
from dataclasses import dataclass, field
from typing import List, Optional

from revali.config import EngineCfg
from revali.state import RunLog


@dataclass
class EngineRequest:
    role: str                    # "reviewer" | "diagnoser", for messages
    prompt: str
    schema_text: str             # JSON schema the answer must match
    model: str
    fallback_model: str          # "" = none; comma-separated list otherwise
    effort: str
    budget_usd: float
    timeout_s: int
    cwd: str
    raw_path: str                # where the raw CLI output is saved
    may_write: List[str] = field(default_factory=list)    # directories the session is expected to edit;
                                                          # an engine may grant broader write access,
                                                          # revali's guard_worktree reverts anything outside
    read_only: bool = False                                # no edits, no shell at all
    shell_allow: List[str] = field(default_factory=list)  # command prefixes it may run, e.g. "git diff"


@dataclass
class EngineResult:
    data: dict                   # the structured answer ({} when none)
    payload: dict                # the CLI's whole result object, engine-specific
    raw: str
    model_requested: str
    model_actual: str            # "" when the engine cannot tell
    fallback: bool               # the work was done by a model outside the requested family
    cost: float                  # 0.0 when the engine cannot tell
    denials: list                # tool calls the engine refused, engine-specific rows
    duration_ms: int


class Engine:
    name = ""
    supports_schema = True       # False: the prompt asks the model to write JSON to raw_path itself

    def __init__(self, cfg: EngineCfg):
        self.cfg = cfg
        self.tiers = list(cfg.tiers)
        self.helper_prefix = cfg.helper_prefix

    def run(self, request: EngineRequest, log: Optional[RunLog] = None) -> EngineResult:
        raise NotImplementedError

    def model_family(self, model: str) -> str:
        """The tier name inside a model id ("claude-opus-5" -> "opus"), else the id itself."""
        m = (model or "").lower()
        for tier in self.tiers:
            t = tier.lower()
            if m == t or t in m:
                return t
        return m

    def pick_actual_model(self, model_usage: dict, requested: str):
        """The non-helper model that did the work; fallback when it is not the requested family."""
        candidates = {k: v for k, v in (model_usage or {}).items()
                      if not (self.helper_prefix and k.startswith(self.helper_prefix))}
        if not candidates:
            return "", False
        wanted = self.model_family(requested)
        for name in candidates:
            if self.model_family(name) == wanted:
                return name, False
        best = max(candidates, key=lambda k: float((candidates[k] or {}).get("costUSD", 0) or 0))
        return best, True
