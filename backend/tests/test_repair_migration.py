import pytest
from sqlalchemy import create_engine, inspect, text

from alembic import command
from app.db.migrations import make_alembic_config


def test_repair_upgrade_preserves_existing_patch_reviews_and_logs(tmp_path):
    url = f"sqlite:///{(tmp_path / 'repair-migration.db').as_posix()}"
    config = make_alembic_config(url)
    command.upgrade(config, "20260905_0003")
    engine = create_engine(url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO repositories (id, name, owner, url, default_branch) "
                "VALUES (:id, 'repo', 'owner', 'https://github.com/owner/repo', 'main')"
            ),
            {"id": "1" * 32},
        )
        connection.execute(
            text(
                "INSERT INTO benchmark_tasks (id, repository_id, issue_number, issue_title, "
                "base_commit, status, issue_comments, setup_commands, test_commands) "
                "VALUES (:id, :repo, 1, 'Fix issue', 'base', 'ready', '[]', '[]', '[]')"
            ),
            {"id": "2" * 32, "repo": "1" * 32},
        )
        connection.execute(
            text(
                "INSERT INTO agent_runs (id, benchmark_task_id, model_provider, model_name, status) "
                "VALUES (:id, :task, 'mock', 'mock', 'completed')"
            ),
            {"id": "3" * 32, "task": "2" * 32},
        )
        connection.execute(
            text(
                "INSERT INTO generated_patches (id, agent_run_id, patch_text, changed_files) "
                "VALUES (:id, :run, 'original diff', '[]')"
            ),
            {"id": "4" * 32, "run": "3" * 32},
        )
        connection.execute(
            text(
                "INSERT INTO human_reviews (id, generated_patch_id, decision, reviewer_name) "
                "VALUES (:id, :patch, 'approved', 'Reviewer')"
            ),
            {"id": "5" * 32, "patch": "4" * 32},
        )
        connection.execute(
            text(
                "INSERT INTO test_results (id, agent_run_id, phase, command, passed, exit_code, stdout) "
                "VALUES (:id, :run, 'post_patch', 'pytest', true, 0, 'passed')"
            ),
            {"id": "6" * 32, "run": "3" * 32},
        )
    command.upgrade(config, "head")
    with engine.connect() as connection:
        patch = connection.execute(text("SELECT * FROM generated_patches")).mappings().one()
        assert patch["id"] == "4" * 32 and patch["version"] == 1 and patch["is_selected"]
        assert (
            connection.execute(text("SELECT decision FROM human_reviews")).scalar_one()
            == "approved"
        )
        result = connection.execute(text("SELECT * FROM test_results")).mappings().one()
        assert result["generated_patch_id"] == patch["id"] and result["stdout"] == "passed"
        assert connection.execute(
            text("SELECT final_patch_passed_tests FROM agent_runs")
        ).scalar_one()
        assert not inspect(connection).get_indexes("generated_patches")[0]["unique"]
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO generated_patches (id, agent_run_id, patch_text, changed_files, version) "
                "VALUES (:id, :run, 'new diff', '[]', 2)"
            ),
            {"id": "7" * 32, "run": "3" * 32},
        )
    with pytest.raises(RuntimeError, match="multiple patch versions"):
        command.downgrade(config, "20260905_0003")
    with engine.connect() as connection:
        assert connection.execute(text("SELECT COUNT(*) FROM generated_patches")).scalar_one() == 2
        assert (
            connection.execute(text("SELECT decision FROM human_reviews")).scalar_one()
            == "approved"
        )
    engine.dispose()
