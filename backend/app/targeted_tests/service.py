from __future__ import annotations

import re
import shlex
from pathlib import PurePosixPath
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.github.test_detection import is_likely_test_file
from app.models import AgentEvent, AgentRun, IndexedFile, RepositoryIndex
from app.schemas.agent_run import AgentRunConfig
from app.schemas.targeted_tests import TargetedTestSelectionRead

EVENT_TYPE = "targeted_tests_selected"
_SHELL_CONTROL_RE = re.compile(r"[;&|<>\r\n]")
_PYTHON_RUNNERS = {"pytest", "py.test"}
_JS_RUNNERS = {"jest", "vitest"}
_SOURCE_SUFFIXES = {".py", ".js", ".jsx", ".ts", ".tsx"}
_INSPECTION_TOOLS = {"read_file", "retrieve_relevant_files"}


class TargetedTestSelectionService:
    """Select only safely derived commands, otherwise retain the complete configured suite."""

    def __init__(self, db: Session, agent_run_id: UUID) -> None:
        self._db = db
        self._run = db.get(AgentRun, agent_run_id)
        if self._run is None:
            raise LookupError("Agent run not found.")

    def stored_config(self) -> AgentRunConfig:
        event = self._db.scalar(
            select(AgentEvent)
            .where(
                AgentEvent.agent_run_id == self._run.id,
                AgentEvent.event_type == "agent_run_configured",
            )
            .order_by(AgentEvent.created_at.desc(), AgentEvent.id.desc())
            .limit(1)
        )
        if event is None:
            return AgentRunConfig(
                model_provider=self._run.model_provider,
                model_name=self._run.model_name,
            )
        try:
            return AgentRunConfig.model_validate((event.payload_json or {}).get("config", {}))
        except ValidationError:
            return AgentRunConfig(
                model_provider=self._run.model_provider,
                model_name=self._run.model_name,
            )

    def select(
        self,
        *,
        max_commands: int,
        trusted_gold_files: bool = False,
        persist: bool = True,
    ) -> TargetedTestSelectionRead:
        if not 1 <= max_commands <= 20:
            raise ValueError("targeted test command limit must be between 1 and 20")
        configured = _unique_strings(self._run.benchmark_task.test_commands or [])
        if not configured:
            result = TargetedTestSelectionRead(
                selected_commands=[],
                selection_reason="No configured test commands are available.",
                confidence="low",
                fallback_to_full_suite=True,
            )
            return self._store(result, trusted_gold_files=False) if persist else result

        changed_files = _safe_paths(
            self._run.generated_patch.changed_files if self._run.generated_patch else []
        )
        candidate_files = self._candidate_files()
        inspected_files = self._inspected_files()
        indexed_files = self._indexed_files()
        visible_files = _unique_strings(
            [*changed_files, *candidate_files, *inspected_files, *indexed_files]
        )
        direct_tests = _unique_strings(
            path
            for path in [*changed_files, *candidate_files, *inspected_files]
            if is_likely_test_file(path)
        )
        source_files = [
            path
            for path in [*changed_files, *candidate_files, *inspected_files]
            if _is_source_file(path) and not is_likely_test_file(path)
        ]
        inferred_tests = _infer_related_tests(
            source_files,
            visible_files,
            repository_language=self._run.benchmark_task.repository.language,
        )

        if direct_tests:
            selected = _commands_for_test_files(configured, direct_tests, max_commands)
            if selected:
                result = TargetedTestSelectionRead(
                    selected_commands=selected,
                    selection_reason="Changed or inspected test files map to recognized configured test runners.",
                    confidence="high"
                    if any(path in changed_files for path in direct_tests)
                    else "medium",
                    fallback_to_full_suite=False,
                )
                return self._store(result, trusted_gold_files=False) if persist else result

        if inferred_tests:
            selected = _commands_for_test_files(configured, inferred_tests, max_commands)
            if selected:
                result = TargetedTestSelectionRead(
                    selected_commands=selected,
                    selection_reason="Related test files were inferred from source paths and repository evidence.",
                    confidence="medium",
                    fallback_to_full_suite=False,
                )
                return self._store(result, trusted_gold_files=False) if persist else result

        if trusted_gold_files and self._run.benchmark_task.gold_patch is not None:
            gold_tests = _safe_paths(self._run.benchmark_task.gold_patch.test_files or [])
            # Trusted hints may select an existing command, but never create a command containing
            # a gold-only path. This keeps public TestResult rows agent-safe.
            selected = _matching_configured_commands(configured, gold_tests, max_commands)
            if selected:
                result = TargetedTestSelectionRead(
                    selected_commands=selected,
                    selection_reason="Trusted evaluation hints matched existing configured test commands.",
                    confidence="high",
                    fallback_to_full_suite=False,
                )
                return self._store(result, trusted_gold_files=True) if persist else result

        result = TargetedTestSelectionRead(
            selected_commands=configured,
            selection_reason=(
                "No safe targeted subset could be derived from recognized configured commands; "
                "using the full configured test suite."
            ),
            confidence="low",
            fallback_to_full_suite=True,
        )
        return self._store(result, trusted_gold_files=False) if persist else result

    def _inspected_files(self) -> list[str]:
        paths: list[str] = []
        events = self._db.scalars(
            select(AgentEvent)
            .where(
                AgentEvent.agent_run_id == self._run.id,
                AgentEvent.event_type == "agent_tool_call",
            )
            .order_by(AgentEvent.created_at.asc(), AgentEvent.id.asc())
        )
        for event in events:
            payload = event.payload_json or {}
            if payload.get("success") is False or payload.get("tool_name") not in _INSPECTION_TOOLS:
                continue
            paths.extend(_string_list(payload.get("files_read")))
        return _safe_paths(paths)

    def _candidate_files(self) -> list[str]:
        event = self._db.scalar(
            select(AgentEvent)
            .where(
                AgentEvent.agent_run_id == self._run.id,
                AgentEvent.event_type == "candidate_files_submitted",
            )
            .order_by(AgentEvent.created_at.desc(), AgentEvent.id.desc())
            .limit(1)
        )
        ranked = (event.payload_json or {}).get("ranked_files") if event else []
        if not isinstance(ranked, list):
            return []
        return _safe_paths([item.get("path") for item in ranked if isinstance(item, dict)])

    def _indexed_files(self) -> list[str]:
        repository_index = self._db.scalar(
            select(RepositoryIndex).where(RepositoryIndex.agent_run_id == self._run.id)
        )
        if repository_index is None:
            return []
        return _safe_paths(
            list(
                self._db.scalars(
                    select(IndexedFile.file_path).where(
                        IndexedFile.repository_index_id == repository_index.id
                    )
                )
            )
        )

    def _store(
        self,
        result: TargetedTestSelectionRead,
        *,
        trusted_gold_files: bool,
    ) -> TargetedTestSelectionRead:
        self._db.add(
            AgentEvent(
                agent_run_id=self._run.id,
                event_type=EVENT_TYPE,
                payload_json={
                    **result.model_dump(mode="json"),
                    "trusted_gold_files_used": trusted_gold_files,
                },
            )
        )
        self._db.commit()
        return result


