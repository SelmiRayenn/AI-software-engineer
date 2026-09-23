# Evaluation Reports

Agent run reports provide a portable, read-only summary of one run for review, audit, and
sharing. Reports are generated from persisted run data and are available as structured JSON or
Markdown. Benchmark pack run reports provide the corresponding aggregate evidence across an
ordered task roster.

## Endpoints

```text
GET /agent-runs/{run_id}/report.json
GET /agent-runs/{run_id}/report.md
GET /benchmark-pack-runs/{pack_run_id}/report.json
GET /benchmark-pack-runs/{pack_run_id}/report.md
```

Both responses use `Content-Disposition: attachment` with a run-specific filename. A missing run
returns `404 Agent run not found`.

Example:

```bash
curl -OJ http://localhost:8000/agent-runs/00000000-0000-4000-8000-000000000000/report.json
curl -OJ http://localhost:8000/agent-runs/00000000-0000-4000-8000-000000000000/report.md
curl -OJ http://localhost:8000/benchmark-pack-runs/00000000-0000-4000-8000-000000000000/report.json
curl -OJ http://localhost:8000/benchmark-pack-runs/00000000-0000-4000-8000-000000000000/report.md
```

## Report Contents

The JSON format is versioned with `schema_version`. Both formats include:

- run, benchmark task, repository, issue, and model metadata
- the recorded run configuration, when available
- a bounded chronological trace summary and event counts
- files inspected and files modified
- selected patch metadata, quality signals, review status, and a bounded diff
- visible setup, baseline, and post-patch test summaries with bounded logs
- hidden evaluation aggregate counts only
- evaluation metrics and issue-resolution signals
- persistent failure classification for failed runs
- token, estimated cost, and execution-time totals

The patch `reference` points to the existing run patch endpoint when the full diff is larger than
the embedded report limit.

## Benchmark Pack Run Contents

Pack reports use the persisted pack/run snapshots so the exported roster does not change when a
task title, pack membership, or current metric is edited later. They include:

- pack identity, description, version, and source
- pack-run status, timestamps, model/provider, and effective run configuration
- task completion/failure counts and visible, hidden, and issue-resolution rates
- average localization and issue-specific scores
- total tokens, estimated cost, and execution time
- an ordered per-task results table with metric snapshots
- failure category counts and recurring bounded failure groups
- failed-task details with redacted, byte-limited summaries
- explicit limitations covering sequential execution, hidden-evaluation visibility, pricing, and
  partial or missing results

An empty historical pack run exports successfully with zero-valued aggregates, an empty task list,
and a limitation explaining that no task results were recorded.

## Public Demo Snapshot

Use the public snapshot when README, portfolio, or demo material needs aggregate evidence without
the debugging detail contained in run-level reports:

```text
GET /reports/public-demo-snapshot
GET /reports/public-demo-snapshot?format=md
```

Optional filters are `benchmark_pack_id`, `model_provider`, and `model_name`. `limit` defaults to
20 and accepts 1-100; it is the combined limit across selected successful and failed examples, not
a per-category limit. Selection is deterministic and balanced when both categories are available.
Successful examples are completed runs whose stored metric reports `issue_resolved=true`; failed
examples are runs with status `failed`.

The snapshot includes a timestamp, echoed filters, optional pack identity, aggregate analytics,
the first ten ranked model configurations, total token/cost/time usage, and compact run examples.
Failed examples expose only the persistent failure category. They never include the failure message,
trace, test logs, model response, patch, or event payload.

Repository owner/name and task title are included. Repository URLs are omitted by default. Set
`PUBLIC_DEMO_REDACT_REPOSITORY_URLS=false` only when the stored repository URLs are safe to publish;
credentials, query strings, and fragments are still removed before export.

Example:

```bash
curl -OJ "http://localhost:8000/reports/public-demo-snapshot?benchmark_pack_id=PACK_UUID&limit=12"
curl -OJ "http://localhost:8000/reports/public-demo-snapshot?model_provider=mock&format=md"
```

An empty result is a successful zero-valued snapshot with explanatory notes. It is not treated as
an API error, which allows README/demo generation before the first qualifying run exists.

## Sharing Safety

Reports deliberately exclude trusted benchmark solution material:

- `GoldPatch.patch_text`, gold changed files, and gold test files are never queried for export.
- Hidden test names, commands, file payloads, patches, and command output are omitted. Only pass,
  run, and failure counts are included.
- Common API keys, bearer tokens, access tokens, passwords, and secrets are redacted from visible
  text.
- Visible command output, issue text, review notes, failure summaries, trace events, and patch diffs
  are bounded. JSON fields expose `truncated` and `original_size_bytes`; Markdown includes an
  explicit truncation note.
- Paths associated with protected benchmark or hidden evaluation data are filtered from exported
  file lists.
- Pack reports read only agent-visible task snapshots and aggregate metric/failure records. Hidden
  test commands, files, patches, names, and logs are never included.
- Public snapshots contain scalar analytics, sanitized names, safe failure categories, and bounded
  run selections only. They never include raw logs, traces, failure messages, patches, or prompts.

Current export limits are 50,000 bytes for the embedded patch diff, 8,192 bytes per visible test
stream, 8,192 bytes for the issue body, 200 trace events, and 2,048 bytes per pack-task failure
message. These limits keep reports useful for debugging without turning them into an unrestricted
data export surface.
