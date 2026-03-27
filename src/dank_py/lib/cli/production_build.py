"""`dank build:prod` command."""

from __future__ import annotations

import json
import platform as host_platform
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from dank_py.lib.cli.bundle_routing import resolve_bundle_routing
from dank_py.lib.config.loader import load_config
from dank_py.lib.constants import DEFAULT_BASE_IMAGE
from dank_py.lib.docker.manager import DockerCommandError, DockerManager
from dank_py.lib.targeting.resolver import resolve_targets_for_run_build


@dataclass(slots=True)
class ProductionBuildCommandOptions:
    config_path: str
    agent_name: str | None
    bundle_name: str | None
    bundle_agents: str | None
    adhoc_bundle_name: str | None
    prompt_routing: str | None
    default_agent: str | None
    tag: str
    registry: str | None
    namespace: str | None
    tag_by_agent: bool
    platform: str
    push: bool | None
    load: bool | None
    no_cache: bool
    base_image: str
    pull_base: bool
    force_base: bool
    output_metadata: str | None
    verbose: bool


@dataclass(slots=True)
class ProductionBuildItem:
    target: str
    target_type: str
    image_name: str
    success: bool
    pushed: bool
    loaded: bool
    agent_ids: list[str]
    error: str | None = None


@dataclass(slots=True)
class ProductionBuildCommandResult:
    success: bool
    results: list[ProductionBuildItem]
    platform: str
    push: bool
    load: bool
    metadata_path: str | None = None


def _build_image_name(
    manager: DockerManager,
    *,
    target_name: str,
    tag: str,
    registry: str | None,
    namespace: str | None,
    tag_by_agent: bool,
) -> str:
    def _normalize_repo_path(value: str) -> str:
        parts = [segment.strip() for segment in str(value).split("/") if segment.strip()]
        if not parts:
            return manager.normalize_docker_name(value)
        return "/".join(manager.normalize_docker_name(part) for part in parts)

    normalized_target = manager.normalize_docker_name(target_name)
    normalized_tag = manager.normalize_docker_name(tag)

    if tag_by_agent:
        repo_base = _normalize_repo_path(namespace or "dank-py-agent")
        final_tag = normalized_target
    else:
        if namespace:
            # For cloud registries (e.g. ECR), keep namespace as the repository path
            # and tag by target so all related images can live under one repository.
            repo_base = _normalize_repo_path(namespace)
            final_tag = normalized_target
        else:
            repo_base = normalized_target
            final_tag = normalized_tag
    if registry:
        repo_base = f"{registry.rstrip('/')}/{repo_base}"

    return f"{repo_base}:{final_tag}"


def _host_platform_default() -> str:
    machine = host_platform.machine().lower()
    if machine in {"arm64", "aarch64"}:
        return "linux/arm64"
    if machine in {"x86_64", "amd64"}:
        return "linux/amd64"
    return "linux/amd64"


def _resolve_push_load(options: ProductionBuildCommandOptions) -> tuple[bool, bool]:
    push = options.push
    load = options.load

    if push is None and load is None:
        resolved_push = bool(options.registry)
        resolved_load = not resolved_push
        return resolved_push, resolved_load

    resolved_push = bool(push) if push is not None else False
    resolved_load = bool(load) if load is not None else (not resolved_push)
    return resolved_push, resolved_load


def _resolve_platform(options: ProductionBuildCommandOptions, *, push: bool) -> str:
    requested = (options.platform or "").strip().lower()
    if requested and requested != "auto":
        return options.platform
    if push:
        return "linux/amd64"
    return _host_platform_default()


