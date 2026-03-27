"""Docker manager for dank-py build/run workflows."""

from __future__ import annotations

import fnmatch
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any

from dank_py.lib.config.models import AgentConfig
from dank_py.lib.constants import (
    AGENT_CODE_DIR,
    BUILD_CONTEXT_PREFIX,
    DANK_BUILD_DIR,
    DEFAULT_BASE_IMAGE,
    DEFAULT_IGNORE_PATTERNS,
    DEFAULT_IMAGE_TAG_SUFFIX,
    DEFAULT_PORT,
)
from dank_py.lib.runtime.generator import write_generated_bundle_index, write_generated_index


class DockerCommandError(RuntimeError):
    """Raised when a docker command fails."""


@dataclass(slots=True)
class BuildResult:
    image_tag: str
    context_path: Path


@dataclass(slots=True)
class ProductionBuildResult:
    image_name: str
    pushed: bool
    loaded: bool
    context_path: Path


@dataclass(slots=True)
class ContainerStatusRecord:
    name: str
    image: str
    state: str
    status_text: str
    ports: str
    target_type: str | None = None
    bundle_name: str | None = None
    bundle_hash: str | None = None
    agent_ids: list[str] | None = None


@dataclass(slots=True)
class ResolvedLogTarget:
    container_name: str
    target_type: str | None
    host_port: int | None
    agent_id: str | None = None


