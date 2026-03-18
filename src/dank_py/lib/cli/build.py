"""`dank build` command."""

from __future__ import annotations

from dataclasses import dataclass

from dank_py.lib.cli.bundle_routing import resolve_bundle_routing
from dank_py.lib.config.loader import load_config
from dank_py.lib.constants import DEFAULT_BASE_IMAGE, DEFAULT_IMAGE_TAG_SUFFIX
from dank_py.lib.docker.manager import DockerManager
from dank_py.lib.targeting.resolver import resolve_targets_for_run_build


@dataclass(slots=True)
class BuildCommandOptions:
    config_path: str
    agent_name: str | None
    bundle_name: str | None
    bundle_agents: str | None
    adhoc_bundle_name: str | None
    prompt_routing: str | None
    default_agent: str | None
    tag: str | None
    base_image: str
    pull_base: bool
    skip_base_build: bool
    force_base: bool
    cleanup_context: bool = False
    verbose: bool = False


@dataclass(slots=True)
class BuildCommandResult:
    target_type: str
    target_name: str
    image_tag: str
    context_path: str
    agent_ids: list[str]


def build_command(options: BuildCommandOptions) -> list[BuildCommandResult]:
    loaded = load_config(options.config_path)
    targets = resolve_targets_for_run_build(
        loaded,
        agent_name=options.agent_name,
        bundle_name=options.bundle_name,
        bundle_agents=options.bundle_agents,
        adhoc_bundle_name=options.adhoc_bundle_name,
    )

    manager = DockerManager()
    manager.ensure_docker_available()

    if options.tag and len(targets) != 1:
        raise ValueError("`--tag` can only be used when building a single target.")

    results: list[BuildCommandResult] = []
    for target in targets:
        if target.is_bundle:
            routing = resolve_bundle_routing(
                loaded=loaded,
                agents=target.agents,
                bundle_name=target.bundle_name or target.name,
                target_type=target.target_type,
                prompt_routing_override=options.prompt_routing,
                default_agent_override=options.default_agent,
            )
            image_tag = options.tag or f"dank-py-bundle-{manager.normalize_docker_name(target.name)}:{DEFAULT_IMAGE_TAG_SUFFIX}"
            built = manager.build_bundle_image(
                project_root=loaded.project_root,
                bundle_name=target.name,
                agents=target.agents,
                prompt_routing=routing.prompt_routing,
                default_agent=routing.default_agent_id,
                image_tag=image_tag,
                build_base=not options.skip_base_build,
                base_image=options.base_image or DEFAULT_BASE_IMAGE,
                force_base=options.force_base,
                pull_base=options.pull_base,
                cleanup_context=options.cleanup_context,
                verbose=options.verbose,
            )
        else:
            agent = target.agents[0]
            image_tag = options.tag or f"dank-py-agent-{manager.normalize_docker_name(agent.name)}:{DEFAULT_IMAGE_TAG_SUFFIX}"
            built = manager.build_agent_image(
                project_root=loaded.project_root,
                agent=agent,
                image_tag=image_tag,
                build_base=not options.skip_base_build,
                base_image=options.base_image or DEFAULT_BASE_IMAGE,
                force_base=options.force_base,
                pull_base=options.pull_base,
                cleanup_context=options.cleanup_context,
                verbose=options.verbose,
            )

        results.append(
            BuildCommandResult(
                target_type=target.target_type,
                target_name=target.name,
                image_tag=built.image_tag,
                context_path=str(built.context_path),
                agent_ids=[str(agent.id or agent.name) for agent in target.agents],
            )
        )

    return results
