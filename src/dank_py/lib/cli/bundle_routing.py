"""Bundle prompt routing option resolution."""

from __future__ import annotations

import re
from dataclasses import dataclass

from dank_py.lib.config.loader import LoadedConfig
from dank_py.lib.config.models import AgentConfig, BundleConfig

PROMPT_AGENT_HEADER = "x-dank-agent-id"


def _normalize(value: str | None) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"[^a-z0-9_.-]", "-", text)
    text = re.sub(r"-+", "-", text).strip("-.")
    return text


def _resolve_agent_reference(ref: str, agents: list[AgentConfig]) -> str | None:
    normalized_ref = _normalize(ref)
    if not normalized_ref:
        return None

    for agent in agents:
        if _normalize(agent.id) == normalized_ref:
            return str(agent.id)
    for agent in agents:
        if _normalize(agent.name) == normalized_ref:
            return str(agent.id or agent.name)
    return None


@dataclass(slots=True)
class ResolvedBundleRouting:
    prompt_routing: str
    default_agent_id: str | None


def resolve_bundle_routing(
    *,
    loaded: LoadedConfig,
    agents: list[AgentConfig],
    bundle_name: str | None = None,
    target_type: str | None = None,
    prompt_routing_override: str | None = None,
    default_agent_override: str | None = None,
) -> ResolvedBundleRouting:
    if not agents:
        raise ValueError("Bundle target must include at least one agent.")

    bundle_cfg: BundleConfig | None = None
    if loaded.config.bundles and target_type == "configured_bundle" and bundle_name:
        bundle_cfg = next((item for item in loaded.config.bundles if item.name == bundle_name), None)

    config_routing = bundle_cfg.prompt_routing if bundle_cfg else "required"
    config_default_agent = bundle_cfg.default_agent if bundle_cfg else None

    override_routing = (prompt_routing_override or "").strip().lower() or None
    if override_routing and override_routing not in {"required", "default"}:
        raise ValueError("Invalid prompt routing override. Use 'required' or 'default'.")

    if default_agent_override and override_routing == "required":
        raise ValueError("`--default-agent` cannot be used with `--prompt-routing required`.")

    if default_agent_override and override_routing is None:
        effective_routing = "default"
    else:
        effective_routing = override_routing or config_routing

    if effective_routing == "required" and config_default_agent and not default_agent_override and bundle_cfg:
        config_source = (
            f"bundle '{bundle_name}'" if bundle_cfg and bundle_name else "bundle config"
        )
        raise ValueError(
            f"{config_source} sets default_agent while prompt routing is required. "
            "Set prompt_routing to 'default' or remove default_agent."
        )

    if effective_routing == "required":
        return ResolvedBundleRouting(
            prompt_routing="required",
            default_agent_id=None,
        )

    desired_default = default_agent_override or config_default_agent
    if desired_default:
        resolved = _resolve_agent_reference(desired_default, agents)
        if not resolved:
            available = ", ".join(sorted({str(a.id or a.name) for a in agents}))
            raise ValueError(
                f"Default agent '{desired_default}' is not part of this bundle target. "
                f"Available: {available}"
            )
        default_agent_id = resolved
    else:
        default_agent_id = str(agents[0].id or agents[0].name)

    return ResolvedBundleRouting(
        prompt_routing="default",
        default_agent_id=default_agent_id,
    )
