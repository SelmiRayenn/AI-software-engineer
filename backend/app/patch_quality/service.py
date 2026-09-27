from __future__ import annotations

import math
import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models import AgentEvent, BenchmarkTask, GeneratedPatch, PatchQuality

MINIMIZATION_VERSION = 1
MINIMIZATION_WARNING_CODES = {
    "broad_patch",
    "large_rewrite",
    "formatting_only_change",
    "uninspected_file_modified",
    "generated_block_suspected",
    "excessive_test_changes",
    "excessive_config_changes",
}
_MINIMIZATION_WARNING_MESSAGES = {
    "broad_patch": "Patch spans enough files, hunks, or lines to be difficult to review.",
    "large_rewrite": "Patch contains a large contiguous rewrite.",
    "formatting_only_change": "Patch contains formatting-only hunks.",
    "uninspected_file_modified": "Patch modifies files not inspected or candidate-ranked before editing.",
    "generated_block_suspected": "Patch contains blocks that look generated or mechanically repeated.",
    "excessive_test_changes": "Patch changes an unusually large test surface.",
    "excessive_config_changes": "Patch changes an unusually large configuration surface.",
}

_SOURCE_EXTENSIONS = {
    ".c",
    ".cc",
    ".cpp",
    ".cs",
    ".go",
    ".h",
    ".hpp",
    ".java",
    ".js",
    ".jsx",
    ".kt",
    ".kts",
    ".php",
    ".py",
    ".rb",
    ".rs",
    ".scala",
    ".sh",
    ".sql",
    ".svelte",
    ".swift",
    ".ts",
    ".tsx",
    ".vue",
}
_DOC_EXTENSIONS = {".adoc", ".md", ".rst"}
_DOC_FILE_NAMES = {"authors", "changelog", "contributing", "license", "readme"}
_CONFIG_EXTENSIONS = {".cfg", ".conf", ".ini", ".json", ".toml", ".yaml", ".yml"}
_LOCKFILE_NAMES = {
    "cargo.lock",
    "composer.lock",
    "gemfile.lock",
    "go.sum",
    "npm-shrinkwrap.json",
    "package-lock.json",
    "pipfile.lock",
    "pnpm-lock.yaml",
    "poetry.lock",
    "uv.lock",
    "yarn.lock",
}
_DEPENDENCY_FILE_NAMES = {
    "cargo.toml",
    "composer.json",
    "gemfile",
    "go.mod",
    "package.json",
    "pipfile",
    "pom.xml",
    "pyproject.toml",
    "setup.cfg",
    "setup.py",
}
_GENERATED_PATH_PARTS = {
    ".git",
    ".mypy_cache",
    ".next",
    ".nox",
    ".parcel-cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".turbo",
    ".venv",
    "__pycache__",
    "build",
    "coverage",
    "dist",
    "htmlcov",
    "node_modules",
    "out",
    "target",
    "venv",
}
_GENERATED_SUFFIXES = {".class", ".map", ".min.js", ".min.css", ".pyc", ".pyo"}


class PatchQualityError(RuntimeError):
    pass


class PatchQualityNotFoundError(PatchQualityError):
    pass


class PatchQualityRejectedError(PatchQualityError):
    def __init__(self, violations: list[str]) -> None:
        self.violations = violations
        super().__init__("Patch rejected by quality guardrails: " + "; ".join(violations))


@dataclass(frozen=True)
class PatchQualityAnalysis:
    changed_file_count: int
    added_lines: int
    removed_lines: int
    total_changed_lines: int
    changed_hunk_count: int
    added_removed_ratio: float
    file_kind_counts: dict[str, int]
    duplicate_edit_count: int
    formatting_only_hunk_count: int
    unrelated_formatting_hunk_count: int
    large_rewrite_hunk_count: int
    generated_block_count: int
    uninspected_files: list[str]
    minimization_score: float
    minimization_warnings: list[str]
    minimization_penalties: dict[str, float]
    changed_source_files: list[str]
    changed_test_files: list[str]
    changed_docs_config_files: list[str]
    suspicious_generated_files: list[str]
    unrelated_files: list[str]
    whitespace_only: bool
    dependency_files: list[str]
    lockfiles: list[str]
    warnings: list[str]
    hard_limit_violations: list[str]
    edit_scope_violations: list[str]

    @property
    def accepted(self) -> bool:
        return not self.hard_limit_violations


