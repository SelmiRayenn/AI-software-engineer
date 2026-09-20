from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.core.task_statuses import VALID_TASK_STATUSES
from app.github import parse_github_repo_url
from app.models import BenchmarkPack, BenchmarkPackTask, BenchmarkTask
from app.schemas.benchmark_validation import (
    BenchmarkPackValidationReport,
    BenchmarkPackValidationStatistics,
    BenchmarkTaskValidationReport,
    BenchmarkTaskValidationStatistics,
    ValidationReportItem,
    ValidationReportSummary,
)


class BenchmarkTaskValidationNotFound(RuntimeError):
    pass


class BenchmarkPackValidationNotFound(RuntimeError):
    pass


class BenchmarkValidationReportService:
    def __init__(self, db: Session) -> None:
        self._db = db

    def task_report(self, task_id: UUID) -> BenchmarkTaskValidationReport:
        task = self._db.scalar(
            select(BenchmarkTask)
            .where(BenchmarkTask.id == task_id)
            .options(
                selectinload(BenchmarkTask.repository),
                selectinload(BenchmarkTask.gold_patch),
                selectinload(BenchmarkTask.hidden_eval_tests),
                selectinload(BenchmarkTask.import_record),
                selectinload(BenchmarkTask.pack_memberships).selectinload(
                    BenchmarkPackTask.benchmark_pack
                ),
            )
        )
        if task is None:
            raise BenchmarkTaskValidationNotFound("Benchmark task not found.")
        return build_benchmark_task_validation_report(task)

    def pack_report(self, pack_id: UUID) -> BenchmarkPackValidationReport:
        pack = self._db.scalar(
            select(BenchmarkPack)
            .where(BenchmarkPack.id == pack_id)
            .options(
                selectinload(BenchmarkPack.task_memberships)
                .selectinload(BenchmarkPackTask.benchmark_task)
                .selectinload(BenchmarkTask.repository),
                selectinload(BenchmarkPack.task_memberships)
                .selectinload(BenchmarkPackTask.benchmark_task)
                .selectinload(BenchmarkTask.gold_patch),
                selectinload(BenchmarkPack.task_memberships)
                .selectinload(BenchmarkPackTask.benchmark_task)
                .selectinload(BenchmarkTask.hidden_eval_tests),
            )
        )
        if pack is None:
            raise BenchmarkPackValidationNotFound("Benchmark pack not found.")
        return build_benchmark_pack_validation_report(pack)


