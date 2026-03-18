"""`dank logs` command."""

from __future__ import annotations

import asyncio
import json
import re
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from dank_py.lib.docker.manager import DockerCommandError, DockerManager, ResolvedLogTarget


@dataclass(slots=True)
class LogsCommandOptions:
    target: str | None
    follow: bool
    tail: int
    since: str | None


@dataclass(slots=True)
class LogsCommandResult:
    targets: list[str]
    follow: bool


_REQUEST_TAG_RE = re.compile(r"\[request:(start|end|error)\]")
_AGENT_ID_RE = re.compile(r"agent_id=([A-Za-z0-9_.-]+)")
_DOCKER_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}T[^ ]+)\s+(.*)$")


def _strip_multiplex_header(line: str) -> str:
    # Defensive: dockerode streams include an 8-byte header; CLI `docker logs` usually does not.
    if len(line) >= 8 and line[0] in {"\x00", "\x01", "\x02"}:
        return line[8:]
    return line


def _supports_color() -> bool:
    return sys.stdout.isatty()


def _paint(text: str, code: str) -> str:
    if not _supports_color():
        return text
    return f"\033[{code}m{text}\033[0m"


def _dim(text: str) -> str:
    return _paint(text, "2")


def _cyan(text: str) -> str:
    return _paint(text, "36")


def _green(text: str) -> str:
    return _paint(text, "32")


def _yellow(text: str) -> str:
    return _paint(text, "33")


def _red(text: str) -> str:
    return _paint(text, "31")


def _magenta(text: str) -> str:
    return _paint(text, "35")


def _split_docker_timestamp(line: str) -> tuple[str | None, str]:
    match = _DOCKER_TS_RE.match(line)
    if not match:
        return None, line
    return match.group(1), match.group(2)


def _extract_agent_id(text: str) -> str | None:
    match = _AGENT_ID_RE.search(text)
    return match.group(1) if match else None


def _pretty_docker_log_line(line: str, *, container: str | None = None) -> str:
    clean = _strip_multiplex_header(line).rstrip("\n")
    if not clean:
        return ""

    ts, message = _split_docker_timestamp(clean)
    ts_part = f"{_dim(ts)} " if ts else ""
    container_part = f"{_cyan(container)} " if container else ""

    request_match = _REQUEST_TAG_RE.search(message)
    if request_match:
        kind = request_match.group(1)
        agent_id = _extract_agent_id(message) or "unknown-agent"
        if kind == "start":
            icon = _yellow("🟡")
            label = _yellow("request:start")
        elif kind == "end":
            icon = _green("🟢")
            label = _green("request:end")
        else:
            icon = _red("🔴")
            label = _red("request:error")
        return f"{ts_part}{container_part}{icon} {_magenta(agent_id)} {label} | {message}"

    if '"GET ' in message or '"POST ' in message or '"WS ' in message:
        return f"{ts_part}{container_part}{_cyan('🌐')} {message}"

    if "Started server process" in message or "Application startup complete" in message:
        return f"{ts_part}{container_part}{_green('✅')} {message}"
    if "Waiting for application startup" in message:
        return f"{ts_part}{container_part}{_yellow('⏳')} {message}"

    if "ERROR" in message:
        return f"{ts_part}{container_part}{_red('❌')} {message}"
    if "WARN" in message:
        return f"{ts_part}{container_part}{_yellow('⚠️')} {message}"
    return f"{ts_part}{container_part}{message}"


def _format_runtime_log(entry: dict) -> str:
    timestamp = int(entry.get("timestamp", int(time.time() * 1000)))
    iso = datetime.fromtimestamp(timestamp / 1000.0, tz=UTC).isoformat().replace("+00:00", "Z")
    stream = str(entry.get("stream") or "stdout")
    message = str(entry.get("message") or "")
    scope = str(entry.get("scope") or "container")
    agent_id = entry.get("agent_id")
    trace_id = entry.get("trace_id")
    trace_part = f" trace={trace_id}" if trace_id else ""
    if scope == "agent" and agent_id:
        request_match = _REQUEST_TAG_RE.search(message)
        if request_match:
            kind = request_match.group(1)
            if kind == "start":
                icon = _yellow("🟡")
                label = _yellow("request:start")
            elif kind == "end":
                icon = _green("🟢")
                label = _green("request:end")
            else:
                icon = _red("🔴")
                label = _red("request:error")
            return f"{_dim(iso)} [{stream}] {icon} {_magenta(str(agent_id))}{trace_part} {label} | {message}"
        return f"{_dim(iso)} [{stream}] {_magenta(str(agent_id))}{trace_part} | {message}"
    return _pretty_docker_log_line(f"{iso} {message}")


def _parse_since_to_start_ms(raw: str | None) -> int | None:
    value = str(raw or "").strip()
    if not value:
        return None
    suffix_map = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    suffix = value[-1].lower()
    if suffix in suffix_map:
        try:
            amount = float(value[:-1])
            return int((time.time() - (amount * suffix_map[suffix])) * 1000)
        except ValueError:
            return None
    try:
        numeric = float(value)
    except ValueError:
        return None
    if numeric > 1_000_000_000_000:
        return int(numeric)
    return int(numeric * 1000)