class PatchQualityService:
    def __init__(
        self,
        db: Session,
        *,
        max_patch_files: int | None = None,
        max_patch_changed_lines: int | None = None,
        block_lockfile_changes: bool | None = None,
        block_dependency_file_changes: bool | None = None,
        require_edited_files_in_candidates: bool | None = None,
        require_edited_files_in_plan: bool | None = None,
        allow_test_file_edits: bool | None = None,
        allow_doc_file_edits: bool | None = None,
        allow_config_file_edits: bool | None = None,
    ) -> None:
        self._db = db
        self.max_patch_files = max_patch_files or settings.max_patch_files
        self.max_patch_changed_lines = max_patch_changed_lines or settings.max_patch_changed_lines
        self.block_lockfile_changes = (
            settings.block_lockfile_changes_by_default
            if block_lockfile_changes is None
            else block_lockfile_changes
        )
        self.block_dependency_file_changes = (
            settings.block_dependency_file_changes_by_default
            if block_dependency_file_changes is None
            else block_dependency_file_changes
        )
        self.require_edited_files_in_candidates = (
            settings.require_edited_files_in_candidates
            if require_edited_files_in_candidates is None
            else require_edited_files_in_candidates
        )
        self.require_edited_files_in_plan = (
            settings.require_edited_files_in_plan
            if require_edited_files_in_plan is None
            else require_edited_files_in_plan
        )
        self.allow_test_file_edits = (
            settings.allow_test_file_edits
            if allow_test_file_edits is None
            else allow_test_file_edits
        )
        self.allow_doc_file_edits = (
            settings.allow_doc_file_edits if allow_doc_file_edits is None else allow_doc_file_edits
        )
        self.allow_config_file_edits = (
            settings.allow_config_file_edits
            if allow_config_file_edits is None
            else allow_config_file_edits
        )

    def analyze_patch(
        self,
        *,
        patch_text: str,
        changed_files: list[str],
        benchmark_task: BenchmarkTask,
        agent_run_id: UUID | None = None,
        trusted_file_guardrail_override: bool = False,
    ) -> PatchQualityAnalysis:
        protected_path_present = any(
            _is_protected_benchmark_path(_normalize_path(path)) for path in changed_files
        )
        normalized_files = sorted(
            {
                normalized
                for path in changed_files
                if (normalized := _normalize_path(path))
                and not _is_protected_benchmark_path(normalized)
            }
        )
        added_lines, removed_lines = _changed_line_counts(patch_text)
        total_changed_lines = added_lines + removed_lines
        test_files = [path for path in normalized_files if _is_test_file(path)]
        docs_config_files = [path for path in normalized_files if _is_docs_or_config_file(path)]
        source_files = [
            path
            for path in normalized_files
            if path not in test_files
            and path not in docs_config_files
            and PurePosixPath(path).suffix.lower() in _SOURCE_EXTENSIONS
        ]
        generated_files = [path for path in normalized_files if _is_generated_file(path)]
        lockfiles = [path for path in normalized_files if _is_lockfile(path)]
        dependency_files = [
            path for path in normalized_files if path not in lockfiles and _is_dependency_file(path)
        ]
        gold_files = {
            _normalize_path(path)
            for path in (
                benchmark_task.gold_patch.changed_files if benchmark_task.gold_patch else []
            )
        }
        unrelated_files = (
            [path for path in normalized_files if path not in gold_files] if gold_files else []
        )
        hunks = _parse_hunks(patch_text, normalized_files)
        file_kind_counts = _file_kind_counts(normalized_files)
        formatting_hunks = [hunk for hunk in hunks if _hunk_is_formatting_only(hunk)]
        unrelated_formatting_hunks = [
            hunk for hunk in formatting_hunks if not gold_files or hunk.file_path not in gold_files
        ]
        large_rewrite_hunks = [hunk for hunk in hunks if _is_large_rewrite(hunk)]
        generated_hunks = [hunk for hunk in hunks if _looks_generated(hunk)]
        duplicate_edit_count = _duplicate_edit_count(hunks)
        inspected_files = (
            _pre_edit_evidence_files(self._db, agent_run_id) if agent_run_id else set()
        )
        uninspected_files = (
            [path for path in normalized_files if path not in inspected_files]
            if agent_run_id
            else []
        )
        minimization_warnings = _minimization_warnings(
            changed_file_count=len(normalized_files),
            changed_hunk_count=len(hunks),
            total_changed_lines=total_changed_lines,
            file_kind_counts=file_kind_counts,
            hunks=hunks,
            formatting_only_hunk_count=len(formatting_hunks),
            large_rewrite_hunk_count=len(large_rewrite_hunks),
            generated_block_count=len(generated_hunks),
            uninspected_files=uninspected_files,
        )
        minimization_score, minimization_penalties = _minimization_score(
            changed_file_count=len(normalized_files),
            changed_hunk_count=len(hunks),
            total_changed_lines=total_changed_lines,
            duplicate_edit_count=duplicate_edit_count,
            formatting_only_hunk_count=len(formatting_hunks),
            large_rewrite_hunk_count=len(large_rewrite_hunks),
            generated_block_count=len(generated_hunks),
            uninspected_file_count=len(uninspected_files),
            warnings=minimization_warnings,
        )

        edit_scope_violations = (
            self._edit_scope_violations(
                normalized_files=normalized_files,
                source_files=source_files,
                test_files=test_files,
                agent_run_id=agent_run_id,
            )
            if agent_run_id is not None
            else []
        )

        violations: list[str] = []
        if len(normalized_files) > self.max_patch_files:
            violations.append(
                f"Patch changes {len(normalized_files)} files; maximum is {self.max_patch_files}."
            )
        if total_changed_lines > self.max_patch_changed_lines:
            violations.append(
                f"Patch changes {total_changed_lines} lines; maximum is "
                f"{self.max_patch_changed_lines}."
            )
        if generated_files:
            violations.append("Patch touches generated, cache, or build-output files.")
        if protected_path_present:
            violations.append("Patch touches hidden benchmark or gold solution files.")
        if lockfiles and self.block_lockfile_changes and not benchmark_task.allow_lockfile_changes:
            violations.append("Lockfile changes are not allowed for this benchmark task.")
        if (
            dependency_files
            and self.block_dependency_file_changes
            and not benchmark_task.allow_dependency_file_changes
        ):
            violations.append("Dependency file changes are not allowed for this benchmark task.")
        if not trusted_file_guardrail_override:
            violations.extend(edit_scope_violations)

        warnings: list[str] = []
        file_warning_threshold = max(1, math.ceil(self.max_patch_files * 0.75))
        line_warning_threshold = max(1, math.ceil(self.max_patch_changed_lines * 0.75))
        if file_warning_threshold <= len(normalized_files) <= self.max_patch_files:
            warnings.append("Patch is close to the configured changed-file limit.")
        if line_warning_threshold <= total_changed_lines <= self.max_patch_changed_lines:
            warnings.append("Patch is close to the configured changed-line limit.")
        if unrelated_files:
            warnings.append(
                f"Patch changes {len(unrelated_files)} file(s) outside the gold patch file set."
            )
        whitespace_only = _is_whitespace_only(patch_text)
        if whitespace_only:
            warnings.append("Patch only changes formatting or whitespace.")
        if lockfiles and benchmark_task.allow_lockfile_changes:
            warnings.append("Patch changes lockfiles under an explicit task allowance.")
        if dependency_files and benchmark_task.allow_dependency_file_changes:
            warnings.append("Patch changes dependency files under an explicit task allowance.")
        if trusted_file_guardrail_override and edit_scope_violations:
            warnings.append(
                "Trusted operator override bypassed edited-file justification guardrails."
            )
        warnings.extend(_MINIMIZATION_WARNING_MESSAGES[code] for code in minimization_warnings)

        return PatchQualityAnalysis(
            changed_file_count=len(normalized_files),
            added_lines=added_lines,
            removed_lines=removed_lines,
            total_changed_lines=total_changed_lines,
            changed_hunk_count=len(hunks),
            added_removed_ratio=round(added_lines / max(removed_lines, 1), 4),
            file_kind_counts=file_kind_counts,
            duplicate_edit_count=duplicate_edit_count,
            formatting_only_hunk_count=len(formatting_hunks),
            unrelated_formatting_hunk_count=len(unrelated_formatting_hunks),
            large_rewrite_hunk_count=len(large_rewrite_hunks),
            generated_block_count=len(generated_hunks),
            uninspected_files=uninspected_files,
            minimization_score=minimization_score,
            minimization_warnings=minimization_warnings,
            minimization_penalties=minimization_penalties,
            changed_source_files=source_files,
            changed_test_files=test_files,
            changed_docs_config_files=docs_config_files,
            suspicious_generated_files=generated_files,
            unrelated_files=unrelated_files,
            whitespace_only=whitespace_only,
            dependency_files=dependency_files,
            lockfiles=lockfiles,
            warnings=warnings,
            hard_limit_violations=violations,
            edit_scope_violations=edit_scope_violations,
        )

    def _edit_scope_violations(
        self,
        *,
        normalized_files: list[str],
        source_files: list[str],
        test_files: list[str],
        agent_run_id: UUID,
    ) -> list[str]:
        candidate_files = _latest_candidate_files(self._db, agent_run_id)
        planned_files = _latest_accepted_plan_files(self._db, agent_run_id)
        violations: list[str] = []

        if self.require_edited_files_in_candidates:
            unranked_source_files = sorted(set(source_files) - candidate_files)
            if unranked_source_files:
                violations.append(
                    "Source files must be candidate-ranked before editing: "
                    + ", ".join(unranked_source_files)
                    + "."
                )

        if self.require_edited_files_in_plan:
            unplanned_files = sorted(set(normalized_files) - planned_files)
            if unplanned_files:
                violations.append(
                    "Edited files must appear in the accepted plan's likely-to-modify list: "
                    + ", ".join(unplanned_files)
                    + "."
                )

        if test_files and not self.allow_test_file_edits:
            violations.append("Test file edits are disabled by patch policy.")

        doc_files = {path for path in normalized_files if _is_docs_file(path)}
        config_files = {path for path in normalized_files if _is_config_file(path)}
        justified_files = candidate_files | planned_files
        unjustified_docs = sorted(doc_files - justified_files)
        unjustified_config = sorted(config_files - justified_files)
        if unjustified_docs and not self.allow_doc_file_edits:
            violations.append(
                "Documentation files require candidate ranking, an accepted plan, or operator "
                "configuration: " + ", ".join(unjustified_docs) + "."
            )
        if unjustified_config and not self.allow_config_file_edits:
            violations.append(
                "Configuration files require candidate ranking, an accepted plan, or operator "
                "configuration: " + ", ".join(unjustified_config) + "."
            )

        classified = set(source_files) | set(test_files) | doc_files | config_files
        unjustified_other = sorted(set(normalized_files) - classified - justified_files)
        if unjustified_other:
            violations.append(
                "Other edited files require candidate ranking or an accepted plan: "
                + ", ".join(unjustified_other)
                + "."
            )
        return violations

    def enforce(self, analysis: PatchQualityAnalysis) -> None:
        if analysis.hard_limit_violations:
            raise PatchQualityRejectedError(analysis.hard_limit_violations)

    def store(
        self,
        generated_patch: GeneratedPatch,
        analysis: PatchQualityAnalysis,
        *,
        commit: bool = True,
    ) -> PatchQuality:
        quality = generated_patch.quality
        if quality is None:
            quality = PatchQuality(generated_patch_id=generated_patch.id)
            self._db.add(quality)
        for field_name in (
            "changed_file_count",
            "added_lines",
            "removed_lines",
            "total_changed_lines",
            "changed_source_files",
            "changed_test_files",
            "changed_docs_config_files",
            "suspicious_generated_files",
            "unrelated_files",
            "whitespace_only",
            "dependency_files",
            "lockfiles",
            "warnings",
            "hard_limit_violations",
            "changed_hunk_count",
            "added_removed_ratio",
            "file_kind_counts",
            "duplicate_edit_count",
            "formatting_only_hunk_count",
            "unrelated_formatting_hunk_count",
            "large_rewrite_hunk_count",
            "generated_block_count",
            "uninspected_files",
            "minimization_score",
            "minimization_warnings",
            "minimization_penalties",
        ):
            setattr(quality, field_name, getattr(analysis, field_name))
        quality.minimization_version = MINIMIZATION_VERSION
        quality.max_patch_files = self.max_patch_files
        quality.max_patch_changed_lines = self.max_patch_changed_lines
        if commit:
            self._db.commit()
            self._db.refresh(quality)
        return quality

    def get_or_create(self, patch_id: UUID) -> PatchQuality:
        generated_patch = self._db.get(GeneratedPatch, patch_id)
        if generated_patch is None:
            raise PatchQualityNotFoundError("Generated patch not found.")
        if (
            generated_patch.quality is not None
            and generated_patch.quality.minimization_version >= MINIMIZATION_VERSION
            and generated_patch.quality.minimization_score is not None
        ):
            return generated_patch.quality
        analysis = self.analyze_patch(
            patch_text=generated_patch.patch_text,
            changed_files=generated_patch.changed_files,
            benchmark_task=generated_patch.agent_run.benchmark_task,
            agent_run_id=generated_patch.agent_run_id,
        )
        return self.store(generated_patch, analysis)


