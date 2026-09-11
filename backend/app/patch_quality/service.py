from __future__ import annotations

import math
import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from uuid import UUID

from sqlalchemy.orm import Session

from app.core.config import settings
from app.models import BenchmarkTask, GeneratedPatch, PatchQuality

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

    def analyze_patch(
        self,
        *,
        patch_text: str,
        changed_files: list[str],
        benchmark_task: BenchmarkTask,
    ) -> PatchQualityAnalysis:
        normalized_files = sorted({_normalize_path(path) for path in changed_files})
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
        if lockfiles and self.block_lockfile_changes and not benchmark_task.allow_lockfile_changes:
            violations.append("Lockfile changes are not allowed for this benchmark task.")
        if (
            dependency_files
            and self.block_dependency_file_changes
            and not benchmark_task.allow_dependency_file_changes
        ):
            violations.append("Dependency file changes are not allowed for this benchmark task.")

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

        return PatchQualityAnalysis(
            changed_file_count=len(normalized_files),
            added_lines=added_lines,
            removed_lines=removed_lines,
            total_changed_lines=total_changed_lines,
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
        )

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
        ):
            setattr(quality, field_name, getattr(analysis, field_name))
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
        if generated_patch.quality is not None:
            return generated_patch.quality
        analysis = self.analyze_patch(
            patch_text=generated_patch.patch_text,
            changed_files=generated_patch.changed_files,
            benchmark_task=generated_patch.agent_run.benchmark_task,
        )
        return self.store(generated_patch, analysis)


def _normalize_path(path: str) -> str:
    normalized = PurePosixPath(path.replace("\\", "/")).as_posix()
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


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
    value = PurePosixPath(path)
    parts = {part.lower() for part in value.parts}
    name = value.name.lower()
    return bool(
        "docs" in parts
        or value.suffix.lower() in _DOC_EXTENSIONS | _CONFIG_EXTENSIONS
        or value.stem.lower() in _DOC_FILE_NAMES
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
    strip_whitespace = lambda lines: re.sub(r"\s+", "", "\n".join(lines))
    return strip_whitespace(added) == strip_whitespace(removed)
