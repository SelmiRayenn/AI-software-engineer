from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from statistics import fmean
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.agent_event import AgentEvent
from app.models.agent_run import AgentRun
from app.schemas.analytics import (
    CandidateHitRateByModel,
    FileLocalizationAnalytics,
    LocalizationByModel,
    LocalizationByRepository,
    MissedGoldFile,
)

_INSPECTION_TOOLS = {"read_file", "retrieve_relevant_files", "search_code"}


@dataclass(frozen=True)
class _RunLocalization:
    run: AgentRun
    gold_files: frozenset[str]
    inspected_files: tuple[str, ...]
    edited_files: frozenset[str]
    candidate_files: tuple[str, ...]

    @property
    def localization_score(self) -> float:
        return len(set(self.inspected_files) & self.gold_files) / len(self.gold_files)

    @property
    def edited_precision(self) -> float:
        if not self.edited_files:
            return 0.0
        return len(self.edited_files & self.gold_files) / len(self.edited_files)

    @property
    def edited_recall(self) -> float:
        return len(self.edited_files & self.gold_files) / len(self.gold_files)

    def top_k_hit(self, limit: int) -> bool:
        return bool(set(self.inspected_files[:limit]) & self.gold_files)

    def candidate_top_k_hit(self, limit: int) -> bool:
        return bool(set(self.candidate_files[:limit]) & self.gold_files)


def build_file_localization_analytics(
    db: Session, runs: list[AgentRun]
) -> FileLocalizationAnalytics:
    records = _build_records(db, runs)
    if not records:
        return FileLocalizationAnalytics()

    by_model: dict[tuple[str, str], list[_RunLocalization]] = defaultdict(list)
    by_repository: dict[UUID, list[_RunLocalization]] = defaultdict(list)
    for record in records:
        by_model[(record.run.model_provider, record.run.model_name)].append(record)
        by_repository[record.run.benchmark_task.repository_id].append(record)

    model_rows = [
        LocalizationByModel(
            model_provider=provider,
            model_name=model,
            **_aggregate(group),
        )
        for (provider, model), group in by_model.items()
    ]
    repository_rows = []
    for repository_id, group in by_repository.items():
        repository = group[0].run.benchmark_task.repository
        repository_rows.append(
            LocalizationByRepository(
                repository_id=repository_id,
                repository_owner=repository.owner,
                repository_name=repository.name,
                repository_url=repository.url,
                **_aggregate(group),
            )
        )

    return FileLocalizationAnalytics(
        **_aggregate(records),
        most_common_missed_gold_files=_missed_gold_files(records),
        localization_by_model=sorted(
            model_rows,
            key=lambda row: (
                -row.average_file_localization_score,
                -row.top1_accuracy,
                row.model_provider.lower(),
                row.model_name.lower(),
            ),
        ),
        localization_by_repository=sorted(
            repository_rows,
            key=lambda row: (
                -row.average_file_localization_score,
                row.repository_owner.lower(),
                row.repository_name.lower(),
            ),
        ),
        candidate_hit_rate_by_model=sorted(
            (
                CandidateHitRateByModel(
                    model_provider=provider,
                    model_name=model,
                    runs_with_candidate_files=len(candidate_records),
                    candidate_hit_rate=_rate(
                        record.candidate_top_k_hit(len(record.candidate_files))
                        for record in candidate_records
                    ),
                )
                for (provider, model), group in by_model.items()
                if (candidate_records := [record for record in group if record.candidate_files])
            ),
            key=lambda row: (
                -row.candidate_hit_rate,
                row.model_provider.lower(),
                row.model_name.lower(),
            ),
        ),
    )


def _build_records(db: Session, runs: list[AgentRun]) -> list[_RunLocalization]:
    eligible_runs = [
        run
        for run in runs
        if run.benchmark_task.gold_patch is not None
        and _normalized_files(run.benchmark_task.gold_patch.changed_files)
    ]
    if not eligible_runs:
        return []

    run_ids = [run.id for run in eligible_runs]
    events = list(
        db.scalars(
            select(AgentEvent)
            .where(AgentEvent.agent_run_id.in_(run_ids))
            .order_by(AgentEvent.agent_run_id, AgentEvent.created_at, AgentEvent.id)
        ).all()
    )
    events_by_run: dict[UUID, list[AgentEvent]] = defaultdict(list)
    for event in events:
        events_by_run[event.agent_run_id].append(event)

    records = []
    for run in eligible_runs:
        gold_patch = run.benchmark_task.gold_patch
        assert gold_patch is not None
        generated_patch = run.generated_patch
        records.append(
            _RunLocalization(
                run=run,
                gold_files=frozenset(_normalized_files(gold_patch.changed_files)),
                inspected_files=_ordered_pre_edit_inspections(events_by_run[run.id]),
                edited_files=frozenset(
                    _normalized_files(
                        generated_patch.changed_files if generated_patch is not None else []
                    )
                ),
                candidate_files=_latest_pre_edit_candidates(events_by_run[run.id]),
            )
        )
    return records


