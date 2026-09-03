from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from sqlalchemy import select

from app.db.init_db import init_db
from app.db.session import SessionLocal
from app.models import BenchmarkTask, GoldPatch, Repository

DATASET_PATH = ROOT / "benchmarks" / "manual_tasks.json"


def load_manual_benchmarks(dataset_path: Path = DATASET_PATH) -> None:
    init_db()
    tasks = json.loads(dataset_path.read_text(encoding="utf-8"))

    with SessionLocal() as db:
        loaded_count = 0
        for task_data in tasks:
            repository_data = task_data["repository"]
            repository = _get_or_create_repository(db, repository_data)
            task = _get_or_create_task(db, repository, task_data)
            _create_gold_patch_if_present(db, task, task_data.get("gold_patch"))
            loaded_count += 1

        db.commit()

    print(f"Loaded {loaded_count} manual benchmark task drafts from {dataset_path}.")


def _get_or_create_repository(db, repository_data: dict[str, Any]) -> Repository:
    repository = db.scalar(
        select(Repository).where(
            Repository.owner == repository_data["owner"],
            Repository.name == repository_data["name"],
        )
    )
    if repository is not None:
        return repository

    repository = Repository(
        owner=repository_data["owner"],
        name=repository_data["name"],
        url=repository_data["url"],
        default_branch=repository_data.get("default_branch", "main"),
        language=repository_data.get("language"),
    )
    db.add(repository)
    db.flush()
    return repository


def _get_or_create_task(
    db,
    repository: Repository,
    task_data: dict[str, Any],
) -> BenchmarkTask:
    task = db.scalar(
        select(BenchmarkTask).where(
            BenchmarkTask.repository_id == repository.id,
            BenchmarkTask.issue_title == task_data["issue_title"],
        )
    )
    if task is not None:
        return task

    task = BenchmarkTask(
        repository_id=repository.id,
        issue_number=task_data.get("issue_number") or 0,
        issue_title=task_data["issue_title"],
        issue_body=task_data.get("issue_body"),
        issue_comments=task_data.get("issue_comments", []),
        pull_request_number=task_data.get("pull_request_number"),
        base_commit=task_data.get("base_commit") or "",
        fix_commit=task_data.get("fix_commit"),
        linked_pr_url=task_data.get("linked_pr_url"),
        setup_commands=task_data.get("setup_commands", []),
        test_commands=task_data.get("test_commands", []),
        notes=task_data.get("notes"),
        status=task_data.get("status", "draft"),
    )
    db.add(task)
    db.flush()
    return task


def _create_gold_patch_if_present(
    db,
    task: BenchmarkTask,
    gold_patch_data: dict[str, Any] | None,
) -> None:
    if not gold_patch_data or task.gold_patch is not None:
        return

    db.add(
        GoldPatch(
            benchmark_task_id=task.id,
            changed_files=gold_patch_data.get("changed_files", []),
            patch_text=gold_patch_data.get("patch_text", ""),
            test_files=gold_patch_data.get("test_files", []),
        )
    )


if __name__ == "__main__":
    load_manual_benchmarks()
