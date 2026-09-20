from __future__ import annotations

import re
from typing import Literal

TASK_DIFFICULTIES = ("easy", "medium", "hard", "expert", "unknown")
TaskDifficulty = Literal["easy", "medium", "hard", "expert", "unknown"]
MAX_TASK_TAGS = 25
MAX_TASK_TAG_LENGTH = 64
TAG_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def normalize_difficulty(value: str) -> str:
    normalized = value.strip().lower()
    if normalized not in TASK_DIFFICULTIES:
        allowed = ", ".join(TASK_DIFFICULTIES)
        raise ValueError(f"Difficulty must be one of: {allowed}.")
    return normalized


def normalize_tags(values: list[str]) -> list[str]:
    if len(values) > MAX_TASK_TAGS:
        raise ValueError(f"At most {MAX_TASK_TAGS} tags are allowed.")

    normalized_tags: list[str] = []
    seen: set[str] = set()
    for value in values:
        tag = value.strip().lower()
        if len(tag) > MAX_TASK_TAG_LENGTH or not TAG_PATTERN.fullmatch(tag):
            raise ValueError(
                "Tags must be lowercase slugs using letters, digits, and single hyphens."
            )
        if tag not in seen:
            normalized_tags.append(tag)
            seen.add(tag)
    return normalized_tags