def _ordered_pre_edit_inspections(events: list[AgentEvent]) -> tuple[str, ...]:
    ordered: list[str] = []
    seen: set[str] = set()
    for event in events:
        payload = event.payload_json or {}
        if event.event_type == "patch_submitted" or _string_list(payload.get("files_modified")):
            break
        if event.event_type != "agent_tool_call":
            continue
        if payload.get("success") is False:
            continue

        tool_name = payload.get("tool_name")
        if tool_name not in _INSPECTION_TOOLS:
            continue
        for file_path in _string_list(payload.get("files_read")):
            normalized = _normalize_file_path(file_path)
            if normalized and normalized not in seen:
                seen.add(normalized)
                ordered.append(normalized)
    return tuple(ordered)


def _latest_pre_edit_candidates(events: list[AgentEvent]) -> tuple[str, ...]:
    latest: tuple[str, ...] = ()
    for event in events:
        payload = event.payload_json or {}
        if event.event_type == "patch_submitted" or _string_list(payload.get("files_modified")):
            break
        if event.event_type != "candidate_files_submitted":
            continue
        ranked_files = payload.get("ranked_files")
        if not isinstance(ranked_files, list):
            continue
        paths: list[str] = []
        for item in ranked_files:
            if not isinstance(item, dict) or not isinstance(item.get("path"), str):
                continue
            normalized = _normalize_file_path(item["path"])
            if normalized and normalized not in paths:
                paths.append(normalized)
        latest = tuple(paths)
    return latest


def _aggregate(records: list[_RunLocalization]) -> dict[str, int | float]:
    count = len(records)
    if not count:
        return {
            "total_runs_with_gold_files": 0,
            "runs_with_candidate_files": 0,
            "average_file_localization_score": 0.0,
            "top1_accuracy": 0.0,
            "top3_accuracy": 0.0,
            "top5_accuracy": 0.0,
            "edited_file_precision": 0.0,
            "edited_file_recall": 0.0,
            "average_files_read": 0.0,
            "average_files_edited": 0.0,
            "candidate_top1_accuracy": 0.0,
            "candidate_top3_accuracy": 0.0,
            "candidate_top5_accuracy": 0.0,
            "average_candidate_count": 0.0,
        }
    candidate_records = [record for record in records if record.candidate_files]
    return {
        "total_runs_with_gold_files": count,
        "runs_with_candidate_files": len(candidate_records),
        "average_file_localization_score": _average(
            record.localization_score for record in records
        ),
        "top1_accuracy": _rate(record.top_k_hit(1) for record in records),
        "top3_accuracy": _rate(record.top_k_hit(3) for record in records),
        "top5_accuracy": _rate(record.top_k_hit(5) for record in records),
        "edited_file_precision": _average(record.edited_precision for record in records),
        "edited_file_recall": _average(record.edited_recall for record in records),
        "average_files_read": _average(len(record.inspected_files) for record in records),
        "average_files_edited": _average(len(record.edited_files) for record in records),
        "candidate_top1_accuracy": _rate(
            record.candidate_top_k_hit(1) for record in candidate_records
        ),
        "candidate_top3_accuracy": _rate(
            record.candidate_top_k_hit(3) for record in candidate_records
        ),
        "candidate_top5_accuracy": _rate(
            record.candidate_top_k_hit(5) for record in candidate_records
        ),
        "average_candidate_count": _average(
            len(record.candidate_files) for record in candidate_records
        ),
    }


def _missed_gold_files(records: list[_RunLocalization]) -> list[MissedGoldFile]:
    opportunities: Counter[tuple[str, str, str]] = Counter()
    misses: Counter[tuple[str, str, str]] = Counter()
    for record in records:
        repository = record.run.benchmark_task.repository
        inspected = set(record.inspected_files)
        for file_path in record.gold_files:
            key = (repository.owner, repository.name, file_path)
            opportunities[key] += 1
            if file_path not in inspected:
                misses[key] += 1

    return [
        MissedGoldFile(
            repository_owner=owner,
            repository_name=name,
            file_path=file_path,
            missed_run_count=count,
            gold_run_count=opportunities[(owner, name, file_path)],
            miss_rate=_rounded(count / opportunities[(owner, name, file_path)]),
        )
        for (owner, name, file_path), count in sorted(
            misses.items(),
            key=lambda item: (-item[1], item[0][0], item[0][1], item[0][2]),
        )
    ]


def _normalized_files(files: Iterable[str]) -> set[str]:
    return {_normalize_file_path(file_path) for file_path in files if file_path}


def _normalize_file_path(file_path: str) -> str:
    normalized = file_path.replace("\\", "/").removeprefix("./")
    if normalized.startswith("a/"):
        return normalized.removeprefix("a/")
    return normalized.removeprefix("b/")


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item]


def _rate(values: Iterable[bool]) -> float:
    items = list(values)
    return _rounded(sum(items) / len(items)) if items else 0.0


def _average(values: Iterable[float | int]) -> float:
    items = [float(value) for value in values]
    return _rounded(fmean(items)) if items else 0.0


def _rounded(value: float) -> float:
    return round(value, 6)
