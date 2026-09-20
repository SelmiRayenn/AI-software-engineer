# Benchmark Imports

Import SWE-bench-style task records from a UTF-8 JSON array or JSONL file. Importing is an
operator action: it writes gold solutions and hidden evaluation data. It does not fetch GitHub,
clone repositories, run commands, apply patches, or call models.

## Supported Record

```json
{
  "task_id": "example__calculator-42",
  "repo": "example/calculator",
  "repo_url": "https://github.com/example/calculator",
  "issue_number": 42,
  "problem_statement": "Division by zero should raise a clear error.",
  "base_commit": "0123456789abcdef0123456789abcdef01234567",
  "patch": "diff --git a/core.py b/core.py\n--- a/core.py\n+++ b/core.py\n@@ -1 +1 @@\n-old\n+fixed\n",
  "test_patch": "diff --git a/tests/test_core.py b/tests/test_core.py\n--- a/tests/test_core.py\n+++ b/tests/test_core.py\n@@ -1 +1 @@\n-old\n+assert True\n",
  "fail_to_pass": ["tests/test_core.py::test_zero"],
  "pass_to_pass": ["tests/test_core.py::test_division"],
  "environment_setup_commit": "abcdef0123456789abcdef0123456789abcdef01",
  "hints_text": "Optional curator context, withheld from agent prompts.",
  "created_at": "2024-01-01T12:00:00Z"
}
```

These repository names, commits and diffs are illustrative, not a runnable historical task.
For JSONL, put one complete JSON object on each line. For JSON, wrap records in an array.

Required fields are `task_id`, `problem_statement`, `base_commit`, and at least one of `repo`
or `repo_url`. All other fields above are optional and may be null. An absent issue number
is stored as null. Repository names use `owner/name`; URLs must be HTTPS GitHub repository
URLs without credentials, query parameters, or subpaths. Both fields must agree when supplied.
Names are normalized to lowercase and matching repository records are reused.

Task IDs are case-sensitive, globally unique import identifiers (up to 255 characters using
letters, digits, `.`, `_`, `/`, `-`, starting with a letter or digit). They are separate from
the platform's task UUIDs. `problem_statement` becomes the issue body; its first line, capped
at 500 characters, becomes the title. The source timestamp is preserved as
`BenchmarkImport.source_created_at`; platform `created_at` records the import time.

This is a defined SWE-bench-style subset, not a complete SWE-bench harness adapter. Unknown
fields are rejected. Translate `instance_id` to `task_id`, `FAIL_TO_PASS` to `fail_to_pass`,
and `PASS_TO_PASS` to `pass_to_pass` when adapting upstream records; JSON-encoded test lists
must be decoded into actual arrays first. Setup recipes and dataset-specific execution
environments are not inferred. Git commit existence and patch applicability require later
workspace verification.

## Script

Activate the backend environment, set `DATABASE_URL`, and run from `backend/` so its `.env`
configuration is loaded:

```sh
alembic upgrade head
python ../scripts/import_benchmark_tasks.py /path/to/tasks.jsonl
python ../scripts/import_benchmark_tasks.py /path/to/tasks.json --format json
python ../scripts/import_benchmark_tasks.py /path/to/tasks.jsonl --pack-id PACK_UUID
```

The script detects an array by the first non-whitespace `[`; otherwise it reads JSONL.
`--format jsonl` forces JSONL. It does not apply migrations automatically. Revision
`20260919_0009` adds import provenance, nullable issue numbers, and trusted hidden patch/metadata
storage. The script is a trusted backend operation using direct database credentials; it does
not require an HTTP operator token.

`--pack-id` assigns each newly imported task to an existing benchmark pack. The HTTP equivalent
is `POST /benchmark-imports?pack_id=PACK_UUID`. Successful rows append after the pack's highest
order index in source order. Failed and duplicate rows do not consume positions. An unknown pack
produces one document-level `pack_not_found` error and no rows are imported. See
[benchmark packs](benchmark-packs.md) for ordering, summaries, and concurrency behavior.

