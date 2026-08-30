"""Configuration in three layers, most specific wins:

    defaults.toml (shipped with revali) < ~/.revali/config.toml (user) < revali.toml (project)

Unknown keys are errors in every layer: a typo in a command name must not
silently disable a step. No default value lives in this module; they are all
in defaults.toml.
"""
import os
import tomllib
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from revali import CONFIG_VERSION, V1_PLATFORMS

TOOL_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULTS_FILE = os.path.join(TOOL_ROOT, "defaults.toml")
PROJECT_FILE = "revali.toml"
USER_DIR_ENV = "REVALI_HOME"
SECTIONS = ("project", "review", "validate", "merge", "paths", "engines")
PLATFORM_DEFAULTS_KEY = "platform"   # [validate.platform] in defaults.toml
USER_TOP_KEYS = ("checklist", "history_path")
RETIRED_USER_KEYS = {"review_model": "[review] model", "validate_model": "[validate] model"}
RETIRED_REVIEW_ENGINES = ("prompt", "hybrid")   # the old meaning of review.engine, now review.strategy


class ConfigError(Exception):
    def __init__(self, problems):
        self.problems = list(problems)
        super().__init__("; ".join(self.problems))


# Dataclass fields carry types only; values come from defaults.toml.

@dataclass
class PathsCfg:
    state_dir: str = ""
    logs_dir: str = ""
    history_file: str = ""


@dataclass
class EngineCfg:
    name: str = ""
    tiers: List[str] = field(default_factory=list)
    helper_prefix: str = ""


@dataclass
class ProjectCfg:
    base_branch: str = ""
    platforms: List[str] = field(default_factory=list)
    lint: str = ""
    test_dir: str = ""
    test_file_pattern: str = ""
    test_guide: str = ""
    change_source: str = ""
    context_files: List[str] = field(default_factory=list)
    config_version: int = CONFIG_VERSION


@dataclass
class ReviewCfg:
    engine: str = ""
    strategy: str = ""
    model: str = ""
    fallback_model: str = ""
    effort: str = ""
    max_fixes: int = 0
    max_diff_lines: int = 0
    small_max_lines: int = 0
    budget_usd: float = 0.0
    checklist: str = ""
    checklist_builtin: str = ""
    prompt: str = ""
    schema: str = ""
    timeout_min: int = 0
    exclude: List[str] = field(default_factory=list)
    security_paths: List[str] = field(default_factory=list)


@dataclass
class PlatformCfg:
    name: str = ""
    runner: str = ""
    distro: str = ""
    network: bool = False
    command_timeout_min: int = 0
    sandbox_dir: str = ""
    setup: str = ""
    build: str = ""
    test: str = ""
    new_test: str = ""
    check_name: str = ""


@dataclass
class ValidateCfg:
    engine: str = ""
    model: str = ""
    fallback_model: str = ""
    effort: str = ""
    budget_usd: float = 0.0
    prompt: str = ""
    schema: str = ""
    platforms: dict = field(default_factory=dict)  # name -> PlatformCfg


@dataclass
class MergeCfg:
    auto_merge: bool = False
    method: str = ""
    wait_for_checks: bool = False
    checks_timeout_min: int = 0


@dataclass
class Config:
    project: ProjectCfg
    review: ReviewCfg
    validate: ValidateCfg
    merge: MergeCfg
    paths: PathsCfg
    engines: Dict[str, EngineCfg]
    path: str = ""
    warnings: List[str] = field(default_factory=list)

    def engine_for(self, role: str) -> EngineCfg:
        """role: 'review' or 'validate'."""
        name = self.review.engine if role == "review" else self.validate.engine
        return self.engines[name]

    def foreign_ladders(self, engine_name: str) -> List[List[str]]:
        """The tiers of every engine except the named one."""
        return [list(e.tiers) for n, e in self.engines.items() if n != engine_name]


@dataclass
class UserConfig:
    checklist: str = ""
    history_path: str = ""
    path: str = ""
    sections: dict = field(default_factory=dict)   # raw [section] tables for layering


def user_home() -> str:
    return os.environ.get(USER_DIR_ENV) or os.path.join(os.path.expanduser("~"), ".revali")


def tool_file(configured: str, repo_root: str, *default_parts: str) -> str:
    """A configurable file: empty = the one shipped with revali, else relative to the repo."""
    if configured:
        return configured if os.path.isabs(configured) else os.path.join(repo_root, configured)
    return os.path.join(TOOL_ROOT, *default_parts)


# ---- filling dataclasses ----------------------------------------------------

