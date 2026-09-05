"""Engine registry. Adding a CLI = one module here plus an [engines.<name>] table in
defaults.toml."""

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
        raise ConfigError(
            ["engine '%s' is not implemented (available: %s)" % (name, ", ".join(available()))]
        )
    return ENGINES[name](cfg)


def for_role(cfg: Config, role: str) -> Engine:
    """role: 'review' or 'validate'; the name-to-role mapping lives in Config.engine_for."""
    engine_cfg = cfg.engine_for(role)
    return get_engine(engine_cfg.name, engine_cfg)