def build_benchmark_task_validation_report(
    task: BenchmarkTask,
) -> BenchmarkTaskValidationReport:
    items: list[ValidationReportItem] = []
    imported = task.import_record is not None
    repository = task.repository
    if repository is None:
        _item(
            items,
            "error",
            "repository_missing",
            "The task does not reference an available repository record.",
            "Restore or assign the repository before running the task.",
        )
    elif not isinstance(repository.url, str) or not repository.url.strip():
        _item(
            items,
            "error",
            "repository_url_missing",
            "The repository URL is missing.",
            "Set a public HTTPS GitHub repository URL.",
        )
    else:
        try:
            parse_github_repo_url(repository.url)
        except ValueError:
            _item(
                items,
                "error",
                "repository_url_invalid",
                "The repository URL is not a valid GitHub repository URL.",
                "Use an HTTPS github.com owner/repository URL.",
            )

    if not _nonblank(task.base_commit):
        _item(
            items,
            "error",
            "base_commit_missing",
            "The task has no base commit.",
            "Set the exact repository commit that the agent must start from.",
        )
    if not _nonblank(task.issue_title) and not _nonblank(task.issue_body):
        _item(
            items,
            "error",
            "problem_statement_missing",
            "The task has no issue title or problem statement.",
            "Provide a concise title and enough issue context to reproduce the problem.",
        )
    elif not _nonblank(task.issue_body):
        _item(
            items,
            "warning",
            "problem_statement_body_missing",
            "Only an issue title is available; the detailed problem statement is empty.",
            "Add reproduction details and expected behavior to the issue body.",
        )

    _validate_task_commands(items, task.setup_commands, "setup")
    _validate_task_commands(items, task.test_commands, "test")

    gold_expected = not imported or task.status in {"ready", "running", "completed", "failed"}
    if task.gold_patch is None:
        _item(
            items,
            "error" if gold_expected else "warning",
            "gold_patch_missing",
            (
                "The task requires a trusted gold patch but none is stored."
                if gold_expected
                else "The imported draft did not include a trusted gold patch."
            ),
            "Attach a verified gold patch before marking the task ready for evaluation.",
        )
    else:
        if not isinstance(task.gold_patch.changed_files, list) or not task.gold_patch.changed_files:
            _item(
                items,
                "error",
                "gold_changed_files_missing",
                "The gold patch has no changed-file metadata.",
                "Re-import or rebuild the gold patch from the verified fix.",
            )
        if not _nonblank(task.gold_patch.patch_text):
            _item(
                items,
                "warning",
                "gold_patch_empty",
                "The gold patch text is empty, limiting patch comparison.",
                "Store the verified unified diff for complete evaluation.",
            )

    hidden_tests = list(task.hidden_eval_tests or [])
    enabled_hidden_tests = [test for test in hidden_tests if test.enabled]
    executable_hidden_tests = [test for test in hidden_tests if _executable_hidden_test(test)]
    hidden_expected = bool(task.gold_patch and task.gold_patch.test_files)
    if hidden_expected and not hidden_tests:
        _item(
            items,
            "warning",
            "hidden_tests_missing",
            "The gold patch identifies test files, but no hidden evaluation suite is stored.",
            "Create a hidden evaluation suite from verified issue-specific tests.",
        )
    elif hidden_tests and not enabled_hidden_tests:
        _item(
            items,
            "warning",
            "hidden_tests_disabled",
            "Hidden evaluation metadata exists, but no hidden suite is enabled.",
            "Review the trusted payload and enable an executable hidden suite when safe.",
        )
    elif enabled_hidden_tests and not executable_hidden_tests:
        _item(
            items,
            "warning",
            "hidden_tests_not_executable",
            "Hidden evaluation suites are enabled, but none has valid commands.",
            "Configure trusted commands before relying on hidden-test coverage.",
        )
    elif executable_hidden_tests:
        _item(
            items,
            "info",
            "hidden_tests_available",
            f"{len(executable_hidden_tests)} executable hidden evaluation suite(s) are available.",
            "Keep hidden suites private and run them only through trusted evaluation paths.",
        )

    if task.status not in VALID_TASK_STATUSES:
        _item(
            items,
            "error",
            "task_status_invalid",
            "The task has an unsupported lifecycle status.",
            "Move the task to a defined lifecycle status.",
        )
    elif task.status == "draft":
        _item(
            items,
            "warning",
            "task_status_draft",
            "The task is still a draft and will not be selected for execution.",
            "Resolve blocking items, validate the task, then mark it ready.",
        )
    elif task.status == "failed":
        _item(
            items,
            "warning",
            "task_status_failed",
            "The task is in failed status and will not be selected for pack execution.",
            "Review the failure, repair the task definition, and revalidate it.",
        )
    elif task.status == "archived":
        _item(
            items,
            "info",
            "task_status_archived",
            "The task is archived and intentionally unavailable for new runs.",
            "Create or restore a non-archived task version if it should run again.",
        )

    if imported:
        _validate_import_metadata(items, task)

    memberships = list(task.pack_memberships or [])
    pack_ids = [membership.benchmark_pack_id for membership in memberships]
    if len(pack_ids) != len(set(pack_ids)):
        _item(
            items,
            "error",
            "duplicate_pack_membership",
            "The task appears more than once in the same benchmark pack.",
            "Keep one membership per task and pack.",
        )
    for membership in memberships:
        if membership.benchmark_pack is None:
            _item(
                items,
                "error",
                "pack_membership_orphaned",
                "A pack membership references a missing benchmark pack.",
                "Remove the orphaned membership or restore the pack.",
            )
        if membership.order_index is None or membership.order_index < 0:
            _item(
                items,
                "error",
                "pack_membership_order_invalid",
                "A pack membership has an invalid order index.",
                "Assign a unique non-negative order index within the pack.",
            )
        if not isinstance(membership.tags, list):
            _item(
                items,
                "error",
                "pack_membership_tags_invalid",
                "A pack membership has invalid tag metadata.",
                "Store tags as a list of non-empty strings.",
            )
    if memberships:
        _item(
            items,
            "info",
            "pack_memberships_present",
            f"The task belongs to {len(memberships)} benchmark pack(s).",
            "Keep pack versions stable when task definitions change.",
        )

    if task.status == "ready" and any(item.severity == "error" for item in items):
        _item(
            items,
            "error",
            "ready_status_inconsistent",
            "The task is marked ready despite blocking validation errors.",
            "Move it to draft or failed until all blocking items are fixed.",
        )

    return BenchmarkTaskValidationReport(
        subject_id=task.id,
        overall_status=_overall_status(items),
        items=items,
        summary=_summary(items),
        statistics=BenchmarkTaskValidationStatistics(
            pack_membership_count=len(memberships),
            hidden_test_count=len(hidden_tests),
            enabled_hidden_test_count=len(enabled_hidden_tests),
            imported=imported,
        ),
    )


