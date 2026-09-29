# Docker Sandbox Proof of Concept

The sandbox endpoint clones a repository, checks out a specific commit, copies the checked-out repo
into a temporary Docker container, runs setup and test commands, and returns structured command
results.

The same Docker command session also runs agent test tools and orchestrated evaluation phases.
Untrusted setup/test commands never fall back to execution on the backend host.

## Endpoint

```text
POST /sandbox/run
```

Request body:

```json
{
  "repository_url": "/sandbox-workspaces/manual-repo",
  "base_commit": "<commit-sha>",
  "setup_commands": ["python --version"],
  "test_commands": ["python hello.py"],
  "command_timeout_seconds": 60,
  "network_mode": "none"
}
```

Response includes:

- Overall status.
- Unique workspace id.
- Workspace root, workspace path, and whether the workspace was retained.
- Clone and checkout result.
- One structured result per setup command.
- One structured result per test command.
- stdout, stderr, exit code, timeout flag, and duration for each command.
- Per-command `network_policy` with requested/effective mode, phase, exception flag, and reason.
- Structured `error_code`, `error`, and `cleanup_error` fields when something fails.

## Workspace Lifecycle

Each sandbox run receives a unique workspace id. The backend creates a matching workspace directory
under `SANDBOX_WORKSPACE_ROOT`; if that variable is empty, it uses an OS temporary directory named
`agent-benchmark-sandbox-workspaces`.

The checked-out repository lives under:

```text
<workspace-root>/sandbox-<workspace-id>/repo
```

The workspace directory also contains `.sandbox-workspace.json` metadata with the workspace id,
root, path, repository path, creation time, and retention flag.

Cleanup is controlled by `SANDBOX_RETAIN_WORKSPACES`:

- `false`: remove the workspace after the sandbox response is created.
- `true`: keep the workspace for debugging.

Cleanup refuses to delete paths outside the configured workspace root and refuses to delete the root
directory itself. Sandbox workspace creation also validates that the workspace path stays under the
configured root before anything is cloned.

## Safety Defaults

The command container uses these defaults:

- No privileged container mode.
- `no-new-privileges` security option.
- Linux capabilities dropped.
- Memory limit: `1g`.
- CPU limit: `1.0`.
- Process limit: `256`.
- Per-command timeout: `120` seconds.
- Output capture limit: `200000` bytes per stream.
- Network disabled for setup and test commands unless explicitly enabled.
- No host repository bind mount; the checked-out repo is copied into the container.

The backend still needs access to Git and Docker. In Docker Compose, the backend mounts the Docker socket so it can create short-lived command containers. Only expose this API to trusted operators.

## Configuration

```text
SANDBOX_WORKSPACE_ROOT=/sandbox-workspaces
SANDBOX_RETAIN_WORKSPACES=false
SANDBOX_MEMORY_LIMIT=1g
SANDBOX_CPU_LIMIT=1.0
SANDBOX_COMMAND_TIMEOUT_SECONDS=120
SANDBOX_MAX_OUTPUT_BYTES=200000
```

Additional sandbox settings currently available:

```text
SANDBOX_IMAGE=python:3.12-slim
SANDBOX_PIDS_LIMIT=256
SANDBOX_CLONE_TIMEOUT_SECONDS=120
SANDBOX_MAX_COMMAND_TIMEOUT_SECONDS=600
SANDBOX_NETWORK_MODE=none
SANDBOX_ALLOW_NETWORK_DURING_SETUP=false
SANDBOX_ALLOW_NETWORK_DURING_TESTS=false
SANDBOX_ALLOWED_HOSTS=
SANDBOX_PULL_IMAGE=true
```

`SANDBOX_CPU_LIMIT` and `SANDBOX_MAX_OUTPUT_BYTES` replace the earlier `SANDBOX_CPUS` and
`SANDBOX_MAX_LOG_BYTES` names. The backend still accepts the old names for compatibility.

## Network Policy

Only Linux Docker containers with `none` or `bridge` networking are supported. Host networking,
shared-container networking, `default`, and custom networks are rejected with
`error_code: "network_mode_unsupported"`. Unsupported platforms fail closed; there is no networked
or host-process fallback. Containers remain unprivileged even when a network exception is enabled.

| Phase | Default | Explicit exception |
| --- | --- | --- |
| Setup | `none` | `SANDBOX_ALLOW_NETWORK_DURING_SETUP=true` selects `bridge` |
| Baseline, post-patch, hidden evaluation, lint, format check | `none` | Both `SANDBOX_ALLOW_NETWORK_DURING_TESTS=true` and mode `bridge` |
| Agent `run_tests` and flakiness repetitions | `none` | Same test policy |

For dependency installation during setup only, enable `SANDBOX_ALLOW_NETWORK_DURING_SETUP=true`
and leave the other defaults unchanged. Omit request `network_mode` to use that setup exception.
A request for `none` always disables networking, including setup. A request for `bridge` cannot
override either phase's server-side permission. Tests require an explicit bridge mode, either in
`SANDBOX_NETWORK_MODE` or the sandbox request, as well as the test permission flag.