def _type_problem(current, value) -> str:
    if isinstance(current, bool):
        return "" if isinstance(value, bool) else "must be true/false"
    if isinstance(current, int):
        return "" if isinstance(value, int) and not isinstance(value, bool) else "must be an integer"
    if isinstance(current, float):
        return "" if isinstance(value, (int, float)) and not isinstance(value, bool) else "must be a number"
    if isinstance(current, str):
        return "" if isinstance(value, str) else "must be a string"
    if isinstance(current, list):
        return "" if isinstance(value, list) else "must be a list"
    return ""


def _fill(dc_type, data: dict, section: str, problems: list, type_errors: Optional[list] = None, **fixed):
    """Instantiate a dataclass from a dict, rejecting unknown keys and wrong types.
    Type mismatches are also appended to `type_errors` when given."""
    obj = dc_type(**fixed)
    known = {f.name for f in obj.__dataclass_fields__.values()}
    for key, value in data.items():
        if key not in known or key in fixed:
            problems.append("%s: unknown key '%s'" % (section, key))
            continue
        why = _type_problem(getattr(obj, key), value)
        if why:
            problems.append("%s.%s %s" % (section, key, why))
            if type_errors is not None:
                type_errors.append("%s.%s" % (section, key))
            continue
        setattr(obj, key, value)
    return obj


def _split_validate(vdata: dict):
    """[validate] scalar keys and its [validate.<name>] sub-tables."""
    scalars = {k: v for k, v in vdata.items() if not isinstance(v, dict)}
    tables = {k: v for k, v in vdata.items() if isinstance(v, dict)}
    return scalars, tables


def _check_layer(sections: dict, label: str, problems: list) -> List[str]:
    """Every layer must use known sections and keys, checked against the dataclasses.
    Returns the keys whose value had the wrong type."""
    bad: List[str] = []
    for name in sections:
        if name not in SECTIONS:
            problems.append("%s: unknown section [%s]" % (label, name))
    _fill(ProjectCfg, sections.get("project", {}), "%s: project" % label, problems, bad)
    _fill(ReviewCfg, sections.get("review", {}), "%s: review" % label, problems, bad)
    _fill(MergeCfg, sections.get("merge", {}), "%s: merge" % label, problems, bad)
    _fill(PathsCfg, sections.get("paths", {}), "%s: paths" % label, problems, bad)
    scalars, tables = _split_validate(sections.get("validate", {}))
    _fill(ValidateCfg, scalars, "%s: validate" % label, problems, bad)
    for pname, table in tables.items():
        _fill(PlatformCfg, table, "%s: validate.%s" % (label, pname), problems, bad, name=pname)
    for ename, table in sections.get("engines", {}).items():
        if not isinstance(table, dict):
            problems.append("%s: engines.%s must be a table" % (label, ename))
            continue
        _fill(EngineCfg, table, "%s: engines.%s" % (label, ename), problems, bad, name=ename)
    return bad


def merge_layers(*layers: dict) -> dict:
    """Section-wise overlay of raw TOML dicts, earlier layers first.

    Every [validate.<name>] table starts from [validate.platform] merged across
    all layers, then takes the per-platform tables in layer order; so a later
    layer's [validate.platform] still reaches a platform an earlier layer named.
    [engines.<name>] tables layer per engine."""
    merged = {"project": {}, "review": {}, "validate": {}, "merge": {}, "paths": {},
              "engines": {}, "_platforms": {}}
    platform_defaults: dict = {}
    platform_layers: List[Tuple[str, dict]] = []
    for layer in layers:
        if not layer:
            continue
        for name in ("project", "review", "merge", "paths"):
            merged[name].update(layer.get(name, {}))
        scalars, tables = _split_validate(layer.get("validate", {}))
        merged["validate"].update(scalars)
        if PLATFORM_DEFAULTS_KEY in tables:
            platform_defaults.update(tables.pop(PLATFORM_DEFAULTS_KEY))
        platform_layers.extend(tables.items())
        for ename, table in layer.get("engines", {}).items():
            if isinstance(table, dict):
                base = merged["engines"].get(ename, {})
                base.update(table)
                merged["engines"][ename] = base
    for pname, table in platform_layers:
        base = merged["_platforms"].get(pname) or dict(platform_defaults)
        base.update(table)
        merged["_platforms"][pname] = base
    merged["_platform_defaults"] = platform_defaults
    return merged


def _single_component(value: str) -> bool:
    return bool(value) and value not in (".", "..") and "/" not in value and "\\" not in value


# ---- loading ----------------------------------------------------------------

_DEFAULTS_CACHE: dict = {}


