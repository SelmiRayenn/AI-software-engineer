# Patch Management

Patch management is the backend layer that turns sandbox workspace changes into stored
`GeneratedPatch` records and safely applies candidate unified diffs during agent runs.

This layer manages generated agent patches only. Hidden benchmark solutions remain stored in
`GoldPatch` and are exposed only through evaluation/admin routes.

## API

```text
GET /agent-runs/{run_id}/diff
GET /agent-runs/{run_id}/patch
GET /agent-runs/{run_id}/patches
POST /agent-runs/{run_id}/patch/apply
GET /patches/{patch_id}/quality
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
an immutable version of the run's `GeneratedPatch`.

Request:

```json
{
  "patch_text": "diff --git a/src/example.py b/src/example.py\n..."
}
```

Patch application is currently allowed only for `queued` and `running` runs. Completed runs are
immutable and can only be inspected.

Before the workspace is changed, the backend runs the same quality policy used by agent
`submit_patch` calls. A rejected patch is not applied or stored.

### Get Stored Generated Patch

`GET /agent-runs/{run_id}/patch` returns the selected generated patch for a run, or the latest
candidate before selection. It includes `version` and `is_selected`. `GET /agent-runs/{run_id}/patches`
returns all versions in order. New submissions receive new IDs and pending human reviews; they
never overwrite earlier reviewed contents. These endpoints do not expose `GoldPatch` data.

## Safety Rules

The patch service enforces:

- Patch paths must stay inside the prepared workspace.
- Absolute paths and `..` traversal are rejected.
- Hidden benchmark/gold paths are rejected.
- Binary patches are rejected for now.
- Patch text is capped by `PATCH_MAX_BYTES`.
- Changed file count is capped by `PATCH_MAX_CHANGED_FILES`.
- Quality file count is capped by `MAX_PATCH_FILES`.
- Added plus removed lines are capped by `MAX_PATCH_CHANGED_LINES`.
- Generated files, cache files, dependency build output, and common compiled artifacts are blocked.
- Lockfiles and dependency manifests are blocked by default unless the benchmark task explicitly
  allows the relevant category.
- Patch application must pass `git apply --check` before mutation.
- Patch application is blocked for immutable run states.

Default limits:

```text
PATCH_MAX_BYTES=1000000
PATCH_MAX_CHANGED_FILES=100
MAX_PATCH_FILES=20
MAX_PATCH_CHANGED_LINES=1000
BLOCK_LOCKFILE_CHANGES_BY_DEFAULT=true
BLOCK_DEPENDENCY_FILE_CHANGES_BY_DEFAULT=true
```

`PATCH_MAX_CHANGED_FILES` is the low-level parser safety ceiling. `MAX_PATCH_FILES` is the tighter
quality policy and is the effective default for accepted patches.

## Quality Reports

Every accepted `GeneratedPatch` receives one `PatchQuality` record. Older patch rows are analyzed
and stored lazily on the first quality request:

```text
GET /patches/{patch_id}/quality
```

The response reports changed files and lines, source/test/docs-config categories, suspicious
generated paths, dependency and lockfile touches, whitespace-only detection, warnings, and hard
violations. The report stores the file and line thresholds used for later audit. It exposes only the
count of files unrelated to the hidden gold patch; it never returns the gold changed-file list.

Soft warnings start at 75 percent of either quality limit. Whitespace-only changes, unrelated
files, and explicitly allowed dependency/lockfile changes are also warnings. Warnings remain
auditable but do not prevent storage.

Benchmark curators may set `allow_lockfile_changes` or `allow_dependency_file_changes` on a task
when those edits are essential to the historical fix. Both fields default to `false`.

## Workspace Lifecycle

Agent runs now record `workspace_id` and `workspace_path` when a sandbox workspace is prepared.
The patch endpoints use that recorded workspace. Runs created before this field existed, or runs
whose workspace has been manually deleted, cannot use diff/apply endpoints.

The Docker Compose setup maps `./sandbox-workspaces` to the backend container, and the directory is
ignored by Git.

## Current Limits

- Binary patch support is intentionally blocked.
- Patch application is synchronous.
- Quality analysis is deterministic and path/diff based; it does not judge semantic correctness.
- The generated-file denylist intentionally favors safety and may need project-specific expansion.
