# Scripts

Scripts for local development, maintenance, and benchmark operations live here.

`import_benchmark_tasks.py` imports trusted JSON/JSONL records as draft tasks and prints a JSON
result summary. Activate the backend environment, apply migrations, and run from `backend/`:

```sh
python ../scripts/import_benchmark_tasks.py /path/to/tasks.jsonl
```

See [benchmark imports](../docs/benchmark-imports.md) for the format, access rules and exit codes.

Planned examples:

- Local smoke checks
- Benchmark fixture import
- Sandbox image preparation
- Metrics export