The legacy request `network_enabled=false` narrows policy to `none`; `true` requests `bridge` but
still requires phase permission. Do not provide both request fields. The deprecated global
`SANDBOX_NETWORK_ENABLED` is no longer used to grant access.

`SANDBOX_ALLOWED_HOSTS` is a future placeholder, not an enforced hostname filter. Leave it empty.
A nonempty value with a network exception is rejected rather than offering a false allowlist
guarantee. An enabled bridge exception allows unrestricted outbound access, including any reachable
private services. Keep exceptions limited to trusted setup commands where necessary.

Managed runs store `sandbox_network_policy` AgentEvents for each command. Traces show disabled,
exception, or blocked decisions; JSON and Markdown run reports summarize effective modes and
exception counts/phases. Hidden evaluation audit records contain policy metadata only, never its
commands or test payloads. The standalone sandbox response includes the same policy per result;
its top-level `network_enabled` means at least one command had a network exception.

Repository cloning and Docker image pulling are trusted preparation operations performed outside
the command container and may still use the backend/daemon network. Model-provider requests are
also outside this policy. `none` isolates command-container networking, not the entire backend.

## Command Container Lifecycle

One managed run shares a container filesystem across setup, agent test tools, and evaluation.
When switching from network-enabled setup to network-disabled tests, the runner stops setup
processes, snapshots the container filesystem into a temporary image, and starts a fresh container
with `network_mode=none`. Installed dependencies survive, but network connections/processes do not.
Temporary images and containers are removed when the session closes. Workspace retention keeps
only the host checkout, not container state.

Host edits are copied into Docker without bind mounts or inherited host environment variables.
Workspace links/junctions are rejected during synchronization. Setup/test-created files remain
container-local and are not copied back to the agent workspace. Use Linux-compatible commands and
paths, not host virtual-environment executables. Each standalone test endpoint opens a fresh session;
separate requests do not share installed dependencies. Use a prepared `SANDBOX_IMAGE` for standalone
post-patch testing, or the managed orchestrator to preserve setup state across phases.

## Manual Test With Docker Compose

Create a tiny local Git repository under the shared sandbox workspace:

```powershell
New-Item -ItemType Directory -Force sandbox-workspaces\manual-repo
Set-Content sandbox-workspaces\manual-repo\hello.py "print('hello from sandbox')"
git -C sandbox-workspaces\manual-repo init
git -C sandbox-workspaces\manual-repo config user.email "dev@example.com"
git -C sandbox-workspaces\manual-repo config user.name "Dev User"
git -C sandbox-workspaces\manual-repo add hello.py
git -C sandbox-workspaces\manual-repo commit -m "Add hello script"
$commit = git -C sandbox-workspaces\manual-repo rev-parse HEAD
```

Start the stack:

```powershell
docker compose up --build
```

In another terminal, call the sandbox endpoint:

```powershell
$body = @{
  repository_url = "/sandbox-workspaces/manual-repo"
  base_commit = $commit
  setup_commands = @("python --version")
  test_commands = @(
    "python hello.py",
    'python -c "from pathlib import Path; assert Path(''hello.py'').exists(); print(''file exists'')"',
    'python -c "import socket; assert {name for _, name in socket.if_nameindex()} == {''lo''}"'
  )
  command_timeout_seconds = 60
  network_mode = "none"
} | ConvertTo-Json

Invoke-RestMethod `
  -Method Post `
  -Uri "http://localhost:8000/sandbox/run" `
  -ContentType "application/json" `
  -Body $body
```

Expected result:

- `status` is `passed`.
- `clone_result.passed` is `true`.
- `checkout_result.passed` is `true`.
- Each setup and test result includes command output and duration.
- Each command has `network_policy.effective_network_mode = "none"`.
- The network check confirms that the Linux container sees only its loopback interface.
- `workspace_retained` is `false` unless `SANDBOX_RETAIN_WORKSPACES=true`.

## Local Backend Variant

If the backend runs directly on your machine instead of inside Docker Compose, set `repository_url` to the absolute path of the local test repository:

```powershell
$repo = (Resolve-Path sandbox-workspaces\manual-repo).Path
```

## Troubleshooting

If Docker is unavailable, the endpoint returns:

```json
{
  "status": "sandbox_error",
  "error_code": "docker_unavailable",
  "error": "Docker is unavailable. Ensure Docker Desktop or the Docker daemon is running and the backend can access the Docker socket."
}
```

Check that Docker Desktop or the Docker daemon is running. For Docker Compose, confirm the backend
container can access the mounted Docker socket.

For `network_mode_unsupported`, select Linux containers, use only `none`/`bridge`, and leave
`SANDBOX_ALLOWED_HOSTS` empty. The error is returned without retrying with weaker isolation.
Dependency downloads failing during setup commonly indicate that the explicit setup exception is
disabled. Prefer an image containing dependencies when fully offline execution is required.

If command output is missing at the beginning of a long log, check `stdout_truncated` or
`stderr_truncated`. The sandbox keeps the last `SANDBOX_MAX_OUTPUT_BYTES` bytes for each stream.