def build_benchmark_pack_validation_report(pack: BenchmarkPack) -> BenchmarkPackValidationReport:
    items: list[ValidationReportItem] = []
    memberships = list(pack.task_memberships or [])
    if not memberships:
        _item(
            items,
            "error",
            "pack_empty",
            "The benchmark pack has no tasks.",
            "Add validated benchmark tasks before running the pack.",
        )
    order_counts = Counter(membership.order_index for membership in memberships)
    duplicate_orders = sorted(index for index, count in order_counts.items() if count > 1)
    if duplicate_orders:
        _item(
            items,
            "error",
            "duplicate_order_indexes",
            f"The pack contains {len(duplicate_orders)} duplicate order index value(s).",
            "Assign every task a unique order index.",
        )
    if any(
        membership.order_index is None or membership.order_index < 0 for membership in memberships
    ):
        _item(
            items,
            "error",
            "invalid_order_indexes",
            "One or more pack tasks have invalid order indexes.",
            "Use unique non-negative order indexes.",
        )

    tasks = [membership.benchmark_task for membership in memberships if membership.benchmark_task]
    missing_task_count = len(memberships) - len(tasks)
    if missing_task_count:
        _item(
            items,
            "error",
            "pack_tasks_missing",
            f"{missing_task_count} pack membership(s) reference missing tasks.",
            "Remove orphaned memberships or restore the referenced tasks.",
        )
    task_ids = [task.id for task in tasks]
    if len(task_ids) != len(set(task_ids)):
        _item(
            items,
            "error",
            "duplicate_tasks",
            "The pack contains the same benchmark task more than once.",
            "Keep each task only once in a pack.",
        )

    statuses = Counter(task.status for task in tasks)
    ready_count = statuses["ready"]
    if memberships and ready_count == 0:
        _item(
            items,
            "error",
            "pack_has_no_ready_tasks",
            "The pack contains no ready tasks.",
            "Resolve task validation issues and mark at least one task ready.",
        )
    elif memberships:
        _item(
            items,
            "info",
            "ready_task_count",
            f"{ready_count} of {len(memberships)} pack task(s) are ready.",
            "Run only the saved ready-task roster for reproducible comparisons.",
        )
    non_ready = sum(statuses[status] for status in ("draft", "failed", "archived"))
    if non_ready:
        _item(
            items,
            "warning",
            "non_ready_tasks_present",
            (
                f"The pack contains {statuses['draft']} draft, {statuses['failed']} failed, "
                f"and {statuses['archived']} archived task(s)."
            ),
            "Publish a pack version containing only tasks intended for execution.",
        )
    unknown_status_count = sum(
        count for status, count in statuses.items() if status not in VALID_TASK_STATUSES
    )
    if unknown_status_count:
        _item(
            items,
            "error",
            "pack_task_status_invalid",
            f"{unknown_status_count} task(s) have unsupported lifecycle statuses.",
            "Correct task statuses before running the pack.",
        )

    missing_repository_count = sum(task.repository is None for task in tasks)
    invalid_repository_count = sum(
        task.repository is not None and not _valid_repository_url(task.repository.url)
        for task in tasks
    )
    if missing_repository_count:
        _item(
            items,
            "error",
            "pack_repositories_missing",
            f"{missing_repository_count} task(s) reference missing repositories.",
            "Restore or reassign each missing repository record.",
        )
    if invalid_repository_count:
        _item(
            items,
            "error",
            "pack_repository_urls_invalid",
            f"{invalid_repository_count} task(s) have invalid repository URLs.",
            "Use public HTTPS GitHub owner/repository URLs.",
        )

    missing_setup = sum(not _configured_commands(task.setup_commands) for task in tasks)
    missing_tests = sum(not _configured_commands(task.test_commands) for task in tasks)
    if missing_setup:
        _item(
            items,
            "warning",
            "pack_setup_commands_missing",
            f"{missing_setup} task(s) have no valid setup commands.",
            "Record deterministic environment setup commands for every task.",
        )
    if missing_tests:
        _item(
            items,
            "error",
            "pack_test_commands_missing",
            f"{missing_tests} task(s) have no valid visible test commands.",
            "Configure deterministic baseline and post-patch test commands.",
        )

    missing_gold = sum(task.gold_patch is None for task in tasks if task.status == "ready")
    if missing_gold:
        _item(
            items,
            "error",
            "pack_ready_tasks_missing_gold",
            f"{missing_gold} ready task(s) have no gold patch.",
            "Move those tasks out of ready status or attach verified gold patches.",
        )

    difficulty_count = sum(
        _nonblank(membership.difficulty or membership.benchmark_task.difficulty)
        and (membership.difficulty or membership.benchmark_task.difficulty) != "unknown"
        for membership in memberships
    )
    tag_count = sum(
        any(_nonblank(tag) for tag in (membership.tags or membership.benchmark_task.tags))
        for membership in memberships
    )
    if memberships and difficulty_count < len(memberships):
        _item(
            items,
            "warning",
            "difficulty_coverage_incomplete",
            f"{len(memberships) - difficulty_count} task(s) have no difficulty label.",
            "Assign a consistent difficulty label to every pack task.",
        )
    if memberships and tag_count < len(memberships):
        _item(
            items,
            "warning",
            "tag_coverage_incomplete",
            f"{len(memberships) - tag_count} task(s) have no tags.",
            "Tag every task for repository, language, and issue-category analysis.",
        )

    hidden_test_count = sum(
        any(_executable_hidden_test(test) for test in task.hidden_eval_tests or [])
        for task in tasks
    )
    if tasks and hidden_test_count < len(tasks):
        _item(
            items,
            "warning",
            "hidden_test_coverage_incomplete",
            f"{len(tasks) - hidden_test_count} task(s) have no enabled hidden evaluation suite.",
            "Add verified hidden tests or document why visible-only evaluation is acceptable.",
        )

    task_count = len(memberships)
    repository_count = len({task.repository_id for task in tasks if task.repository is not None})
    return BenchmarkPackValidationReport(
        subject_id=pack.id,
        overall_status=_overall_status(items),
        items=items,
        summary=_summary(items),
        statistics=BenchmarkPackValidationStatistics(
            task_count=task_count,
            ready_task_count=ready_count,
            draft_task_count=statuses["draft"],
            running_task_count=statuses["running"],
            completed_task_count=statuses["completed"],
            failed_task_count=statuses["failed"],
            archived_task_count=statuses["archived"],
            unknown_status_task_count=unknown_status_count,
            repository_count=repository_count,
            hidden_test_task_count=hidden_test_count,
            hidden_test_coverage=_coverage(hidden_test_count, len(tasks)),
            difficulty_coverage=_coverage(difficulty_count, task_count),
            tag_coverage=_coverage(tag_count, task_count),
            missing_setup_command_count=missing_setup,
            missing_test_command_count=missing_tests,
        ),
    )


