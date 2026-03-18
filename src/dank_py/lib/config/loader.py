"""Config loading helpers."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from dank_py.lib.config.models import AgentConfig, ProjectConfig
from dank_py.lib.constants import DEFAULT_CONFIG_FILE


class ConfigLoadError(RuntimeError):
    """Raised when config cannot be loaded or validated."""


@dataclass(slots=True)
class LoadedConfig:
    config: ProjectConfig
    config_path: Path

    @property
    def project_root(self) -> Path:
        return self.config_path.parent


def load_config(config_path: str | Path | None = None) -> LoadedConfig:
    path = Path(config_path or DEFAULT_CONFIG_FILE).expanduser().resolve()
    if not path.exists():
        raise ConfigLoadError(f"Config file not found: {path}")

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigLoadError(f"Invalid JSON in config file {path}: {exc}") from exc

    try:
        config = ProjectConfig.model_validate(raw)
    except ValidationError as exc:
        raise ConfigLoadError(f"Config validation failed for {path}: {exc}") from exc

    return LoadedConfig(config=config, config_path=path)


def select_agent(loaded: LoadedConfig, agent_name: str | None = None) -> AgentConfig:
    agents = loaded.config.agents
    if agent_name is None:
        return agents[0]

    for agent in agents:
        if agent.id == agent_name:
            return agent

    for agent in agents:
        if agent.name == agent_name:
            return agent

    available = ", ".join(f"{agent.name}({agent.id})" for agent in agents)
    raise ConfigLoadError(
        f"Agent '{agent_name}' not found in {loaded.config_path}. Available agents: {available}"
    )