def _commands_for_test_files(
    configured: list[str],
    test_files: list[str],
    max_commands: int,
) -> list[str]:
    matching = _matching_configured_commands(configured, test_files, max_commands)
    if matching:
        return matching
    for command in configured:
        runner = _recognized_runner(command)
        if runner is None or _explicit_test_targets(command):
            continue
        safe_test_files = [path for path in test_files if _safe_cli_path(path)]
        if not safe_test_files:
            continue
        separator = " -- " if runner == "npm" else " "
        return [f"{command}{separator}{path}" for path in safe_test_files[:max_commands]]
    return []


def _matching_configured_commands(
    configured: list[str],
    test_files: list[str],
    max_commands: int,
) -> list[str]:
    selected: list[str] = []
    for command in configured:
        if _recognized_runner(command) is None:
            continue
        targets = _explicit_test_targets(command)
        if targets and any(
            _target_matches(path, target) for path in test_files for target in targets
        ):
            selected.append(command)
        if len(selected) >= max_commands:
            break
    return selected


def _recognized_runner(command: str) -> str | None:
    if not command.strip() or _SHELL_CONTROL_RE.search(command):
        return None
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        return None
    if not tokens:
        return None
    executable = PurePosixPath(tokens[0].replace("\\", "/")).name.lower()
    if executable in _PYTHON_RUNNERS:
        return "pytest"
    if executable.startswith("python") and tokens[1:3] == ["-m", "pytest"]:
        return "pytest"
    if executable in _JS_RUNNERS:
        return executable
    if executable == "npx" and len(tokens) > 1 and tokens[1].lower() in _JS_RUNNERS:
        return tokens[1].lower()
    if executable in {"npm", "pnpm", "yarn"} and len(tokens) > 1:
        invocation = [token.lower() for token in tokens[1:3]]
        if invocation[0] == "test" or invocation == ["run", "test"]:
            return "npm"
    return None