def _runtime_logs_endpoint(target: ResolvedLogTarget) -> str:
    if target.host_port is None:
        raise DockerCommandError(
            f"Could not determine host port for container '{target.container_name}'"
        )
    if not target.agent_id:
        raise DockerCommandError("Runtime log endpoint requires an agent-scoped target.")
    return f"http://localhost:{target.host_port}/traces/{target.agent_id}"


def _runtime_stream_endpoint(target: ResolvedLogTarget) -> str:
    if target.host_port is None:
        raise DockerCommandError(
            f"Could not determine host port for container '{target.container_name}'"
        )
    if not target.agent_id:
        raise DockerCommandError("Runtime stream endpoint requires an agent-scoped target.")
    return f"ws://localhost:{target.host_port}/logs/stream/{target.agent_id}"


def _fetch_runtime_logs(target: ResolvedLogTarget, options: LogsCommandOptions) -> None:
    endpoint = _runtime_logs_endpoint(target)
    query: dict[str, str] = {
        "limit": str(max(1, int(options.tail))),
        "offset": "0",
    }
    start_ms = _parse_since_to_start_ms(options.since)
    if start_ms is not None:
        query["startTime"] = str(start_ms)

    url = endpoint
    if query:
        url = f"{endpoint}?{urlencode(query)}"
    req = Request(url, method="GET")
    with urlopen(req, timeout=10) as response:
        payload = json.loads(response.read().decode("utf-8"))
    data = payload.get("data", {}) if isinstance(payload, dict) else {}
    traces = data.get("traces", []) if isinstance(data, dict) else []
    if isinstance(traces, list) and traces:
        for trace in traces:
            if not isinstance(trace, dict):
                continue
            trace_id = str(trace.get("trace_id") or "unknown")
            status = str(trace.get("status") or "unknown")
            agent_id = str(trace.get("agent_id") or target.agent_id or "unknown")
            duration = trace.get("durationMs")
            event_count = trace.get("eventCount")
            print(
                f"{_cyan('trace')} {trace_id} | "
                f"{_magenta(agent_id)} | status={status} | "
                f"duration_ms={duration} | events={event_count}"
            )
            for entry in trace.get("events", []):
                if isinstance(entry, dict):
                    print(_format_runtime_log(entry))
            print("")
        return

    logs = data.get("logs", []) if isinstance(data, dict) else []
    for entry in logs:
        if isinstance(entry, dict):
            print(_format_runtime_log(entry))


async def _follow_runtime_logs(target: ResolvedLogTarget) -> None:
    try:
        import websockets
    except Exception as exc:  # noqa: BLE001
        raise DockerCommandError("websockets dependency is required for runtime log streaming.") from exc

    url = _runtime_stream_endpoint(target)
    async with websockets.connect(url) as ws:
        async for message in ws:
            payload = json.loads(message)
            event_type = payload.get("type")
            if event_type == "initial":
                for entry in payload.get("data", []):
                    print(_format_runtime_log(entry))
                continue
            if event_type == "log":
                print(_format_runtime_log(payload.get("data", {})))
                continue
            if event_type == "error":
                raise DockerCommandError(str(payload.get("error") or "Runtime log stream error"))


def _stream_target(
    manager: DockerManager,
    *,
    container_name: str,
    options: LogsCommandOptions,
    prefix: str | None = None,
) -> None:
    process = manager.stream_container_logs(
        container_name,
        follow=options.follow,
        tail=options.tail,
        since=options.since,
    )
    if process.stdout is None:
        raise DockerCommandError(f"No log stream available for container '{container_name}'")

    for chunk in process.stdout:
        rendered = _pretty_docker_log_line(chunk, container=prefix)
        if rendered:
            print(rendered)

    process.wait()
    if process.returncode != 0:
        raise DockerCommandError(f"Failed to read logs from container '{container_name}'")


def logs_command(options: LogsCommandOptions) -> LogsCommandResult:
    manager = DockerManager()
    manager.ensure_docker_available()

    if options.follow and not options.target:
        containers = sorted(manager.list_dank_containers())
        if len(containers) == 1:
            single = containers[0]
            _stream_target(manager, container_name=single, options=options)
            return LogsCommandResult(targets=[single], follow=True)
        if not containers:
            return LogsCommandResult(targets=[], follow=False)
        raise ValueError(
            "`--follow` with no target is only supported when exactly one dank-py container is running."
        )

    if options.target:
        target = manager.resolve_log_target(options.target)
        if target.agent_id:
            if options.follow:
                asyncio.run(_follow_runtime_logs(target))
            else:
                _fetch_runtime_logs(target, options)
            return LogsCommandResult(targets=[target.container_name], follow=options.follow)

        _stream_target(manager, container_name=target.container_name, options=options)
        return LogsCommandResult(targets=[target.container_name], follow=options.follow)

    containers = sorted(manager.list_dank_containers())
    if not containers:
        return LogsCommandResult(targets=[], follow=False)

    single_options = LogsCommandOptions(
        target=None,
        follow=False,
        tail=options.tail,
        since=options.since,
    )
    for name in containers:
        print(f"\n=== {name} ===")
        _stream_target(manager, container_name=name, options=single_options, prefix=name)

    return LogsCommandResult(targets=containers, follow=False)