@dataclass
class _PatchHunk:
    file_path: str
    added_lines: list[str]
    removed_lines: list[str]

    @property
    def changed_line_count(self) -> int:
        return len(self.added_lines) + len(self.removed_lines)


def _parse_hunks(patch_text: str, changed_files: list[str]) -> list[_PatchHunk]:
    hunks: list[_PatchHunk] = []
    current_path = changed_files[0] if len(changed_files) == 1 else ""
    current_hunk: _PatchHunk | None = None
    for line in patch_text.splitlines():
        if line.startswith("diff --git a/"):
            match = re.match(r"^diff --git a/(.+) b/(.+)$", line)
            if match:
                current_path = _normalize_path(match.group(2))
            current_hunk = None
            continue
        if line.startswith("+++ b/"):
            current_path = _normalize_path(line[6:])
            continue
        if line.startswith("@@"):
            current_hunk = (
                _PatchHunk(current_path, [], []) if current_path in changed_files else None
            )
            if current_hunk is not None:
                hunks.append(current_hunk)
            continue
        if current_hunk is None or line.startswith(("+++", "---")):
            continue
        if line.startswith("+"):
            current_hunk.added_lines.append(line[1:])
        elif line.startswith("-"):
            current_hunk.removed_lines.append(line[1:])
    return hunks