def _validate_task_commands(items: list[ValidationReportItem], commands: object, kind: str) -> None:
    field = f"{kind}_commands"
    if not isinstance(commands, list):
        _item(
            items,
            "error",
            f"{field}_invalid",
            f"The task's {field} value is not a list.",
            f"Store {field} as a list of explicit commands.",
        )
    elif not commands:
        _item(
            items,
            "warning" if kind == "setup" else "error",
            f"{field}_missing",
            f"The task has no configured {kind} commands.",
            (
                "Record deterministic setup commands, or document that no setup is required."
                if kind == "setup"
                else "Configure at least one deterministic visible test command."
            ),
        )
    elif not all(_nonblank(command) for command in commands):
        _item(
            items,
            "error",
            f"{field}_invalid",
            f"The task's {field} contain blank or non-text values.",
            f"Keep only explicit non-empty {kind} commands.",
        )


def _validate_import_metadata(items: list[ValidationReportItem], task: BenchmarkTask) -> None:
    record = task.import_record
    if record.benchmark_task_id != task.id or not _nonblank(record.task_id):
        _item(
            items,
            "error",
            "import_metadata_inconsistent",
            "The trusted benchmark import metadata is incomplete or linked incorrectly.",
            "Re-import the source record with a unique task_id.",
        )
    if record.environment_setup_commit is not None and not _nonblank(
        record.environment_setup_commit
    ):
        _item(
            items,
            "error",
            "import_setup_commit_invalid",
            "The imported environment setup commit is blank.",
            "Store a valid commit identifier or remove the optional value.",
        )
    if not _nonblank(task.issue_body):
        _item(
            items,
            "error",
            "import_problem_statement_missing",
            "The imported task is missing its original problem statement.",
            "Re-import the record with a non-empty problem_statement.",
        )
    for hidden_test in task.hidden_eval_tests or []:
        metadata = hidden_test.evaluation_metadata
        if metadata is not None and (
            not isinstance(metadata, dict)
            or any(
                key not in {"fail_to_pass", "pass_to_pass"}
                or not isinstance(value, list)
                or any(not _nonblank(item) for item in value)
                for key, value in metadata.items()
            )
        ):
            _item(
                items,
                "error",
                "import_evaluation_metadata_invalid",
                "Imported hidden-evaluation metadata is malformed.",
                "Re-import fail_to_pass and pass_to_pass as lists of test identifiers.",
            )


