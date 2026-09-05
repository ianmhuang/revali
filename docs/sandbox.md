# Sandbox

Where the Validator runs the existing suite and the Reviewer's tests, and
what the three runners need.

`[validate.linux] runner = "wsl"` clones the branch into the WSL distro's own
filesystem (`sandbox_dir`, default `~/.revali/sandbox/<repo>/<branch>/<label>/`,
where `<branch>` is the run's branch with `/` written as `__`, so worktrees of
one repository validate side by side), runs `setup`, `build`,
`test`, `new_test` there with a per-step timeout, copies the logs back, and
deletes the clone and the `<branch>` and `<repo>` directories once empty. A
`new_test` command may contain `{files}`: it is replaced by the reviewer's
test files (paths relative to the repository root, space separated, quoted
when they contain whitespace), this round's files in the smoke run and every
reviewer file on the branch in validation, so only the new tests run rather
than everything a pattern matches; when `{files}` would name no file the
step is skipped and the log says so. Without `{files}` the command runs as
written. Every sandbox session's wall time appears in the log line that
reports its result and, with the per-stage times, in the run's final
`run: timing` line. The distro needs git and whatever `setup` installs; on
Ubuntu 24.04 that means `python3-venv` and `python3-pip` for a Python project.
`runner = "local"` uses a git worktree on the host with no isolation.

`runner = "ssh"` with `host = "<destination>"` does the same as `wsl` on a Linux
host reached over ssh: the branch travels as a git bundle (the host needs no
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