def _file_kind_counts(paths: list[str]) -> dict[str, int]:
    counts = {"source": 0, "test": 0, "docs": 0, "config": 0, "other": 0}
    for path in paths:
        if _is_test_file(path):
            counts["test"] += 1
        elif _is_docs_file(path):
            counts["docs"] += 1
        elif _is_config_file(path):
            counts["config"] += 1
        elif PurePosixPath(path).suffix.lower() in _SOURCE_EXTENSIONS:
            counts["source"] += 1
        else:
            counts["other"] += 1
    return counts


def _hunk_is_formatting_only(hunk: _PatchHunk) -> bool:
    if not hunk.added_lines or not hunk.removed_lines:
        return False
    return _without_whitespace(hunk.added_lines) == _without_whitespace(hunk.removed_lines)


def _is_large_rewrite(hunk: _PatchHunk) -> bool:
    return hunk.changed_line_count >= 120 or (
        hunk.changed_line_count >= 80
        and len(hunk.added_lines) >= 20
        and len(hunk.removed_lines) >= 20
    )


def _looks_generated(hunk: _PatchHunk) -> bool:
    added = [line.strip() for line in hunk.added_lines if line.strip()]
    if not added:
        return False
    lowered = "\n".join(added).lower()
    if any(
        marker in lowered
        for marker in (
            "@generated",
            "auto-generated",
            "autogenerated",
            "generated by",
            "do not edit",
        )
    ):
        return True
    if any(len(line) >= 400 for line in added):
        return True
    unique_ratio = len(set(added)) / len(added)
    return len(added) >= 20 and unique_ratio <= 0.35


