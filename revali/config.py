"""revali.toml (project) and ~/.revali/config.toml (user) loading and validation.

Unknown keys are errors: a typo in a command name must not silently disable a step.
"""
import os
import tomllib
from dataclasses import dataclass, field
from typing import List, Optional

from revali import CONFIG_VERSION, V1_PLATFORMS

PROJECT_FILE = "revali.toml"
USER_DIR_ENV = "REVALI_HOME"


class ConfigError(Exception):
    def __init__(self, problems):
        self.problems = list(problems)
        super().__init__("; ".join(self.problems))


@dataclass
class ProjectCfg:
    base_branch: str = ""
    platforms: List[str] = field(default_factory=lambda: ["linux"])
    lint: str = ""
    test_dir: str = "tests"
    test_file_pattern: str = "test_review_{topic}.py"
    test_guide: str = ""
    change_source: str = "manual"
    context_files: List[str] = field(default_factory=list)
    config_version: int = CONFIG_VERSION


@dataclass
class ReviewCfg:
    engine: str = "prompt"
    model: str = "fable"
    fallback_model: str = "opus,sonnet"
    effort: str = "high"
    max_fixes: int = 2
    max_diff_lines: int = 800
    small_max_lines: int = 50
    budget_usd: float = 2.0
    checklist: str = "CONVENTIONS.md"
    timeout_min: int = 20
    exclude: List[str] = field(default_factory=list)
    security_paths: List[str] = field(default_factory=list)


@dataclass
class PlatformCfg:
    name: str = "linux"
    runner: str = "wsl"
    distro: str = "Ubuntu"
    network: bool = False
    command_timeout_min: int = 15
    setup: str = ""
    build: str = ""
    test: str = ""
    new_test: str = ""
    check_name: str = ""


@dataclass
class ValidateCfg:
    model: str = "opus"
    fallback_model: str = "sonnet"
    effort: str = "high"
    budget_usd: float = 1.0
    platforms: dict = field(default_factory=dict)  # name -> PlatformCfg


@dataclass
class MergeCfg:
    auto_merge: bool = False
    method: str = "squash"
    wait_for_checks: bool = True
    checks_timeout_min: int = 30


@dataclass
class Config:
    project: ProjectCfg
    review: ReviewCfg
    validate: ValidateCfg
    merge: MergeCfg
    path: str = ""
    warnings: List[str] = field(default_factory=list)


@dataclass
class UserConfig:
    checklist: str = ""
    history_path: str = ""
    review_model: str = ""
    validate_model: str = ""
    path: str = ""


def user_home() -> str:
    return os.environ.get(USER_DIR_ENV) or os.path.join(os.path.expanduser("~"), ".revali")


def _fill(dc_type, data: dict, section: str, problems: list, **fixed):
    """Instantiate a dataclass from a dict, rejecting unknown keys and wrong types."""
    obj = dc_type(**fixed)
    known = {f.name for f in obj.__dataclass_fields__.values()}
    for key, value in data.items():
        if key not in known or key in fixed:
            problems.append("%s: unknown key '%s'" % (section, key))
            continue
        current = getattr(obj, key)
        if isinstance(current, bool):
            if not isinstance(value, bool):
                problems.append("%s.%s must be true/false" % (section, key))
                continue
        elif isinstance(current, int) and not isinstance(value, int):
            problems.append("%s.%s must be an integer" % (section, key))
            continue
        elif isinstance(current, float) and not isinstance(value, (int, float)):
            problems.append("%s.%s must be a number" % (section, key))
            continue
        elif isinstance(current, str) and not isinstance(value, str):
            problems.append("%s.%s must be a string" % (section, key))
            continue
        elif isinstance(current, list) and not isinstance(value, list):
            problems.append("%s.%s must be a list" % (section, key))
            continue
        setattr(obj, key, value)
    return obj


def parse_project_config(text: str, path: str = PROJECT_FILE) -> Config:
    problems = []
    warnings = []
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(["%s: not valid TOML: %s" % (path, exc)])

    for section in data:
        if section not in ("project", "review", "validate", "merge"):
            problems.append("unknown section [%s]" % section)

    project = _fill(ProjectCfg, data.get("project", {}), "project", problems)
    review = _fill(ReviewCfg, data.get("review", {}), "review", problems)
    merge = _fill(MergeCfg, data.get("merge", {}), "merge", problems)

    vdata = dict(data.get("validate", {}))
    platform_tables = {k: vdata.pop(k) for k in list(vdata) if isinstance(vdata[k], dict)}
    validate = _fill(ValidateCfg, vdata, "validate", problems)
    for name, table in platform_tables.items():
        validate.platforms[name] = _fill(PlatformCfg, table, "validate.%s" % name, problems, name=name)

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
    if review.engine != "prompt":
        problems.append("review.engine '%s' is not available in this version (use 'prompt')"
                        % review.engine)
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

    if problems:
        raise ConfigError(problems)
    return Config(project=project, review=review, validate=validate, merge=merge,
                  path=path, warnings=warnings)


def load_project_config(repo_root: str) -> Config:
    path = os.path.join(repo_root, PROJECT_FILE)
    if not os.path.isfile(path):
        raise ConfigError(["%s not found in %s (copy templates/revali.toml)" % (PROJECT_FILE, repo_root)])
    with open(path, "r", encoding="utf-8", newline="") as fh:
        return parse_project_config(fh.read(), path)


def load_user_config() -> UserConfig:
    path = os.path.join(user_home(), "config.toml")
    if not os.path.isfile(path):
        return UserConfig(path=path)
    with open(path, "r", encoding="utf-8", newline="") as fh:
        try:
            data = tomllib.loads(fh.read())
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(["%s: not valid TOML: %s" % (path, exc)])
    problems = []
    cfg = _fill(UserConfig, data, "user config", problems, path=path)
    if problems:
        raise ConfigError(problems)
    if cfg.checklist and not os.path.isabs(cfg.checklist):
        cfg.checklist = os.path.join(user_home(), cfg.checklist)
    return cfg


def history_path(user_cfg: Optional[UserConfig] = None) -> str:
    if user_cfg and user_cfg.history_path:
        return user_cfg.history_path
    return os.path.join(user_home(), "history.jsonl")
