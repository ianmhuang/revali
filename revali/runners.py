"""Sandbox runners: run a list of shell steps on a fresh checkout of a ref.

`local`  git worktree on the host; not isolated (documented as such).
`wsl`    fresh clone inside the WSL distro's own filesystem (Windows hosts).
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


def tail(text: str, n: int = 40) -> str:
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


WSL_SCRIPT = r'''#!/usr/bin/env bash
# generated by revali; runs one sandbox session and reports per-step exit codes
set -u
SB="__SANDBOX__"
HOST="__HOST__"
LOGS="__LOGS__"
EXTRA="__EXTRA__"
LABEL="__LABEL__"
REF="__REF__"
STEP_TIMEOUT=__TIMEOUT__
RES="$LOGS/$LABEL.results"
: > "$RES"
rm -rf "$SB" && mkdir -p "$SB"
if ! git -c safe.directory='*' clone -q --no-checkout "$HOST" "$SB/repo" > "$LOGS/$LABEL-clone.log" 2>&1; then
    printf "clone\t128\t0\n" >> "$RES"; exit 0
fi
if ! (cd "$SB/repo" && git checkout -q --detach "$REF") >> "$LOGS/$LABEL-clone.log" 2>&1; then
    printf "clone\t1\t0\n" >> "$RES"; exit 0
fi
if [ -d "$EXTRA" ]; then cp -r "$EXTRA/." "$SB/repo/"; fi
ulimit -u 512 -f 4000000 2>/dev/null
run_step() {
    local name="$1" rc to
    (cd "$SB/repo" && timeout "${STEP_TIMEOUT}s" bash "$LOGS/$LABEL-$name.cmd") > "$LOGS/$LABEL-$name.log" 2>&1
    rc=$?; to=0; if [ "$rc" -eq 124 ]; then to=1; fi
    printf "%s\t%s\t%s\n" "$name" "$rc" "$to" >> "$RES"
    [ "$rc" -eq 0 ]
}
__STEPS__
rm -rf "$SB"; rmdir "$(dirname "$SB")" 2>/dev/null
exit 0
'''


class WslRunner(Runner):
    """Fresh clone inside the WSL distro's own filesystem, steps run by a generated
    bash script; per-step logs and exit codes come back through the host logs dir."""
    name = "wsl"
    SANDBOX_ROOT = "$HOME/.revali/sandbox"

    def __init__(self, plat: PlatformCfg):
        super().__init__(plat)
        self.distro = plat.distro or "Ubuntu"

    def _wsl(self, args: list, timeout: float, log: Logger = None):
        return run(resolve("wsl") + ["-d", self.distro, "-e"] + args, timeout=timeout, log=log)

    def wslpath(self, host_path: str) -> str:
        res = self._wsl(["wslpath", "-a", host_path], timeout=60)
        if not res.ok or not res.stdout.strip():
            raise RunnerError("wslpath failed for %s: %s" % (host_path, res.text.strip()))
        return res.stdout.strip()

    def script(self, host_repo_wsl: str, logs_wsl: str, extra_wsl: str, ref: str,
               steps: List[Tuple[str, str]], label: str, sandbox: str, timeout_s: int) -> str:
        step_lines = []
        for name, cmd in steps:
            if cmd.strip():
                step_lines.append('run_step %s || { rm -rf "$SB"; rmdir "$(dirname "$SB")" 2>/dev/null; exit 0; }' % name)
        text = WSL_SCRIPT
        for key, value in (("__SANDBOX__", sandbox), ("__HOST__", host_repo_wsl), ("__LOGS__", logs_wsl),
                           ("__EXTRA__", extra_wsl), ("__LABEL__", label), ("__REF__", ref),
                           ("__TIMEOUT__", str(int(timeout_s))), ("__STEPS__", "\n".join(step_lines))):
            text = text.replace(key, value)
        return text

    def run(self, repo_root, ref, steps, extra_files, logs_dir, label, log=None):
        os.makedirs(logs_dir, exist_ok=True)
        steps = [(n, c) for n, c in steps if c.strip()]
        extra_dir = os.path.join(logs_dir, "%s-extra" % label)
        shutil.rmtree(extra_dir, ignore_errors=True)
        os.makedirs(extra_dir, exist_ok=True)
        for rel, content in (extra_files or {}).items():
            write_text(os.path.join(extra_dir, rel), content)
        for name, cmd in steps:
            write_text(os.path.join(logs_dir, "%s-%s.cmd" % (label, name)), cmd + "\n")
        repo_name = os.path.basename(os.path.normpath(repo_root)) or "repo"
        sandbox = "%s/%s/%s" % (self.SANDBOX_ROOT, repo_name, label)
        timeout_s = self.plat.command_timeout_min * 60
        script = self.script(self.wslpath(repo_root), self.wslpath(logs_dir), self.wslpath(extra_dir), ref,
                             steps, label, sandbox, timeout_s)
        script_path = os.path.join(logs_dir, "%s.sh" % label)
        write_text(script_path, script)
        results_path = os.path.join(logs_dir, "%s.results" % label)
        if os.path.isfile(results_path):
            os.remove(results_path)
        if log:
            log("[%s] wsl %s: %d step(s) on %s" % (label, self.distro, len(steps), ref[:10]))
        try:
            res = self._wsl(["bash", self.wslpath(script_path)],
                            timeout=timeout_s * max(1, len(steps)) + 300, log=log)
        except ProcTimeout:
            raise RunnerError("wsl session for %s did not finish in time" % label)
        finally:
            shutil.rmtree(extra_dir, ignore_errors=True)
        if not res.ok and not os.path.isfile(results_path):
            raise RunnerError("wsl could not start the sandbox script (exit %d): %s"
                              % (res.returncode, res.text.strip()[:400]))
        rows = []
        if os.path.isfile(results_path):
            with open(results_path, "r", encoding="utf-8", errors="replace") as fh:
                rows = [line.rstrip("\n").split("\t") for line in fh if line.strip()]
        report = RunReport(label=label)
        cmds = dict(steps)
        for row in rows:
            name, rc, to = (row + ["0", "0"])[:3]
            log_path = os.path.join(logs_dir, "%s-%s.log" % (label, name))
            text = ""
            if os.path.isfile(log_path):
                with open(log_path, "r", encoding="utf-8", errors="replace") as fh:
                    text = fh.read()
            if name == "clone":
                raise RunnerError("sandbox clone/checkout failed; see %s" % log_path)
            step = StepResult(name=name, cmd=cmds.get(name, ""), returncode=int(rc), stdout=text,
                              timed_out=(to == "1"), log_path=log_path)
            report.steps.append(step)
            if log:
                log("[%s] %s -> exit %d%s" % (label, name, step.returncode, ", timed out" if step.timed_out else ""))
        if not report.steps and steps:
            raise RunnerError("the sandbox script produced no results; see %s"
                              % os.path.join(logs_dir, "%s-clone.log" % label))
        return report


def get_runner(plat: PlatformCfg) -> Runner:
    fake = os.environ.get(FAKE_ENV)
    if fake:
        return FakeRunner(plat, fake)
    if plat.runner == "local":
        return LocalRunner(plat)
    if plat.runner == "wsl":
        return WslRunner(plat)
    raise RunnerError("unknown runner '%s'" % plat.runner)


def steps_for(plat: PlatformCfg, which: List[str]) -> List[Tuple[str, str]]:
    table = {"setup": plat.setup, "build": plat.build, "test": plat.test, "new_test": plat.new_test}
    return [(name, table[name]) for name in which]