def production_build_command(options: ProductionBuildCommandOptions) -> ProductionBuildCommandResult:
    loaded = load_config(options.config_path)
    manager = DockerManager()
    manager.ensure_docker_available()

    resolved_push, resolved_load = _resolve_push_load(options)
    resolved_platform = _resolve_platform(options, push=resolved_push)

    if resolved_push and resolved_load:
        raise ValueError("Use either --push or --load, not both.")

    targets = resolve_targets_for_run_build(
        loaded,
        agent_name=options.agent_name,
        bundle_name=options.bundle_name,
        bundle_agents=options.bundle_agents,
        adhoc_bundle_name=options.adhoc_bundle_name,
    )

    results: list[ProductionBuildItem] = []
    for target in targets:
        image_name = _build_image_name(
            manager,
            target_name=target.name,
            tag=options.tag,
            registry=options.registry,
            namespace=options.namespace,
            tag_by_agent=options.tag_by_agent,
        )
        try:
            if target.is_bundle:
                routing = resolve_bundle_routing(
                    loaded=loaded,
                    agents=target.agents,
                    bundle_name=target.bundle_name or target.name,
                    target_type=target.target_type,
                    prompt_routing_override=options.prompt_routing,
                    default_agent_override=options.default_agent,
                )
                built = manager.build_production_bundle_image(
                    project_root=loaded.project_root,
                    bundle_name=target.name,
                    agents=target.agents,
                    prompt_routing=routing.prompt_routing,
                    default_agent=routing.default_agent_id,
                    image_name=image_name,
                    platform=resolved_platform,
                    push=resolved_push,
                    load=resolved_load,
                    no_cache=options.no_cache,
                    base_image=options.base_image or DEFAULT_BASE_IMAGE,
                    pull_base=options.pull_base,
                    force_base=options.force_base,
                    cleanup_context=True,
                    verbose=options.verbose,
                )
            else:
                built = manager.build_production_image(
                    project_root=loaded.project_root,
                    agent=target.agents[0],
                    image_name=image_name,
                    platform=resolved_platform,
                    push=resolved_push,
                    load=resolved_load,
                    no_cache=options.no_cache,
                    base_image=options.base_image or DEFAULT_BASE_IMAGE,
                    pull_base=options.pull_base,
                    force_base=options.force_base,
                    cleanup_context=True,
                    verbose=options.verbose,
                )
            results.append(
                ProductionBuildItem(
                    target=target.name,
                    target_type=target.target_type,
                    image_name=built.image_name,
                    success=True,
                    pushed=built.pushed,
                    loaded=built.loaded,
                    agent_ids=[str(agent.id or agent.name) for agent in target.agents],
                )
            )
        except DockerCommandError as exc:
            message = str(exc)
            if "no match for platform in manifest" in message:
                message = (
                    f"{message}\n"
                    f"Hint: base image '{options.base_image}' does not provide platform '{resolved_platform}'.\n"
                    "For local Apple Silicon testing, use --platform linux/arm64.\n"
                    "For production amd64, publish the base image as multi-arch (linux/amd64,linux/arm64) and retry."
                )
            results.append(
                ProductionBuildItem(
                    target=target.name,
                    target_type=target.target_type,
                    image_name=image_name,
                    success=False,
                    pushed=False,
                    loaded=False,
                    agent_ids=[str(agent.id or agent.name) for agent in target.agents],
                    error=message,
                )
            )

    metadata_path: str | None = None
    if options.output_metadata:
        metadata_output = {
            "project": loaded.config.name,
            "buildTimestamp": datetime.now(timezone.utc).isoformat(),
            "targets": [
                {
                    "name": item.target,
                    "target_type": item.target_type,
                    "agent_ids": item.agent_ids,
                    "image_name": item.image_name,
                    "success": item.success,
                    "pushed": item.pushed,
                    "loaded": item.loaded,
                    "error": item.error,
                }
                for item in results
            ],
            "summary": {
                "total": len(results),
                "successful": sum(1 for item in results if item.success),
                "failed": sum(1 for item in results if not item.success),
                "pushed": sum(1 for item in results if item.pushed),
            },
        }
        path = Path(options.output_metadata).expanduser().resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(metadata_output, indent=2) + "\n", encoding="utf-8")
        metadata_path = str(path)

    success = all(item.success for item in results)
    return ProductionBuildCommandResult(
        success=success,
        results=results,
        platform=resolved_platform,
        push=resolved_push,
        load=resolved_load,
        metadata_path=metadata_path,
    )
