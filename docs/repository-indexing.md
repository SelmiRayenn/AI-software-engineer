# Repository Indexing

The backend creates a deterministic file and Python-symbol index of a prepared AgentRun checkout.
It uses the filesystem, Python's standard-library AST parser, and SQL substring search. Ordinary
indexing and lexical retrieval require no model calls. Optional chunk embeddings add semantic
retrieval without importing or executing repository code.

## Database Setup

Apply migrations from `backend/`:

```powershell
python scripts/apply_migrations.py
```

Migration `20260905_0002` follows the existing initial migration and creates:

- `repository_indexes`: one snapshot per run, workspace ID, index version, timestamps, file count,
  total indexed bytes, manifest checksum, and counts of skipped entries by reason.
- `indexed_files`: relative POSIX path, extension, byte size, language guess, file type, preview,
  searchable content, SHA-256 checksum, Python imports, and parse-error flag.
- `indexed_symbols`: top-level Python function, async function, class, and simple assignment names,
  with kind and start/end line numbers. Methods and nested symbols are not indexed separately.

Foreign keys cascade from run to index to files to symbols. Re-indexing replaces files and symbols
in one transaction, retaining the index ID. PostgreSQL locks the run row during indexing to serialize
concurrent rebuilds. A failed rebuild preserves the previous committed snapshot.

Migration `20260905_0003` adds `indexed_chunks` and `chunk_embeddings`. Chunks reference indexed
files and retain their ordinal, text, line range, symbols, and checksum. Each chunk has at most one
embedding with provider, model, dimensions, vector, and an input checksum. Vectors use JSON columns
compatible with PostgreSQL and SQLite; no vector database extension is required. Rebuilding the
file index cascades deletion of its old chunks and vectors so they cannot serve stale results.

## Workspace Requirements

Indexing resolves the run's stored workspace ID and path. The expected layout is:

```text
<SANDBOX_WORKSPACE_ROOT>/agent-run-<workspace-id>/repo
<SANDBOX_WORKSPACE_ROOT>/agent-run-<workspace-id>/.sandbox-workspace.json
```

The path must match the run ID recorded in the workspace metadata (the workspace ID, not the database
run UUID). The indexer validates metadata, root containment, and path components. The API accepts
only a run ID; clients cannot supply a filesystem path. Legacy arbitrary workspace paths are rejected.

The orchestrator creates an initial index after setup and baseline tests, before the agent loop. The
stored snapshot remains searchable after default workspace cleanup. To rebuild an index after the
synchronous run endpoint returns, set `SANDBOX_RETAIN_WORKSPACES=true` **before starting the run**
and restart the backend to load the setting. A snapshot cannot be rebuilt after its checkout is
removed.

## API

Create or rebuild the snapshot (no request body):

```text
POST /agent-runs/{run_id}/index
```

The response is a summary with `id`, `agent_run_id`, `workspace_id`, `index_version`, `indexed_at`,
`created_at`, `file_count`, `total_size_bytes`, `checksum`, and `skipped_counts`. Creation and rebuild
both return HTTP 200. Skipped counts contain reasons and counts only, never protected paths or text.

List files or search the snapshot:

```text
GET /agent-runs/{run_id}/index/files?limit=100&offset=0
GET /agent-runs/{run_id}/index/search?q=calculate
GET /agent-runs/{run_id}/index/search?q=src/calculator.py&field=path
GET /agent-runs/{run_id}/index/search?q=division&field=text
GET /agent-runs/{run_id}/index/search?q=Calculator&field=symbol
```

`field` defaults to `all` (path, full stored text, or symbol). Queries are literal case-insensitive
substrings using database `lower()` semantics; `%` and `_` are escaped, not wildcards. Results are
files, ordered by path, without duplicate rows when multiple symbols match. Query length is 1-200
characters, `limit` is 1-200 (default 100), and `offset` is non-negative. Whitespace-only queries are
rejected. Unicode case folding can differ between PostgreSQL and SQLite.

List/search responses contain file metadata, imports, symbols, and a preview of the first 500
characters. Full searchable text is stored internally and is not serialized by these endpoints.
The preview always shows the start of the file, even when a match occurs later in its content.

## Agent Retrieval Tool

The controlled `retrieve_relevant_files` tool provides ranked file localization over the current
run's stored index. It accepts a `query`, optional `limit` (default 10, maximum 50), and optional
`semantic` (default false). It returns:

- file path, language, file kind, and byte size
- matching top-level Python symbols
- up to three matching line snippets, each capped at 240 characters
- relevance score, lexical score, and optional semantic score