def _item(
    items: list[ValidationReportItem],
    severity: str,
    code: str,
    message: str,
    recommendation: str,
) -> None:
    items.append(
        ValidationReportItem(
            severity=severity, code=code, message=message, recommendation=recommendation
        )
    )


def _summary(items: Iterable[ValidationReportItem]) -> ValidationReportSummary:
    items = list(items)
    counts = Counter(item.severity for item in items)
    return ValidationReportSummary(
        total_count=len(items),
        info_count=counts["info"],
        warning_count=counts["warning"],
        error_count=counts["error"],
    )


def _overall_status(items: Iterable[ValidationReportItem]) -> str:
    severities = {item.severity for item in items}
    if "error" in severities:
        return "blocked"
    if "warning" in severities:
        return "warning"
    return "ready"


def _configured_commands(commands: object) -> bool:
    return (
        isinstance(commands, list) and bool(commands) and all(_nonblank(item) for item in commands)
    )


def _executable_hidden_test(hidden_test: object) -> bool:
    return bool(getattr(hidden_test, "enabled", False)) and _configured_commands(
        getattr(hidden_test, "commands", None)
    )


def _nonblank(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _valid_repository_url(value: object) -> bool:
    if not _nonblank(value):
        return False
    try:
        parse_github_repo_url(value)
    except ValueError:
        return False
    return True


def _coverage(count: int, total: int) -> float:
    return round(count / total, 4) if total else 0.0
