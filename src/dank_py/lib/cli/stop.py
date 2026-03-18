"""`dank stop` command."""

from __future__ import annotations

from dataclasses import dataclass

from dank_py.lib.config.loader import load_config, select_agent
from dank_py.lib.docker.manager import DockerManager
from dank_py.lib.targeting.resolver import resolve_targets_for_run_build


@dataclass(slots=True)
class StopCommandOptions:
    config_path: str
    agent_name: str | None
    bundle_name: str | None
    bundle_agents: str | None
    adhoc_bundle_name: str | None
    all_agents: bool
    remove: bool


@dataclass(slots=True)
class StopCommandResult:
    stopped: list[str]


def stop_command(options: StopCommandOptions) -> StopCommandResult:
    manager = DockerManager()
    manager.ensure_docker_available()

    if options.all_agents:
        stopped = manager.stop_dank_containers(remove=options.remove)
        return StopCommandResult(stopped=stopped)

    selector_count = sum(1 for item in [options.agent_name, options.bundle_name, options.bundle_agents] if item)
    if selector_count == 0:
        stopped = manager.stop_dank_containers(remove=options.remove)
        return StopCommandResult(stopped=stopped)
    if selector_count > 1:
        raise ValueError("Use only one selector: --agent, --bundle, or --bundle-agents.")

    if options.agent_name:
        loaded = load_config(options.config_path)
        agent = select_agent(loaded, options.agent_name)
        container_name = manager.container_name_for_agent(agent.name)
        stopped = manager.stop_dank_containers(container_names=[container_name], remove=options.remove)
        return StopCommandResult(stopped=stopped)

    if options.bundle_name:
        container_name = manager.container_name_for_bundle(options.bundle_name)
        stopped = manager.stop_dank_containers(container_names=[container_name], remove=options.remove)
        return StopCommandResult(stopped=stopped)

    # options.bundle_agents
    loaded = load_config(options.config_path)
    targets = resolve_targets_for_run_build(
        loaded,
        agent_name=None,
        bundle_name=None,
        bundle_agents=options.bundle_agents,
        adhoc_bundle_name=options.adhoc_bundle_name,
    )
    if not targets:
        return StopCommandResult(stopped=[])
    target = targets[0]
    container_name = manager.container_name_for_bundle(target.name)
    stopped = manager.stop_dank_containers(container_names=[container_name], remove=options.remove)
    return StopCommandResult(stopped=stopped)

