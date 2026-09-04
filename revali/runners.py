"""Sandbox runners: run a list of shell steps on a fresh checkout of a ref.

`local`  git worktree on the host; not isolated (documented as such).
`wsl`    fresh clone inside the WSL distro's own filesystem (Windows hosts).
`ssh`    fresh clone from a git bundle on a host reached over ssh; same script as wsl.
`fake`   scenario-driven, selected by REVALI_FAKE_RUNNER=<json path> (tests).
"""
import json
import os
import re
import shlex
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

from revali.config import PlatformCfg
from revali.procs import ExeNotFound, ProcTimeout, resolve, run, run_shell
from revali.state import write_text

Logger = Optional[Callable[[str], None]]
FAKE_ENV = "REVALI_FAKE_RUNNER"
# no password or host-key prompts: a prompt would hang the pipeline
SSH_OPTS = ["-o", "BatchMode=yes"]
SSH_PROBE = "git --version && command -v timeout && command -v bash"
# safety margins on top of the configured timeouts (not tunables): a short remote
# command beyond its connect time, the remote session beyond its steps, one git bundle
SHORT_MARGIN_S = 60
SESSION_MARGIN_S = 300
BUNDLE_TIMEOUT_S = 300


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
            log: Logger = None, scope: str = "") -> RunReport:
        """`scope` is the run's branch as a directory name (`feature__x`): the sandbox and
        ssh runners clone under <sandbox_dir>/<repo>/<scope>/<label> so worktrees of one
        repository can run side by side; empty keeps <repo>/<label>."""
        raise NotImplementedError


def sandbox_dir(*parts: str) -> str:
    """Join the non-empty path parts with `/` (remote paths, never os.path)."""
    return "/".join(p for p in parts if p)