def load_defaults(path: str = DEFAULTS_FILE) -> dict:
    if path in _DEFAULTS_CACHE:
        return _DEFAULTS_CACHE[path]
    if not os.path.isfile(path):
        raise ConfigError(["%s is missing; the revali checkout is incomplete" % path])
    with open(path, "r", encoding="utf-8", newline="") as fh:
        try:
            data = tomllib.loads(fh.read())
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(["%s: not valid TOML: %s" % (path, exc)])
    problems: List[str] = []
    _check_layer(data, "defaults.toml", problems)
    if problems:
        raise ConfigError(problems)
    _DEFAULTS_CACHE[path] = data
    return data


def parse_project_config(text: str, path: str = PROJECT_FILE, defaults: Optional[dict] = None,
                         user_sections: Optional[dict] = None, repo_root: str = "") -> Config:
    problems: List[str] = []
    warnings: List[str] = []
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(["%s: not valid TOML: %s" % (path, exc)])
    defaults = defaults if defaults is not None else load_defaults()
    user_sections = user_sections or {}

    type_errors = _check_layer(user_sections, "user config", problems)
    type_errors += _check_layer(data, path, problems)
    if type_errors:
        # A wrong-typed value leaves the zero value behind; semantic checks on
        # that would only repeat the problem in another wording.
        raise ConfigError(problems)

    # Unknown keys were reported per layer above; fill the merged result
    # quietly so the semantic checks below still run and everything is
    # reported in one pass.
    quiet: List[str] = []
    merged = merge_layers(defaults, user_sections, data)
    project = _fill(ProjectCfg, merged["project"], "project", quiet)
    review = _fill(ReviewCfg, merged["review"], "review", quiet)
    merge = _fill(MergeCfg, merged["merge"], "merge", quiet)
    paths = _fill(PathsCfg, merged["paths"], "paths", quiet)
    validate = _fill(ValidateCfg, merged["validate"], "validate", quiet)
    for pname, table in merged["_platforms"].items():
        validate.platforms[pname] = _fill(PlatformCfg, table, "validate.%s" % pname, quiet, name=pname)
    engines = {}
    for ename, table in merged["engines"].items():
        engines[ename] = _fill(EngineCfg, table, "engines.%s" % ename, quiet, name=ename)

    # Semantic checks.
    if project.config_version != CONFIG_VERSION:
        problems.append("project.config_version is %s, this revali expects %d"
                        % (project.config_version, CONFIG_VERSION))
    if not project.platforms:
        problems.append("project.platforms must list at least one platform")
    for name in project.platforms:
        if name not in V1_PLATFORMS:
            problems.append("platform '%s' is not supported in this version (v1.0: %s)"
                            % (name, ", ".join(V1_PLATFORMS)))
            continue
        plat = validate.platforms.get(name)
        if plat is None:
            problems.append("missing [validate.%s] section for listed platform" % name)
            continue
        if not plat.new_test.strip():
            problems.append("validate.%s.new_test is required (how to run test_dir)" % name)
        if plat.runner not in ("wsl", "local"):
            problems.append("validate.%s.runner must be wsl or local" % name)
        if plat.runner == "wsl" and not plat.sandbox_dir.strip():
            problems.append("validate.%s.sandbox_dir must not be empty for the wsl runner" % name)
    for role, cfg in (("review", review), ("validate", validate)):
        if role == "review" and cfg.engine in RETIRED_REVIEW_ENGINES and cfg.engine not in engines:
            problems.append("%s.engine '%s' is now %s.strategy; engine names the CLI: use engine = \"claude\""
                            % (role, cfg.engine, role))
        elif cfg.engine not in engines:
            problems.append("%s.engine '%s' is unknown (available: %s)"
                            % (role, cfg.engine, ", ".join(sorted(engines)) or "none"))
    for ename, eng in engines.items():
        if not eng.tiers:
            problems.append("engines.%s.tiers must list at least one tier" % ename)
    if review.strategy != "prompt":
        problems.append("review.strategy '%s' is not available in this version (use 'prompt')"
                        % review.strategy)
    if project.change_source != "manual":
        problems.append("project.change_source '%s' is not available in this version"
                        % project.change_source)
    if review.max_fixes < 0:
        problems.append("review.max_fixes must be >= 0")
    if merge.method not in ("squash", "merge", "rebase"):
        problems.append("merge.method must be squash, merge or rebase")
    if merge.auto_merge:
        warnings.append("merge.auto_merge is ignored in this version; merges stay manual")
        merge.auto_merge = False
    if "{topic}" not in project.test_file_pattern:
        problems.append("project.test_file_pattern must contain {topic}")
    if not _single_component(paths.state_dir):
        problems.append("paths.state_dir must be a single directory name (got %r)" % paths.state_dir)
    if not _single_component(paths.logs_dir):
        problems.append("paths.logs_dir must be a single directory name (got %r)" % paths.logs_dir)
    if not _single_component(paths.history_file):
        problems.append("paths.history_file must be a single file name (got %r)" % paths.history_file)
    if "history_file" in data.get("paths", {}):
        problems.append("paths.history_file is a user-level key (~/.revali/config.toml), not a project one")
    if repo_root:
        for key, value in (("review.prompt", review.prompt), ("review.schema", review.schema),
                           ("review.checklist_builtin", review.checklist_builtin),
                           ("validate.prompt", validate.prompt), ("validate.schema", validate.schema)):
            if value and not os.path.isfile(tool_file(value, repo_root)):
                problems.append("%s: file not found: %s" % (key, value))

    if problems:
        raise ConfigError(problems)
    return Config(project=project, review=review, validate=validate, merge=merge, paths=paths,
                  engines=engines, path=path, warnings=warnings)


