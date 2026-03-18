from __future__ import annotations

import json
from pathlib import Path

from dank_py.lib.cli.run import RunCommandOptions, run_command
from dank_py.lib.docker.manager import DockerManager


def _write_config(path: Path) -> None:
    payload = {
        "name": "env-test",
        "version": "1",
        "agents": [
            {
                "name": "agent-one",
                "id": "agent-one",
                "entry": {
                    "file": "agent.py",
                    "symbol": "run",
                    "method": None,
                    "call_type": "auto",
                    "call_style": "auto",
                },
            }
        ],
        "bundles": [],
    }
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def test_run_auto_loads_project_dotenv(monkeypatch, tmp_path: Path) -> None:
    config_path = tmp_path / "dank.config.json"
    _write_config(config_path)
    (tmp_path / ".env").write_text("OPENAI_API_KEY=abc\n", encoding="utf-8")

    captured: dict[str, object] = {}

    monkeypatch.setattr(DockerManager, "ensure_docker_available", lambda self: None)
    monkeypatch.setattr(DockerManager, "get_container_host_port", lambda self, _name: None)
    monkeypatch.setattr(
        DockerManager,
        "find_available_host_port",
        lambda self, requested_port, avoid_ports=None: requested_port,
    )

    def _fake_run_agent_container(
        self,
        image_tag: str,
        agent_name: str,
        *,
        agent_id: str | None = None,
        host_port: int = 3000,
        detach: bool = False,
        quiet: bool = False,
        env_files: list[str] | None = None,
        env_vars: list[str] | None = None,
    ) -> str:
        captured["env_files"] = env_files or []
        captured["env_vars"] = env_vars or []
        return f"dank-py-{agent_name}"

    monkeypatch.setattr(DockerManager, "run_agent_container", _fake_run_agent_container)

    result = run_command(
        RunCommandOptions(
            config_path=str(config_path),
            agent_name=None,
            bundle_name=None,
            bundle_agents=None,
            adhoc_bundle_name=None,
            prompt_routing=None,
            default_agent=None,
            tag=None,
            base_image="dankcloud/dank-py-base:v0.1.2",
            pull_base=False,
            no_build=True,
            detached=True,
            port=3000,
            force_base=False,
            keep_build_context=False,
            verbose=False,
            quiet=False,
            env_files=[],
            env_vars=["OPENAI_MODEL=gpt-4o-mini", "LANGSMITH_API_KEY"],
            no_auto_env_file=False,
        )
    )

    assert captured["env_files"] == [str((tmp_path / ".env").resolve())]
    assert captured["env_vars"] == ["OPENAI_MODEL=gpt-4o-mini", "LANGSMITH_API_KEY"]
    assert result.env_files == [str((tmp_path / ".env").resolve())]
    assert result.env_var_keys == ["OPENAI_MODEL", "LANGSMITH_API_KEY"]


def test_run_no_auto_env_file(monkeypatch, tmp_path: Path) -> None:
    config_path = tmp_path / "dank.config.json"
    _write_config(config_path)
    (tmp_path / ".env").write_text("OPENAI_API_KEY=abc\n", encoding="utf-8")

    captured: dict[str, object] = {}

    monkeypatch.setattr(DockerManager, "ensure_docker_available", lambda self: None)
    monkeypatch.setattr(DockerManager, "get_container_host_port", lambda self, _name: None)
    monkeypatch.setattr(
        DockerManager,
        "find_available_host_port",
        lambda self, requested_port, avoid_ports=None: requested_port,
    )

    def _fake_run_agent_container(
        self,
        image_tag: str,
        agent_name: str,
        *,
        agent_id: str | None = None,
        host_port: int = 3000,
        detach: bool = False,
        quiet: bool = False,
        env_files: list[str] | None = None,
        env_vars: list[str] | None = None,
    ) -> str:
        captured["env_files"] = env_files or []
        return f"dank-py-{agent_name}"

    monkeypatch.setattr(DockerManager, "run_agent_container", _fake_run_agent_container)

    result = run_command(
        RunCommandOptions(
            config_path=str(config_path),
            agent_name=None,
            bundle_name=None,
            bundle_agents=None,
            adhoc_bundle_name=None,
            prompt_routing=None,
            default_agent=None,
            tag=None,
            base_image="dankcloud/dank-py-base:v0.1.2",
            pull_base=False,
            no_build=True,
            detached=True,
            port=3000,
            force_base=False,
            keep_build_context=False,
            verbose=False,
            quiet=False,
            env_files=[],
            env_vars=[],
            no_auto_env_file=True,
        )
    )

    assert captured["env_files"] == []
    assert result.env_files == []