class DockerManager:
    def __init__(self) -> None:
        self.source_root = Path(__file__).resolve().parents[4]
        self._docker_cmd: str | None = None

    def _has_source_assets(self) -> bool:
        return (
            (self.source_root / "docker" / "entrypoint.py").exists()
            and (self.source_root / "docker" / "default_index.py").exists()
            and (self.source_root / "docker" / "Dockerfile").exists()
            and (self.source_root / "src" / "dank_runtime").exists()
        )

    def _docker_asset_bytes(self, filename: str) -> bytes:
        source_asset = self.source_root / "docker" / filename
        if source_asset.exists():
            return source_asset.read_bytes()

        try:
            packaged_asset = resources.files("dank_py").joinpath("docker_assets", filename)
            return packaged_asset.read_bytes()
        except Exception as exc:  # noqa: BLE001
            raise DockerCommandError(
                f"Docker asset '{filename}' not found in source tree or installed package."
            ) from exc

    def _write_docker_asset(self, filename: str, destination: Path) -> None:
        destination.write_bytes(self._docker_asset_bytes(filename))

    def _copy_runtime_package(self, destination: Path) -> None:
        source_runtime = self.source_root / "src" / "dank_runtime"
        if source_runtime.exists():
            shutil.copytree(source_runtime, destination)
            return

        try:
            runtime_root = resources.files("dank_runtime")
        except Exception as exc:  # noqa: BLE001
            raise DockerCommandError("dank_runtime package assets are not available.") from exc

        self._copy_resource_tree(runtime_root, destination)

    def _copy_resource_tree(self, source: Any, destination: Path) -> None:
        destination.mkdir(parents=True, exist_ok=True)
        for child in source.iterdir():
            child_destination = destination / child.name
            if child.is_dir():
                self._copy_resource_tree(child, child_destination)
            else:
                child_destination.write_bytes(child.read_bytes())

    def _docker_candidates(self) -> list[str]:
        env_cmd = str(os.getenv("DANK_PY_DOCKER_CMD", "")).strip()
        candidates = []
        if env_cmd:
            candidates.append(env_cmd)
        candidates.extend(
            [
                "docker",
                "/usr/local/bin/docker",
                "/opt/homebrew/bin/docker",
                "/usr/bin/docker",
            ]
        )
        deduped: list[str] = []
        seen: set[str] = set()
        for item in candidates:
            if item in seen:
                continue
            seen.add(item)
            deduped.append(item)
        return deduped

    def _command_exists(self, command: str) -> bool:
        try:
            result = subprocess.run(
                [command, "--version"],
                capture_output=True,
                text=True,
                check=False,
            )
            return result.returncode == 0
        except Exception:  # noqa: BLE001
            return False

    def _resolve_docker_command(self, *, required: bool = True) -> str | None:
        if self._docker_cmd:
            return self._docker_cmd
        for candidate in self._docker_candidates():
            if self._command_exists(candidate):
                self._docker_cmd = candidate
                return self._docker_cmd
        if not required:
            return None
        raise DockerCommandError(
            "Docker executable not found in PATH. Install Docker Desktop "
            "(macOS/Windows) or Docker Engine (Linux), then retry."
        )

    def _is_interactive(self) -> bool:
        return bool(sys.stdin and sys.stdin.isatty())

    def _is_env_true(self, value: str | None) -> bool:
        normalized = str(value or "").strip().lower()
        return normalized in {"1", "true", "yes", "on"}

    def _prompt_yes_no(self, question: str, *, default: bool = False) -> bool:
        suffix = "[Y/n]" if default else "[y/N]"
        try:
            raw = input(f"{question} {suffix}: ").strip().lower()
        except EOFError:
            return default
        if not raw:
            return default
        return raw in {"y", "yes"}

    def _docker_daemon_accessible(self) -> bool:
        docker_cmd = self._resolve_docker_command(required=False)
        if not docker_cmd:
            return False
        try:
            result = subprocess.run(
                [docker_cmd, "info"],
                capture_output=True,
                text=True,
                check=False,
            )
            return result.returncode == 0
        except Exception:  # noqa: BLE001
            return False

    def _wait_for_docker(self, *, timeout_seconds: int = 120, interval_seconds: float = 2.0) -> bool:
        deadline = time.time() + max(1, int(timeout_seconds))
        while time.time() < deadline:
            if self._docker_daemon_accessible():
                return True
            time.sleep(max(0.2, float(interval_seconds)))
        return False

    def _docker_install_help(self) -> str:
        if sys.platform == "darwin":
            return (
                "Install Docker Desktop from https://www.docker.com/products/docker-desktop/ "
                "or run `brew install --cask docker`."
            )
        if sys.platform.startswith("linux"):
            return (
                "Install Docker Engine for your distro (e.g. `sudo apt-get install docker.io`), "
                "then ensure the daemon is running."
            )
        if sys.platform == "win32":
            return "Install Docker Desktop from https://www.docker.com/products/docker-desktop/."
        return "Install Docker and ensure the daemon is running."

    def install_docker(self) -> None:
        platform = sys.platform
        if platform == "darwin":
            if self._command_exists("brew"):
                self._run(["brew", "install", "--cask", "docker"], stream_output=True)
                return
            raise DockerCommandError(
                "Docker is not installed and Homebrew is not available for automatic install. "
                + self._docker_install_help()
            )
        if platform.startswith("linux"):
            if self._command_exists("apt-get"):
                self._run(["sudo", "apt-get", "update"], stream_output=True)
                self._run(["sudo", "apt-get", "install", "-y", "docker.io"], stream_output=True)
                return
            if self._command_exists("dnf"):
                self._run(["sudo", "dnf", "install", "-y", "docker"], stream_output=True)
                return
            if self._command_exists("yum"):
                self._run(["sudo", "yum", "install", "-y", "docker"], stream_output=True)
                return
            raise DockerCommandError(
                "Automatic Docker install is not supported for this Linux environment. "
                + self._docker_install_help()
            )
        if platform == "win32":
            if self._command_exists("winget"):
                self._run(
                    ["winget", "install", "-e", "--id", "Docker.DockerDesktop"],
                    stream_output=True,
                )
                return
            raise DockerCommandError(
                "Docker is not installed and winget is unavailable for automatic install. "
                + self._docker_install_help()
            )
        raise DockerCommandError(
            f"Unsupported platform for automatic Docker install: {platform}. "
            + self._docker_install_help()
        )

    def start_docker(self) -> None:
        platform = sys.platform
        if platform == "darwin":
            self._run(["open", "-a", "Docker"], check=False)
            return
        if platform.startswith("linux"):
            if self._command_exists("systemctl"):
                self._run(["sudo", "systemctl", "start", "docker"], stream_output=True)
                return
            raise DockerCommandError(
                "Unable to auto-start Docker on Linux (systemctl not found). Start the daemon manually."
            )
        if platform == "win32":
            self._run(
                ["powershell", "-NoProfile", "-Command", "Start-Process 'Docker Desktop'"],
                check=False,
            )
            return
        raise DockerCommandError(f"Unsupported platform for auto-start: {platform}")

    def _trim_output(self, text: str, *, max_lines: int = 40) -> str:
        lines = [line for line in text.splitlines() if line.strip()]
        if len(lines) <= max_lines:
            return "\n".join(lines)
        tail = "\n".join(lines[-max_lines:])
        return f"... ({len(lines) - max_lines} lines omitted)\n{tail}"

    def _run(
        self,
        cmd: list[str],
        cwd: Path | None = None,
        check: bool = True,
        *,
        stream_output: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        effective_cmd = list(cmd)
        if effective_cmd and effective_cmd[0] == "docker":
            docker_cmd = self._resolve_docker_command(required=True)
            if docker_cmd:
                effective_cmd[0] = docker_cmd
        try:
            if stream_output:
                result = subprocess.run(
                    effective_cmd,
                    cwd=str(cwd) if cwd else None,
                    check=False,
                    text=True,
                )
                if check and result.returncode != 0:
                    raise DockerCommandError(
                        f"Command failed ({result.returncode}): {' '.join(effective_cmd)}. See output above."
                    )
                return result

            return subprocess.run(
                effective_cmd,
                cwd=str(cwd) if cwd else None,
                capture_output=True,
                text=True,
                check=check,
            )
        except KeyboardInterrupt:
            raise
        except subprocess.CalledProcessError as exc:
            stderr = (exc.stderr or "").strip()
            stdout = (exc.stdout or "").strip()
            details = stderr or stdout or str(exc)
            raise DockerCommandError(
                f"Command failed: {' '.join(effective_cmd)}\n{self._trim_output(details)}"
            ) from exc

    def _docker_run(
        self,
        args: list[str],
        *,
        capture_output: bool = True,
        text: bool = True,
        check: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        docker_cmd = self._resolve_docker_command(required=True) or "docker"
        return subprocess.run(
            [docker_cmd, *args],
            capture_output=capture_output,
            text=text,
            check=check,
        )

    def _is_transient_snapshot_error(self, message: str) -> bool:
        lowered = message.lower()
        return (
            "parent snapshot" in lowered and "does not exist" in lowered
        ) or "failed to prepare extraction snapshot" in lowered

    def _build_with_retry(
        self,
        cmd: list[str],
        *,
        verbose: bool = False,
        allow_no_cache_retry: bool = True,
    ) -> None:
        try:
            self._run(cmd, stream_output=verbose)
            return
        except DockerCommandError as exc:
            if not allow_no_cache_retry or not self._is_transient_snapshot_error(str(exc)):
                raise

            retry_cmd = cmd.copy()
            if "--no-cache" not in retry_cmd:
                retry_cmd.insert(2, "--no-cache")
            self._run(retry_cmd, stream_output=True)
            return

    def ensure_docker_available(self) -> None:
        prompt_allowed = self._is_interactive() and not self._is_env_true(os.getenv("DANK_PY_DOCKER_NO_PROMPT"))
        auto_install = self._is_env_true(os.getenv("DANK_PY_DOCKER_AUTO_INSTALL"))
        auto_start = not self._is_env_true(os.getenv("DANK_PY_DOCKER_NO_AUTO_START"))

        docker_cmd = self._resolve_docker_command(required=False)
        if not docker_cmd:
            should_install = auto_install
            if not should_install and prompt_allowed:
                should_install = self._prompt_yes_no("Docker is not installed. Install it now?", default=False)
            if should_install:
                self.install_docker()
                docker_cmd = self._resolve_docker_command(required=False)
            if not docker_cmd:
                raise DockerCommandError(
                    "Docker executable not found. " + self._docker_install_help()
                )

        self._docker_cmd = docker_cmd
        if self._docker_daemon_accessible():
            return

        should_start = auto_start
        if not should_start and prompt_allowed:
            should_start = self._prompt_yes_no("Docker is installed but not running. Start it now?", default=True)
        if should_start:
            print("Docker daemon is not accessible. Attempting to start/wait for Docker...")
            self.start_docker()
            if self._wait_for_docker(timeout_seconds=120):
                return

        raise DockerCommandError(
            "Docker daemon is not accessible. Start Docker Desktop/daemon and retry."
        )

    def image_exists(self, image_name: str) -> bool:
        result = self._docker_run(["image", "inspect", image_name], capture_output=True, text=True, check=False)
        return result.returncode == 0

    def build_base_image(self, image_name: str = DEFAULT_BASE_IMAGE, force: bool = False) -> str:
        if self.image_exists(image_name) and not force:
            return image_name

        if self._has_source_assets():
            dockerfile = self.source_root / "docker" / "Dockerfile"
            self._run(
                [
                    "docker",
                    "build",
                    "-t",
                    image_name,
                    "-f",
                    str(dockerfile),
                    str(self.source_root),
                ],
                stream_output=True,
            )
            return image_name

        temp_context = Path(tempfile.mkdtemp(prefix="dank-py-base-context-"))
        try:
            docker_dir = temp_context / "docker"
            docker_dir.mkdir(parents=True, exist_ok=True)
            self._write_docker_asset("Dockerfile", docker_dir / "Dockerfile")
            self._write_docker_asset("entrypoint.py", docker_dir / "entrypoint.py")
            self._write_docker_asset("default_index.py", docker_dir / "default_index.py")
            self._write_docker_asset(
                "requirements-runtime.txt",
                docker_dir / "requirements-runtime.txt",
            )

            runtime_destination = temp_context / "src" / "dank_runtime"
            self._copy_runtime_package(runtime_destination)

            self._run(
                [
                    "docker",
                    "build",
                    "-t",
                    image_name,
                    "-f",
                    str(docker_dir / "Dockerfile"),
                    str(temp_context),
                ],
                stream_output=True,
            )
        finally:
            shutil.rmtree(temp_context, ignore_errors=True)
        return image_name

    def pull_base_image(self, image_name: str = DEFAULT_BASE_IMAGE) -> str:
        self._run(["docker", "pull", image_name], stream_output=True)
        if not self.image_exists(image_name):
            raise DockerCommandError(f"Base image '{image_name}' was not pulled successfully.")
        return image_name

    def _read_ignore_patterns(self, project_root: Path) -> list[str]:
        patterns = []
        ignore_file = project_root / ".dankignore"
        if ignore_file.exists():
            lines = ignore_file.read_text(encoding="utf-8").splitlines()
            for line in lines:
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                patterns.append(stripped)
        return patterns

    def _should_ignore(self, relative_path: str, patterns: list[str]) -> bool:
        normalized = relative_path.strip("/")
        if not normalized:
            return False

        parts = normalized.split("/")
        if any(part in DEFAULT_IGNORE_PATTERNS for part in parts):
            return True

        for pattern in patterns:
            if pattern.endswith("/"):
                prefix = pattern.rstrip("/")
                if normalized == prefix or normalized.startswith(prefix + "/"):
                    return True
            if fnmatch.fnmatch(normalized, pattern):
                return True
            if fnmatch.fnmatch(Path(normalized).name, pattern):
                return True

        return False

    def _copy_project(self, project_root: Path, destination: Path) -> None:
        patterns = self._read_ignore_patterns(project_root)
        destination.mkdir(parents=True, exist_ok=True)

        for root, dirs, files in os.walk(project_root):
            root_path = Path(root)
            rel_root = root_path.relative_to(project_root)
            rel_root_str = rel_root.as_posix()

            filtered_dirs = []
            for dirname in dirs:
                rel_dir = f"{rel_root_str}/{dirname}" if rel_root_str != "." else dirname
                if not self._should_ignore(rel_dir, patterns):
                    filtered_dirs.append(dirname)
            dirs[:] = filtered_dirs

            for filename in files:
                rel_file = f"{rel_root_str}/{filename}" if rel_root_str != "." else filename
                if self._should_ignore(rel_file, patterns):
                    continue

                source_file = root_path / filename
                target_file = destination / rel_file
                target_file.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source_file, target_file)

    def _sanitize(self, value: str) -> str:
        lowered = value.lower()
        sanitized = re.sub(r"[^a-z0-9_.-]", "-", lowered)
        sanitized = re.sub(r"-+", "-", sanitized).strip("-.")
        return sanitized or "agent"

    def container_name_for_agent(self, agent_name: str) -> str:
        return f"dank-py-{self._sanitize(agent_name)}"

    def container_name_for_bundle(self, bundle_name: str) -> str:
        return f"dank-py-bundle-{self._sanitize(bundle_name)}"

    def normalize_docker_name(self, value: str) -> str:
        return self._sanitize(value)

    def _list_used_host_ports(self) -> set[int]:
        result = self._docker_run(["ps", "--format", "{{.Ports}}"], capture_output=True, text=True, check=False)
        if result.returncode != 0:
            return set()

        used: set[int] = set()
        for line in (result.stdout or "").splitlines():
            for match in re.finditer(r":(\d+)->", line):
                try:
                    used.add(int(match.group(1)))
                except ValueError:
                    continue
        return used

    def _is_port_bindable(self, port: int) -> bool:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("0.0.0.0", int(port)))
            return True
        except OSError:
            return False
        finally:
            sock.close()

    def find_available_host_port(
        self,
        requested_port: int,
        *,
        max_search: int = 500,
        avoid_ports: set[int] | None = None,
    ) -> int:
        start = max(1, int(requested_port))
        used = self._list_used_host_ports()
        avoided = avoid_ports or set()
        for candidate in range(start, start + max_search):
            if candidate in used or candidate in avoided:
                continue
            if self._is_port_bindable(candidate):
                return candidate
        raise DockerCommandError(
            f"Could not find an available host port in range {start}-{start + max_search - 1}"
        )

    def get_container_host_port(self, container_name: str) -> int | None:
        result = self._docker_run(["inspect", container_name], capture_output=True, text=True, check=False)
        if result.returncode != 0:
            return None
        try:
            payload = json.loads(result.stdout)
        except Exception:  # noqa: BLE001
            return None
        if not isinstance(payload, list) or not payload:
            return None
        item = payload[0] if isinstance(payload[0], dict) else {}
        network = item.get("NetworkSettings", {}) if isinstance(item.get("NetworkSettings"), dict) else {}
        ports = network.get("Ports", {}) if isinstance(network.get("Ports"), dict) else {}
        bindings = ports.get("3000/tcp")
        if not isinstance(bindings, list) or not bindings:
            return None
        binding = bindings[0] if isinstance(bindings[0], dict) else {}
        host_port = str(binding.get("HostPort", "")).strip()
        if not host_port:
            return None
        try:
            return int(host_port)
        except ValueError:
            return None

    def create_build_context(self, project_root: Path, agent: AgentConfig, base_image: str) -> Path:
        context_root = project_root / DANK_BUILD_DIR / f"{BUILD_CONTEXT_PREFIX}-{self._sanitize(agent.name)}"
        if context_root.exists():
            shutil.rmtree(context_root)
        context_root.mkdir(parents=True, exist_ok=True)

        agent_code_dest = context_root / "agent-code"
        self._copy_project(project_root, agent_code_dest)

        # Always overwrite with generated runtime wiring file
        write_generated_index(agent, agent_code_dest / "index.py")

        runtime_dir = context_root / "runtime"
        runtime_dir.mkdir(parents=True, exist_ok=True)
        self._write_docker_asset("entrypoint.py", runtime_dir / "entrypoint.py")
        self._write_docker_asset("default_index.py", runtime_dir / "default_index.py")
        self._copy_runtime_package(runtime_dir / "dank_runtime")

        requirements_file = None
        for candidate in ("requirements.lock.txt", "requirements.txt"):
            source = project_root / candidate
            if source.exists():
                requirements_file = candidate
                shutil.copy2(source, context_root / candidate)
                break

        dockerfile = self._build_agent_dockerfile(base_image=base_image, requirements_file=requirements_file)
        (context_root / "Dockerfile").write_text(dockerfile, encoding="utf-8")
        return context_root

    def create_bundle_build_context(
        self,
        *,
        project_root: Path,
        bundle_name: str,
        agents: list[AgentConfig],
        base_image: str,
        prompt_routing: str = "required",
        default_agent: str | None = None,
    ) -> Path:
        context_root = project_root / DANK_BUILD_DIR / f"{BUILD_CONTEXT_PREFIX}-bundle-{self._sanitize(bundle_name)}"
        if context_root.exists():
            shutil.rmtree(context_root)
        context_root.mkdir(parents=True, exist_ok=True)

        agent_code_dest = context_root / "agent-code"
        self._copy_project(project_root, agent_code_dest)
        write_generated_bundle_index(
            agents,
            agent_code_dest / "index.py",
            prompt_routing=prompt_routing,
            default_agent=default_agent,
        )

        runtime_dir = context_root / "runtime"
        runtime_dir.mkdir(parents=True, exist_ok=True)
        self._write_docker_asset("entrypoint.py", runtime_dir / "entrypoint.py")
        self._write_docker_asset("default_index.py", runtime_dir / "default_index.py")
        self._copy_runtime_package(runtime_dir / "dank_runtime")

        requirements_file = None
        for candidate in ("requirements.lock.txt", "requirements.txt"):
            source = project_root / candidate
            if source.exists():
                requirements_file = candidate
                shutil.copy2(source, context_root / candidate)
                break

        dockerfile = self._build_agent_dockerfile(base_image=base_image, requirements_file=requirements_file)
        (context_root / "Dockerfile").write_text(dockerfile, encoding="utf-8")
        return context_root

    def _cleanup_build_context(self, context_path: Path) -> None:
        if context_path.exists():
            shutil.rmtree(context_path, ignore_errors=True)

        build_root = context_path.parent
        if build_root.name != DANK_BUILD_DIR or not build_root.exists():
            return

        try:
            next(build_root.iterdir())
            return
        except StopIteration:
            pass
        except OSError:
            return

        try:
            build_root.rmdir()
        except OSError:
            return

    def _build_agent_dockerfile(self, base_image: str, requirements_file: str | None) -> str:
        requirements_copy = ""
        requirements_install = ""
        if requirements_file:
            requirements_copy = f"COPY {requirements_file} /tmp/{requirements_file}\n"
            requirements_install = f"RUN pip install --no-cache-dir -r /tmp/{requirements_file} && rm -f /tmp/{requirements_file}\n"

        return (
            f"FROM {base_image}\n"
            "USER root\n"
            "WORKDIR /app\n"
            f"{requirements_copy}"
            f"{requirements_install}"
            "COPY runtime/entrypoint.py /app/entrypoint.py\n"
            "COPY runtime/default_index.py /app/default_index.py\n"
            "COPY runtime/dank_runtime/ /app/dank_runtime/\n"
            "COPY --chown=dankuser:dankuser agent-code/ " + AGENT_CODE_DIR + "/\n"
            "ENV PYTHONPATH=/app:/app/agent-code\n"
            "USER dankuser\n"
        )

    def build_agent_image(
        self,
        project_root: Path,
        agent: AgentConfig,
        *,
        image_tag: str | None = None,
        build_base: bool = True,
        base_image: str = DEFAULT_BASE_IMAGE,
        force_base: bool = False,
        pull_base: bool = False,
        cleanup_context: bool = False,
        verbose: bool = False,
    ) -> BuildResult:
        # Local build is an explicit fallback path for development.
        if force_base and build_base:
            self.build_base_image(image_name=base_image, force=True)

        if pull_base:
            pull_error: Exception | None = None
            try:
                self.pull_base_image(base_image)
            except Exception as exc:  # noqa: BLE001
                pull_error = exc
            if pull_error is not None:
                if not self.image_exists(base_image) and build_base:
                    self.build_base_image(image_name=base_image, force=False)
                if not self.image_exists(base_image):
                    raise DockerCommandError(
                        f"Failed to pull base image '{base_image}'. "
                        "No local fallback is available."
                    ) from pull_error

        if not self.image_exists(base_image):
            pull_error: Exception | None = None
            try:
                self.pull_base_image(base_image)
            except Exception as exc:  # noqa: BLE001
                pull_error = exc

            if not self.image_exists(base_image) and build_base:
                self.build_base_image(image_name=base_image, force=False)

            if not self.image_exists(base_image):
                message = (
                    f"Base image '{base_image}' is not available locally and pull failed. "
                    "Use --base-image with a valid registry image, or enable local base build."
                )
                if pull_error:
                    raise DockerCommandError(f"{message}\nPull error: {pull_error}") from pull_error
                raise DockerCommandError(message)

        tag = image_tag or f"dank-py-agent-{self._sanitize(agent.name)}:{DEFAULT_IMAGE_TAG_SUFFIX}"
        context = self.create_build_context(project_root=project_root, agent=agent, base_image=base_image)

        try:
            self._build_with_retry(
                ["docker", "build", "-t", tag, "-f", str(context / "Dockerfile"), str(context)],
                verbose=verbose,
            )
            return BuildResult(image_tag=tag, context_path=context)
        finally:
            if cleanup_context:
                self._cleanup_build_context(context)

    def build_bundle_image(
        self,
        *,
        project_root: Path,
        bundle_name: str,
        agents: list[AgentConfig],
        prompt_routing: str = "required",
        default_agent: str | None = None,
        image_tag: str | None = None,
        build_base: bool = True,
        base_image: str = DEFAULT_BASE_IMAGE,
        force_base: bool = False,
        pull_base: bool = False,
        cleanup_context: bool = False,
        verbose: bool = False,
    ) -> BuildResult:
        if force_base and build_base:
            self.build_base_image(image_name=base_image, force=True)

        if pull_base:
            pull_error: Exception | None = None
            try:
                self.pull_base_image(base_image)
            except Exception as exc:  # noqa: BLE001
                pull_error = exc
            if pull_error is not None:
                if not self.image_exists(base_image) and build_base:
                    self.build_base_image(image_name=base_image, force=False)
                if not self.image_exists(base_image):
                    raise DockerCommandError(
                        f"Failed to pull base image '{base_image}'. "
                        "No local fallback is available."
                    ) from pull_error

        if not self.image_exists(base_image):
            pull_error: Exception | None = None
            try:
                self.pull_base_image(base_image)
            except Exception as exc:  # noqa: BLE001
                pull_error = exc

            if not self.image_exists(base_image) and build_base:
                self.build_base_image(image_name=base_image, force=False)

            if not self.image_exists(base_image):
                message = (
                    f"Base image '{base_image}' is not available locally and pull failed. "
                    "Use --base-image with a valid registry image, or enable local base build."
                )
                if pull_error:
                    raise DockerCommandError(f"{message}\nPull error: {pull_error}") from pull_error
                raise DockerCommandError(message)

        tag = image_tag or f"dank-py-bundle-{self._sanitize(bundle_name)}:{DEFAULT_IMAGE_TAG_SUFFIX}"
        context = self.create_bundle_build_context(
            project_root=project_root,
            bundle_name=bundle_name,
            agents=agents,
            base_image=base_image,
            prompt_routing=prompt_routing,
            default_agent=default_agent,
        )
        try:
            self._build_with_retry(
                ["docker", "build", "-t", tag, "-f", str(context / "Dockerfile"), str(context)],
                verbose=verbose,
            )
            return BuildResult(image_tag=tag, context_path=context)
        finally:
            if cleanup_context:
                self._cleanup_build_context(context)

    def build_production_image(
        self,
        project_root: Path,
        agent: AgentConfig,
        *,
        image_name: str,
        platform: str = "linux/amd64",
        push: bool = False,
        load: bool = True,
        no_cache: bool = False,
        base_image: str = DEFAULT_BASE_IMAGE,
        force_base: bool = False,
        pull_base: bool = False,
        cleanup_context: bool = True,
        verbose: bool = False,
    ) -> ProductionBuildResult:
        if push and load and "," in platform:
            raise DockerCommandError("Cannot use --load with multi-platform builds. Use --push for multi-platform.")

        if force_base:
            self.build_base_image(image_name=base_image, force=True)
        if pull_base:
            try:
                self.pull_base_image(base_image)
            except Exception:
                if not self.image_exists(base_image):
                    self.build_base_image(image_name=base_image, force=False)
        elif not self.image_exists(base_image):
            try:
                self.pull_base_image(base_image)
            except Exception:
                # fallback for local development
                self.build_base_image(image_name=base_image, force=False)

        context = self.create_build_context(project_root=project_root, agent=agent, base_image=base_image)
        cmd = [
            "docker",
            "buildx",
            "build",
            "--platform",
            platform,
            "-t",
            image_name,
            "-f",
            str(context / "Dockerfile"),
        ]

        if no_cache:
            cmd.append("--no-cache")

        if push:
            cmd.append("--push")
            effective_load = False
        else:
            if load:
                cmd.append("--load")
            effective_load = load

        cmd.append(str(context))

        try:
            self._build_with_retry(
                cmd,
                verbose=verbose,
                allow_no_cache_retry=not no_cache,
            )
            return ProductionBuildResult(
                image_name=image_name,
                pushed=push,
                loaded=effective_load,
                context_path=context,
            )
        finally:
            if cleanup_context:
                self._cleanup_build_context(context)

    def build_production_bundle_image(
        self,
        *,
        project_root: Path,
        bundle_name: str,
        agents: list[AgentConfig],
        prompt_routing: str = "required",
        default_agent: str | None = None,
        image_name: str,
        platform: str = "linux/amd64",
        push: bool = False,
        load: bool = True,
        no_cache: bool = False,
        base_image: str = DEFAULT_BASE_IMAGE,
        force_base: bool = False,
        pull_base: bool = False,
        cleanup_context: bool = True,
        verbose: bool = False,
    ) -> ProductionBuildResult:
        if push and load and "," in platform:
            raise DockerCommandError("Cannot use --load with multi-platform builds. Use --push for multi-platform.")

        if force_base:
            self.build_base_image(image_name=base_image, force=True)
        if pull_base:
            try:
                self.pull_base_image(base_image)
            except Exception:
                if not self.image_exists(base_image):
                    self.build_base_image(image_name=base_image, force=False)
        elif not self.image_exists(base_image):
            try:
                self.pull_base_image(base_image)
            except Exception:
                self.build_base_image(image_name=base_image, force=False)

        context = self.create_bundle_build_context(
            project_root=project_root,
            bundle_name=bundle_name,
            agents=agents,
            base_image=base_image,
            prompt_routing=prompt_routing,
            default_agent=default_agent,
        )
        cmd = [
            "docker",
            "buildx",
            "build",
            "--platform",
            platform,
            "-t",
            image_name,
            "-f",
            str(context / "Dockerfile"),
        ]
        if no_cache:
            cmd.append("--no-cache")
        if push:
            cmd.append("--push")
            effective_load = False
        else:
            if load:
                cmd.append("--load")
            effective_load = load
        cmd.append(str(context))

        try:
            self._build_with_retry(
                cmd,
                verbose=verbose,
                allow_no_cache_retry=not no_cache,
            )
            return ProductionBuildResult(
                image_name=image_name,
                pushed=push,
                loaded=effective_load,
                context_path=context,
            )
        finally:
            if cleanup_context:
                self._cleanup_build_context(context)

    def run_agent_container(
        self,
        image_tag: str,
        agent_name: str,
        *,
        agent_id: str | None = None,
        host_port: int = DEFAULT_PORT,
        detach: bool = False,
        quiet: bool = False,
        env_files: list[str] | None = None,
        env_vars: list[str] | None = None,
    ) -> str:
        container_name = self.container_name_for_agent(agent_name)

        # Remove pre-existing container if present
        self._docker_run(["rm", "-f", container_name], capture_output=True, text=True, check=False)

        cmd = [
            "docker",
            "run",
            "--name",
            container_name,
            "-p",
            f"{host_port}:{DEFAULT_PORT}",
            "-e",
            f"AGENT_NAME={agent_name}",
            "-e",
            f"AGENT_ID={agent_id or self._sanitize(agent_name)}",
            "-e",
            f"UVICORN_ACCESS_LOG={'false' if quiet else 'true'}",
            "-e",
            f"UVICORN_LOG_LEVEL={'warning' if quiet else 'info'}",
            "--label",
            "dank.target_type=agent",
            "--label",
            f"dank.agent_ids={self._sanitize(agent_id or agent_name)}",
        ]
        for env_file in env_files or []:
            cmd.extend(["--env-file", str(env_file)])
        for env_var in env_vars or []:
            cmd.extend(["-e", str(env_var)])

        if detach:
            cmd.append("-d")
        else:
            cmd.append("--rm")

        cmd.append(image_tag)
        self._run(cmd, stream_output=not detach)
        return container_name

    def run_bundle_container(
        self,
        *,
        image_tag: str,
        bundle_name: str,
        agent_ids: list[str],
        host_port: int = DEFAULT_PORT,
        detach: bool = True,
        quiet: bool = False,
        bundle_hash: str | None = None,
        target_type: str = "bundle",
        prompt_routing: str | None = None,
        default_agent: str | None = None,
        env_files: list[str] | None = None,
        env_vars: list[str] | None = None,
    ) -> str:
        container_name = self.container_name_for_bundle(bundle_name)
        self._docker_run(["rm", "-f", container_name], capture_output=True, text=True, check=False)

        normalized_agent_ids = [self._sanitize(agent_id) for agent_id in agent_ids if agent_id]
        label_agent_ids = ",".join(normalized_agent_ids)

        cmd = [
            "docker",
            "run",
            "--name",
            container_name,
            "-p",
            f"{host_port}:{DEFAULT_PORT}",
            "-e",
            f"AGENT_NAME={bundle_name}",
            "-e",
            f"UVICORN_ACCESS_LOG={'false' if quiet else 'true'}",
            "-e",
            f"UVICORN_LOG_LEVEL={'warning' if quiet else 'info'}",
            "--label",
            f"dank.target_type={target_type}",
            "--label",
            f"dank.bundle_name={bundle_name}",
            "--label",
            f"dank.agent_ids={label_agent_ids}",
        ]
        for env_file in env_files or []:
            cmd.extend(["--env-file", str(env_file)])
        for env_var in env_vars or []:
            cmd.extend(["-e", str(env_var)])
        if prompt_routing:
            cmd.extend(["-e", f"DANK_PROMPT_ROUTING={prompt_routing}"])
        if default_agent:
            cmd.extend(["-e", f"DANK_DEFAULT_AGENT={default_agent}"])
        if bundle_hash:
            cmd.extend(["--label", f"dank.bundle_hash={bundle_hash}"])

        if detach:
            cmd.append("-d")
        else:
            cmd.append("--rm")

        cmd.append(image_tag)
        self._run(cmd, stream_output=not detach)
        return container_name

    def resolve_log_target(self, target: str) -> ResolvedLogTarget:
        value = str(target or "").strip()
        if not value:
            raise DockerCommandError("Log target cannot be empty.")

        def _single_agent_id(record: ContainerStatusRecord) -> str | None:
            if record.target_type != "agent":
                return None
            ids = [item for item in (record.agent_ids or []) if str(item).strip()]
            if len(ids) != 1:
                return None
            return str(ids[0])

        records = self.list_dank_container_status()
        by_name = {record.name: record for record in records}
        containers = sorted(by_name.keys())
        if value in by_name:
            record = by_name[value]
            return ResolvedLogTarget(
                container_name=record.name,
                target_type=record.target_type,
                host_port=self.get_container_host_port(record.name),
            )

        direct_candidates = [
            self.container_name_for_agent(value),
            self.container_name_for_bundle(value),
        ]
        for candidate in direct_candidates:
            if candidate in by_name:
                record = by_name[candidate]
                return ResolvedLogTarget(
                    container_name=record.name,
                    target_type=record.target_type,
                    host_port=self.get_container_host_port(record.name),
                    agent_id=_single_agent_id(record),
                )

        normalized = self._sanitize(value)
        agent_matches: list[tuple[ContainerStatusRecord, str]] = []
        for record in records:
            for agent_id in record.agent_ids or []:
                if self._sanitize(agent_id) == normalized:
                    agent_matches.append((record, agent_id))
                    break
        if len(agent_matches) == 1:
            record, matched_agent_id = agent_matches[0]
            return ResolvedLogTarget(
                container_name=record.name,
                target_type=record.target_type,
                host_port=self.get_container_host_port(record.name),
                agent_id=matched_agent_id,
            )
        if len(agent_matches) > 1:
            names = ", ".join(sorted(match[0].name for match in agent_matches))
            raise DockerCommandError(
                f"Agent target '{value}' is ambiguous across multiple containers: {names}"
            )

        fuzzy = [name for name in containers if normalized in name]
        if len(fuzzy) == 1:
            record = by_name[fuzzy[0]]
            return ResolvedLogTarget(
                container_name=record.name,
                target_type=record.target_type,
                host_port=self.get_container_host_port(record.name),
            )
        if len(fuzzy) > 1:
            raise DockerCommandError(
                f"Log target '{value}' is ambiguous. Matching containers: {', '.join(sorted(fuzzy))}"
            )

        if containers:
            raise DockerCommandError(
                f"Container '{value}' not found. Available containers: {', '.join(sorted(containers))}"
            )
        raise DockerCommandError("No running or created dank-py containers found.")

    def stream_container_logs(
        self,
        container_name: str,
        *,
        follow: bool = False,
        tail: int = 100,
        since: str | None = None,
    ) -> subprocess.Popen[str]:
        docker_cmd = self._resolve_docker_command(required=True) or "docker"
        cmd = [
            docker_cmd,
            "logs",
            "--timestamps",
            "--tail",
            str(max(1, int(tail))),
        ]
        if since:
            cmd.extend(["--since", str(since)])
        if follow:
            cmd.append("--follow")
        cmd.append(container_name)

        return subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )

    def get_container_status(self, container_name: str) -> str:
        result = self._docker_run(
            ["inspect", "-f", "{{.State.Status}}", container_name],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            return "not-found"
        return (result.stdout or "").strip() or "unknown"

    def stop_container(self, container_name: str, *, remove: bool = True) -> None:
        self._docker_run(["stop", container_name], capture_output=True, text=True, check=False)
        if remove:
            self._docker_run(["rm", "-f", container_name], capture_output=True, text=True, check=False)

    def list_dank_containers(self) -> list[str]:
        result = self._docker_run(["ps", "-a", "--format", "{{.Names}}"], capture_output=True, text=True, check=False)
        if result.returncode != 0:
            return []
        names = [line.strip() for line in (result.stdout or "").splitlines() if line.strip()]
        return [name for name in names if name.startswith("dank-py-")]

    def list_dank_container_status(self) -> list[ContainerStatusRecord]:
        container_names = self.list_dank_containers()
        records: list[ContainerStatusRecord] = []
        for name in container_names:
            result = self._docker_run(["inspect", name], capture_output=True, text=True, check=False)
            if result.returncode != 0:
                continue
            try:
                payload = json.loads(result.stdout)
            except Exception:  # noqa: BLE001
                continue
            if not isinstance(payload, list) or not payload:
                continue
            data = payload[0] if isinstance(payload[0], dict) else {}
            config = data.get("Config", {}) if isinstance(data.get("Config"), dict) else {}
            state_obj = data.get("State", {}) if isinstance(data.get("State"), dict) else {}
            network = data.get("NetworkSettings", {}) if isinstance(data.get("NetworkSettings"), dict) else {}
            labels = config.get("Labels", {}) if isinstance(config.get("Labels"), dict) else {}
            ports_mapping = network.get("Ports", {}) if isinstance(network.get("Ports"), dict) else {}

            def _format_ports(mapping: dict[str, Any]) -> str:
                chunks: list[str] = []
                for container_port, host_bindings in mapping.items():
                    if not host_bindings:
                        continue
                    if not isinstance(host_bindings, list):
                        continue
                    for binding in host_bindings:
                        if not isinstance(binding, dict):
                            continue
                        host_ip = str(binding.get("HostIp", ""))
                        host_port = str(binding.get("HostPort", ""))
                        if host_port:
                            chunks.append(f"{host_ip}:{host_port}->{container_port}")
                return ", ".join(chunks)

            label_agent_ids = str(labels.get("dank.agent_ids", "")).strip()
            agent_ids = [item for item in label_agent_ids.split(",") if item]

            records.append(
                ContainerStatusRecord(
                    name=name,
                    image=str(config.get("Image", "")),
                    state=str(state_obj.get("Status", "unknown")),
                    status_text=str(state_obj.get("Status", "unknown")),
                    ports=_format_ports(ports_mapping),
                    target_type=str(labels.get("dank.target_type")) if labels.get("dank.target_type") else None,
                    bundle_name=str(labels.get("dank.bundle_name")) if labels.get("dank.bundle_name") else None,
                    bundle_hash=str(labels.get("dank.bundle_hash")) if labels.get("dank.bundle_hash") else None,
                    agent_ids=agent_ids,
                )
            )
        return records

    def stop_dank_containers(self, *, container_names: list[str] | None = None, remove: bool = True) -> list[str]:
        targets = container_names or self.list_dank_containers()
        stopped: list[str] = []
        for name in targets:
            if not name:
                continue
            self.stop_container(name, remove=remove)
            stopped.append(name)
        return stopped

    def list_dank_images(self) -> list[str]:
        result = self._docker_run(
            ["images", "--format", "{{.Repository}}:{{.Tag}}"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            return []
        refs = [line.strip() for line in (result.stdout or "").splitlines() if line.strip()]
        return [
            ref
            for ref in refs
            if ref.startswith("dank-py-agent-")
            or "/dank-py-agent-" in ref
            or ref.startswith("dank-py-bundle-")
            or "/dank-py-bundle-" in ref
        ]

    def docker_status(self) -> str:
        docker_cmd = self._resolve_docker_command(required=False)
        if not docker_cmd:
            return "not_installed"
        self._docker_cmd = docker_cmd
        if self._docker_daemon_accessible():
            return "running"
        return "daemon_unavailable"

    def list_base_images(self) -> list[str]:
        if self.docker_status() != "running":
            return []
        result = self._docker_run(
            ["images", "--format", "{{.Repository}}:{{.Tag}}"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            return []
        refs = [line.strip() for line in (result.stdout or "").splitlines() if line.strip()]
        filtered = [
            ref
            for ref in refs
            if "dank-py-base:" in ref
        ]
        return list(dict.fromkeys(filtered))

    def remove_dank_images(self, *, include_base: bool = False) -> list[str]:
        images = self.list_dank_images()
        if include_base:
            base_refs = [
                ref
                for ref in [DEFAULT_BASE_IMAGE]
                if self.image_exists(ref)
            ]
            images.extend(base_refs)

        removed: list[str] = []
        for ref in dict.fromkeys(images):
            result = self._docker_run(["image", "rm", "-f", ref], capture_output=True, text=True, check=False)
            if result.returncode == 0:
                removed.append(ref)
        return removed