def load_project_config(repo_root: str, user_cfg: Optional[UserConfig] = None) -> Config:
    path = os.path.join(repo_root, PROJECT_FILE)
    if not os.path.isfile(path):
        raise ConfigError(["%s not found in %s (copy templates/revali.toml)" % (PROJECT_FILE, repo_root)])
    if user_cfg is None:
        user_cfg = load_user_config()
    with open(path, "r", encoding="utf-8", newline="") as fh:
        return parse_project_config(fh.read(), path, user_sections=user_cfg.sections, repo_root=repo_root)


def load_user_config() -> UserConfig:
    path = os.path.join(user_home(), "config.toml")
    if not os.path.isfile(path):
        return UserConfig(path=path)
    with open(path, "r", encoding="utf-8", newline="") as fh:
        try:
            data = tomllib.loads(fh.read())
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(["%s: not valid TOML: %s" % (path, exc)])
    problems: List[str] = []
    cfg = UserConfig(path=path)
    for key, value in data.items():
        if key in RETIRED_USER_KEYS:
            problems.append("user config: '%s' moved to %s" % (key, RETIRED_USER_KEYS[key]))
        elif key in USER_TOP_KEYS:
            if not isinstance(value, str):
                problems.append("user config: %s must be a string" % key)
            else:
                setattr(cfg, key, value)
        elif isinstance(value, dict):
            cfg.sections[key] = value
        else:
            problems.append("user config: unknown key '%s'" % key)
    _check_layer(cfg.sections, "user config", problems)
    if problems:
        raise ConfigError(problems)
    if cfg.checklist and not os.path.isabs(cfg.checklist):
        cfg.checklist = os.path.join(user_home(), cfg.checklist)
    return cfg


def paths_for(repo_root: str) -> PathsCfg:
    """The [paths] table for a repo: from its config when it loads; else the raw [paths]
    table of revali.toml (so a broken config still points at the right state dir) over
    the defaults. Used by commands that must find the state directory before a full
    config is required."""
    try:
        return load_project_config(repo_root).paths
    except ConfigError:
        pass
    paths = _fill(PathsCfg, load_defaults().get("paths", {}), "paths", [])
    for file in (os.path.join(user_home(), "config.toml"), os.path.join(repo_root, PROJECT_FILE)):
        for key, value in _raw_paths(file).items():
            if key in ("state_dir", "logs_dir") and isinstance(value, str) and _single_component(value):
                setattr(paths, key, value)
    return paths


def _raw_paths(file: str) -> dict:
    """The [paths] table of a TOML file as written, {} when absent or unreadable."""
    if not os.path.isfile(file):
        return {}
    try:
        with open(file, "r", encoding="utf-8", newline="") as fh:
            table = tomllib.loads(fh.read()).get("paths", {})
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    return table if isinstance(table, dict) else {}


def history_path(user_cfg: Optional[UserConfig] = None) -> str:
    """history_path from the user file wins; else [paths] history_file from the user file
    or defaults.toml, under the user directory. The project file has no say (history is
    per machine)."""
    if user_cfg and user_cfg.history_path:
        return user_cfg.history_path
    paths = _fill(PathsCfg, load_defaults().get("paths", {}), "paths", [])
    if user_cfg:
        name = user_cfg.sections.get("paths", {}).get("history_file")
        if name is not None:
            if not isinstance(name, str) or not _single_component(name):
                raise ConfigError(["user config: paths.history_file must be a single file name (got %r)" % (name,)])
            paths.history_file = name
    return os.path.join(user_home(), paths.history_file)
