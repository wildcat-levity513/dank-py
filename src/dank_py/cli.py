"""dank-py CLI entrypoint."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

from dank_py import __version__ as PACKAGE_VERSION
from dank_py.lib.cli.build import BuildCommandOptions, build_command
from dank_py.lib.cli.clean import CleanCommandOptions, clean_command
from dank_py.lib.cli.deps import deps_command
from dank_py.lib.cli.init import init_command
from dank_py.lib.cli.inspect import (
    apply_candidates_to_config,
    apply_entry_to_config,
    inspect_command,
    inspect_payload,
)
from dank_py.lib.cli.logs import LogsCommandOptions, logs_command
from dank_py.lib.cli.production_build import ProductionBuildCommandOptions, production_build_command
from dank_py.lib.cli.run import RunCommandOptions, run_command
from dank_py.lib.cli.status import status_command
from dank_py.lib.cli.stop import StopCommandOptions, stop_command
from dank_py.lib.constants import DEFAULT_BASE_IMAGE, DEFAULT_CONFIG_FILE, DEFAULT_LOCK_PYTHON_VERSION, DEFAULT_PORT
from dank_py.lib.docker.manager import DockerManager


def _read_source_tree_version() -> str | None:
    pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
    if not pyproject.exists():
        return None
    try:
        content = pyproject.read_text(encoding="utf-8")
    except Exception:  # noqa: BLE001
        return None
    match = re.search(r'(?m)^version\\s*=\\s*\"([^\"]+)\"\\s*$', content)
    if not match:
        return None
    return str(match.group(1)).strip() or None


def _version_payload() -> dict[str, object]:
    manager = DockerManager()
    docker_status = manager.docker_status()
    installed_base_images: list[str] = manager.list_base_images() if docker_status == "running" else []
    source_tree_version = _read_source_tree_version()
    return {
        "cli_version": PACKAGE_VERSION,
        "source_tree_version": source_tree_version,
        "default_base_image": DEFAULT_BASE_IMAGE,
        "docker_status": docker_status,
        "installed_base_images": installed_base_images,
        "default_base_image_installed": DEFAULT_BASE_IMAGE in installed_base_images,
    }


def _print_version(payload: dict[str, object]) -> None:
    print(f"dank-py {payload.get('cli_version', 'unknown')}")
    source_tree_version = payload.get("source_tree_version")
    if source_tree_version and source_tree_version != payload.get("cli_version"):
        print(f"source tree version: {source_tree_version}")
    print(f"default base image: {payload.get('default_base_image', DEFAULT_BASE_IMAGE)}")
    print(f"docker: {payload.get('docker_status', 'unknown')}")
    installed = payload.get("installed_base_images")
    if isinstance(installed, list) and installed:
        print("installed base images:")
        for ref in installed:
            suffix = " (default)" if ref == payload.get("default_base_image") else ""
            print(f"  - {ref}{suffix}")
    else:
        print("installed base images: none")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dank",
        description="dank-py - framework-agnostic Python agent containerization CLI",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="Scaffold a new dank-py project")
    init_parser.add_argument("name", nargs="?", help="Optional directory name")
    init_parser.add_argument("--force", action="store_true", help="Overwrite existing scaffold files")

    auto_init_parser = subparsers.add_parser(
        "auto-init",
        help="Run init + inspect + deps and auto-apply top inspect candidate to config",
    )
    auto_init_parser.add_argument("name", nargs="?", help="Optional directory name")
    auto_init_parser.add_argument("--force", action="store_true", help="Overwrite existing scaffold files")
    auto_init_parser.add_argument(
        "--validate-dry",
        action="store_true",
        help="Run isolated non-live smoke validation (imports/resolve/invoke)",
    )
    auto_init_parser.add_argument(
        "--validate-full",
        action="store_true",
        help="Run isolated live validation (requires real env vars and non-mock outputs)",
    )
    auto_init_parser.add_argument(
        "--strict",
        action="store_true",
        help="Alias for --validate-full",
    )
    auto_init_parser.add_argument(
        "--fallback-freeze",
        action="store_true",
        help="Allow pip freeze fallback when resolver-based lock generation is unavailable",
    )
    auto_init_parser.add_argument(
        "--no-discover-imports",
        action="store_true",
        help="Disable import-based dependency discovery when no dependency files exist",
    )
    auto_init_parser.add_argument(
        "--install-tools",
        action="store_true",
        help="Auto-install missing resolver tools (pip-tools) in current environment",
    )
    auto_init_parser.add_argument(
        "--no-install-prompt",
        action="store_true",
        help="Disable interactive prompt to install missing resolver tools",
    )
    auto_init_parser.add_argument(
        "--lock-python-version",
        default=DEFAULT_LOCK_PYTHON_VERSION,
        help=f"Target Python version for resolver-based lock generation (default: {DEFAULT_LOCK_PYTHON_VERSION})",
    )
    auto_init_parser.add_argument(
        "--include-lock-comments",
        action="store_true",
        help="Keep resolver metadata comments in requirements.lock.txt",
    )
    auto_init_parser.add_argument(
        "--no-refresh-lock",
        action="store_true",
        help="Reuse existing requirements.lock.txt instead of regenerating it",
    )

    deps_parser = subparsers.add_parser("deps", help="Prepare dependency lock file")
    deps_parser.add_argument("--project-dir", help="Project directory (default: current directory)")
    deps_parser.add_argument(
        "-c",
        "--config",
        default=DEFAULT_CONFIG_FILE,
        help="Path to dank.config.json (used for validation modes)",
    )
    deps_parser.add_argument(
        "--validate-dry",
        action="store_true",
        help="Run isolated non-live smoke validation (imports/resolve/invoke)",
    )
    deps_parser.add_argument(
        "--validate-full",
        action="store_true",
        help="Run isolated live validation (requires real env vars and non-mock outputs)",
    )
    deps_parser.add_argument(
        "--fallback-freeze",
        action="store_true",
        help="Allow pip freeze fallback when resolver-based lock generation is unavailable",
    )
    deps_parser.add_argument(
        "--no-discover-imports",
        action="store_true",
        help="Disable import-based dependency discovery when no dependency files exist",
    )
    deps_parser.add_argument(
        "--install-tools",
        action="store_true",
        help="Auto-install missing resolver tools (pip-tools) in current environment",
    )
    deps_parser.add_argument(
        "--no-install-prompt",
        action="store_true",
        help="Disable interactive prompt to install missing resolver tools",
    )
    deps_parser.add_argument(
        "--lock-python-version",
        default=DEFAULT_LOCK_PYTHON_VERSION,
        help=f"Target Python version for resolver-based lock generation (default: {DEFAULT_LOCK_PYTHON_VERSION})",
    )
    deps_parser.add_argument(
        "--include-lock-comments",
        action="store_true",
        help="Keep resolver metadata comments in requirements.lock.txt",
    )
    deps_parser.add_argument(
        "--no-refresh-lock",
        action="store_true",
        help="Reuse existing requirements.lock.txt instead of regenerating it",
    )

    inspect_parser = subparsers.add_parser("inspect", help="Inspect project for agent/model candidates")
    inspect_parser.add_argument("--project-dir", help="Project directory (default: current directory)")
    inspect_parser.add_argument("--json", action="store_true", help="Print JSON output")
    inspect_parser.add_argument(
        "--interactive",
        action="store_true",
        help="Interactively choose and apply candidate (default behavior)",
    )
    inspect_parser.add_argument("--apply", action="store_true", help="Apply selected candidate to config")
    inspect_parser.add_argument(
        "--candidate-index",
        type=int,
        default=1,
        help="1-based candidate index to apply when using --apply (default: 1)",
    )
    inspect_parser.add_argument(
        "-c",
        "--config",
        default=DEFAULT_CONFIG_FILE,
        help="Path to dank.config.json for --apply/--interactive",
    )

    build_parser = subparsers.add_parser("build", help="Build agent container image")
    build_parser.add_argument("-c", "--config", default=DEFAULT_CONFIG_FILE, help="Path to dank.config.json")
    build_parser.add_argument("--agent", help="Build one agent target by name or id")
    build_parser.add_argument("--bundle", help="Build one configured bundle by bundle name")
    build_parser.add_argument(
        "--bundle-agents",
        help="Build one ad-hoc bundle from csv agent ids/names, or 'all'",
    )
    build_parser.add_argument(
        "--bundle-name",
        help="Optional ad-hoc bundle name (only with --bundle-agents)",
    )
    build_parser.add_argument(
        "--prompt-routing",
        choices=["required", "default"],
        help="Bundle /prompt routing mode override",
    )
    build_parser.add_argument(
        "--default-agent",
        help="Bundle default agent id/name for prompt-routing=default",
    )
    build_parser.add_argument("--tag", help="Output image tag")
    build_parser.add_argument("--base-image", default=DEFAULT_BASE_IMAGE, help="Base image name (registry or local)")
    build_parser.add_argument(
        "--pull-base",
        action="store_true",
        help="Force pull of base image before target build",
    )
    build_parser.add_argument(
        "--no-base-build",
        action="store_true",
        help="Do not locally build base image if pull fails",
    )
    build_parser.add_argument("--force-base", action="store_true", help="Force rebuild of base image")
    build_parser.add_argument("--verbose", action="store_true", help="Stream raw Docker build logs")
    build_parser.add_argument("--json", action="store_true", help="Print JSON output")

    prod_build_parser = subparsers.add_parser("build:prod", help="Build production image(s) with buildx")
    prod_build_parser.add_argument("-c", "--config", default=DEFAULT_CONFIG_FILE, help="Path to dank.config.json")
    prod_build_parser.add_argument("--agent", help="Build one agent target by name or id")
    prod_build_parser.add_argument("--bundle", help="Build one configured bundle by bundle name")
    prod_build_parser.add_argument(
        "--bundle-agents",
        help="Build one ad-hoc bundle from csv agent ids/names, or 'all'",
    )
    prod_build_parser.add_argument(
        "--bundle-name",
        help="Optional ad-hoc bundle name (only with --bundle-agents)",
    )
    prod_build_parser.add_argument(
        "--prompt-routing",
        choices=["required", "default"],
        help="Bundle /prompt routing mode override",
    )
    prod_build_parser.add_argument(
        "--default-agent",
        help="Bundle default agent id/name for prompt-routing=default",
    )
    prod_build_parser.add_argument("--tag", default="latest", help="Image tag")
    prod_build_parser.add_argument("--registry", help="Registry host (e.g. 123.dkr.ecr.us-east-1.amazonaws.com)")
    prod_build_parser.add_argument("--namespace", help="Repository namespace/prefix")
    prod_build_parser.add_argument(
        "--tag-by-agent",
        action="store_true",
        help="Use agent name as tag and shared namespace as repository",
    )
    prod_build_parser.add_argument(
        "--platform",
        default="auto",
        help="Buildx platform(s). Default: auto (push=>linux/amd64, local=>host arch)",
    )
    prod_build_parser.add_argument(
        "--push",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Push image(s) to registry (default: auto)",
    )
    prod_build_parser.add_argument(
        "--load",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Load built image(s) into local Docker (default: auto)",
    )
    prod_build_parser.add_argument("--no-cache", action="store_true", help="Disable Docker build cache")
    prod_build_parser.add_argument("--base-image", default=DEFAULT_BASE_IMAGE, help="Base image name")
    prod_build_parser.add_argument(
        "--pull-base",
        action="store_true",
        help="Force pull of base image before production build",
    )
    prod_build_parser.add_argument("--force-base", action="store_true", help="Force rebuild/pull of base image")
    prod_build_parser.add_argument("--output-metadata", help="Write build metadata JSON to file")
    prod_build_parser.add_argument("--verbose", action="store_true", help="Stream raw buildx output")
    prod_build_parser.add_argument("--json", action="store_true", help="Print JSON result")

    run_parser = subparsers.add_parser("run", help="Run agent/bundle container target(s)")
    run_parser.add_argument("-c", "--config", default=DEFAULT_CONFIG_FILE, help="Path to dank.config.json")
    run_parser.add_argument("--agent", help="Run one agent target by name or id")
    run_parser.add_argument("--bundle", help="Run one configured bundle by bundle name")
    run_parser.add_argument(
        "--bundle-agents",
        help="Run one ad-hoc bundle from csv agent ids/names, or 'all'",
    )
    run_parser.add_argument(
        "--bundle-name",
        help="Optional ad-hoc bundle name (only with --bundle-agents)",
    )
    run_parser.add_argument(
        "--prompt-routing",
        choices=["required", "default"],
        help="Bundle /prompt routing mode override",
    )
    run_parser.add_argument(
        "--default-agent",
        help="Bundle default agent id/name for prompt-routing=default",
    )
    run_parser.add_argument("--tag", help="Image tag to run/build")
    run_parser.add_argument("--base-image", default=DEFAULT_BASE_IMAGE, help="Base image name (registry or local)")
    run_parser.add_argument(
        "--pull-base",
        action="store_true",
        help="Force pull of base image before image build",
    )
    run_parser.add_argument("--no-build", action="store_true", help="Run an existing image without rebuilding")
    run_mode_group = run_parser.add_mutually_exclusive_group()
    run_mode_group.add_argument(
        "-d",
        "--detached",
        dest="detached",
        action="store_true",
        help="Run container in detached mode",
    )
    run_mode_group.add_argument(
        "--foreground",
        dest="detached",
        action="store_false",
        help="Run in foreground and attach to container output (default)",
    )
    run_parser.set_defaults(detached=False)
    run_parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="Host port mapping")
    run_parser.add_argument(
        "--force-base",
        action="store_true",
        help="Force local rebuild of base image (dev mode)",
    )
    run_parser.add_argument(
        "--keep-build-context",
        action="store_true",
        help="Keep generated .dank-py build context for debugging",
    )
    run_parser.add_argument("--verbose", action="store_true", help="Stream raw Docker build logs")
    run_parser.add_argument("--quiet", action="store_true", help="Reduce runtime request/startup logs")
    run_parser.add_argument(
        "--env-file",
        dest="env_files",
        action="append",
        default=[],
        help="Env file to inject at runtime (repeatable)",
    )
    run_parser.add_argument(
        "-e",
        "--env",
        dest="env_vars",
        action="append",
        default=[],
        help="Runtime env var to inject (KEY=VALUE or KEY, repeatable)",
    )
    run_parser.add_argument(
        "--no-auto-env-file",
        action="store_true",
        help="Disable automatic use of project .env when no --env-file is provided",
    )
    run_parser.add_argument("--json", action="store_true", help="Print JSON output")

    logs_parser = subparsers.add_parser("logs", help="View logs from dank-py containers")
    logs_parser.add_argument("target", nargs="?", help="Agent name, bundle name, or container name")
    logs_parser.add_argument("-f", "--follow", action="store_true", help="Follow log output")
    logs_parser.add_argument(
        "-t",
        "--tail",
        type=int,
        default=100,
        help="Number of log lines to show (default: 100)",
    )
    logs_parser.add_argument("--since", help="Show logs since timestamp or duration")

    stop_parser = subparsers.add_parser("stop", help="Stop running dank-py containers")
    stop_parser.add_argument("-c", "--config", default=DEFAULT_CONFIG_FILE, help="Path to dank.config.json")
    stop_parser.add_argument("--agent", help="Stop one agent target by name or id")
    stop_parser.add_argument("--bundle", help="Stop one configured/named bundle container")
    stop_parser.add_argument(
        "--bundle-agents",
        help="Stop one ad-hoc bundle by csv agent ids/names, or 'all'",
    )
    stop_parser.add_argument(
        "--bundle-name",
        help="Optional ad-hoc bundle name (only with --bundle-agents)",
    )
    stop_parser.add_argument("--all", action="store_true", help="Stop all running dank-py containers")
    stop_parser.add_argument("--keep", action="store_true", help="Stop without removing container")

    status_parser = subparsers.add_parser("status", help="Show status of dank-py containers and images")
    status_parser.add_argument("--json", action="store_true", help="Print JSON output")

    clean_parser = subparsers.add_parser("clean", help="Clean dank-py docker resources")
    clean_parser.add_argument("--project-dir", help="Project directory for build-context cleanup")
    clean_parser.add_argument("--all", action="store_true", help="Clean containers, images, and build contexts")
    clean_parser.add_argument("--containers", action="store_true", help="Clean dank-py containers")
    clean_parser.add_argument("--images", action="store_true", help="Clean dank-py agent images")
    clean_parser.add_argument("--build-contexts", action="store_true", help="Clean .dank-py build contexts")
    clean_parser.add_argument("--include-base", action="store_true", help="Also remove local base image")

    version_parser = subparsers.add_parser("version", help="Show dank-py and base image version info")
    version_parser.add_argument("--json", action="store_true", help="Print JSON output")

    return parser


def main(argv: list[str] | None = None) -> None:
    raw_args = list(argv) if argv is not None else list(sys.argv[1:])
    if raw_args and all(arg in {"-v", "--version"} for arg in raw_args):
        _print_version(_version_payload())
        return

    parser = _build_parser()
    args = parser.parse_args(raw_args)

    try:
        def _validation_mode(dry: bool, full: bool) -> str:
            if dry and full:
                raise ValueError("Use only one of --validate-dry or --validate-full")
            if full:
                return "full"
            if dry:
                return "dry"
            return "none"

        if args.command == "version":
            payload = _version_payload()
            if args.json:
                print(json.dumps(payload, indent=2))
            else:
                _print_version(payload)
            return

        if args.command == "init":
            target = init_command(name=args.name, force=args.force)
            print(f"Initialized dank-py scaffold at {target}")
            return

        if args.command == "auto-init":
            if args.strict and args.validate_dry:
                raise ValueError("Use only one of --validate-dry or --strict/--validate-full")
            if args.strict:
                args.validate_full = True
            validation_mode = _validation_mode(args.validate_dry, args.validate_full)
            print("Auto-init: initializing scaffold ...")
            target = init_command(name=args.name, force=args.force)
            print("Auto-init: scanning project for entry candidates ...")
            inspect_results = inspect_payload(project_dir=str(target))
            entry_candidates = inspect_results.get("entry_candidates", []) if isinstance(inspect_results, dict) else []
            print(f"Auto-init: found {len(entry_candidates)} entry candidate(s).")
            print("Auto-init: applying confident inspect candidates to dank.config.json ...")
            applied_count = apply_candidates_to_config(
                project_dir=str(target),
                config_path=DEFAULT_CONFIG_FILE,
                candidate_indexes=None,
                min_score=75,
                max_agents=10,
            )
            print("Auto-init: preparing dependency lock ...")
            if validation_mode == "dry":
                print("Auto-init: running dry dependency validation ...")
            elif validation_mode == "full":
                print("Auto-init: running strict full dependency validation ...")

            deps_result = deps_command(
                project_dir=str(target),
                validate=validation_mode,
                config_path=str(target / DEFAULT_CONFIG_FILE),
                refresh_lock=not args.no_refresh_lock,
                fallback_freeze=args.fallback_freeze,
                discover_imports=not args.no_discover_imports,
                install_tools=args.install_tools,
                prompt_install_tools=not args.no_install_prompt,
                lock_python_version=args.lock_python_version,
                include_lock_comments=args.include_lock_comments,
            )

            print(f"Initialized dank-py scaffold at {target}")
            if applied_count > 0:
                print(f"Auto-updated config from {applied_count} inspect candidate(s).")
            print(f"Dependency lock ready: {deps_result.lock_path}")
            if deps_result.validation_mode != "none":
                print(
                    f"Dependency validation ({deps_result.validation_mode}) passed for "
                    f"{deps_result.validated_agents} agent(s)"
                )
            return

        if args.command == "deps":
            validation_mode = _validation_mode(args.validate_dry, args.validate_full)
            result = deps_command(
                project_dir=args.project_dir,
                validate=validation_mode,
                config_path=args.config,
                refresh_lock=not args.no_refresh_lock,
                fallback_freeze=args.fallback_freeze,
                discover_imports=not args.no_discover_imports,
                install_tools=args.install_tools,
                prompt_install_tools=not args.no_install_prompt,
                lock_python_version=args.lock_python_version,
                include_lock_comments=args.include_lock_comments,
            )
            print(f"Dependency lock ready: {result.lock_path}")
            if result.validation_mode != "none":
                print(
                    f"Dependency validation ({result.validation_mode}) passed for "
                    f"{result.validated_agents} agent(s)"
                )
            return

        if args.command == "inspect":
            if args.json:
                print(inspect_command(project_dir=args.project_dir, as_json=True))
                return

            if args.apply:
                idx = max(0, args.candidate_index - 1)
                applied_count = apply_candidates_to_config(
                    project_dir=args.project_dir,
                    config_path=args.config,
                    candidate_indexes=None if args.candidate_index == 1 else [idx],
                    min_score=75,
                    max_agents=10,
                )
                if applied_count <= 0:
                    raise RuntimeError("Failed to apply candidate to config.")
                if args.candidate_index == 1:
                    print(f"Applied {applied_count} candidate(s) to {args.config}")
                else:
                    print(f"Applied candidate #{idx + 1} to {args.config}")
                return

            payload = inspect_payload(project_dir=args.project_dir)
            entries = payload.get("entry_candidates", [])
            if not entries:
                print("No entry candidates found.")
                return

            print("Entry Candidates:")
            for idx, entry in enumerate(entries[:10], start=1):
                method_suffix = f".{entry.get('method')}" if entry.get("method") else ""
                print(
                    f"{idx}. {entry.get('file')}: {entry.get('symbol')}{method_suffix} "
                    f"(score={entry.get('score')}, reason={entry.get('reason')})"
                )

            selection = input(
                "Select candidate number(s) to apply (e.g. 1,2) or 'all' for confident set (Enter to skip): "
            ).strip()
            if not selection:
                print("No changes applied.")
                return

            if selection.lower() == "all":
                applied_count = apply_candidates_to_config(
                    project_dir=args.project_dir,
                    config_path=args.config,
                    candidate_indexes=None,
                    min_score=75,
                    max_agents=10,
                )
                if applied_count <= 0:
                    raise RuntimeError("Failed to apply candidate set to config.")
                print(f"Applied {applied_count} candidate(s) to {args.config}")
                return

            parsed_indexes: list[int] = []
            for part in [p.strip() for p in selection.split(",") if p.strip()]:
                try:
                    idx = int(part) - 1
                except ValueError as exc:
                    raise ValueError("Invalid selection. Use numeric indexes separated by commas.") from exc
                if idx < 0 or idx >= len(entries):
                    raise ValueError("Selected index is out of range.")
                parsed_indexes.append(idx)

            if len(parsed_indexes) > 1:
                applied_count = apply_candidates_to_config(
                    project_dir=args.project_dir,
                    config_path=args.config,
                    candidate_indexes=parsed_indexes,
                    min_score=0,
                    max_agents=20,
                )
                if applied_count <= 0:
                    raise RuntimeError("Failed to apply selected candidates to config.")
                print(f"Applied {applied_count} candidate(s) to {args.config}")
                return

            selected_index = parsed_indexes[0]
            applied_count = apply_candidates_to_config(
                project_dir=args.project_dir,
                config_path=args.config,
                candidate_indexes=[selected_index],
                min_score=0,
                max_agents=1,
            )
            if applied_count != 1:
                raise RuntimeError("Failed to apply selected candidate to config.")

            selected = entries[selected_index]
            default_file = str(selected.get("file") or "")
            default_symbol = str(selected.get("symbol") or "")
            default_method = selected.get("method")

            file_value = input(f"Entry file [{default_file}]: ").strip() or default_file
            symbol_value = input(f"Entry symbol [{default_symbol}]: ").strip() or default_symbol

            method_default_text = default_method if default_method else "none"
            method_raw = input(f"Entry method [{method_default_text}] (type 'none' for null): ").strip()
            if not method_raw:
                method_value = default_method
            elif method_raw.lower() == "none":
                method_value = None
            else:
                method_value = method_raw

            call_type_raw = input("Call type [auto] (auto|callable|method): ").strip().lower() or "auto"
            if call_type_raw not in {"auto", "callable", "method"}:
                raise ValueError("Invalid call_type. Expected one of: auto, callable, method.")

            call_style_raw = input("Call style [auto] (auto|single_arg|kwargs): ").strip().lower() or "auto"
            if call_style_raw not in {"auto", "single_arg", "kwargs"}:
                raise ValueError("Invalid call_style. Expected one of: auto, single_arg, kwargs.")

            applied = apply_entry_to_config(
                project_dir=args.project_dir,
                config_path=args.config,
                entry_values={
                    "file": file_value,
                    "symbol": symbol_value,
                    "method": method_value,
                    "call_type": call_type_raw,
                    "call_style": call_style_raw,
                },
            )
            if not applied:
                raise RuntimeError("Failed to apply selected candidate to config.")
            print(f"Applied candidate #{selected_index + 1} to {args.config}")
            return

        if args.command == "build":
            if not args.json:
                print("Checking Docker and building image target(s)...")
            results = build_command(
                BuildCommandOptions(
                    config_path=args.config,
                    agent_name=args.agent,
                    bundle_name=args.bundle,
                    bundle_agents=args.bundle_agents,
                    adhoc_bundle_name=args.bundle_name,
                    prompt_routing=args.prompt_routing,
                    default_agent=args.default_agent,
                    tag=args.tag,
                    base_image=args.base_image,
                    pull_base=args.pull_base,
                    skip_base_build=args.no_base_build,
                    force_base=args.force_base,
                    verbose=args.verbose,
                )
            )
            if args.json:
                print(
                    json.dumps(
                        [
                            {
                                "target_type": item.target_type,
                                "target_name": item.target_name,
                                "image_tag": item.image_tag,
                                "context_path": item.context_path,
                                "agent_ids": item.agent_ids,
                            }
                            for item in results
                        ],
                        indent=2,
                    )
                )
            else:
                for item in results:
                    print(f"Built image: {item.image_tag}")
                    print(f"  target: {item.target_name} ({item.target_type})")
                    print(f"  context: {item.context_path}")
            return

        if args.command == "build:prod":
            if not args.json:
                print("🏗️  Building production image(s)...")
            result = production_build_command(
                ProductionBuildCommandOptions(
                    config_path=args.config,
                    agent_name=args.agent,
                    bundle_name=args.bundle,
                    bundle_agents=args.bundle_agents,
                    adhoc_bundle_name=args.bundle_name,
                    prompt_routing=args.prompt_routing,
                    default_agent=args.default_agent,
                    tag=args.tag,
                    registry=args.registry,
                    namespace=args.namespace,
                    tag_by_agent=args.tag_by_agent,
                    platform=args.platform,
                    push=args.push,
                    load=args.load,
                    no_cache=args.no_cache,
                    base_image=args.base_image,
                    pull_base=args.pull_base,
                    force_base=args.force_base,
                    output_metadata=args.output_metadata,
                    verbose=args.verbose,
                )
            )
            if args.json:
                payload = {
                    "success": result.success,
                    "platform": result.platform,
                    "push": result.push,
                    "load": result.load,
                    "results": [
                        {
                            "target": item.target,
                            "target_type": item.target_type,
                            "image_name": item.image_name,
                            "success": item.success,
                            "pushed": item.pushed,
                            "loaded": item.loaded,
                            "agent_ids": item.agent_ids,
                            "error": item.error,
                        }
                        for item in result.results
                    ],
                    "metadata_path": result.metadata_path,
                }
                print(json.dumps(payload, indent=2))
            else:
                mode = "push" if result.push else ("load" if result.load else "cache-only")
                print(f"⚙️  Mode: {mode} | Platform: {result.platform}")
                success = [item for item in result.results if item.success]
                failed = [item for item in result.results if not item.success]
                for item in success:
                    action = "pushed" if item.pushed else "built"
                    print(f"✅ {item.target} ({item.target_type}): {action} {item.image_name}")
                for item in failed:
                    print(f"❌ {item.target} ({item.target_type}): {item.error}")
                if result.metadata_path:
                    print(f"📄 Metadata: {result.metadata_path}")
                print(f"📊 Summary: {len(success)} succeeded, {len(failed)} failed")
            if not result.success:
                raise SystemExit(1)
            return

        if args.command == "run":
            if not args.json:
                if args.agent:
                    print("🚀 Starting Dank Python agent...")
                elif args.bundle or args.bundle_agents:
                    print("🚀 Starting Dank Python bundle...")
                else:
                    print("🚀 Starting Dank Python targets...")
                if args.no_build:
                    print("⏭️  Skipping image build")
                else:
                    if args.agent:
                        print("📦 Building image...")
                    elif args.bundle or args.bundle_agents:
                        print("📦 Building bundle image...")
                    else:
                        print("📦 Building target images...")
                if args.agent:
                    if args.detached:
                        print("▶️  Starting container (detached)")
                    else:
                        print("▶️  Starting container (foreground, Ctrl+C to stop)")
                elif args.bundle or args.bundle_agents:
                    if args.detached:
                        print("▶️  Starting bundle container (detached)")
                    else:
                        print("▶️  Starting bundle container (foreground, Ctrl+C to stop)")
                else:
                    print("▶️  Starting target container(s)...")
                if not args.no_build and not args.verbose:
                    print("ℹ️  Build output is compact in default mode. Use --verbose to stream Docker logs.")
            result = run_command(
                RunCommandOptions(
                    config_path=args.config,
                    agent_name=args.agent,
                    bundle_name=args.bundle,
                    bundle_agents=args.bundle_agents,
                    adhoc_bundle_name=args.bundle_name,
                    prompt_routing=args.prompt_routing,
                    default_agent=args.default_agent,
                    tag=args.tag,
                    base_image=args.base_image,
                    pull_base=args.pull_base,
                    no_build=args.no_build,
                    detached=args.detached,
                    port=args.port,
                    force_base=args.force_base,
                    keep_build_context=args.keep_build_context,
                    verbose=args.verbose,
                    quiet=args.quiet,
                    env_files=args.env_files,
                    env_vars=args.env_vars,
                    no_auto_env_file=args.no_auto_env_file,
                )
            )
            if args.json:
                print(
                    json.dumps(
                        {
                            "detached": result.detached,
                            "env_files": result.env_files,
                            "env_var_keys": result.env_var_keys,
                            "agents": [
                                {
                                    "target_type": item.target_type,
                                    "target_name": item.target_name,
                                    "agent_ids": item.agent_ids,
                                    "container_name": item.container_name,
                                    "image_tag": item.image_tag,
                                    "port": item.port,
                                    "prompt_agent_header": item.prompt_agent_header,
                                    "prompt_routing": item.prompt_routing,
                                    "default_agent_id": item.default_agent_id,
                                }
                                for item in result.agents
                            ],
                        },
                        indent=2,
                    )
                )
            else:
                if result.env_files:
                    print(f"🔐 Runtime env files: {', '.join(result.env_files)}")
                if result.env_var_keys:
                    print(f"🔐 Runtime env vars: {', '.join(result.env_var_keys)}")
                for item in result.agents:
                    print(f"✅ Running container: {item.container_name}")
                    if item.target_type == "separate_agent":
                        print(f"   🤖 Agent: {item.target_name}")
                    else:
                        print(f"   📦 Bundle: {item.target_name}")
                        print(f"   🤖 Agents: {', '.join(item.agent_ids)}")
                        print(f"   📡 Header: {item.prompt_agent_header or 'x-dank-agent-id'}")
                        print(f"   🧭 Prompt routing: {item.prompt_routing or 'required'}")
                        if item.prompt_routing == "default":
                            print(f"   🎯 Default agent: {item.default_agent_id or 'none'}")
                    print(f"   🔌 Port: {item.port}")
                    print(f"   🔎 Health: http://localhost:{item.port}/health")
                    print(f"   💬 Prompt: http://localhost:{item.port}/prompt")
                    print(f"   📊 Status: http://localhost:{item.port}/status")
                    print(f"   📈 Metrics: http://localhost:{item.port}/metrics")
                    print(f"   📋 Logs: http://localhost:{item.port}/logs")
                    print(f"   📡 Log Stream: ws://localhost:{item.port}/logs/stream")
                    print(f"   🧵 Traces: http://localhost:{item.port}/traces")
                if len(result.agents) > 1:
                    print(f"📊 Summary: started {len(result.agents)} container(s)")
                if not result.detached and len(result.agents) == 1:
                    monitored = result.agents[0]
                    manager = DockerManager()
                    print("👀 Monitoring container (Ctrl+C to stop)...")
                    try:
                        logs_command(
                            LogsCommandOptions(
                                target=monitored.container_name,
                                follow=True,
                                tail=100,
                                since=None,
                            )
                        )
                    except KeyboardInterrupt:
                        print("\n🛑 Stopping container...")
                    finally:
                        manager.stop_container(monitored.container_name, remove=True)
                        print("✅ Container stopped")
            return

        if args.command == "logs":
            if args.target:
                print(f"📋 Logs for target: {args.target}")
                if args.follow:
                    print("👀 Following logs (Ctrl+C to stop)...")
            else:
                print("📋 Logs from all dank-py containers")

            result = logs_command(
                LogsCommandOptions(
                    target=args.target,
                    follow=args.follow,
                    tail=max(1, int(args.tail)),
                    since=args.since,
                )
            )
            if not result.targets:
                print("No dank-py containers found.")
            return

        if args.command == "stop":
            print("🛑 Stopping agents...")
            result = stop_command(
                StopCommandOptions(
                    config_path=args.config,
                    agent_name=args.agent,
                    bundle_name=args.bundle,
                    bundle_agents=args.bundle_agents,
                    adhoc_bundle_name=args.bundle_name,
                    all_agents=args.all,
                    remove=not args.keep,
                )
            )
            if result.stopped:
                for name in result.stopped:
                    print(f"✅ Stopped: {name}")
                print(f"📊 Summary: {len(result.stopped)} container(s) stopped")
            else:
                print("No matching running dank-py containers found.")
            return

        if args.command == "status":
            result = status_command()
            if args.json:
                payload = {
                    "containers": [
                        {
                            "name": item.name,
                            "image": item.image,
                            "state": item.state,
                            "status": item.status,
                            "ports": item.ports,
                            "target_type": item.target_type,
                            "bundle_name": item.bundle_name,
                            "bundle_hash": item.bundle_hash,
                            "agent_ids": item.agent_ids,
                        }
                        for item in result.containers
                    ],
                    "images": result.images,
                }
                print(json.dumps(payload, indent=2))
            else:
                def _supports_color() -> bool:
                    return sys.stdout.isatty() and os.getenv("NO_COLOR") is None

                use_color = _supports_color()
                color_reset = "\033[0m" if use_color else ""
                color_green = "\033[32m" if use_color else ""
                color_red = "\033[31m" if use_color else ""
                color_yellow = "\033[33m" if use_color else ""
                color_dim = "\033[2m" if use_color else ""
                color_cyan = "\033[36m" if use_color else ""

                def _paint(text: str, color: str) -> str:
                    if not use_color or not color:
                        return text
                    return f"{color}{text}{color_reset}"

                def _bucket(state: str) -> str:
                    lowered = (state or "").strip().lower()
                    if lowered == "running":
                        return "running"
                    if lowered in {"created", "exited", "dead", "paused"}:
                        return "stopped"
                    return "other"

                def _status_token(state: str) -> str:
                    lowered = (state or "").strip().lower()
                    if lowered == "running":
                        return _paint("● running", color_green)
                    if lowered in {"created", "exited", "dead", "paused"}:
                        return _paint(f"● {lowered or 'stopped'}", color_red)
                    return _paint(f"● {lowered or 'unknown'}", color_yellow)

                def _render_container(item) -> None:
                    target_label = item.target_type or "unknown"
                    ports = item.ports or "-"
                    print(f"  {_status_token(item.state)}  {_paint(item.name, color_cyan)}")
                    print(f"    type: {target_label} | ports: {ports}")
                    print(f"    image: {item.image}")
                    if item.bundle_name:
                        print(f"    bundle: {item.bundle_name}")
                    if item.agent_ids:
                        print(f"    agents: {', '.join(item.agent_ids)}")

                running = [item for item in result.containers if _bucket(item.state) == "running"]
                stopped = [item for item in result.containers if _bucket(item.state) == "stopped"]
                other = [item for item in result.containers if _bucket(item.state) == "other"]

                print("📊 Dank-Py Status")
                if not result.containers:
                    print("Containers: none")
                else:
                    print("")
                    print(_paint(f"Running Containers ({len(running)})", color_green))
                    if running:
                        for item in running:
                            _render_container(item)
                    else:
                        print(f"  {_paint('none', color_dim)}")

                    if stopped:
                        print("")
                        print(_paint(f"Stopped/Created Containers ({len(stopped)})", color_red))
                        for item in stopped:
                            _render_container(item)

                    if other:
                        print("")
                        print(_paint(f"Other Containers ({len(other)})", color_yellow))
                        for item in other:
                            _render_container(item)

                print("")
                if result.images:
                    print("Available Images:")
                    for image in result.images:
                        print(f"  • {image}")
                else:
                    print("Available Images: none")
            return

        if args.command == "clean":
            print("🧹 Cleaning dank-py resources...")
            result = clean_command(
                CleanCommandOptions(
                    project_dir=args.project_dir,
                    all_resources=args.all,
                    containers=args.containers,
                    images=args.images,
                    build_contexts=args.build_contexts,
                    include_base=args.include_base,
                )
            )
            if result.removed_containers:
                print(f"✅ Containers removed: {len(result.removed_containers)}")
            if result.removed_images:
                print(f"✅ Images removed: {len(result.removed_images)}")
            if result.removed_build_context:
                print("✅ Removed .dank-py build context")
            if not (result.removed_containers or result.removed_images or result.removed_build_context):
                print("No dank-py resources found to clean.")
            return

    except KeyboardInterrupt:
        print("\n🛑 Stopped.", file=sys.stderr)
        raise SystemExit(130)
    except Exception as exc:  # noqa: BLE001
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
