# Docker Sandbox Proof of Concept

The sandbox endpoint clones a repository, checks out a specific commit, copies the checked-out repo
into a temporary Docker container, runs setup and test commands, and returns structured command
results.

This is not connected to the AI agent yet.

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
  "network_enabled": false
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
SANDBOX_NETWORK_ENABLED=false
SANDBOX_PULL_IMAGE=true
```

`SANDBOX_CPU_LIMIT` and `SANDBOX_MAX_OUTPUT_BYTES` replace the earlier `SANDBOX_CPUS` and
`SANDBOX_MAX_LOG_BYTES` names. The backend still accepts the old names for compatibility.

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
    "python -c \"from pathlib import Path; assert Path('hello.py').exists(); print('file exists')\""
  )
  command_timeout_seconds = 60
  network_enabled = $false
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

If command output is missing at the beginning of a long log, check `stdout_truncated` or
`stderr_truncated`. The sandbox keeps the last `SANDBOX_MAX_OUTPUT_BYTES` bytes for each stream.