def _duplicate_edit_count(hunks: list[_PatchHunk]) -> int:
    signatures: list[tuple[tuple[str, ...], tuple[str, ...]]] = []
    added_lines: list[str] = []
    for hunk in hunks:
        removed = tuple(line.strip() for line in hunk.removed_lines if line.strip())
        added = tuple(line.strip() for line in hunk.added_lines if line.strip())
        if removed or added:
            signatures.append((removed, added))
        added_lines.extend(added)
    repeated_hunks = len(signatures) - len(set(signatures))
    repeated_lines = len(added_lines) - len(set(added_lines))
    return max(0, repeated_hunks) + max(0, repeated_lines)


def _minimization_warnings(
    *,
    changed_file_count: int,
    changed_hunk_count: int,
    total_changed_lines: int,
    file_kind_counts: dict[str, int],
    hunks: list[_PatchHunk],
    formatting_only_hunk_count: int,
    large_rewrite_hunk_count: int,
    generated_block_count: int,
    uninspected_files: list[str],
) -> list[str]:
    warnings: list[str] = []
    if changed_file_count >= 6 or changed_hunk_count >= 10 or total_changed_lines >= 250:
        warnings.append("broad_patch")
    if large_rewrite_hunk_count:
        warnings.append("large_rewrite")
    if formatting_only_hunk_count:
        warnings.append("formatting_only_change")
    if uninspected_files:
        warnings.append("uninspected_file_modified")
    if generated_block_count:
        warnings.append("generated_block_suspected")

    changed_lines_by_kind = {"source": 0, "test": 0, "docs": 0, "config": 0, "other": 0}
    for hunk in hunks:
        kind = _file_kind(hunk.file_path)
        changed_lines_by_kind[kind] += hunk.changed_line_count
    if file_kind_counts["test"] >= 3 or changed_lines_by_kind["test"] > max(
        100, changed_lines_by_kind["source"] * 2
    ):
        warnings.append("excessive_test_changes")
    if file_kind_counts["config"] >= 3 or changed_lines_by_kind["config"] > 100:
        warnings.append("excessive_config_changes")
    return [warning for warning in warnings if warning in MINIMIZATION_WARNING_CODES]