def _explicit_test_targets(command: str) -> list[str]:
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        return []
    targets: list[str] = []
    for token in tokens:
        normalized = token.split("::", 1)[0].replace("\\", "/").strip("./")
        if normalized and (
            is_likely_test_file(normalized)
            or normalized.lower() in {"test", "tests", "spec", "specs", "__tests__"}
            or normalized.lower().startswith(("tests/", "test/", "spec/", "specs/"))
        ):
            targets.append(normalized)
    return _unique_strings(targets)


def _target_matches(path: str, target: str) -> bool:
    normalized_path = path.replace("\\", "/").strip("./")
    normalized_target = target.replace("\\", "/").strip("./")
    return (
        normalized_path == normalized_target
        or normalized_path.startswith(f"{normalized_target}/")
        or PurePosixPath(normalized_path).name == PurePosixPath(normalized_target).name
    )


def _infer_related_tests(
    source_files: list[str],
    known_files: list[str],
    *,
    repository_language: str | None,
) -> list[str]:
    test_files = [path for path in known_files if is_likely_test_file(path)]
    related: list[str] = []
    for source in source_files:
        source_stem = _logical_stem(source)
        source_family = _language_family(source, repository_language)
        if not source_stem:
            continue
        for test_file in test_files:
            if (
                _logical_stem(test_file) == source_stem
                and _language_family(test_file, repository_language) == source_family
                and test_file not in related
            ):
                related.append(test_file)
    return related


def _language_family(path: str, repository_language: str | None) -> str:
    suffix = PurePosixPath(path.replace("\\", "/")).suffix.lower()
    if suffix == ".py":
        return "python"
    if suffix in {".js", ".jsx", ".ts", ".tsx"}:
        return "javascript"
    return (repository_language or "unknown").strip().lower()


def _logical_stem(path: str) -> str:
    name = PurePosixPath(path.replace("\\", "/")).name.lower()
    for suffix in (".tsx", ".jsx", ".py", ".ts", ".js"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
            break
    for suffix in (".test", ".spec", "_test", "_spec"):
        name = name.removesuffix(suffix)
    name = name.removeprefix("test_")
    return name


def _is_source_file(path: str) -> bool:
    return PurePosixPath(path.replace("\\", "/")).suffix.lower() in _SOURCE_SUFFIXES


def _safe_cli_path(path: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9_./@+-]+", path))


def _safe_paths(values: object) -> list[str]:
    paths: list[str] = []
    for value in _string_list(values):
        normalized = value.replace("\\", "/").strip()
        parts = PurePosixPath(normalized).parts
        if (
            not normalized
            or PurePosixPath(normalized).is_absolute()
            or re.match(r"^[A-Za-z]:/", normalized)
            or ".." in parts
            or _SHELL_CONTROL_RE.search(normalized)
            or any(
                marker in normalized.lower()
                for marker in ("gold_patch", "gold_solution", "hidden_eval", ".benchmark")
            )
        ):
            continue
        normalized = normalized.removeprefix("./")
        paths.append(normalized)
    return _unique_strings(paths)


def _string_list(value: object) -> list[str]:
    if isinstance(value, (list, tuple)):
        return [item for item in value if isinstance(item, str)]
    return []


def _unique_strings(values: object) -> list[str]:
    if not isinstance(values, (list, tuple)) and not hasattr(values, "__iter__"):
        return []
    result: list[str] = []
    for value in values:
        if isinstance(value, str) and value.strip() and value not in result:
            result.append(value)
    return result
