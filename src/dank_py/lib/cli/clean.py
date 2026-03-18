"""`dank clean` command."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from dank_py.lib.constants import DANK_BUILD_DIR
from dank_py.lib.docker.manager import DockerManager


@dataclass(slots=True)
class CleanCommandOptions:
    project_dir: str | None
    all_resources: bool
    containers: bool
    images: bool
    build_contexts: bool
    include_base: bool


@dataclass(slots=True)
class CleanCommandResult:
    removed_containers: list[str]
    removed_images: list[str]
    removed_build_context: bool


def clean_command(options: CleanCommandOptions) -> CleanCommandResult:
    manager = DockerManager()
    manager.ensure_docker_available()

    no_flags = not any([options.all_resources, options.containers, options.images, options.build_contexts])
    clean_containers = options.all_resources or options.containers or no_flags
    clean_images = options.all_resources or options.images or no_flags
    clean_contexts = options.all_resources or options.build_contexts or no_flags

    removed_containers: list[str] = []
    removed_images: list[str] = []
    removed_build_context = False

    if clean_containers:
        removed_containers = manager.stop_dank_containers(remove=True)

    if clean_images:
        removed_images = manager.remove_dank_images(include_base=options.include_base)

    if clean_contexts:
        project_root = Path(options.project_dir or Path.cwd()).resolve()
        build_root = project_root / DANK_BUILD_DIR
        if build_root.exists():
            shutil.rmtree(build_root, ignore_errors=True)
            removed_build_context = True

    return CleanCommandResult(
        removed_containers=removed_containers,
        removed_images=removed_images,
        removed_build_context=removed_build_context,
    )