def _minimization_score(
    *,
    changed_file_count: int,
    changed_hunk_count: int,
    total_changed_lines: int,
    duplicate_edit_count: int,
    formatting_only_hunk_count: int,
    large_rewrite_hunk_count: int,
    generated_block_count: int,
    uninspected_file_count: int,
    warnings: list[str],
) -> tuple[float, dict[str, float]]:
    penalties = {
        "extra_files": min(0.15, max(changed_file_count - 1, 0) * 0.02),
        "extra_hunks": min(0.10, max(changed_hunk_count - changed_file_count, 0) * 0.01),
        "changed_lines": min(0.15, max(total_changed_lines - 20, 0) * 0.0005),
        "duplicate_edits": min(0.10, duplicate_edit_count * 0.02),
        "formatting_hunks": min(0.10, formatting_only_hunk_count * 0.05),
        "large_rewrites": min(0.20, large_rewrite_hunk_count * 0.20),
        "generated_blocks": min(0.20, generated_block_count * 0.20),
        "uninspected_files": min(0.20, uninspected_file_count * 0.05),
        "broad_patch": 0.25 if "broad_patch" in warnings else 0.0,
        "excessive_test_changes": 0.10 if "excessive_test_changes" in warnings else 0.0,
        "excessive_config_changes": 0.10 if "excessive_config_changes" in warnings else 0.0,
    }
    rounded = {key: round(value, 4) for key, value in penalties.items() if value > 0}
    return round(max(0.0, 1.0 - sum(rounded.values())), 4), rounded