class LocalRunner(Runner):
    """git worktree on the host. Every step runs through the host shell."""
    name = "local"

    def run(self, repo_root, ref, steps, extra_files, logs_dir, label, log=None, scope=""):
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

    def run(self, repo_root, ref, steps, extra_files, logs_dir, label, log=None, scope=""):
        sc = self._scenario()
        os.makedirs(logs_dir, exist_ok=True)
        log_file = os.environ.get("REVALI_FAKE_LOG")
        if log_file:
            with open(log_file, "a", encoding="utf-8") as fh:
                fh.write(json.dumps({"exe": "runner", "label": label, "ref": ref, "scope": scope,
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


SANDBOX_SCRIPT = r'''#!/usr/bin/env bash
# generated by revali; runs one sandbox session and reports per-step exit codes
set -u
SB="__SANDBOX__"
HOST="__HOST__"
LOGS="__LOGS__"
CMDS="__CMDS__"
EXTRA="__EXTRA__"
LABEL="__LABEL__"
REF="__REF__"
SCOPE="__SCOPE__"
STEP_TIMEOUT=__TIMEOUT__
RES="$LOGS/$LABEL.results"
: > "$RES"
cleanup() {
    rm -rf "$SB"
    rmdir "$(dirname "$SB")" 2>/dev/null
    if [ -n "$SCOPE" ]; then rmdir "$(dirname "$(dirname "$SB")")" 2>/dev/null; fi
    return 0
}
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
    (cd "$SB/repo" && timeout "${STEP_TIMEOUT}s" bash "$CMDS/$LABEL-$name.cmd") > "$LOGS/$LABEL-$name.log" 2>&1
    rc=$?; to=0; if [ "$rc" -eq 124 ]; then to=1; fi
    printf "%s\t%s\t%s\n" "$name" "$rc" "$to" >> "$RES"
    [ "$rc" -eq 0 ]
}
__STEPS__
cleanup
exit 0
'''


def sandbox_root(plat: PlatformCfg) -> Tuple[str, str]:
    """(shell form, scp form) of sandbox_dir: "~/x" -> ("$HOME/x", "x"), so a remote shell
    expands the home directory and scp gets a home-relative path (sftp mode expands no $HOME)."""
    root = plat.sandbox_dir.strip().rstrip("/")
    if root == "~":
        return "$HOME", "."
    if root.startswith("~/"):
        return "$HOME" + root[1:], root[2:]
    return root, root


def remote_name(repo_root: str) -> str:
    """The repository's directory name reduced to [A-Za-z0-9._-], so remote paths never
    need quoting on the scp side (sftp and legacy scp disagree on how to quote)."""
    name = os.path.basename(os.path.normpath(repo_root)) or "repo"
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name) or "repo"


def shell_path(path: str) -> str:
    """Quote a remote path for a shell command line, keeping a leading $HOME expandable."""
    if path == "$HOME":
        return '"$HOME"'
    if path.startswith("$HOME/"):
        return '"$HOME"/' + shlex.quote(path[6:])
    return shlex.quote(path)


def render_script(source: str, logs: str, extra: str, ref: str, steps: List[Tuple[str, str]],
                  label: str, sandbox: str, timeout_s: int, cmds: str = "", scope: str = "") -> str:
    """The sandbox script with its placeholders filled; `source` is what git clone reads
    (a repository path or a bundle file); `logs`, `extra` and `cmds` (where the per-step
    .cmd files sit, default `logs`) are paths on the executing side."""
    step_lines = []
    for name, cmd in steps:
        if cmd.strip():
            step_lines.append("run_step %s || { cleanup; exit 0; }" % name)
    text = SANDBOX_SCRIPT
    for key, value in (("__SANDBOX__", sandbox), ("__HOST__", source), ("__LOGS__", logs),
                       ("__CMDS__", cmds or logs), ("__EXTRA__", extra), ("__LABEL__", label), ("__REF__", ref),
                       ("__TIMEOUT__", str(int(timeout_s))), ("__STEPS__", "\n".join(step_lines)),
                       ("__SCOPE__", scope)):
        text = text.replace(key, value)
    return text


def stage_inputs(logs_dir: str, label: str, steps: List[Tuple[str, str]],
                 extra_files: Dict[str, str]) -> Tuple[List[Tuple[str, str]], str]:
    """Write one .cmd file per non-empty step and the extra files under logs_dir; drop a
    stale results file. Returns (non-empty steps, extra dir)."""
    os.makedirs(logs_dir, exist_ok=True)
    steps = [(n, c) for n, c in steps if c.strip()]
    extra_dir = os.path.join(logs_dir, "%s-extra" % label)
    shutil.rmtree(extra_dir, ignore_errors=True)
    os.makedirs(extra_dir, exist_ok=True)
    for rel, content in (extra_files or {}).items():
        write_text(os.path.join(extra_dir, rel), content)
    for name, cmd in steps:
        write_text(os.path.join(logs_dir, "%s-%s.cmd" % (label, name)), cmd + "\n")
    results_path = os.path.join(logs_dir, "%s.results" % label)
    if os.path.isfile(results_path):
        os.remove(results_path)
    return steps, extra_dir


def report_from_results(logs_dir: str, label: str, steps: List[Tuple[str, str]],
                        log: Logger = None) -> RunReport:
    """Build the report from <label>.results and the per-step logs the script wrote."""
    results_path = os.path.join(logs_dir, "%s.results" % label)
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


class WslRunner(Runner):
    """Fresh clone inside the WSL distro's own filesystem, steps run by a generated
    bash script; per-step logs and exit codes come back through the host logs dir."""
    name = "wsl"

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
               steps: List[Tuple[str, str]], label: str, sandbox: str, timeout_s: int,
               scope: str = "") -> str:
        return render_script(host_repo_wsl, logs_wsl, extra_wsl, ref, steps, label, sandbox, timeout_s,
                             scope=scope)

    def run(self, repo_root, ref, steps, extra_files, logs_dir, label, log=None, scope=""):
        steps, extra_dir = stage_inputs(logs_dir, label, steps, extra_files)
        repo_name = os.path.basename(os.path.normpath(repo_root)) or "repo"
        root, _ = sandbox_root(self.plat)
        sandbox = sandbox_dir(root, repo_name, scope, label)
        timeout_s = self.plat.command_timeout_min * 60
        script = self.script(self.wslpath(repo_root), self.wslpath(logs_dir), self.wslpath(extra_dir), ref,
                             steps, label, sandbox, timeout_s, scope=scope)
        script_path = os.path.join(logs_dir, "%s.sh" % label)
        write_text(script_path, script)
        results_path = os.path.join(logs_dir, "%s.results" % label)
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
        return report_from_results(logs_dir, label, steps, log)


class SshRunner(Runner):
    """Fresh clone from a git bundle on a host reached over ssh: the inputs (bundle, script,
    step commands, extra files) go up with scp, the script runs under bash there, the
    per-step logs come back with scp, and the staging directories are removed whatever
    happened. Port, user and key come from ~/.ssh/config; nothing here prompts."""
    name = "ssh"

    def __init__(self, plat: PlatformCfg):
        super().__init__(plat)
        self.host = plat.host.strip()

    def _opts(self) -> list:
        # 0 (a PlatformCfg built by hand) leaves ssh at its own default
        if self.plat.connect_timeout_s > 0:
            return SSH_OPTS + ["-o", "ConnectTimeout=%d" % self.plat.connect_timeout_s]
        return list(SSH_OPTS)

    def _ssh(self, command: str, timeout: float, log: Logger = None):
        """Run one shell command line on the host (ssh hands it to the remote shell as is)."""
        return run(resolve("ssh") + self._opts() + [self.host, command], timeout=timeout, log=log)

    def _scp(self, args: list, cwd: str, timeout: float, log: Logger = None):
        return run(resolve("scp") + self._opts() + ["-q", "-r"] + args, cwd=cwd, timeout=timeout, log=log)

    def _short(self) -> float:
        """Budget for a call that only connects and runs a trivial command."""
        return max(self.plat.connect_timeout_s, 0) + SHORT_MARGIN_S

    def _failure(self, what: str, res) -> str:
        return "ssh: %s host '%s' (exit %d): %s" % (what, self.host, res.returncode,
                                                   res.text.strip()[:400] or "no output")

    def probe(self):
        resolve("scp")   # the transport needs both; report a missing scp before anything is pushed
        return self._ssh(SSH_PROBE, timeout=self._short())

    def script(self, bundle: str, logs: str, extra: str, ref: str, steps: List[Tuple[str, str]],
               label: str, sandbox: str, timeout_s: int, scope: str = "") -> str:
        # the .cmd files travel with the bundle, so they live in the inbox, not the logs dir
        return render_script(bundle, logs, extra, ref, steps, label, sandbox, timeout_s,
                             cmds=os.path.dirname(bundle), scope=scope)

    def run(self, repo_root, ref, steps, extra_files, logs_dir, label, log=None, scope=""):
        steps, extra_dir = stage_inputs(logs_dir, label, steps, extra_files)
        bundle = "%s.bundle" % label
        bundle_path = os.path.join(logs_dir, bundle)
        # --all: a bare sha has no ref name and git refuses to bundle it; the script checks out `ref`
        res = run(resolve("git") + ["bundle", "create", "--quiet", bundle_path, "--all"], cwd=repo_root,
                  timeout=BUNDLE_TIMEOUT_S, log=log)
        if not res.ok:
            shutil.rmtree(extra_dir, ignore_errors=True)
            raise RunnerError("git bundle create failed: %s" % res.text.strip()[:400])
        repo_name = remote_name(repo_root)
        shell_root, scp_root = sandbox_root(self.plat)
        repo_base = "%s/%s" % (shell_root, repo_name)
        base = sandbox_dir(repo_base, scope)           # <root>/<repo>/<branch>, or <root>/<repo>
        inbox = "%s/%s-in" % (base, label)
        rlogs = "%s/%s-logs" % (base, label)
        sandbox = "%s/%s" % (base, label)
        scp_base = sandbox_dir(scp_root, repo_name, scope)
        scp_inbox = "%s/%s-in" % (scp_base, label)
        scp_logs = "%s/%s-logs" % (scp_base, label)
        timeout_s = self.plat.command_timeout_min * 60
        script = self.script("%s/%s" % (inbox, bundle), rlogs, "%s/%s" % (inbox, os.path.basename(extra_dir)),
                             ref, steps, label, sandbox, timeout_s, scope=scope)
        script_name = "%s.sh" % label
        write_text(os.path.join(logs_dir, script_name), script)
        uploads = [bundle, script_name, os.path.basename(extra_dir)] + ["%s-%s.cmd" % (label, n) for n, _ in steps]
        results_path = os.path.join(logs_dir, "%s.results" % label)
        if log:
            log("[%s] ssh %s: %d step(s) on %s" % (label, self.host, len(steps), ref[:10]))
        transfer_s = self.plat.transfer_timeout_min * 60 or None   # None: no limit
        res = None
        try:
            r = self._ssh("mkdir -p %s %s" % (shell_path(inbox), shell_path(rlogs)), timeout=self._short(), log=log)
            if not r.ok:
                raise RunnerError(self._failure("could not reach", r))
            r = self._scp(uploads + ["%s:%s/" % (self.host, scp_inbox)], cwd=logs_dir, timeout=transfer_s, log=log)
            if not r.ok:
                raise RunnerError(self._failure("could not copy the inputs to", r))
            try:
                res = self._ssh("bash %s" % shell_path("%s/%s" % (inbox, script_name)),
                                timeout=timeout_s * max(1, len(steps)) + SESSION_MARGIN_S, log=log)
            except ProcTimeout:
                raise RunnerError("ssh session on %s for %s did not finish in time" % (self.host, label))
            r = self._scp(["%s:%s/." % (self.host, scp_logs), "."], cwd=logs_dir, timeout=transfer_s, log=log)
            if not r.ok and not os.path.isfile(results_path):
                raise RunnerError(self._failure("could not copy the logs back from", r))
        except ExeNotFound as exc:
            raise RunnerError(str(exc))
        finally:
            self._cleanup([inbox, rlogs, sandbox], [base, repo_base] if scope else [base], log)
            shutil.rmtree(extra_dir, ignore_errors=True)
            if os.path.isfile(bundle_path):
                os.remove(bundle_path)
        if res is not None and not res.ok and not os.path.isfile(results_path):
            raise RunnerError("the sandbox script on %s could not start (exit %d): %s"
                              % (self.host, res.returncode, res.text.strip()[:400]))
        return report_from_results(logs_dir, label, steps, log)

    def _cleanup(self, paths: List[str], parents: List[str], log: Logger = None) -> None:
        """Remove the staging dirs and the clone on the host, then the parent dirs (innermost
        first) when nothing else is in them; a failure is logged, never raised."""
        # the exit code is rm's
        command = "rm -rf %s && rmdir --ignore-fail-on-non-empty %s" % (
            " ".join(shell_path(p) for p in paths), " ".join(shell_path(p) for p in parents))
        try:
            r = self._ssh(command, timeout=self._short())
        except (ProcTimeout, ExeNotFound) as exc:
            r = None
            reason = str(exc)
        else:
            reason = r.text.strip()[:200]
        if log:
            if r is not None and r.ok:
                log("[ssh] removed %s on %s" % (", ".join(paths), self.host))
            else:
                log("[ssh] could not remove %s on %s (left for you to delete): %s"
                    % (", ".join(paths), self.host, reason or "no output"))


def probe_runner(plat: PlatformCfg) -> str:
    """Return "" when the runner can start, else one line for preflight. The fake runner always can."""
    if os.environ.get(FAKE_ENV):
        return ""
    try:
        if plat.runner == "ssh":
            res = SshRunner(plat).probe()
            if not res.ok:
                return ("ssh host '%s' is unreachable or lacks git, bash or coreutils timeout (exit %d, ssh = %s): %s; "
                        "if the host is new, run `ssh %s` once by hand to accept its key"
                        % (plat.host, res.returncode, resolve("ssh")[0], res.text.strip()[:300] or "no output",
                           plat.host))
        elif plat.runner == "wsl":
            runner = WslRunner(plat)
            res = runner._wsl(["bash", "-c", SSH_PROBE], timeout=plat.connect_timeout_s + 60)
            if not res.ok:
                return ("WSL distro '%s' did not start or lacks git, bash or coreutils timeout (exit %d): %s"
                        % (runner.distro, res.returncode, res.text.strip()[:300] or "no output"))
    except ExeNotFound as exc:
        return str(exc)
    except ProcTimeout as exc:
        return "%s runner probe timed out: %s" % (plat.runner, exc)
    return ""


def get_runner(plat: PlatformCfg) -> Runner:
    fake = os.environ.get(FAKE_ENV)
    if fake:
        return FakeRunner(plat, fake)
    if plat.runner == "local":
        return LocalRunner(plat)
    if plat.runner == "wsl":
        return WslRunner(plat)
    if plat.runner == "ssh":
        return SshRunner(plat)
    raise RunnerError("unknown runner '%s'" % plat.runner)


def steps_for(plat: PlatformCfg, which: List[str]) -> List[Tuple[str, str]]:
    table = {"setup": plat.setup, "build": plat.build, "test": plat.test, "new_test": plat.new_test}
    return [(name, table[name]) for name in which]
