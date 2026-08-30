"""Engine registry. Adding a CLI = one module here plus an [engines.<name>] table in defaults.toml."""
from typing import Dict, List, Type

from revali.config import Config, ConfigError, EngineCfg
from revali.engines.base import Engine, EngineRequest, EngineResult  # noqa: F401  (re-exported)
from revali.engines.claude import ClaudeEngine

ENGINES: Dict[str, Type[Engine]] = {
    ClaudeEngine.name: ClaudeEngine,
}


def available() -> List[str]:
    return sorted(ENGINES)


def get_engine(name: str, cfg: EngineCfg) -> Engine:
    if name not in ENGINES:
        raise ConfigError(["engine '%s' is not implemented (available: %s)" % (name, ", ".join(available()))])
    return ENGINES[name](cfg)


def for_role(cfg: Config, role: str) -> Engine:
    """role: 'review' or 'validate'."""
    name = cfg.review.engine if role == "review" else cfg.validate.engine
    return get_engine(name, cfg.engine_for(role))


def foreign_ladders(cfg: Config, engine_name: str) -> List[List[str]]:
    return cfg.foreign_ladders(engine_name)
