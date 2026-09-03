# Docker Sandbox Proof of Concept

The sandbox endpoint clones a repository, checks out a specific commit, copies the checked-out repo into a temporary Docker container, runs setup and test commands, and returns structured command results.

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
- Clone and checkout result.
- One structured result per setup command.
- One structured result per test command.
- stdout, stderr, exit code, timeout flag, and duration for each command.

## Safety Defaults

The command container uses these defaults:

- No privileged container mode.
- `no-new-privileges` security option.
- Linux capabilities dropped.
- Memory limit: `1g`.
- CPU limit: `1.0`.
- Process limit: `256`.
- Per-command timeout: `120` seconds.
- Network disabled for setup and test commands unless explicitly enabled.
- No host repository bind mount; the checked-out repo is copied into the container.

The backend still needs access to Git and Docker. In Docker Compose, the backend mounts the Docker socket so it can create short-lived command containers. Only expose this API to trusted operators.

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

## Local Backend Variant

If the backend runs directly on your machine instead of inside Docker Compose, set `repository_url` to the absolute path of the local test repository:

```powershell
$repo = (Resolve-Path sandbox-workspaces\manual-repo).Path
```
