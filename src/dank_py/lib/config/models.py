"""Pydantic models for dank.config.json."""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class IOModelRef(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    model: str | None = None
    schema_: dict[str, Any] | None = Field(default=None, alias="schema")

    @model_validator(mode="after")
    def ensure_not_empty_schema(self) -> "IOModelRef":
        if self.schema_ is not None and not isinstance(self.schema_, dict):
            raise ValueError("schema must be a JSON object")
        return self


class IOConfig(BaseModel):
    input: IOModelRef = Field(default_factory=IOModelRef)
    output: IOModelRef = Field(default_factory=IOModelRef)
    strict_output: bool = True


class EntryConfig(BaseModel):
    file: str
    symbol: str
    method: str | None = None
    call_type: Literal["auto", "callable", "method"] = "auto"
    call_style: Literal["auto", "single_arg", "kwargs"] = "auto"


class AgentConfig(BaseModel):
    name: str
    id: str | None = None
    entry: EntryConfig
    io: IOConfig = Field(default_factory=IOConfig)


class BundleConfig(BaseModel):
    name: str
    agents: list[str]
    prompt_routing: Literal["required", "default"] = "required"
    default_agent: str | None = None

    @model_validator(mode="after")
    def ensure_agents_exist(self) -> "BundleConfig":
        refs = [str(item).strip() for item in self.agents if str(item).strip()]
        if not refs:
            raise ValueError("bundle agents must include at least one agent reference")
        self.agents = refs
        if self.default_agent is not None:
            value = str(self.default_agent).strip()
            self.default_agent = value or None
        if self.prompt_routing == "required" and self.default_agent:
            raise ValueError(
                f"bundle '{self.name}' default_agent requires prompt_routing='default'."
            )
        return self


def _normalize_identifier(value: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9_.-]", "-", str(value).strip().lower())
    normalized = re.sub(r"-+", "-", normalized).strip("-.")
    return normalized or "agent"


class ProjectConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    version: str = "1"
    agents: list[AgentConfig]
    bundles: list[BundleConfig] = Field(default_factory=list)

    @model_validator(mode="after")
    def ensure_agents_exist(self) -> "ProjectConfig":
        if not self.agents:
            raise ValueError("`agents` must contain at least one agent")

        # Default id to normalized name when not explicitly provided.
        seen_ids: set[str] = set()
        seen_names: set[str] = set()
        for agent in self.agents:
            if not agent.id:
                agent.id = _normalize_identifier(agent.name)
            if agent.id in seen_ids:
                raise ValueError(f"duplicate agent id: {agent.id}")
            if agent.name in seen_names:
                raise ValueError(f"duplicate agent name: {agent.name}")
            seen_ids.add(agent.id)
            seen_names.add(agent.name)

        if self.bundles:
            bundle_names: set[str] = set()
            known_refs = set(seen_ids) | set(seen_names)
            for bundle in self.bundles:
                if bundle.name in bundle_names:
                    raise ValueError(f"duplicate bundle name: {bundle.name}")
                bundle_names.add(bundle.name)
                bundle_known_refs = set(bundle.agents)
                for agent_ref in bundle.agents:
                    if agent_ref not in known_refs:
                        raise ValueError(
                            f"bundle '{bundle.name}' references unknown agent '{agent_ref}'"
                        )
                if bundle.default_agent and bundle.default_agent not in bundle_known_refs:
                    normalized = _normalize_identifier(bundle.default_agent)
                    has_match = any(
                        _normalize_identifier(agent.id or agent.name) == normalized
                        for agent in self.agents
                        if (agent.id in bundle_known_refs or agent.name in bundle_known_refs)
                    )
                    if not has_match:
                        raise ValueError(
                            f"bundle '{bundle.name}' default_agent '{bundle.default_agent}' is not in bundle agents"
                        )
        return self