Standard output is a JSON summary. Exit code 0 means no failures (duplicates are allowed);
1 means one or more rows or the document failed; 2 indicates invalid command-line arguments.

## API

Set `TRUSTED_OPERATOR_TOKEN` in the backend and send it as `X-Operator-Token`. An unset token
disables the route (503); a missing or incorrect token returns 403. Send file contents directly,
not a filesystem path or multipart form:

```sh
curl -X POST http://localhost:8000/benchmark-imports \
  -H "X-Operator-Token: $TRUSTED_OPERATOR_TOKEN" \
  -H "Content-Type: application/x-ndjson" \
  --data-binary @tasks.jsonl
```

For an array file, use `Content-Type: application/json` and `--data-binary @tasks.json`.
Unsupported media types return 415. Bodies over 20 MiB return 413. Both entry points limit
imports to 10,000 records, reject non-UTF-8 text, and accept a UTF-8 BOM. Individual patch fields
are capped at 1,000,000 characters, problem statements and hints at 500,000 characters, and
test lists at 10,000 entries of at most 2,000 characters each.

## Summary and Transactions

```json
{
  "imported_count": 1,
  "skipped_count": 1,
  "failed_count": 1,
  "errors": [
    {"row": 2, "task_id": "example__calculator-42", "code": "duplicate_task_id",
     "message": "task_id already imported; existing data was left unchanged.", "outcome": "skipped"},
    {"row": 3, "task_id": "example__calculator-43", "code": "invalid_record",
     "message": "Invalid task record; check: base_commit.", "outcome": "failed"}
  ],
  "created_task_ids": ["11111111-1111-4111-8111-111111111111"]
}
```

Rows are one-based array positions or physical JSONL line numbers. Blank JSONL lines are ignored.
A document-level failure has `row: null`. Malformed JSONL lines and invalid rows are reported while
other rows continue; malformed JSON arrays fail as a whole before any writes. An empty array is
a successful no-op; an empty file is an error. HTTP 200 means a summary is available, not that
every row succeeded: always inspect `failed_count`.

Each row commits its repository, task, provenance, gold patch and hidden suite together. A failed
row rolls back without discarding earlier successes. Duplicate IDs, including repeated imports,
are skipped without overwriting data. A database uniqueness constraint also prevents concurrent
duplicates. Repository creation conflicts during concurrent imports are reported for retry.
Errors identify rows/IDs and safe field names without echoing payloads or database exception text.

## Trusted Data and Readiness

- `patch` is stored only in `GoldPatch.patch_text`; file paths are extracted from diff headers.
  Test paths from gold and hidden diffs populate `GoldPatch.test_files` when a gold patch exists.
- `test_patch`, `fail_to_pass`, or `pass_to_pass` creates a **disabled** `HiddenEvalTest`.
  The patch is stored in `patch_text`; the two lists are stored in `evaluation_metadata`.
  Test identifiers are data, never shell commands. No commands are generated from those lists.
- Hints and the environment setup commit are stored only in trusted import provenance. They
  are not appended to the problem statement, issue comments, or prompts.
- Task lists, run details, prompt context and prompt previews exclude gold patches, hidden
  payloads and trusted import metadata. The existing hidden-suite read route and gold-patch
  evaluation read route both require the operator token.

All imported tasks start in `draft` with empty setup/test commands. A curator must configure and
verify the environment and visible test commands before marking the task ready. Imported records
may omit a GitHub issue/PR; ordinary GitHub task validation keeps those requirements. The current
readiness checks still require a GoldPatch, so imports without one remain drafts.

The current hidden evaluator accepts text file payloads, not unified test patches. Imported
patch suites therefore remain disabled; execution rejects patch-bearing suites if they are
enabled directly in the database. To run them now, an operator must materialize the intended
tests against the correct base and create a runnable file-payload suite with explicit commands
through the trusted hidden-test endpoint. This importer does not execute upstream SWE-bench
test IDs or provision their environments automatically.

See [hidden evaluation tests](hidden-evaluation-tests.md) for isolation and execution limits.
