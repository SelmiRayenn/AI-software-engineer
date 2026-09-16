from __future__ import annotations

import re
from pathlib import PurePosixPath

_TEST_DIRECTORIES = {"__tests__", "spec", "specs", "test", "tests", "testing"}
_DOC_EXTENSIONS = {".adoc", ".md", ".rst"}
_DOC_NAMES = {"authors", "changelog", "contributing", "license", "readme"}
_CONFIG_EXTENSIONS = {".cfg", ".conf", ".ini", ".json", ".toml", ".yaml", ".yml"}
_CONFIG_NAMES = {
    "dockerfile",
    "makefile",
    "package.json",
    "pyproject.toml",
    "setup.cfg",
    "setup.py",
}
_SOURCE_EXTENSIONS = {
    ".c",
    ".cc",
    ".cpp",
    ".cs",
    ".go",
    ".java",
    ".js",
    ".jsx",
    ".kt",
    ".php",
    ".py",
    ".rb",
    ".rs",
    ".scala",
    ".swift",
    ".ts",
    ".tsx",
    ".vue",
}


def is_likely_test_file(path: str) -> bool:
    """Classify common test paths without treating every `test` substring as a test."""
    normalized = path.replace("\\", "/").strip("/").lower()
    if not normalized:
        return False

    candidate = PurePosixPath(normalized)
    filename = candidate.name
    directory_parts = candidate.parts[:-1]
    stem = candidate.stem

    if any(
        part in _TEST_DIRECTORIES
        or part.startswith("test_")
        or part.endswith(("_test", "_tests"))
        for part in directory_parts
    ):
        return True
    if filename.startswith("test_") or filename in {"test.py", "tests.py", "conftest.py"}:
        return True
    if filename.endswith("_test.py") or stem.endswith(("_test", "_spec")):
        return True
    if re.search(r"\.(?:spec|test)\.[^.]+$", filename):
        return True
    if re.match(r"(?:^test.+|.+test)\.java$", filename) or re.search(r"tests?\.cs$", filename):
        return True
    return filename.endswith(".feature")


def classify_pull_request_file(path: str) -> str:
    if is_likely_test_file(path):
        return "test"

    candidate = PurePosixPath(path.replace("\\", "/"))
    filename = candidate.name.lower()
    suffix = candidate.suffix.lower()
    if suffix in _DOC_EXTENSIONS or candidate.stem.lower() in _DOC_NAMES:
        return "docs"
    if suffix in _CONFIG_EXTENSIONS or filename in _CONFIG_NAMES or filename.startswith("."):
        return "config"
    if suffix in _SOURCE_EXTENSIONS:
        return "source"
    return "other"
