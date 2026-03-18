from __future__ import annotations

import pytest

from dank_py.lib.docker.manager import DockerCommandError, DockerManager


def test_ensure_docker_available_no_remediation_needed(monkeypatch):
    manager = DockerManager()
    calls = {"install": 0, "start": 0}

    monkeypatch.setattr(manager, "_resolve_docker_command", lambda required=False: "docker")
    monkeypatch.setattr(manager, "_docker_daemon_accessible", lambda: True)
    monkeypatch.setattr(manager, "install_docker", lambda: calls.__setitem__("install", calls["install"] + 1))
    monkeypatch.setattr(manager, "start_docker", lambda: calls.__setitem__("start", calls["start"] + 1))

    manager.ensure_docker_available()

    assert calls["install"] == 0
    assert calls["start"] == 0


def test_ensure_docker_available_prompts_install_when_missing(monkeypatch):
    manager = DockerManager()
    calls = {"install": 0}
    responses = iter([None, "docker"])

    monkeypatch.setattr(manager, "_resolve_docker_command", lambda required=False: next(responses))
    monkeypatch.setattr(manager, "_is_interactive", lambda: True)
    monkeypatch.setattr(manager, "_prompt_yes_no", lambda _q, default=False: True)
    monkeypatch.setattr(manager, "_docker_daemon_accessible", lambda: True)
    monkeypatch.setattr(manager, "install_docker", lambda: calls.__setitem__("install", calls["install"] + 1))

    manager.ensure_docker_available()

    assert calls["install"] == 1


def test_ensure_docker_available_attempts_start_when_daemon_down(monkeypatch):
    manager = DockerManager()
    calls = {"start": 0}

    monkeypatch.setattr(manager, "_resolve_docker_command", lambda required=False: "docker")
    monkeypatch.setattr(manager, "_docker_daemon_accessible", lambda: False)
    monkeypatch.setattr(manager, "_wait_for_docker", lambda timeout_seconds=120: True)
    monkeypatch.setattr(manager, "start_docker", lambda: calls.__setitem__("start", calls["start"] + 1))

    manager.ensure_docker_available()

    assert calls["start"] == 1


def test_ensure_docker_available_raises_when_install_declined(monkeypatch):
    manager = DockerManager()

    monkeypatch.setattr(manager, "_resolve_docker_command", lambda required=False: None)
    monkeypatch.setattr(manager, "_is_interactive", lambda: True)
    monkeypatch.setattr(manager, "_prompt_yes_no", lambda _q, default=False: False)

    with pytest.raises(DockerCommandError):
        manager.ensure_docker_available()