Scoring adds weight for literal query/token matches in paths, symbols, and indexed text. Symbol
matches receive the highest weight. Test files receive a small boost for test/failure terms, while
source files receive a small boost for module/error/function terms. Ties are ordered by path, so an
unchanged index and query produce stable results. In lexical mode, files without a lexical match
are omitted. The response includes `retrieval_mode` and an optional `fallback_reason`.

The tool resolves the index by `agent_run_id`; callers cannot provide another index or filesystem
path. It defensively filters reserved gold paths even if malformed protected rows already exist.
Every invocation emits an `agent_tool_call` event with sanitized arguments, status, duration,
result count, and returned paths. Snippet contents are not copied into the tool event.

Example from PowerShell after retaining a run's workspace:

```powershell
$runId = "<agent-run-uuid>"
Invoke-RestMethod -Method Post -Uri "http://localhost:8000/agent-runs/$runId/index"
Invoke-RestMethod -Uri "http://localhost:8000/agent-runs/$runId/index/files"
Invoke-RestMethod -Uri "http://localhost:8000/agent-runs/$runId/index/search?q=calculate&field=symbol"
```

Missing runs or indexes return 404. Missing workspace/metadata or an unstable checkout returns 409.
Invalid queries, workspace identity/path violations, and repository limits return 422.

## Extraction And Limits

Traversal and result ordering are deterministic. File checksums hash raw bytes with SHA-256. The
snapshot checksum hashes the sorted JSON list of `(path, checksum)` pairs. Identical accepted files
produce identical checksums; generated row UUIDs and timestamps are not deterministic. Index version
1 identifies this extraction policy.

Language guesses use file extensions. `file_type` is `test`, `source`, `docs`, `config`, or `other`.
Test names/directories take priority, then configuration, then documentation. Python imports include
nested imports in source order and are normalized with `ast.unparse`. Python encoding declarations
are honored; other text must decode as UTF-8. Syntax errors do not discard readable files: metadata
and text are stored with `python_parse_error=true`, without symbols or imports.

Service-side `IndexLimits` defaults:

| Limit | Default | Behavior |
| --- | --- | --- |
| Bytes per file | 256,000 | Skip oversized files |
| Total accepted bytes | 20,000,000 | Abort rebuild, preserve previous index |
| Indexed files | 10,000 | Abort rebuild, preserve previous index |
| Visited entries | 50,000 | Abort rebuild, preserve previous index |
| UTF-8 path bytes | 1,024 | Skip oversized paths |

Limits can be injected into `RepositoryIndexService` by backend code; HTTP clients cannot raise them.
File reads are bounded even if a file grows after its size check.

## Optional Embeddings

Apply migrations, create the run's index, then build embeddings explicitly (no request body):

```text
POST /agent-runs/{run_id}/index/embeddings
GET /agent-runs/{run_id}/index/search?q=withdraw+funds&semantic=true
```

The build uses the configured provider and returns chunk count, provider/model, dimensions, and
aggregate token/cost usage. It works from stored index content even after workspace cleanup. A
missing run/index returns 404, disabled/unconfigured provider returns 409, and provider failure
returns 502. Builds replace chunks and embeddings together; a failure preserves the previous
snapshot. A rebuild calls the provider again and can incur cost. This first version holds the run's
database lock during the synchronous build to serialize it against file-index rebuilds.

The search endpoint keeps its existing file-list response. `semantic=true` requires `field=all`
and orders files by hybrid relevance, with pagination applied after ranking. `semantic=false`
keeps the existing literal substring search. The controlled agent tool exposes scores and matching
snippets through:

```json
{"name": "retrieve_relevant_files", "arguments": {"query": "withdraw funds", "limit": 10, "semantic": true}}
```

Semantic retrieval embeds the query only when compatible stored vectors exist. It checks provider,
model, dimensions, chunk checksums, protected paths, and finite nonzero vector values. Missing or
incompatible embeddings, disabled real calls, and provider errors fall back to lexical ranking.
The tool reports `embeddings_unavailable`, `compatible_embeddings_unavailable`,
`embedding_provider_unavailable`, or `embedding_limit_exceeded` as its fallback reason. Plain
lexical calls do not instantiate or call an embedding provider.

For each file, semantic score is the highest positive cosine similarity among its chunks. Hybrid
score is `lexical_score + 10 * semantic_score`; files may match semantically without sharing query
words. Ties use path order. A semantic-only match includes the selected chunk's symbols and a
bounded snippet. These scores are ranking heuristics, not probabilities.