def _pre_edit_evidence_files(db: Session, agent_run_id: UUID) -> set[str]:
    evidence: set[str] = set()
    events = db.scalars(
        select(AgentEvent)
        .where(AgentEvent.agent_run_id == agent_run_id)
        .order_by(AgentEvent.created_at.asc(), AgentEvent.id.asc())
    )
    for event in events:
        payload = event.payload_json or {}
        if event.event_type == "agent_tool_call":
            if payload.get("success") is not True:
                continue
            if payload.get("tool_name") == "write_file" or payload.get("files_modified"):
                break
            evidence.update(_normalized_payload_paths(payload.get("files_read")))
        elif event.event_type == "candidate_files_submitted":
            ranked = payload.get("ranked_files")
            if isinstance(ranked, list):
                evidence.update(
                    _normalize_path(item["path"])
                    for item in ranked
                    if isinstance(item, dict) and isinstance(item.get("path"), str)
                )
    return evidence


def _latest_candidate_files(db: Session, agent_run_id: UUID) -> set[str]:
    event = db.scalar(
        select(AgentEvent)
        .where(
            AgentEvent.agent_run_id == agent_run_id,
            AgentEvent.event_type == "candidate_files_submitted",
        )
        .order_by(AgentEvent.created_at.desc(), AgentEvent.id.desc())
        .limit(1)
    )
    if event is None:
        return set()
    ranked_files = (event.payload_json or {}).get("ranked_files")
    if not isinstance(ranked_files, list):
        return set()
    return {
        normalized
        for item in ranked_files
        if isinstance(item, dict) and isinstance(item.get("path"), str)
        if (normalized := _normalize_path(item["path"]))
        and not _is_protected_benchmark_path(normalized)
    }


def _latest_accepted_plan_files(db: Session, agent_run_id: UUID) -> set[str]:
    events = db.scalars(
        select(AgentEvent)
        .where(
            AgentEvent.agent_run_id == agent_run_id,
            AgentEvent.event_type == "plan_submitted",
        )
        .order_by(AgentEvent.created_at.desc(), AgentEvent.id.desc())
    )
    for event in events:
        payload = event.payload_json or {}
        plan = payload.get("plan")
        if payload.get("accepted") is not True or not isinstance(plan, dict):
            continue
        likely_files = plan.get("files_likely_to_modify")
        if not isinstance(likely_files, list):
            return set()
        return {
            normalized
            for path in likely_files
            if isinstance(path, str)
            if (normalized := _normalize_path(path))
            and not _is_protected_benchmark_path(normalized)
        }
    return set()


def _normalized_payload_paths(value: object) -> set[str]:
    if not isinstance(value, list):
        return set()
    return {_normalize_path(path) for path in value if isinstance(path, str) and path.strip()}


