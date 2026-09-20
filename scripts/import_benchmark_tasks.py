from __future__ import annotations

import argparse
import sys
from pathlib import Path
from uuid import UUID

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.benchmark_imports import BenchmarkImportService
from app.db.session import SessionLocal


def main() -> int:
    parser = argparse.ArgumentParser(description="Import trusted benchmark JSON/JSONL as drafts.")
    parser.add_argument("file", type=Path)
    parser.add_argument("--format", choices=("auto", "json", "jsonl"), default="auto")
    parser.add_argument("--pack-id", type=UUID, help="Append newly imported tasks to this pack.")
    args = parser.parse_args()
    with SessionLocal() as db:
        result = BenchmarkImportService(db).import_file(
            args.file, format=args.format, pack_id=args.pack_id
        )
    print(result.model_dump_json(indent=2))
    return 1 if result.failed_count else 0


if __name__ == "__main__":
    raise SystemExit(main())
