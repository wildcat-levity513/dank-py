from __future__ import annotations

from typing import Any

import pytest

from dank_py.lib.cli.logs import LogsCommandOptions, logs_command
from dank_py.lib.docker.manager import ResolvedLogTarget


class _FakeProcess:
    def __init__(self, lines: list[str], returncode: int = 0):
        self.stdout = iter(lines)
        self.returncode = returncode

    def wait(self) -> int:
        return self.returncode


class _FakeDockerManager:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def ensure_docker_available(self) -> None:
        return None

    def resolve_log_target(self, target: str) -> ResolvedLogTarget:
        return ResolvedLogTarget(
            container_name=f"dank-py-{target}",
            target_type="agent",
            host_port=3000,
            agent_id=None,
        )

    def stream_container_logs(self, container_name: str, *, follow: bool, tail: int, since: str | None):
        self.calls.append((container_name, {"follow": follow, "tail": tail, "since": since}))
        return _FakeProcess([f"{container_name}:line-1\n", f"{container_name}:line-2\n"])

    def list_dank_containers(self) -> list[str]:
        return ["dank-py-alpha", "dank-py-beta"]


def test_logs_command_target(monkeypatch, capsys):
    fake = _FakeDockerManager()
    monkeypatch.setattr("dank_py.lib.cli.logs.DockerManager", lambda: fake)

    result = logs_command(
        LogsCommandOptions(
            target="alpha",
            follow=False,
            tail=50,
            since=None,
        )
    )

    output = capsys.readouterr().out
    assert "dank-py-alpha:line-1" in output
    assert result.targets == ["dank-py-alpha"]
    assert fake.calls[0][1]["tail"] == 50


def test_logs_command_all(monkeypatch, capsys):
    fake = _FakeDockerManager()
    monkeypatch.setattr("dank_py.lib.cli.logs.DockerManager", lambda: fake)

    result = logs_command(
        LogsCommandOptions(
            target=None,
            follow=False,
            tail=25,
            since="10m",
        )
    )

    output = capsys.readouterr().out
    assert "=== dank-py-alpha ===" in output
    assert "[dank-py-alpha]" in output
    assert "=== dank-py-beta ===" in output
    assert result.targets == ["dank-py-alpha", "dank-py-beta"]


def test_logs_command_follow_requires_target(monkeypatch):
    fake = _FakeDockerManager()
    monkeypatch.setattr("dank_py.lib.cli.logs.DockerManager", lambda: fake)

    with pytest.raises(ValueError):
        logs_command(
            LogsCommandOptions(
                target=None,
                follow=True,
                tail=100,
                since=None,
            )
        )


def test_logs_command_agent_in_bundle_uses_runtime_fetch(monkeypatch):
    class _RuntimeTargetManager(_FakeDockerManager):
        def resolve_log_target(self, target: str) -> ResolvedLogTarget:
            return ResolvedLogTarget(
                container_name="dank-py-bundle-all-agents",
                target_type="configured_bundle",
                host_port=3000,
                agent_id="langgraph-state-agent",
            )

    called: dict[str, bool] = {"fetch": False}

    def _fake_fetch(target, options):
        called["fetch"] = True

    monkeypatch.setattr("dank_py.lib.cli.logs.DockerManager", lambda: _RuntimeTargetManager())
    monkeypatch.setattr("dank_py.lib.cli.logs._fetch_runtime_logs", _fake_fetch)

    result = logs_command(
        LogsCommandOptions(
            target="langgraph-state-agent",
            follow=False,
            tail=100,
            since=None,
        )
    )

    assert called["fetch"] is True
    assert result.targets == ["dank-py-bundle-all-agents"]
