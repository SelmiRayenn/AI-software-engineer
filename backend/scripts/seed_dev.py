from sqlalchemy import select

from app.db.init_db import init_db
from app.db.session import SessionLocal
from app.models import AgentRun, BenchmarkTask, GoldPatch, Repository


def seed() -> None:
    init_db()

    with SessionLocal() as db:
        repository = db.scalar(
            select(Repository).where(Repository.url == "https://github.com/example/calculator")
        )

        if repository is None:
            repository = Repository(
                name="calculator",
                owner="example",
                url="https://github.com/example/calculator",
                default_branch="main",
                language="Python",
            )
            db.add(repository)
            db.flush()

        task = db.scalar(
            select(BenchmarkTask).where(
                BenchmarkTask.repository_id == repository.id,
                BenchmarkTask.issue_number == 42,
            )
        )

        if task is None:
            task = BenchmarkTask(
                repository_id=repository.id,
                issue_number=42,
                issue_title="Fix division by zero handling in calculator",
                issue_body=(
                    "The divide function raises an unhelpful exception when the divisor is zero. "
                    "Return a clear validation error instead."
                ),
                issue_comments=[],
                pull_request_number=43,
                base_commit="1111111111111111111111111111111111111111",
                fix_commit="2222222222222222222222222222222222222222",
                linked_pr_url="https://github.com/example/calculator/pull/43",
                setup_commands=["python -m pip install -e ."],
                test_commands=["pytest"],
                notes="Seed task for local development.",
                status="ready",
            )
            db.add(task)
            db.flush()

        if task.gold_patch is None:
            db.add(
                GoldPatch(
                    benchmark_task_id=task.id,
                    changed_files=["calculator/core.py"],
                    patch_text=(
                        "diff --git a/calculator/core.py b/calculator/core.py\n"
                        "--- a/calculator/core.py\n"
                        "+++ b/calculator/core.py\n"
                        "@@ -1,2 +1,4 @@\n"
                        " def divide(a, b):\n"
                        "+    if b == 0:\n"
                        "+        raise ValueError('divisor must not be zero')\n"
                        "     return a / b\n"
                    ),
                    test_files=["tests/test_core.py"],
                )
            )

        run = db.scalar(
            select(AgentRun).where(
                AgentRun.benchmark_task_id == task.id,
                AgentRun.model_provider == "openai",
                AgentRun.model_name == "placeholder-model",
            )
        )

        if run is None:
            db.add(
                AgentRun(
                    benchmark_task_id=task.id,
                    model_provider="openai",
                    model_name="placeholder-model",
                    status="queued",
                )
            )

        db.commit()

    print("Seeded one development repository, benchmark task, gold patch, and agent run.")


if __name__ == "__main__":
    seed()
