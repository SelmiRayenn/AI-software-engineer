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
REQUIRE_EDITED_FILES_IN_CANDIDATES=true
REQUIRE_EDITED_FILES_IN_PLAN=false
ALLOW_TEST_FILE_EDITS=true
ALLOW_DOC_FILE_EDITS=false
ALLOW_CONFIG_FILE_EDITS=false
```

`PATCH_MAX_CHANGED_FILES` is the low-level parser safety ceiling. `MAX_PATCH_FILES` is the tighter
quality policy and is the effective default for accepted patches.

## Edited-File Justification

Patch submission and application use the latest valid `candidate_files_submitted` event and the
latest accepted `plan_submitted` event for the run. Source files must be candidate-ranked by
default. Setting `REQUIRE_EDITED_FILES_IN_PLAN=true` additionally requires every edited file to
appear in the accepted plan's `files_likely_to_modify` list.

Test files are allowed by default and can be disabled with `ALLOW_TEST_FILE_EDITS=false`.
Documentation and configuration files are blocked by default unless their category is enabled, or
the specific path is candidate-ranked or included in an accepted plan. Unclassified files also
require candidate or plan justification. Candidate evidence remains grounded in a successful read
or retrieval, as described in the agent-loop documentation.

`trusted_file_guardrail_override=true` is an explicit run option available only through trusted
operator start paths. It bypasses candidate/plan and file-category restrictions for a special run
and emits a `patch_file_guardrail_override` `AgentEvent` containing the affected paths and bypassed
rules. A later service instance honors that audit record when reapplying the same run's patch.
Public agent-run starts reject this option, and benchmark pack runs require the operator token.

The override never permits path traversal, hidden/gold paths, generated/cache/build output, binary
patches, patch-size limits, dependency/lockfile policy, or immutable run states.

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

Quality reports also include deterministic patch-minimization analysis:

- changed-file, changed-hunk, and changed-line totals plus the added/removed ratio
- separate source, test, docs, config, and other file counts
- repeated-edit, formatting-only hunk, large-rewrite, and generated-looking block counts
- modified files that were not successfully read, retrieved, or candidate-ranked before the first
  edit
- a `minimization_score` from 0.0 to 1.0, warning codes, and a named penalty breakdown

The added/removed ratio is `added_lines / max(removed_lines, 1)`, so an addition-only patch has a
finite, deterministic value.

The score starts at 1.0 and subtracts bounded, additive penalties. Extra files contribute up to
0.15, extra hunks up to 0.10, changed lines beyond the first 20 up to 0.15, duplicate edits up to
0.10, formatting-only hunks up to 0.10, large rewrites up to 0.20, generated-looking blocks up to
0.20, and uninspected files up to 0.20. A broad patch adds 0.25; excessive test and configuration
surfaces add 0.10 each. The final value is clamped to 0.0-1.0 and rounded to four decimals. The
breakdown returned by the API records every non-zero deduction, making the score reproducible.

Warning codes are `broad_patch`, `large_rewrite`, `formatting_only_change`,
`uninspected_file_modified`, `generated_block_suspected`, `excessive_test_changes`, and
`excessive_config_changes`. A patch is broad at six files, ten hunks, or 250 changed lines. A hunk
is a large rewrite at 120 changed lines, or at 80 lines when both sides replace at least 20 lines.
Generated-block detection is deliberately conservative: explicit generated markers, added lines of
400 or more characters, or a 20-line block with 35 percent or less unique content trigger it.
These are review signals, not semantic correctness claims and not new hard rejection rules.

Existing quality rows are recalculated lazily when requested after migration
`20260926_0014`. New generated patches store minimization data when the patch is created.

Soft warnings start at 75 percent of either quality limit. Whitespace-only changes, unrelated
files, and explicitly allowed dependency/lockfile changes are also warnings. Warnings remain
auditable but do not prevent storage.

For the final selected patch, the quality response also includes `code_quality_score`,
`review_ready`, `review_blockers`, and `review_warnings` from the run evaluation. These fields are
null or empty for a non-selected patch or a run that has not been evaluated. Hard quality and
edited-file guardrail violations block readiness. A trusted override remains visible as a warning
and score penalty even when it permits the patch to proceed.

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
- Minimization heuristics identify reviewability risk, not whether a larger change is necessary.
- The generated-file denylist intentionally favors safety and may need project-specific expansion.
- Edited-file classification is deterministic and path-based; unusual project layouts may require
  an explicit candidate/plan entry or trusted operator override.
