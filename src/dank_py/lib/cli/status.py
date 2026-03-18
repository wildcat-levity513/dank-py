"""`dank status` command."""

from __future__ import annotations

from dataclasses import dataclass

from dank_py.lib.docker.manager import DockerManager


@dataclass(slots=True)
class StatusEntry:
    name: str
    image: str
    state: str
    status: str
    ports: str
    target_type: str | None = None
    bundle_name: str | None = None
    bundle_hash: str | None = None
    agent_ids: list[str] | None = None


@dataclass(slots=True)
class StatusCommandResult:
    containers: list[StatusEntry]
    images: list[str]


def status_command() -> StatusCommandResult:
    manager = DockerManager()
    manager.ensure_docker_available()

    records = manager.list_dank_container_status()
    containers = [
        StatusEntry(
            name=record.name,
            image=record.image,
            state=record.state,
            status=record.status_text,
            ports=record.ports,
            target_type=record.target_type,
            bundle_name=record.bundle_name,
            bundle_hash=record.bundle_hash,
            agent_ids=record.agent_ids,
        )
        for record in records
    ]
    images = manager.list_dank_images()
    return StatusCommandResult(containers=containers, images=images)
