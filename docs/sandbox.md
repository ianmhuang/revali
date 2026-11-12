# Sandbox

Where the Validator runs the existing suite and the Reviewer's tests, and
what the three runners need.

`[validate.linux] runner = "wsl"` clones the branch into the WSL distro's own
filesystem (`sandbox_dir`, default
`~/.revali/sandbox/<repo>/<branch>-<key>/<label>/`, where `<repo>` is the
checkout's directory name, `<branch>` is the run's branch with `/` written
as `__`, and `<key>` is 8 hex characters derived from the checkout's real
path and the exact branch name, so worktrees of one repository, repositories
whose directories share a name, and branches such as `feature/x` and
`feature__x` validate side by side, while one checkout and branch keep the
same directory from run to run), runs `setup`, `build`,
`test`, `new_test` there with a per-step timeout, copies the logs back, and
deletes the clone and the `<branch>-<key>` and `<repo>` directories once
empty. Earlier revali versions used `<repo>/<branch>/<label>`; a session
that was killed may have left such a directory behind. revali does not
remove directories of the old layout; delete them by hand when no run is
in progress. The clone is made from the repository's common git directory,
with the commit resolved on the host first, so a linked worktree added on
Windows (whose `.git` file names a Windows path that git inside WSL cannot
read) validates like the primary tree. A
`new_test` command may contain `{files}`: it is replaced by the reviewer's
test files (paths relative to the repository root, space separated, quoted
when they contain whitespace), this round's files in the smoke run and every
reviewer file on the branch in validation, so only the new tests run rather
than everything a pattern matches; when `{files}` would name no file the
step is skipped and the log says so. Without `{files}` the command runs as
written. Validation leaves `test` out when the baseline already ran it on
the same tree (every commit since the baseline is a reviewer test commit
touching only `test_dir`; `[validate] reuse_baseline = false` disables
this), and says so in the log, in `tests.md` and in the PR comment; a fix
round, an author commit or a rewritten history brings the full suite back.
When validation fails in `new_test`, the reviewer's test files are run once
more on the base tip (the commit preflight compared the branch against),
copied into a sandbox clone of that commit, with `setup` and `build` first,
under the label `base-r<round>` (`[validate] rerun_on_base = false`
disables this). The result is evidence for the diagnosis session, which
answers `introduced_by` per failure: `branch` when the test passes on base
or fails there only because the change is absent, `base` when it fails on
both for the same reason, `unknown` otherwise. The rerun never changes the
PASS / FAIL result; when it cannot produce a result (`setup` or `build`
failing on base, a sandbox error, a timeout) `tests.md` records it as
unavailable and the run goes on. It does not happen when the existing
suite (`test`) is what failed, or when the reviewer wrote no test file.
Every sandbox session's wall time appears in the log line that
reports its result and, with the per-stage times, in the run's final
`run: timing` line. The distro needs git and whatever `setup` installs; on
Ubuntu 24.04 that means `python3-venv` and `python3-pip` for a Python project.
The WSL service itself sometimes fails a call for a while
(`Wsl/Service/WSAETIMEDOUT`, in windows of about 30 seconds that can recur).
Every `wsl.exe` call the runner makes (the preflight probe, `wslpath`, the
sandbox script) is retried when its output matches `wsl_transient_errors`:
after `wsl_retry_wait_s` seconds, up to `wsl_retries` times, with one log line
per retry. The sandbox script is not run again once it has started (its
first line creates the results file). When the last attempt fails too, the
run stops with exit 1 as before, and the message adds that `wsl --shutdown`
restarts WSL and stops every running distro, other projects' included;
revali never runs it. With `wsl_retries = 0` there is one attempt and the
same message.
A wsl or ssh sandbox session counts only when its results file has a row
for every step it was given, in order, up to the last one or up to the
first that failed (the script stops there). A session that ended before
that, because `wsl --shutdown` ran (for any project), the WSL VM or the ssh
connection went away, or the script was killed for memory, is a sandbox
error: the run stops with exit 1 and a message saying the session ended
before the first step without a result finished (a row is written when its
step finishes, so that step may have been running, and its log may be
partly filled), never a pass for steps that did not finish. A session with
no row at all is reported the same way for its first step; the message
adds that a script unable to write under its logs directory ends the same
way. A base rerun
records it as unavailable. Rows that do not match the session's steps (an
unknown, repeated or out-of-order step) and a row cut off while being
written are refused the same way. On the ssh runner, when the ssh call
that runs the script ends with exit 255 (the connection went away) and
no results came back or they stop early (a row cut off while being
written included), the message adds that the script may
still be running on the host until its steps time out: revali has no
connection left to stop it. Every ssh session therefore works in
directories of its own on the host: the clone, the uploaded inputs and the
remote logs are `<label>-<id>`, `<label>-<id>-in` and `<label>-<id>-logs`
under `<repo>/<branch>-<key>`, where `<id>` is 8 random hex characters new
for each session, so a later session of the same checkout, branch and label
never reads that script's results or deletes its clone, and its cleanup
removes only its own directories. The message names this session's three
directories and says whether revali removed them; when the removal failed
they are left on the host, and you delete them once the script has ended.
A script still running can create them again after revali removed them,
so check for them once it has ended in that case too.
The names in your logs directory (`<label>.results`, `<label>-<step>.log`)
do not carry the id. The wsl runner keeps `<label>`: ending `wsl.exe` ends
the script inside the distro too. A results row cut off while being
written counts as a session stopped from outside only when it is the last
row; a broken row with rows after it means the results file is damaged,
and the run stops with exit 1 saying so.
A wsl or ssh sandbox caps the size of a file a step writes (`ulimit -f
4000000`, in 1024-byte blocks) and the number of processes (`ulimit -u`,
`[validate.<platform>] max_procs`, default 4096; 0 sets no process limit).
The process limit is not per sandbox: Linux counts every process and
thread of the user in that WSL distro or on that ssh host against it, so
other sandboxes running at the same time (other projects', other
worktrees') and the user's own programs share it. Each WSL distro counts
separately. On 2026-11-10 the suites of four projects peaked at 36 to 116
tasks each, in the WSL distro with nothing else running; the old fixed
limit was 512. When a step fails (not by timeout) with `Resource
temporarily unavailable`, `can't start new thread` or `EAGAIN` in its
output while the limit is set, the script appends a line starting
`revali:` to that step's log naming `max_procs` and its value as a likely
cause (the strings are matched as text, and socket I/O can print
`EAGAIN` too); the step's result is unchanged. The script's own errors go
to `<label>-script.log` in the logs directory. When a session ends early
(no result for a step, or a last row cut off) and that file is not empty,
the error names it and quotes its first non-empty line (up to 200
characters); when the file also holds one of those strings and a limit is
set, it adds a sentence naming `max_procs` as a likely cause, which is what
happens when the script itself cannot start a step because the user's
processes already fill the limit. A host whose hard limit is below
`max_procs` keeps its own, lower limit, and the file-size cap still applies. Linux does not apply
the process limit to root (or to a user with `CAP_SYS_RESOURCE`), so
`max_procs` has no effect when the sandbox runs as root. The local runner
sets neither limit.
`runner = "local"` uses a git worktree on the host with no isolation.

`runner = "ssh"` with `host = "<destination>"` does the same as `wsl` on a Linux
host reached over ssh (a Windows host only with bash as its ssh shell; see
[docs/configuration.md](configuration.md)): the branch travels as a git bundle (the host needs no
GitHub access), the sandbox script and the step commands go up with scp, the
per-step logs come back the same way, and the staging directories under
`sandbox_dir` are removed afterwards (the repository's directory name is
reduced to `[A-Za-z0-9._-]` on the host, and `sandbox_dir` may not contain
whitespace). `host` is anything `ssh` accepts, so user, port and key belong
in `~/.ssh/config`; `connect_timeout_s` and `transfer_timeout_min` bound the
connection and each scp transfer. Every call runs with
`BatchMode=yes`: nothing prompts, so key-based login must already work and
the host key must be known (run `ssh <host>` once by hand). Preflight probes
the host (reachable, git and `timeout` present) before anything is pushed;
the same probe runs inside the WSL distro for `runner = "wsl"`. The ssh runner was
verified against sshd inside WSL (`ListenAddress 127.0.0.1`, a non-default
port, key-only login); on Ubuntu 24.04 `Port` in `sshd_config` is ignored
while `ssh.socket` is active, so disable the socket and enable `ssh.service`
if the port does not change.

Every runner cleans up after its session: the local runner's worktree and
temp directory, the `<label>-extra` directory and the ssh runner's
`<label>.bundle` in the logs directory, the staging directories on the ssh
host. A cleanup that fails is logged, naming what is left for you to delete,
and never changes the session's result: the report, or the error that ended
the session, stands. A file held open on Windows (an antivirus scan, an
editor) is the usual cause.

## Verification record

The ssh runner was verified end to end on 2026-08-30 on the author's
machine: sshd inside the WSL Ubuntu 24.04 distro, `ListenAddress
127.0.0.1` on a non-default port, key-only login, a `Host` entry for it in
the host's `~/.ssh/config`, and `runner = "ssh"` naming that entry in a
throwaway private repository's `revali.toml`. One run went APPROVE, PASS,
`revali merge`, with the validation steps executed on the ssh host; the
staging directories under `sandbox_dir` were gone afterwards. The WSL runner was verified the same
way on 2026-08-30, and again on 2026-09-04 with two worktrees of one
repository validating at the same time in separate `<repo>/<branch>/<label>`
directories.