def _file_kind(path: str) -> str:
    if _is_test_file(path):
        return "test"
    if _is_docs_file(path):
        return "docs"
    if _is_config_file(path):
        return "config"
    if PurePosixPath(path).suffix.lower() in _SOURCE_EXTENSIONS:
        return "source"
    return "other"


def _normalize_path(path: str) -> str:
    normalized = PurePosixPath(path.replace("\\", "/")).as_posix()
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def _is_protected_benchmark_path(path: str) -> bool:
    value = PurePosixPath(path)
    parts = {part.lower() for part in value.parts}
    names = {
        ".benchmark_gold",
        ".gold",
        ".gold_patch",
        ".gold_solution",
        "gold_patch.diff",
        "gold_patch.patch",
        "gold_solution.diff",
        "gold_solution.patch",
    }
    lowered = value.as_posix().lower()
    return bool(
        ".benchmark" in parts
        or parts.intersection(names)
        or any(marker in lowered for marker in ("gold_patch", "gold_solution", "hidden_eval"))
    )


def _changed_line_counts(patch_text: str) -> tuple[int, int]:
    added_lines = 0
    removed_lines = 0
    for line in patch_text.splitlines():
        if line.startswith(("+++", "---")):
            continue
        if line.startswith("+"):
            added_lines += 1
        elif line.startswith("-"):
            removed_lines += 1
    return added_lines, removed_lines


def _is_test_file(path: str) -> bool:
    value = PurePosixPath(path)
    parts = {part.lower() for part in value.parts}
    name = value.name.lower()
    return bool(
        parts.intersection({"spec", "specs", "test", "tests", "__tests__"})
        or name.startswith("test_")
        or name.endswith(
            (
                "_test.py",
                ".spec.js",
                ".spec.jsx",
                ".spec.ts",
                ".spec.tsx",
                ".test.js",
                ".test.jsx",
                ".test.ts",
                ".test.tsx",
            )
        )
    )


def _is_docs_or_config_file(path: str) -> bool:
    return _is_docs_file(path) or _is_config_file(path)


def _is_docs_file(path: str) -> bool:
    value = PurePosixPath(path)
    parts = {part.lower() for part in value.parts}
    return bool(
        "docs" in parts
        or value.suffix.lower() in _DOC_EXTENSIONS
        or value.stem.lower() in _DOC_FILE_NAMES
    )


def _is_config_file(path: str) -> bool:
    value = PurePosixPath(path)
    name = value.name.lower()
    return bool(
        value.suffix.lower() in _CONFIG_EXTENSIONS
        or name.startswith(".")
        or _is_lockfile(path)
        or _is_dependency_file(path)
    )


def _is_generated_file(path: str) -> bool:
    value = PurePosixPath(path)
    parts = {part.lower() for part in value.parts}
    name = value.name.lower()
    return bool(
        parts.intersection(_GENERATED_PATH_PARTS)
        or any(name.endswith(suffix) for suffix in _GENERATED_SUFFIXES)
    )


def _is_lockfile(path: str) -> bool:
    return PurePosixPath(path).name.lower() in _LOCKFILE_NAMES


def _is_dependency_file(path: str) -> bool:
    name = PurePosixPath(path).name.lower()
    return (
        name in _DEPENDENCY_FILE_NAMES
        or (name.startswith("requirements-") and name.endswith(".txt"))
        or name == "requirements.txt"
    )


def _is_whitespace_only(patch_text: str) -> bool:
    added: list[str] = []
    removed: list[str] = []
    for line in patch_text.splitlines():
        if line.startswith(("+++", "---")):
            continue
        if line.startswith("+"):
            added.append(line[1:])
        elif line.startswith("-"):
            removed.append(line[1:])
    if not added or not removed:
        return False
    return _without_whitespace(added) == _without_whitespace(removed)


def _without_whitespace(lines: list[str]) -> str:
    return re.sub(r"\s+", "", "\n".join(lines))