Configuration (also forwarded by Docker Compose):

| Setting | Default | Meaning |
| --- | --- | --- |
| `ENABLE_REAL_EMBEDDINGS` | `false` | Required before any real OpenAI embedding call |
| `EMBEDDINGS_PROVIDER` | `mock` | `mock`, `openai`, or `local` skeleton |
| `OPENAI_EMBEDDING_MODEL` | `text-embedding-3-small` | OpenAI model used for chunks and queries |
| `EMBEDDING_VECTOR_DIMENSIONS` | unset | Provider default; configurable from 1 to 4096 |
| `EMBEDDING_BATCH_SIZE` | `32` | Maximum texts per request, from 1 to 128 |
| `EMBEDDING_CHUNK_SIZE_CHARS` | `1000` | Maximum chunk characters, from 128 to 1500 |
| `EMBEDDING_CHUNK_OVERLAP_CHARS` | `100` | Overlap, smaller than chunk size and at most 500 |
| `EMBEDDING_AUTO_BUILD` | `false` | Build embeddings after managed indexing and before the agent loop |

With auto-build enabled, the existing index-build step also builds embeddings using the configured
provider. An embedding build failure then fails run preparation, preserving the error in the run
trace. Leaving auto-build disabled preserves ordinary lexical runs. The agent cannot create
embeddings or change provider settings through its tool arguments.

Chunking prefers line boundaries and splits long lines, preserving line numbers and overlapping
Python symbol context. Embedding inputs contain only the relative file path, bounded symbol names,
and chunk text. Files over 256,000 bytes, binary/control-byte content, ignored directories, unsafe
paths, environment files, and reserved gold paths are excluded again before embedding. Ordinary
source paths named by gold evaluation remain eligible; the gold solution store is never read.

Every request is limited to 8,000 UTF-8 bytes per input and 128,000 bytes in total, including path
and symbol context. Batches split by both byte budget and configured text count. Builds cap at
10,000 chunks and 2,000,000 vector values per run. Reduce dimensions or use a smaller repository
when the vector limit is reached. Existing vectors must be rebuilt after changing provider/model,
dimensions, or chunk settings. The current snapshot is not refreshed automatically after edits.

Successful builds and query calls store aggregate usage in `embedding_call_completed` events;
`repository_embeddings_created` records build totals. These events omit source text, queries,
vectors, keys, and raw provider errors. Embedding usage is separate from coding-model metrics.
Usage for earlier batches of a failed build rolls back with that build, even though a real provider
may have billed those requests. Consult provider billing for authoritative totals.

The default mock provider produces deterministic hashed token vectors, useful for plumbing tests
but not semantic quality. See [model providers](model-providers.md#embeddings-providers) for real
OpenAI setup. Cosine search scans bounded stored vectors in Python; this foundation has no
approximate-nearest-neighbor index, incremental watcher, or cross-run embedding cache.

## Safety And Current Limits

- Prunes `.git`, `.hg`, `.svn`, `node_modules`, `.venv`, `venv`, `__pycache__`, `.pytest_cache`,
  `.ruff_cache`, `.mypy_cache`, `.tox`, `dist`, and `build` at any depth, case-insensitively.
- Skips symlinks (including links to other in-workspace files), Windows junctions, hard-linked files,
  and non-regular files. POSIX file opens use directory descriptors and `O_NOFOLLOW`; Windows checks
  path components and file identity before returning read data.
- Skips NUL/control-byte binary content and unsupported encodings, regardless of extension.
- Excludes `.benchmark`, workspace metadata, `.env` files, and reserved gold names (`gold`,
  `gold_patch`, `gold_patches`, `gold_solution`, `benchmark_gold`, including hidden, hyphenated, and
  extension-bearing variants). `GoldPatch` is never queried or serialized. Gold data must remain in
  the evaluation store or reserved paths; arbitrary solution copies cannot be identified by content.
- Uses a fixed directory policy, not repository `.gitignore` interpretation. Existing ordinary
  source files remain indexable even if gold evaluation names the same source paths.
- Index a quiet checkout. This is not a filesystem snapshot or a transaction with agent edits;
  detected file mutation fails the rebuild. Re-index explicitly after editing.
- Lexical search stays available without embeddings. Optional semantic search scans bounded
  stored vectors; all modes remain scoped to the current run's index.

Tests use temporary managed workspaces and SQLite. They require no Docker daemon, network access,
or model credentials. Symbolic-link tests run when permitted by the operating system; Windows also
has a junction test. Migration checks cover empty/existing databases, downgrade, and metadata parity.
