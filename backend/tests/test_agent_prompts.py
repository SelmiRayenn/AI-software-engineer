from uuid import uuid4

from app.agents.prompts import render_agent_prompts
from app.models import BenchmarkTask, GoldPatch, Repository


def test_prompt_rendering_includes_issue_repository_tools_and_constraints() -> None:
    task, repository = prompt_models()

    prompts = render_agent_prompts(
        task=task,
        repository=repository,
        allowed_tools=["list_files", "read_file", "run_tests", "submit_patch"],
        configured_test_commands=["pytest -q"],
        max_steps=12,
        max_tool_errors=2,
        command_timeout_seconds=90,
        include_issue_comments=True,
        enable_test_tool=True,
        run_mode="tool_loop",
    )

    assert "Fix incorrect addition" in prompts.issue_context_prompt
    assert "Adding negative numbers fails" in prompts.issue_context_prompt
    assert "example/calculator" in prompts.issue_context_prompt
    assert "Please preserve backwards compatibility" in prompts.issue_context_prompt
    assert "- run_tests" in prompts.tool_use_instructions
    assert "pytest -q" in prompts.tool_use_instructions
    assert "Maximum steps: 12" in prompts.developer_safety_prompt
    assert "Maximum tool errors: 2" in prompts.developer_safety_prompt
    assert "90 seconds" in prompts.developer_safety_prompt
    assert '"name": "submit_patch"' in prompts.patch_submission_instructions


def test_prompt_rendering_excludes_gold_solution_data() -> None:
    task, repository = prompt_models()
    task.gold_patch = GoldPatch(
        benchmark_task_id=task.id,
        changed_files=["secret/gold_solution.py"],
        patch_text="HIDDEN GOLD PATCH CONTENT",
        test_files=["secret/test_gold_solution.py"],
    )

    prompts = render_agent_prompts(
        task=task,
        repository=repository,
        allowed_tools=["list_files", "submit_patch"],
        configured_test_commands=[],
        max_steps=4,
        max_tool_errors=3,
        command_timeout_seconds=120,
        include_issue_comments=False,
        enable_test_tool=False,
        run_mode="scripted",
    )
    rendered = "\n".join(message.content for message in prompts.messages())

    assert "HIDDEN GOLD PATCH CONTENT" not in rendered
    assert "secret/gold_solution.py" not in rendered
    assert "Please preserve backwards compatibility" not in rendered


def test_allowed_tool_prompt_matches_test_tool_setting() -> None:
    task, repository = prompt_models()

    prompts = render_agent_prompts(
        task=task,
        repository=repository,
        allowed_tools=["list_files", "read_file", "submit_patch"],
        configured_test_commands=["pytest -q"],
        max_steps=4,
        max_tool_errors=3,
        command_timeout_seconds=120,
        include_issue_comments=True,
        enable_test_tool=False,
        run_mode="tool_loop",
    )

    assert "- run_tests" not in prompts.tool_use_instructions
    assert "pytest -q" not in prompts.tool_use_instructions
    assert "Test tool status: disabled" in prompts.tool_use_instructions


def test_prompt_preview_redacts_common_credentials() -> None:
    task, repository = prompt_models(issue_body="API_KEY=super-secret-value")

    preview = render_agent_prompts(
        task=task,
        repository=repository,
        allowed_tools=["list_files", "submit_patch"],
        configured_test_commands=[],
        max_steps=4,
        max_tool_errors=3,
        command_timeout_seconds=120,
        include_issue_comments=False,
        enable_test_tool=False,
        run_mode="tool_loop",
    ).redacted_preview()

    assert "super-secret-value" not in preview["issue_context_prompt"]
    assert "[REDACTED]" in preview["issue_context_prompt"]


def prompt_models(
    *,
    issue_body: str = "Adding negative numbers fails.",
) -> tuple[BenchmarkTask, Repository]:
    repository = Repository(
        id=uuid4(),
        name="calculator",
        owner="example",
        url="https://github.com/example/calculator",
        default_branch="main",
        language="Python",
    )
    task = BenchmarkTask(
        id=uuid4(),
        repository_id=repository.id,
        issue_number=42,
        issue_title="Fix incorrect addition",
        issue_body=issue_body,
        issue_comments=[
            {
                "body": "Please preserve backwards compatibility.",
                "html_url": "https://github.com/example/calculator/issues/42#issuecomment-1",
                "user_login": "maintainer",
                "created_at": "2026-01-01T00:00:00Z",
                "updated_at": "2026-01-01T00:00:00Z",
            }
        ],
        pull_request_number=43,
        base_commit="1111111111111111111111111111111111111111",
        setup_commands=[],
        test_commands=["pytest -q"],
        status="ready",
    )
    return task, repository
