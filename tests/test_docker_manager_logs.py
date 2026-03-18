from __future__ import annotations

import subprocess

import pytest

from dank_py.lib.docker.manager import DockerCommandError, DockerManager


def test_resolve_log_container_exact(monkeypatch):
    manager = DockerManager()
    monkeypatch.setattr(
        manager,
        "list_dank_container_status",
        lambda: [
            _record("dank-py-foo", "agent", ["foo"]),
            _record("dank-py-bundle-core", "configured_bundle", ["alpha", "beta"]),
        ],
    )
    monkeypatch.setattr(manager, "get_container_host_port", lambda _name: 3000)
    resolved = manager.resolve_log_target("dank-py-foo")
    assert resolved.container_name == "dank-py-foo"
    assert resolved.agent_id is None


def test_resolve_log_container_agent_name(monkeypatch):
    manager = DockerManager()
    monkeypatch.setattr(
        manager,
        "list_dank_container_status",
        lambda: [_record("dank-py-my-agent", "agent", ["my-agent"])],
    )
    monkeypatch.setattr(manager, "get_container_host_port", lambda _name: 3000)
    resolved = manager.resolve_log_target("my-agent")
    assert resolved.container_name == "dank-py-my-agent"
    assert resolved.agent_id == "my-agent"


def test_resolve_log_container_ambiguous(monkeypatch):
    manager = DockerManager()
    monkeypatch.setattr(manager, "list_dank_container_status", lambda: [
        _record("dank-py-bundle-core-alpha", "adhoc_bundle", ["core"]),
        _record("dank-py-bundle-core-beta", "adhoc_bundle", ["core"]),
    ])
    with pytest.raises(DockerCommandError):
        manager.resolve_log_target("core")


def test_resolve_log_target_maps_agent_id_from_bundle(monkeypatch):
    manager = DockerManager()
    monkeypatch.setattr(
        manager,
        "list_dank_container_status",
        lambda: [_record("dank-py-bundle-all-agents", "configured_bundle", ["langgraph-state-agent"])],
    )
    monkeypatch.setattr(manager, "get_container_host_port", lambda _name: 3000)
    resolved = manager.resolve_log_target("langgraph-state-agent")
    assert resolved.container_name == "dank-py-bundle-all-agents"
    assert resolved.agent_id == "langgraph-state-agent"
    assert resolved.host_port == 3000


def test_stream_container_logs_builds_expected_command(monkeypatch):
    manager = DockerManager()
    captured: dict[str, list[str]] = {}
    monkeypatch.setattr(manager, "_resolve_docker_command", lambda required=True: "docker")

    class _DummyProcess:
        def __init__(self):
            self.stdout = iter([])

        def wait(self):
            return 0

        @property
        def returncode(self):
            return 0

    def _fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        return _DummyProcess()

    monkeypatch.setattr(subprocess, "Popen", _fake_popen)

    manager.stream_container_logs(
        "dank-py-test",
        follow=True,
        tail=200,
        since="10m",
    )

    assert captured["cmd"] == [
        "docker",
        "logs",
        "--timestamps",
        "--tail",
        "200",
        "--since",
        "10m",
        "--follow",
        "dank-py-test",
    ]


def _record(name: str, target_type: str, agent_ids: list[str]):
    from dank_py.lib.docker.manager import ContainerStatusRecord

    return ContainerStatusRecord(
        name=name,
        image="img:latest",
        state="running",
        status_text="running",
        ports="",
        target_type=target_type,
        bundle_name=None,
        bundle_hash=None,
        agent_ids=agent_ids,
    )
