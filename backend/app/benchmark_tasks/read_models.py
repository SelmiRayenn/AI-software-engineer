from app.models import BenchmarkTask, Repository
from app.schemas.benchmark_task import AgentVisibleBenchmarkTaskRead


def to_agent_visible_task(
    task: BenchmarkTask,
    repository: Repository | None = None,
) -> AgentVisibleBenchmarkTaskRead:
    return AgentVisibleBenchmarkTaskRead(
        id=task.id,
        repository_id=task.repository_id,
        repository=repository or task.repository,
        issue_number=task.issue_number,
        issue_title=task.issue_title,
        issue_body=task.issue_body,
        issue_comments=task.issue_comments,
        base_commit=task.base_commit,
        status=task.status,
        created_at=task.created_at,
    )
