from __future__ import annotations

import json
import re
from dataclasses import dataclass

from app.model_providers import ModelMessage
from app.models import BenchmarkTask, Repository

SYSTEM_PROMPT_TEMPLATE = """You are a software engineering agent working in an isolated repository workspace.
Use only the controlled tools listed in the developer instructions. You cannot access the host,
benchmark gold data, approval controls, or publishing credentials."""

DEVELOPER_SAFETY_PROMPT_TEMPLATE = """Solve the reported issue with the smallest reasonable code change.
Inspect relevant files before editing, avoid unrelated files, and use tests when possible.
Never request or inspect gold solution data. Never approve, export, publish, push, or create a pull
request. A human reviewer controls every publication decision.

Run mode: {run_mode}
Maximum steps: {max_steps}
Maximum tool errors: {max_tool_errors}
Command timeout: {command_timeout_seconds} seconds"""

ISSUE_CONTEXT_PROMPT_TEMPLATE = """Fix this benchmark issue.

Repository:
{repository_json}

Base commit: {base_commit}

Issue #{issue_number}: {issue_title}

Issue body:
{issue_body}

Issue comments:
{issue_comments}"""

TOOL_USE_INSTRUCTIONS_TEMPLATE = """Allowed tools:
{allowed_tools}

Configured test commands:
{test_commands}

Inspect before editing. Tool arguments must match the advertised JSON schema exactly. Paths must
be relative to the workspace. Test tool status: {test_tool_status}. {test_tool_instruction}"""

PATCH_SUBMISSION_INSTRUCTIONS_TEMPLATE = """When the solution is ready, inspect it with get_diff and submit it with this structured call:
{"name": "submit_patch", "arguments": {}}

submit_patch stores the current workspace unified diff. Do not send patch text as an argument and
do not attempt to publish it. Submission ends the agent loop and sends the patch to human review."""


@dataclass(frozen=True)
class RenderedAgentPrompts:
    system_prompt: str
    developer_safety_prompt: str
    issue_context_prompt: str
    tool_use_instructions: str
    patch_submission_instructions: str

    def messages(self) -> list[ModelMessage]:
        developer_content = (
            f"{self.developer_safety_prompt}\n\n"
            f"{self.tool_use_instructions}\n\n"
            f"{self.patch_submission_instructions}"
        )
        return [
            ModelMessage(role="system", content=self.system_prompt),
            ModelMessage(role="developer", content=developer_content),
            ModelMessage(role="user", content=self.issue_context_prompt),
        ]

    def redacted_preview(self) -> dict[str, str]:
        return {
            "system_prompt": redact_prompt_text(self.system_prompt),
            "developer_safety_prompt": redact_prompt_text(self.developer_safety_prompt),
            "issue_context_prompt": redact_prompt_text(self.issue_context_prompt),
            "tool_use_instructions": redact_prompt_text(self.tool_use_instructions),
            "patch_submission_instructions": redact_prompt_text(
                self.patch_submission_instructions
            ),
        }


def render_agent_prompts(
    *,
    task: BenchmarkTask,
    repository: Repository,
    allowed_tools: list[str],
    configured_test_commands: list[str],
    max_steps: int,
    max_tool_errors: int,
    command_timeout_seconds: int,
    include_issue_comments: bool,
    enable_test_tool: bool,
    run_mode: str,
) -> RenderedAgentPrompts:
    repository_context = {
        "owner": repository.owner,
        "name": repository.name,
        "url": repository.url,
        "default_branch": repository.default_branch,
        "language": repository.language,
    }
    comments = _agent_visible_comments(task) if include_issue_comments else []
    visible_test_commands = configured_test_commands if enable_test_tool else []

    return RenderedAgentPrompts(
        system_prompt=SYSTEM_PROMPT_TEMPLATE,
        developer_safety_prompt=DEVELOPER_SAFETY_PROMPT_TEMPLATE.format(
            run_mode=run_mode,
            max_steps=max_steps,
            max_tool_errors=max_tool_errors,
            command_timeout_seconds=command_timeout_seconds,
        ),
        issue_context_prompt=ISSUE_CONTEXT_PROMPT_TEMPLATE.format(
            repository_json=json.dumps(repository_context, indent=2),
            base_commit=task.base_commit,
            issue_number=task.issue_number,
            issue_title=task.issue_title,
            issue_body=task.issue_body or "(no issue body)",
            issue_comments=(
                json.dumps(comments, indent=2) if comments else "(not included)"
            ),
        ),
        tool_use_instructions=TOOL_USE_INSTRUCTIONS_TEMPLATE.format(
            allowed_tools="\n".join(f"- {tool_name}" for tool_name in allowed_tools),
            test_commands=(
                "\n".join(f"- {command}" for command in visible_test_commands)
                if visible_test_commands
                else "(none exposed to the model)"
            ),
            test_tool_status="enabled" if enable_test_tool else "disabled",
            test_tool_instruction=(
                "Use run_tests only with one of the configured commands shown above."
                if enable_test_tool
                else "run_tests is unavailable; do not request shell or test execution."
            ),
        ),
        patch_submission_instructions=PATCH_SUBMISSION_INSTRUCTIONS_TEMPLATE,
    )


def redact_prompt_text(value: str) -> str:
    redacted = re.sub(
        r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/=-]+",
        r"\1[REDACTED]",
        value,
    )
    redacted = re.sub(
        r"\b(?:sk-[A-Za-z0-9_-]{8,}|gh[pousr]_[A-Za-z0-9_]{8,})\b",
        "[REDACTED]",
        redacted,
    )
    return re.sub(
        r"(?i)\b(api[_-]?key|access[_-]?token|password|secret)\s*[:=]\s*([^\s,;]+)",
        r"\1=[REDACTED]",
        redacted,
    )


def _agent_visible_comments(task: BenchmarkTask) -> list[dict[str, str | None]]:
    return [
        {
            "body": comment.get("body"),
            "html_url": comment.get("html_url"),
            "user_login": comment.get("user_login"),
            "created_at": comment.get("created_at"),
            "updated_at": comment.get("updated_at"),
        }
        for comment in task.issue_comments or []
    ]
