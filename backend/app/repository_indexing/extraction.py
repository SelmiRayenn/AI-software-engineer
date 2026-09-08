from __future__ import annotations

import ast
import hashlib
import io
import tokenize
from pathlib import PurePosixPath

from app.models import IndexedFile, IndexedSymbol
from app.repository_indexing.errors import SkippedFile

LANGUAGES = {
    ".py": "Python",
    ".pyi": "Python",
    ".js": "JavaScript",
    ".jsx": "JavaScript",
    ".ts": "TypeScript",
    ".tsx": "TypeScript",
    ".go": "Go",
    ".rs": "Rust",
    ".java": "Java",
    ".c": "C",
    ".h": "C",
    ".cpp": "C++",
    ".rb": "Ruby",
    ".sh": "Shell",
    ".ps1": "PowerShell",
    ".html": "HTML",
    ".css": "CSS",
    ".md": "Markdown",
    ".rst": "reStructuredText",
    ".txt": "Text",
    ".toml": "TOML",
    ".json": "JSON",
    ".yaml": "YAML",
    ".yml": "YAML",
    ".ini": "INI",
    ".cfg": "INI",
    ".xml": "XML",
    ".sql": "SQL",
}
CONFIG_EXTENSIONS = {".toml", ".json", ".yaml", ".yml", ".ini", ".cfg", ".xml"}
DOC_EXTENSIONS = {".md", ".rst", ".txt"}


def extract_file(file_path: str, data: bytes) -> IndexedFile:
    path = PurePosixPath(file_path)
    extension = path.suffix.lower()
    if len(file_path.encode("utf-8")) > 1024 or len(extension) > 100:
        raise SkippedFile("oversized_paths")
    if any(byte < 32 and byte not in {9, 10, 12, 13} for byte in data):
        raise SkippedFile("binary_files")
    try:
        encoding = (
            tokenize.detect_encoding(io.BytesIO(data).readline)[0]
            if extension in {".py", ".pyi"}
            else "utf-8-sig"
        )
        content = data.decode(encoding)
    except (UnicodeError, SyntaxError, LookupError) as exc:
        raise SkippedFile("binary_files") from exc

    file = IndexedFile(
        file_path=file_path,
        extension=extension,
        size_bytes=len(data),
        language=LANGUAGES.get(extension, "Text"),
        file_type=_file_type(path),
        preview=content[:500],
        content=content,
        checksum=hashlib.sha256(data).hexdigest(),
        imports=[],
        symbols=[],
        python_parse_error=False,
    )
    if extension in {".py", ".pyi"}:
        _python_structure(file)
    return file


def _file_type(path: PurePosixPath) -> str:
    name = path.name.lower()
    directories = {part.lower() for part in path.parts[:-1]}
    if (
        directories & {"test", "tests", "testing", "__tests__"}
        or name.startswith("test_")
        or name.endswith("_test.py")
        or name == "conftest.py"
    ):
        return "test"
    if (
        directories & {"config", "configs", ".github"}
        or path.suffix.lower() in CONFIG_EXTENSIONS
        or name in {"dockerfile", "makefile", ".gitignore", ".dockerignore", "setup.py"}
        or name.startswith("requirements")
    ):
        return "config"
    if (
        directories & {"doc", "docs"}
        or path.suffix.lower() in DOC_EXTENSIONS
        or name
        in {
            "readme",
            "license",
            "changelog",
            "authors",
        }
    ):
        return "docs"
    return "source" if path.suffix.lower() in LANGUAGES else "other"


def _python_structure(file: IndexedFile) -> None:
    try:
        module = ast.parse(file.content, filename=file.file_path)
    except (SyntaxError, ValueError, RecursionError):
        file.python_parse_error = True
        return

    for node in module.body:
        kind: str
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names = [node.name]
            kind = "class" if isinstance(node, ast.ClassDef) else "function"
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names = [target.id for target in targets if isinstance(target, ast.Name)]
            kind = "variable"
        else:
            continue
        for name in names:
            file.symbols.append(
                IndexedSymbol(
                    name=name,
                    kind=kind,
                    line_number=node.lineno,
                    end_line_number=node.end_lineno or node.lineno,
                )
            )
    file.symbols.sort(key=lambda symbol: (symbol.line_number, symbol.name))
    file.imports = [
        ast.unparse(node)
        for node in sorted(
            (node for node in ast.walk(module) if isinstance(node, (ast.Import, ast.ImportFrom))),
            key=lambda node: (node.lineno, node.col_offset),
        )
    ]
