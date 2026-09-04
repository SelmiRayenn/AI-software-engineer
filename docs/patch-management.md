# Patch Management

Patch management is the backend layer that turns sandbox workspace changes into stored
`GeneratedPatch` records and safely applies candidate unified diffs during agent runs.

This layer manages generated agent patches only. Hidden benchmark solutions remain stored in
`GoldPatch` and are exposed only through evaluation/admin routes.

## API

```text
GET /agent-runs/{run_id}/diff
GET /agent-runs/{run_id}/patch
POST /agent-runs/{run_id}/patch/apply
```

### Get Current Workspace Diff

`GET /agent-runs/{run_id}/diff` reads the current Git diff from the active sandbox workspace
recorded on the agent run.

Response:

```json
{
  "patch_text": "diff --git a/src/example.py b/src/example.py\n...",
  "changed_files": ["src/example.py"],
  "stats": {
    "size_bytes": 384,
    "changed_files_count": 1,
    "additions": 2,
    "deletions": 1
  }
}
```

If the run does not have an active workspace path, the endpoint returns `409 Conflict`.

### Apply A Patch

`POST /agent-runs/{run_id}/patch/apply` applies a unified diff inside the run workspace and stores
or updates the run's `GeneratedPatch`.

Request:

```json
{
  "patch_text": "diff --git a/src/example.py b/src/example.py\n..."
}
```

Patch application is currently allowed only for `queued` and `running` runs. Completed runs are
immutable and can only be inspected.

### Get Stored Generated Patch

`GET /agent-runs/{run_id}/patch` returns the stored generated patch for a run. This endpoint does
not expose `GoldPatch` data.

## Safety Rules

The patch service enforces:

- Patch paths must stay inside the prepared workspace.
- Absolute paths and `..` traversal are rejected.
- Hidden benchmark/gold paths are rejected.
- Binary patches are rejected for now.
- Patch text is capped by `PATCH_MAX_BYTES`.
- Changed file count is capped by `PATCH_MAX_CHANGED_FILES`.
- Patch application must pass `git apply --check` before mutation.
- Patch application is blocked for immutable run states.

Default limits:

```text
PATCH_MAX_BYTES=1000000
PATCH_MAX_CHANGED_FILES=100
```

## Workspace Lifecycle

Agent runs now record `workspace_id` and `workspace_path` when a sandbox workspace is prepared.
The patch endpoints use that recorded workspace. Runs created before this field existed, or runs
whose workspace has been manually deleted, cannot use diff/apply endpoints.

The Docker Compose setup maps `./sandbox-workspaces` to the backend container, and the directory is
ignored by Git.

## Current Limits

- Binary patch support is intentionally blocked.
- Patch application is synchronous.
- Workspace cleanup and retention policies are still basic.
- Patch validation uses Git's unified diff application checks, not semantic code analysis.
