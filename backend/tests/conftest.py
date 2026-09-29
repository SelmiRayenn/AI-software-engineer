import subprocess
import time

import pytest

from app.sandbox.commands import DockerCommandSession
from app.sandbox.network import resolve_network_policy
from app.schemas.sandbox import SandboxCommandResult


@pytest.fixture
def fake_secret_samples() -> dict[str, object]:
    secrets = [
        "sk-proj-FAKEOPENAIKEY1234567890",
        "sk-ant-api03-FAKEANTHROPICKEY1234567890",
        "github_pat_FAKEGITHUBTOKEN1234567890",
        "fake-bearer-token-1234567890",
        "fake-db-password-1234567890",
        "Basic ZmFrZS11c2VyOmZha2UtcGFzc3dvcmQ=",
        "fake-password-value-1234567890",
        "fake-env-value-1234567890",
        "AKIAFAKECLOUDKEY1234",
    ]
    text = "\n".join(
        [
            secrets[0],
            secrets[1],
            secrets[2],
            f"Bearer {secrets[3]}",
            f"postgresql://demo:{secrets[4]}@db.example.test/benchmark",
            f"Authorization: {secrets[5]}",
            f"password={secrets[6]}",
            f"CUSTOM_PRIVATE_VALUE={secrets[7]}",
            f"AWS_ACCESS_KEY_ID={secrets[8]}",
        ]
    )
    return {"text": text, "secrets": secrets}


@pytest.fixture
def trusted_local_commands(monkeypatch):
    """Opt-in test double for existing trusted Python snippets, never untrusted repo code.

    Container and network isolation are exercised separately in test_sandbox_network.py.
    No production host-execution fallback exists.
    """

    def execute(self, *, workspace_path, command, phase, timeout_seconds):
        start = time.monotonic()
        timed_out = False
        try:
            result = subprocess.run(
                command,
                cwd=workspace_path,
                shell=True,
                text=True,
                capture_output=True,
                timeout=timeout_seconds,
                check=False,
            )
            exit_code, stdout, stderr = result.returncode, result.stdout, result.stderr
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            exit_code = 124
            stdout = exc.stdout or ""
            stderr = exc.stderr or ""
            if isinstance(stdout, bytes):
                stdout = stdout.decode("utf-8", errors="replace")
            if isinstance(stderr, bytes):
                stderr = stderr.decode("utf-8", errors="replace")
            stderr += f"\nCommand exceeded timeout of {timeout_seconds} seconds."
        return SandboxCommandResult(
            phase=phase,
            command=command,
            passed=exit_code == 0,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            timed_out=timed_out,
            duration_seconds=time.monotonic() - start,
            network_policy=resolve_network_policy(phase),
        )

    monkeypatch.setattr(DockerCommandSession, "execute", execute)
