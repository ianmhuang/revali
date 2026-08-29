"""Sandbox runners: run a list of shell steps on a fresh checkout of a ref.

`local`  git worktree on the host; not isolated (documented as such).
`wsl`    milestone 3.
`fake`   scenario-driven, selected by REVALI_FAKE_RUNNER=<json path> (tests).
"""
import json
import os
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

from revali.config import PlatformCfg
from revali.procs import ProcTimeout, resolve, run, run_shell
from revali.state import write_text

Logger = Optional[Callable[[str], None]]
FAKE_ENV = "REVALI_FAKE_RUNNER"


class RunnerError(Exception):
    pass


@dataclass
class StepResult:
    name: str
    cmd: str
    returncode: int
    stdout: str = ""
    stderr: str = ""
    duration: float = 0.0
    timed_out: bool = False
    log_path: str = ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out

    @property
    def text(self) -> str:
        return (self.stdout or "") + (self.stderr or "")


@dataclass
class RunReport:
    label: str
    steps: List[StepResult] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(s.ok for s in self.steps)

    @property
    def failed(self) -> Optional[StepResult]:
        for s in self.steps:
            if not s.ok:
                return s
        return None

    def step(self, name: str) -> Optional[StepResult]:
        for s in self.steps:
            if s.name == name:
                return s
        return None


def _tail(text: str, n: int = 40) -> str:
    return "\n".join(text.strip().splitlines()[-n:])


class Runner:
    name = "base"

    def __init__(self, plat: PlatformCfg):
        self.plat = plat

    def run(self, repo_root: str, ref: str, steps: List[Tuple[str, str]],
            extra_files: Dict[str, str], logs_dir: str, label: str,
            log: Logger = None) -> RunReport:
        raise NotImplementedError


class LocalRunner(Runner):
    """git worktree on the host. Every step runs through the host shell."""
    name = "local"

    def run(self, repo_root, ref, steps, extra_files, logs_dir, label, log=None):
        os.makedirs(logs_dir, exist_ok=True)
        tmp = tempfile.mkdtemp(prefix="revali-%s-" % label)
        wt = os.path.join(tmp, "wt")
        git = resolve("git")
        res = run(git + ["worktree", "add", "--detach", "--quiet", wt, ref], cwd=repo_root, log=log, timeout=300)
        if not res.ok:
            shutil.rmtree(tmp, ignore_errors=True)
            raise RunnerError("git worktree add failed: %s" % res.text.strip())
        report = RunReport(label=label)
        try:
            for rel, content in (extra_files or {}).items():
                write_text(os.path.join(wt, rel), content)
            timeout = self.plat.command_timeout_min * 60
            for name, cmd in steps:
                if not cmd.strip():
                    continue
                if log:
                    log("[%s] %s: %s" % (label, name, cmd))
                start = time.monotonic()
                try:
                    r = run_shell(cmd, cwd=wt, timeout=timeout, log=None)
                    step = StepResult(name=name, cmd=cmd, returncode=r.returncode, stdout=r.stdout,
                                      stderr=r.stderr, duration=r.duration)
                except ProcTimeout:
                    step = StepResult(name=name, cmd=cmd, returncode=-1, timed_out=True,
                                      duration=time.monotonic() - start)
                step.log_path = os.path.join(logs_dir, "%s-%s.log" % (label, name))
                write_text(step.log_path, "$ %s\n(exit %d%s)\n\n%s" % (
                    cmd, step.returncode, ", timed out" if step.timed_out else "", step.text))
                report.steps.append(step)
                if log:
                    log("[%s] %s -> exit %d in %.0fs" % (label, name, step.returncode, step.duration))
                if not step.ok:
                    break
        finally:
            run(git + ["worktree", "remove", "--force", wt], cwd=repo_root, timeout=120)
            run(git + ["worktree", "prune"], cwd=repo_root, timeout=60)
            shutil.rmtree(tmp, ignore_errors=True)
        return report


class FakeRunner(Runner):
    """Scenario file: {"default": 0, "results": {"<label>": {"<step>": <exit>}}, "outputs": {...}}."""
    name = "fake"

    def __init__(self, plat: PlatformCfg, scenario_path: str):
        super().__init__(plat)
        self.scenario_path = scenario_path

    def _scenario(self) -> dict:
        try:
            with open(self.scenario_path, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return {}

    def run(self, repo_root, ref, steps, extra_files, logs_dir, label, log=None):
        sc = self._scenario()
        os.makedirs(logs_dir, exist_ok=True)
        log_file = os.environ.get("REVALI_FAKE_LOG")
        if log_file:
            with open(log_file, "a", encoding="utf-8") as fh:
                fh.write(json.dumps({"exe": "runner", "label": label, "ref": ref,
                                     "steps": [n for n, c in steps if c.strip()],
                                     "extra_files": sorted(extra_files or {})}) + "\n")
        report = RunReport(label=label)
        results = (sc.get("results") or {}).get(label, {})
        outputs = (sc.get("outputs") or {}).get(label, {})
        for name, cmd in steps:
            if not cmd.strip():
                continue
            rc = int(results.get(name, sc.get("default", 0)))
            out = outputs.get(name, "fake %s output\n" % name)
            step = StepResult(name=name, cmd=cmd, returncode=rc, stdout=out,
                              log_path=os.path.join(logs_dir, "%s-%s.log" % (label, name)))
            write_text(step.log_path, "$ %s\n(exit %d)\n\n%s" % (cmd, rc, out))
            report.steps.append(step)
            if rc != 0:
                break
        return report


def get_runner(plat: PlatformCfg) -> Runner:
    fake = os.environ.get(FAKE_ENV)
    if fake:
        return FakeRunner(plat, fake)
    if plat.runner == "local":
        return LocalRunner(plat)
    if plat.runner == "wsl":
        raise RunnerError("the wsl runner is not implemented yet (milestone 3); set runner = \"local\" to try")
    raise RunnerError("unknown runner '%s'" % plat.runner)


def steps_for(plat: PlatformCfg, which: List[str]) -> List[Tuple[str, str]]:
    table = {"setup": plat.setup, "build": plat.build, "test": plat.test, "new_test": plat.new_test}
    return [(name, table[name]) for name in which]
