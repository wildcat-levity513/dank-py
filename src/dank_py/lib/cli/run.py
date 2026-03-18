"""`dank run` command."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from dank_py.lib.cli.bundle_routing import PROMPT_AGENT_HEADER, resolve_bundle_routing
from dank_py.lib.cli.build import BuildCommandOptions, build_command
from dank_py.lib.config.loader import load_config
from dank_py.lib.constants import DEFAULT_BASE_IMAGE, DEFAULT_IMAGE_TAG_SUFFIX, DEFAULT_PORT
from dank_py.lib.docker.manager import DockerManager
from dank_py.lib.targeting.resolver import resolve_targets_for_run_build


@dataclass(slots=True)
class RunCommandOptions:
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
    no_build: bool
    detached: bool
    port: int
    force_base: bool
    keep_build_context: bool
    verbose: bool
    quiet: bool
    env_files: list[str]
    env_vars: list[str]
    no_auto_env_file: bool


@dataclass(slots=True)
class RunAgentResult:
    target_type: str
    target_name: str
    agent_ids: list[str]
    image_tag: str
    container_name: str
    port: int
    prompt_agent_header: str | None = None
    prompt_routing: str | None = None
    default_agent_id: str | None = None


@dataclass(slots=True)
class RunCommandResult:
    agents: list[RunAgentResult]
    detached: bool
    env_files: list[str]
    env_var_keys: list[str]


_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _resolve_env_files(
    *,
    project_root: Path,
    env_files: list[str],
    no_auto_env_file: bool,
) -> list[str]:
    resolved: list[str] = []
    for raw in env_files:
        value = str(raw or "").strip()
        if not value:
            continue
        candidate = Path(value).expanduser()
        if not candidate.is_absolute():
            candidate = project_root / candidate
        candidate = candidate.resolve()
        if not candidate.exists() or not candidate.is_file():
            raise ValueError(f"Env file not found: {candidate}")
        resolved.append(str(candidate))
    if resolved:
        return resolved

    if no_auto_env_file:
        return []

    default_env = (project_root / ".env").resolve()
    if default_env.exists() and default_env.is_file():
        return [str(default_env)]
    return []


def _resolve_env_vars(values: list[str]) -> tuple[list[str], list[str]]:
    resolved: list[str] = []
    keys: list[str] = []
    seen: set[str] = set()
    for raw in values:
        value = str(raw or "").strip()
        if not value:
            continue
        if "=" in value:
            key = value.split("=", 1)[0].strip()
        else:
            key = value
        if not key:
            raise ValueError(f"Invalid env var '{raw}'. Expected KEY=VALUE or KEY.")
        if not _ENV_KEY_RE.match(key):
            raise ValueError(f"Invalid env var key '{key}'.")
        resolved.append(value)
        if key not in seen:
            keys.append(key)
            seen.add(key)
    return resolved, keys


def run_command(options: RunCommandOptions) -> RunCommandResult:
    loaded = load_config(options.config_path)
    prompt_header = PROMPT_AGENT_HEADER
    targets = resolve_targets_for_run_build(
        loaded,
        agent_name=options.agent_name,
        bundle_name=options.bundle_name,
        bundle_agents=options.bundle_agents,
        adhoc_bundle_name=options.adhoc_bundle_name,
    )

    manager = DockerManager()

    if options.tag and len(targets) != 1:
        raise ValueError("`--tag` can only be used when running a single target.")

    resolved_env_files = _resolve_env_files(
        project_root=loaded.project_root,
        env_files=options.env_files,
        no_auto_env_file=options.no_auto_env_file,
    )
    resolved_env_vars, resolved_env_keys = _resolve_env_vars(options.env_vars)

    foreground_single = (not options.detached) and len(targets) == 1
    # For foreground UX, run detached internally then attach via formatted `dank logs`.
    run_detached = True if foreground_single else (options.detached or len(targets) > 1)
    results: list[RunAgentResult] = []
    base_port = options.port or DEFAULT_PORT
    allocated_ports: set[int] = set()

    built_map: dict[str, str] = {}
    if not options.no_build:
        built = build_command(
            BuildCommandOptions(
                config_path=options.config_path,
                agent_name=options.agent_name,
                bundle_name=options.bundle_name,
                bundle_agents=options.bundle_agents,
                adhoc_bundle_name=options.adhoc_bundle_name,
                prompt_routing=options.prompt_routing,
                default_agent=options.default_agent,
                tag=options.tag,
                base_image=options.base_image or DEFAULT_BASE_IMAGE,
                pull_base=options.pull_base,
                skip_base_build=False,
                force_base=options.force_base,
                cleanup_context=not options.keep_build_context,
                verbose=options.verbose,
            )
        )
        for item in built:
            built_map[f"{item.target_type}:{item.target_name}"] = item.image_tag

    # `build_command` already validates Docker availability for build flows.
    # For --no-build (or after builds complete), ensure runtime Docker access once.
    if options.no_build or not built_map:
        manager.ensure_docker_available()

    for idx, target in enumerate(targets):
        requested_port = base_port + idx
        if target.is_bundle:
            target_container_name = manager.container_name_for_bundle(target.name)
        else:
            target_container_name = manager.container_name_for_agent(target.agents[0].name)

        preserved_port = manager.get_container_host_port(target_container_name)
        if preserved_port is not None and preserved_port not in allocated_ports:
            port = preserved_port
        else:
            port = manager.find_available_host_port(requested_port, avoid_ports=allocated_ports)
        allocated_ports.add(port)

        if target.is_bundle:
            routing = resolve_bundle_routing(
                loaded=loaded,
                agents=target.agents,
                bundle_name=target.bundle_name or target.name,
                target_type=target.target_type,
                prompt_routing_override=options.prompt_routing,
                default_agent_override=options.default_agent,
            )
            image_tag = (
                options.tag
                or built_map.get(f"{target.target_type}:{target.name}")
                or f"dank-py-bundle-{manager.normalize_docker_name(target.name)}:{DEFAULT_IMAGE_TAG_SUFFIX}"
            )
            container_name = manager.run_bundle_container(
                image_tag=image_tag,
                bundle_name=target.name,
                agent_ids=[str(agent.id or agent.name) for agent in target.agents],
                host_port=port,
                detach=run_detached,
                quiet=options.quiet,
                bundle_hash=target.bundle_hash,
                target_type=target.target_type,
                prompt_routing=routing.prompt_routing,
                default_agent=routing.default_agent_id,
                env_files=resolved_env_files,
                env_vars=resolved_env_vars,
            )
        else:
            agent = target.agents[0]
            image_tag = (
                options.tag
                or built_map.get(f"{target.target_type}:{target.name}")
                or f"dank-py-agent-{manager.normalize_docker_name(agent.name)}:{DEFAULT_IMAGE_TAG_SUFFIX}"
            )
            container_name = manager.run_agent_container(
                image_tag=image_tag,
                agent_name=agent.name,
                agent_id=str(agent.id or agent.name),
                host_port=port,
                detach=run_detached,
                quiet=options.quiet,
                env_files=resolved_env_files,
                env_vars=resolved_env_vars,
            )

        results.append(
            RunAgentResult(
                target_type=target.target_type,
                target_name=target.name,
                agent_ids=[str(agent.id or agent.name) for agent in target.agents],
                image_tag=image_tag,
                container_name=container_name,
                port=port,
                prompt_agent_header=(
                    PROMPT_AGENT_HEADER if target.is_bundle else prompt_header
                ),
                prompt_routing=(routing.prompt_routing if target.is_bundle else None),
                default_agent_id=(routing.default_agent_id if target.is_bundle else None),
            )
        )

    return RunCommandResult(
        agents=results,
        detached=not foreground_single and run_detached,
        env_files=resolved_env_files,
        env_var_keys=resolved_env_keys,
    )
