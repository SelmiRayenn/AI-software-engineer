from dataclasses import dataclass

from sqlalchemy.orm import Session

from app import crud
from app.benchmark_tasks.gold_solution import GoldSolutionStorage
from app.core.task_statuses import TASK_STATUS_READY
from app.github import GitHubClientError, GitHubService, parse_github_repo_url
from app.models import BenchmarkTask, GoldPatch, Repository
from app.schemas.benchmark_task import BenchmarkTaskFromGitHubRequest
from app.schemas.repository import RepositoryCreate


@dataclass(frozen=True)
class BenchmarkTaskCreationResult:
    task: BenchmarkTask
    repository: Repository
    gold_patch: GoldPatch


class BenchmarkTaskCreationError(RuntimeError):
    pass


class GitHubBenchmarkTaskCreator:
    def __init__(
        self,
        db: Session,
        github_service: GitHubService,
        gold_storage: GoldSolutionStorage | None = None,
    ) -> None:
        self._db = db
        self._github_service = github_service
        self._gold_storage = gold_storage or GoldSolutionStorage()

    def create_from_github(
        self,
        request: BenchmarkTaskFromGitHubRequest,
    ) -> BenchmarkTaskCreationResult:
        repo_ref = parse_github_repo_url(request.repository_url)
        repository_data = self._github_service.fetch_repository_metadata(repo_ref)
        issue_data = self._github_service.fetch_issue(repo_ref, request.issue_number)
        comments_data = self._github_service.fetch_issue_comments(repo_ref, request.issue_number)
        pr_data = self._github_service.fetch_pull_request_metadata(
            repo_ref,
            request.pull_request_number,
        )
        files_data = self._github_service.fetch_pull_request_files(
            repo_ref,
            request.pull_request_number,
        )
        commits_data = self._github_service.fetch_pull_request_commits(
            repo_ref,
            request.pull_request_number,
        )
        pull_request = self._github_service.pull_request_preview_from_data(
            pr_data,
            files_data,
            commits_data,
        )
        if not pull_request.merged and not pull_request.merged_at:
            raise BenchmarkTaskCreationError(
                "Pull request must be merged to create a benchmark task"
            )

        repository_preview = self._github_service.repository_preview_from_data(repository_data)
        issue_preview = self._github_service.issue_preview_from_data(issue_data)
        comment_previews = [
            self._github_service.comment_preview_from_data(comment_data)
            for comment_data in comments_data
        ]
        fix_commit = request.fix_commit or self._github_service.fix_commit_from_pull_request(
            pull_request
        )
        if not fix_commit:
            raise BenchmarkTaskCreationError(
                "fix_commit could not be derived from pull request data"
            )

        patch_text = self._pull_request_diff(repo_ref, request.pull_request_number, files_data)
        changed_files = [file.filename for file in pull_request.files]
        test_files = self._github_service.test_files_from_pull_request_files(pull_request.files)

        repository = self._get_or_create_repository(repository_preview)
        task = BenchmarkTask(
            repository_id=repository.id,
            issue_number=issue_preview.number,
            issue_title=issue_preview.title,
            issue_body=issue_preview.body,
            issue_comments=[
                {
                    "body": comment.body,
                    "html_url": comment.html_url,
                    "user_login": comment.user.login if comment.user else None,
                    "created_at": comment.created_at.isoformat() if comment.created_at else None,
                    "updated_at": comment.updated_at.isoformat() if comment.updated_at else None,
                }
                for comment in comment_previews
            ],
            pull_request_number=request.pull_request_number,
            base_commit=request.base_commit,
            fix_commit=fix_commit,
            linked_pr_url=pull_request.html_url,
            setup_commands=request.setup_commands,
            test_commands=request.test_commands,
            notes=request.notes,
            allow_lockfile_changes=request.allow_lockfile_changes,
            allow_dependency_file_changes=request.allow_dependency_file_changes,
            status=TASK_STATUS_READY,
        )
        self._db.add(task)
        self._db.flush()
        gold_patch = self._gold_storage.create_gold_patch(
            db=self._db,
            benchmark_task_id=task.id,
            changed_files=changed_files,
            patch_text=patch_text,
            test_files=test_files,
        )
        self._db.commit()
        self._db.refresh(repository)
        self._db.refresh(task)
        self._db.refresh(gold_patch)
        return BenchmarkTaskCreationResult(task=task, repository=repository, gold_patch=gold_patch)

    def _get_or_create_repository(self, repository_preview) -> Repository:
        repository = crud.get_repository_by_owner_name(
            self._db,
            owner=repository_preview.owner,
            name=repository_preview.name,
        )
        if repository is not None:
            return repository

        repository = Repository(
            **RepositoryCreate(
                name=repository_preview.name,
                owner=repository_preview.owner,
                url=repository_preview.url or repository_preview.html_url,
                default_branch=repository_preview.default_branch,
                language=repository_preview.language,
            ).model_dump()
        )
        self._db.add(repository)
        self._db.flush()
        return repository

    def _pull_request_diff(
        self,
        repo_ref,
        pull_request_number: int,
        files_data: list[dict],
    ) -> str:
        try:
            diff = self._github_service.fetch_pull_request_diff(repo_ref, pull_request_number)
        except GitHubClientError:
            diff = ""

        if diff.strip():
            return diff

        file_patches = []
        for file_data in files_data:
            filename = str(file_data.get("filename") or "")
            patch = file_data.get("patch")
            if filename and patch:
                file_patches.append(f"diff --git a/{filename} b/{filename}\n{patch}")
        return "\n".join(file_patches)
