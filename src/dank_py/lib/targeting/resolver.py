"""Target resolution for run/build/stop bundle and agent selectors."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Literal

from dank_py.lib.config.loader import ConfigLoadError, LoadedConfig, select_agent
from dank_py.lib.config.models import AgentConfig


TargetType = Literal["separate_agent", "configured_bundle", "adhoc_bundle"]


@dataclass(slots=True)
class ResolvedTarget:
    target_type: TargetType
    name: str
    agents: list[AgentConfig]
    bundle_name: str | None = None
    bundle_hash: str | None = None

    @property
    def is_bundle(self) -> bool:
        return self.target_type in {"configured_bundle", "adhoc_bundle"}


def normalize_name(value: str) -> str:
    sanitized = re.sub(r"[^a-zA-Z0-9_.-]", "-", str(value).strip().lower())
    sanitized = re.sub(r"-+", "-", sanitized).strip("-.")
    return sanitized or "target"


def _bundle_hash(agent_ids: list[str]) -> str:
    joined = ",".join(sorted(agent_ids))
    return hashlib.sha1(joined.encode("utf-8")).hexdigest()[:12]


def _resolve_agent_ref(loaded: LoadedConfig, ref: str) -> AgentConfig:
    ref_value = str(ref or "").strip()
    if not ref_value:
        raise ConfigLoadError("Empty agent reference is not valid")

    for agent in loaded.config.agents:
        if agent.id == ref_value:
            return agent
    for agent in loaded.config.agents:
        if agent.name == ref_value:
            return agent

    available = ", ".join(f"{agent.name}({agent.id})" for agent in loaded.config.agents)
    raise ConfigLoadError(f"Agent reference '{ref_value}' not found. Available: {available}")


def _dedupe_agents(agents: list[AgentConfig]) -> list[AgentConfig]:
    seen: set[str] = set()
    output: list[AgentConfig] = []
    for agent in agents:
        key = agent.id or agent.name
        if key in seen:
            continue
        seen.add(key)
        output.append(agent)
    return output


def _resolve_configured_bundle(loaded: LoadedConfig, bundle_name: str) -> ResolvedTarget:
    if not loaded.config.bundles:
        raise ConfigLoadError("No bundles are defined in config.")

    for bundle in loaded.config.bundles:
        if bundle.name == bundle_name:
            agents = _dedupe_agents([_resolve_agent_ref(loaded, ref) for ref in bundle.agents])
            return ResolvedTarget(
                target_type="configured_bundle",
                name=bundle.name,
                bundle_name=bundle.name,
                agents=agents,
            )

    available = ", ".join(bundle.name for bundle in loaded.config.bundles)
    raise ConfigLoadError(f"Bundle '{bundle_name}' not found. Available bundles: {available}")


def _resolve_adhoc_bundle(
    loaded: LoadedConfig,
    *,
    bundle_agents: str,
    bundle_name: str | None,
) -> ResolvedTarget:
    raw = str(bundle_agents or "").strip()
    if not raw:
        raise ConfigLoadError("`--bundle-agents` requires a comma-separated agent list or 'all'.")

    if raw.lower() == "all":
        selected = list(loaded.config.agents)
        derived_name = normalize_name(bundle_name) if bundle_name else "all-agents"
    else:
        refs = [item.strip() for item in raw.split(",") if item.strip()]
        if not refs:
            raise ConfigLoadError("`--bundle-agents` requires at least one agent reference.")
        selected = _dedupe_agents([_resolve_agent_ref(loaded, ref) for ref in refs])
        if len(selected) == 1:
            raise ConfigLoadError("`--bundle-agents` requires at least two agents. Use `--agent` for one.")
        derived_name = normalize_name(bundle_name) if bundle_name else ""

    ids = [str(agent.id or agent.name) for agent in selected]
    digest = _bundle_hash(ids)
    if not derived_name:
        derived_name = f"adhoc-{digest}"

    return ResolvedTarget(
        target_type="adhoc_bundle",
        name=derived_name,
        bundle_name=derived_name,
        bundle_hash=digest,
        agents=selected,
    )


def resolve_targets_for_run_build(
    loaded: LoadedConfig,
    *,
    agent_name: str | None,
    bundle_name: str | None,
    bundle_agents: str | None,
    adhoc_bundle_name: str | None,
) -> list[ResolvedTarget]:
    selector_count = sum(1 for item in [agent_name, bundle_name, bundle_agents] if item)
    if selector_count > 1:
        raise ConfigLoadError("Use only one selector: --agent, --bundle, or --bundle-agents.")

    if adhoc_bundle_name and not bundle_agents:
        raise ConfigLoadError("`--bundle-name` can only be used with `--bundle-agents`.")

    if agent_name:
        agent = select_agent(loaded, agent_name)
        return [
            ResolvedTarget(
                target_type="separate_agent",
                name=agent.name,
                agents=[agent],
            )
        ]

    if bundle_name:
        return [_resolve_configured_bundle(loaded, bundle_name)]

    if bundle_agents:
        return [
            _resolve_adhoc_bundle(
                loaded,
                bundle_agents=bundle_agents,
                bundle_name=adhoc_bundle_name,
            )
        ]

    if loaded.config.bundles:
        targets: list[ResolvedTarget] = []
        bundled_ids: set[str] = set()

        for bundle in loaded.config.bundles:
            target = _resolve_configured_bundle(loaded, bundle.name)
            targets.append(target)
            for agent in target.agents:
                bundled_ids.add(str(agent.id or agent.name))

        for agent in loaded.config.agents:
            agent_id = str(agent.id or agent.name)
            if agent_id in bundled_ids:
                continue
            targets.append(
                ResolvedTarget(
                    target_type="separate_agent",
                    name=agent.name,
                    agents=[agent],
                )
            )
        return targets

    return [
        ResolvedTarget(
            target_type="separate_agent",
            name=agent.name,
            agents=[agent],
        )
        for agent in loaded.config.agents
    ]
