#!/usr/bin/env python3
"""Dank Python runtime entrypoint."""

from __future__ import annotations

import asyncio
from contextvars import ContextVar
import importlib
import importlib.util
import inspect
import json
import os
import platform
import re
import sys
import threading
import time
import traceback
import tracemalloc
import uuid
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from jsonschema import ValidationError as JsonSchemaValidationError
from jsonschema import validate as jsonschema_validate
from pydantic import BaseModel as PydanticBaseModel
from pydantic import ValidationError as PydanticValidationError

from dank_runtime.constants import DEFAULT_INDEX_PATH, RUNTIME_INDEX_PATH
from dank_runtime.engine import InvocationError
from dank_runtime.engine import invoke as invoke_with_style
from dank_runtime.engine import resolve_callable
from dank_runtime.logging import LogBufferService, StreamCaptureService

APP_ROOT = Path("/app")
AGENT_CODE_ROOT = APP_ROOT / "agent-code"
DEFAULT_PROMPT_AGENT_HEADER = "x-dank-agent-id"
TRACE_ID_HEADER = "x-dank-trace-id"
INCLUDE_TRACE_HEADER = "x-dank-include-trace"
LOG_STREAM_POLL_SECONDS = 0.5
LOG_PREVIEW_MAX_CHARS = 1200
TRACE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")

for candidate in (APP_ROOT, AGENT_CODE_ROOT):
    value = str(candidate)
    if value not in os.sys.path:
        os.sys.path.insert(0, value)


def _normalize_identifier(value: str | None) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"[^a-z0-9_.-]", "-", text)
    text = re.sub(r"-+", "-", text).strip("-.")
    return text or "agent"


class RuntimeState:
    def __init__(self) -> None:
        self.runtime_module = None
        self.mode = "single"
        self.prompt_agent_header = DEFAULT_PROMPT_AGENT_HEADER
        self.prompt_routing = "required"
        self.start_time = time.time()
        self.agents: dict[str, dict[str, Any]] = {}
        self.default_agent_id: str | None = None
        self.cpu_usage_start = os.times()
        self.cpu_usage_start_time = time.perf_counter()
        self.log_buffer = LogBufferService()
        self.active_trace_ids: set[str] = set()
        self.trace_ids_lock = threading.Lock()


state = RuntimeState()
_CURRENT_LOG_AGENT_ID: ContextVar[str | None] = ContextVar("dank_log_agent_id", default=None)
_CURRENT_LOG_TRACE_ID: ContextVar[str | None] = ContextVar("dank_log_trace_id", default=None)
_stream_capture = StreamCaptureService(
    log_buffer=state.log_buffer,
    agent_context=_CURRENT_LOG_AGENT_ID,
    trace_context=_CURRENT_LOG_TRACE_ID,
)


def _load_module_from_path(path: Path, module_name: str):
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load module from {path}")

    module = importlib.util.module_from_spec(spec)
    existing = sys.modules.get(module_name)
    sys.modules[module_name] = module
    original = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(module)
    except Exception:
        if existing is None:
            sys.modules.pop(module_name, None)
        else:
            sys.modules[module_name] = existing
        raise
    finally:
        sys.dont_write_bytecode = original

    _rebuild_pydantic_models(module)
    return module


def _rebuild_pydantic_model(model: Any) -> None:
    rebuild = getattr(model, "model_rebuild", None)
    if not callable(rebuild):
        return
    try:
        rebuild(force=True)
    except TypeError:
        try:
            rebuild()
        except Exception:
            return
    except Exception:
        return


def _rebuild_pydantic_models(module: Any) -> None:
    for value in vars(module).values():
        _rebuild_pydantic_model(value)


def _load_runtime_module():
    generated = Path(RUNTIME_INDEX_PATH)
    if generated.exists():
        return _load_module_from_path(generated, "dank_generated_index")

    return _load_module_from_path(Path(DEFAULT_INDEX_PATH), "dank_default_index")


def _load_user_symbol(entry: dict[str, Any]):
    file_ref = entry.get("file")
    symbol_name = entry.get("symbol")
    if not file_ref or not symbol_name:
        return None

    file_path = Path(file_ref)
    if not file_path.is_absolute():
        file_path = AGENT_CODE_ROOT / file_ref

    if not file_path.exists():
        raise RuntimeError(f"Configured entry file does not exist: {file_path}")

    module = _load_module_from_path(file_path, f"dank_user_module_{abs(hash(file_path))}")
    if not hasattr(module, symbol_name):
        raise RuntimeError(f"Configured symbol '{symbol_name}' not found in {file_path}")

    symbol = getattr(module, symbol_name)
    if inspect.isclass(symbol):
        try:
            symbol = symbol()
        except TypeError as exc:
            raise RuntimeError(
                f"Configured class symbol '{symbol_name}' requires constructor arguments"
            ) from exc
    return symbol


def _load_model(model_path: str):
    if ":" not in model_path:
        raise RuntimeError(f"Invalid model path '{model_path}', expected module:ClassName")

    module_name, symbol_name = model_path.split(":", 1)
    module = importlib.import_module(module_name)
    if not hasattr(module, symbol_name):
        raise RuntimeError(f"Model symbol '{symbol_name}' not found in module '{module_name}'")

    model = getattr(module, symbol_name)
    if not isinstance(model, type) or not issubclass(model, PydanticBaseModel):
        raise RuntimeError(f"Model '{model_path}' is not a Pydantic BaseModel subclass")

    _rebuild_pydantic_model(model)
    return model


def _normalize(value: Any) -> Any:
    if isinstance(value, PydanticBaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {str(k): _normalize(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_normalize(v) for v in value]
    return value


def _validate_payload(payload: Any, ref: dict[str, Any], strict: bool, phase: str) -> Any:
    model_ref = ref.get("model") if isinstance(ref, dict) else None
    schema_ref = ref.get("schema") if isinstance(ref, dict) else None

    if model_ref:
        try:
            model = _load_model(model_ref)
            validated = model.model_validate(payload)
            return validated.model_dump(mode="json")
        except (RuntimeError, PydanticValidationError) as exc:
            if strict:
                raise HTTPException(status_code=422 if phase == "input" else 500, detail=str(exc)) from exc
            return _normalize(payload)

    if schema_ref:
        try:
            jsonschema_validate(instance=payload, schema=schema_ref)
            return payload
        except JsonSchemaValidationError as exc:
            if strict:
                raise HTTPException(status_code=422 if phase == "input" else 500, detail=str(exc)) from exc
            return _normalize(payload)

    return _normalize(payload)


def _resolve_target(runtime_module: Any, symbol: Any, spec: dict[str, Any]) -> Any:
    if hasattr(runtime_module, "resolve_target"):
        try:
            return runtime_module.resolve_target(symbol, spec)
        except TypeError:
            return runtime_module.resolve_target(symbol)
    entry = spec.get("entry", {}) if isinstance(spec, dict) else {}
    return resolve_callable(
        symbol,
        method=entry.get("method"),
        call_type=entry.get("call_type", "auto"),
    )


def _register_agent(spec: dict[str, Any], runtime_module: Any) -> str:
    entry = spec.get("entry", {}) if isinstance(spec, dict) else {}
    symbol = None
    callable_target = None

    try:
        symbol = _load_user_symbol(entry)
    except Exception:
        if Path(RUNTIME_INDEX_PATH).exists():
            raise

    if symbol is not None:
        callable_target = _resolve_target(runtime_module, symbol, spec)
    elif hasattr(runtime_module, "resolve_target"):
        try:
            callable_target = runtime_module.resolve_target(None, spec)
        except TypeError:
            callable_target = runtime_module.resolve_target(None)

    agent_name = str(spec.get("name") or os.getenv("AGENT_NAME", "unknown"))
    agent_id = _normalize_identifier(spec.get("id") or agent_name)

    state.agents[agent_id] = {
        "id": agent_id,
        "name": agent_name,
        "spec": spec,
        "symbol": symbol,
        "callable": callable_target,
        "calls_total": 0,
        "errors_total": 0,
        "last_call_at": None,
        "last_processing_ms": None,
        "last_error": None,
    }
    return agent_id


def _resolve_agent_key(ref: str | None) -> str | None:
    normalized_ref = _normalize_identifier(ref)
    if not normalized_ref:
        return None
    if normalized_ref in state.agents:
        return normalized_ref
    for key, rec in state.agents.items():
        if _normalize_identifier(rec.get("name")) == normalized_ref:
            return key
    return None


def initialize_runtime() -> None:
    runtime_module = _load_runtime_module()
    state.runtime_module = runtime_module

    bundle_spec = getattr(runtime_module, "BUNDLE_SPEC", None)
    if isinstance(bundle_spec, dict) and isinstance(bundle_spec.get("agents"), list):
        state.mode = "bundle"
        state.prompt_agent_header = DEFAULT_PROMPT_AGENT_HEADER
        state.prompt_routing = str(bundle_spec.get("prompt_routing") or "required").lower()
        if state.prompt_routing not in {"required", "default"}:
            state.prompt_routing = "required"
        for item in bundle_spec.get("agents", []):
            if not isinstance(item, dict):
                continue
            _register_agent(item, runtime_module)

        env_routing = str(os.getenv("DANK_PROMPT_ROUTING", "")).strip().lower()
        if env_routing in {"required", "default"}:
            state.prompt_routing = env_routing

        explicit_default = os.getenv("DANK_DEFAULT_AGENT") or bundle_spec.get("default_agent")
        resolved_default = _resolve_agent_key(str(explicit_default) if explicit_default else None)
        if state.prompt_routing == "default":
            if resolved_default:
                state.default_agent_id = resolved_default
            elif state.agents:
                state.default_agent_id = next(iter(state.agents.keys()))
        else:
            state.default_agent_id = None
        return

    state.mode = "single"
    state.prompt_agent_header = DEFAULT_PROMPT_AGENT_HEADER
    state.prompt_routing = "required"
    agent_spec = getattr(runtime_module, "AGENT_SPEC", {}) or {}
    if not isinstance(agent_spec, dict):
        agent_spec = {}
    default_id = _register_agent(agent_spec, runtime_module)
    state.default_agent_id = default_id


def _invoke_with_runtime_module(callable_obj: Any, payload: Any, spec: dict[str, Any]) -> Any:
    runtime_module = state.runtime_module
    if runtime_module is None:
        raise RuntimeError("Runtime module is not initialized")

    if hasattr(runtime_module, "invoke_target"):
        try:
            return runtime_module.invoke_target(callable_obj, payload, spec)
        except TypeError:
            return runtime_module.invoke_target(callable_obj, payload)
    return None


async def _invoke_agent(agent_rec: dict[str, Any], payload: Any) -> Any:
    callable_target = agent_rec.get("callable")
    if callable_target is None:
        raise HTTPException(status_code=500, detail="No callable target available")

    maybe_result = _invoke_with_runtime_module(callable_target, payload, agent_rec.get("spec", {}))
    if maybe_result is not None:
        if inspect.isawaitable(maybe_result):
            return await maybe_result
        return maybe_result

    entry = agent_rec.get("spec", {}).get("entry", {})
    call_style = entry.get("call_style", "auto")
    return await invoke_with_style(callable_target, payload, call_style=call_style)


def _choose_agent_for_request(request: Request) -> dict[str, Any]:
    if state.mode == "bundle":
        header_value = request.headers.get(state.prompt_agent_header)
        if header_value:
            agent_key = _resolve_agent_key(header_value)
            rec = state.agents.get(agent_key or "")
            if not rec:
                raise HTTPException(status_code=404, detail=f"Unknown agent id '{header_value}'")
            return rec

        if state.prompt_routing == "default":
            default_id = state.default_agent_id
            if default_id and default_id in state.agents:
                return state.agents[default_id]

        raise HTTPException(
            status_code=400,
            detail=f"Missing required header '{state.prompt_agent_header}' for bundled runtime",
        )

    default_id = state.default_agent_id
    if not default_id or default_id not in state.agents:
        raise HTTPException(status_code=500, detail="Runtime has no configured agent")

    header_value = request.headers.get(state.prompt_agent_header)
    if header_value:
        requested = _normalize_identifier(header_value)
        if requested != default_id:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Header '{state.prompt_agent_header}' points to '{header_value}', "
                    f"but this container serves '{default_id}'"
                ),
            )
    return state.agents[default_id]


def _parse_int(value: Any, default: int, *, minimum: int = 0) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = int(default)
    return max(minimum, parsed)


def _safe_json(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except Exception:  # noqa: BLE001
        return repr(value)


def _preview(value: Any, max_chars: int = LOG_PREVIEW_MAX_CHARS) -> str:
    text = _safe_json(value)
    if len(text) <= max_chars:
        return text
    return f"{text[:max_chars]}... (truncated)"


def _emit_runtime_log(stream: str, message: str) -> None:
    if stream == "stderr":
        print(message, file=sys.stderr, flush=True)
        return
    print(message, flush=True)


def _parse_bool(value: Any) -> bool:
    text = str(value or "").strip().lower()
    return text in {"1", "true", "yes", "on"}


def _resolve_trace_id(header_value: str | None) -> str:
    if header_value:
        candidate = str(header_value).strip()
        if TRACE_ID_PATTERN.match(candidate):
            return candidate
    return f"trc_{uuid.uuid4().hex}"


def _validate_trace_id_or_404(trace_id: str) -> str:
    candidate = str(trace_id or "").strip()
    if not TRACE_ID_PATTERN.match(candidate):
        raise HTTPException(status_code=404, detail=f"Unknown trace id '{trace_id}'")
    return candidate


def _reserve_trace_id(trace_id: str) -> bool:
    with state.trace_ids_lock:
        if trace_id in state.active_trace_ids:
            return False
        existing = state.log_buffer.get_logs(trace_id=trace_id, limit=1, offset=0)
        if int(existing.get("total", 0)) > 0:
            return False
        state.active_trace_ids.add(trace_id)
        return True


def _release_trace_id(trace_id: str) -> None:
    with state.trace_ids_lock:
        state.active_trace_ids.discard(trace_id)


def _resolve_log_agent_id(ref: str | None) -> str | None:
    if ref is None:
        return None
    key = _resolve_agent_key(ref)
    if not key:
        raise HTTPException(status_code=404, detail=f"Unknown agent id '{ref}'")
    return key


def _parse_time_window(request: Request) -> tuple[int | None, int | None]:
    start_time_raw = request.query_params.get("startTime")
    end_time_raw = request.query_params.get("endTime")
    minutes_ago_raw = request.query_params.get("minutesAgo")
    start_time = _parse_int(start_time_raw, default=0, minimum=0) if start_time_raw else None
    end_time = _parse_int(end_time_raw, default=0, minimum=0) if end_time_raw else None
    if start_time is None and minutes_ago_raw:
        minutes_ago = _parse_int(minutes_ago_raw, default=10, minimum=0)
        final_end = end_time if end_time is not None else int(time.time() * 1000)
        start_time = max(0, final_end - (minutes_ago * 60 * 1000))
        end_time = final_end
    return start_time, end_time


def _log_get_response(request: Request, *, force_limit: int | None = None) -> dict[str, Any]:
    start_time, end_time = _parse_time_window(request)
    limit_raw = request.query_params.get("limit")
    offset_raw = request.query_params.get("offset")
    stream = request.query_params.get("stream")
    limit = force_limit if force_limit is not None else _parse_int(limit_raw, default=100, minimum=1)
    offset = _parse_int(offset_raw, default=0, minimum=0)

    data = state.log_buffer.get_logs(
        start_time=start_time,
        end_time=end_time,
        limit=limit,
        offset=offset,
        stream=stream,
    )
    return {"success": True, "data": data}


def _log_stats_response() -> dict[str, Any]:
    data = state.log_buffer.get_stats()
    return {"success": True, "data": data}


def _trace_status(events: list[dict[str, Any]]) -> str:
    status = "in_progress"
    for entry in events:
        message = str(entry.get("message") or "")
        if "[request:error]" in message:
            status = "error"
        elif "[request:end]" in message:
            status = "success"
    return status


def _group_traces_from_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for event in events:
        trace_id = str(event.get("trace_id") or "").strip()
        if not trace_id:
            continue
        grouped.setdefault(trace_id, []).append(event)

    traces: list[dict[str, Any]] = []
    for trace_id, trace_events in grouped.items():
        sorted_events = sorted(
            trace_events,
            key=lambda item: (int(item.get("timestamp", 0)), int(item.get("seq", 0))),
        )
        public_events: list[dict[str, Any]] = []
        for item in sorted_events:
            public_item = dict(item)
            public_item.pop("seq", None)
            public_item.pop("scope", None)
            public_item.pop("agent_id", None)
            public_item.pop("trace_id", None)
            public_events.append(public_item)
        start_time = int(sorted_events[0].get("timestamp", 0)) if sorted_events else None
        end_time = int(sorted_events[-1].get("timestamp", 0)) if sorted_events else None
        agent_id = next((str(item.get("agent_id")) for item in sorted_events if item.get("agent_id")), None)
        traces.append(
            {
                "trace_id": trace_id,
                "agent_id": agent_id,
                "status": _trace_status(sorted_events),
                "startTime": start_time,
                "endTime": end_time,
                "durationMs": (end_time - start_time) if start_time is not None and end_time is not None else None,
                "eventCount": len(public_events),
                "events": public_events,
            }
        )

    traces.sort(key=lambda item: int(item.get("startTime") or 0), reverse=True)
    return traces


def _trace_payload_for_trace_id(trace_id: str) -> dict[str, Any] | None:
    payload = state.log_buffer.get_logs(
        trace_id=trace_id,
        limit=state.log_buffer.max_size,
        offset=0,
    )
    traces = _group_traces_from_events(list(payload.get("logs", [])))
    if not traces:
        return None
    return traces[0]


def _default_trace_agent_id() -> str:
    if len(state.agents) == 1:
        return next(iter(state.agents.keys()))
    if state.default_agent_id and state.default_agent_id in state.agents:
        return state.default_agent_id
    raise HTTPException(
        status_code=400,
        detail="Multiple agents available. Specify /traces/{agent_id}.",
    )


def _traces_response_for_agent(agent_id: str, request: Request) -> dict[str, Any]:
    start_time, end_time = _parse_time_window(request)
    limit = _parse_int(request.query_params.get("limit"), default=100, minimum=1)
    offset = _parse_int(request.query_params.get("offset"), default=0, minimum=0)

    payload = state.log_buffer.get_logs(
        start_time=start_time,
        end_time=end_time,
        limit=state.log_buffer.max_size,
        offset=0,
        agent_id=agent_id,
    )
    traces = _group_traces_from_events(list(payload.get("logs", [])))
    paged = traces[offset : offset + limit]
    return {
        "success": True,
        "data": {
            "agent_id": agent_id,
            "traces": paged,
            "total": len(traces),
            "limit": limit,
            "offset": offset,
            "hasMore": offset + limit < len(traces),
        },
    }


def _trace_response_for_trace_id(trace_id: str) -> dict[str, Any]:
    trace = _trace_payload_for_trace_id(trace_id)
    if trace is None:
        raise HTTPException(status_code=404, detail=f"Unknown trace id '{trace_id}'")
    return {"success": True, "data": trace}


def _traces_stats_response_for_agent(agent_id: str, request: Request) -> dict[str, Any]:
    start_time, end_time = _parse_time_window(request)
    payload = state.log_buffer.get_logs(
        start_time=start_time,
        end_time=end_time,
        limit=state.log_buffer.max_size,
        offset=0,
        agent_id=agent_id,
    )
    traces = _group_traces_from_events(list(payload.get("logs", [])))
    success = len([trace for trace in traces if trace.get("status") == "success"])
    error = len([trace for trace in traces if trace.get("status") == "error"])
    in_progress = len([trace for trace in traces if trace.get("status") == "in_progress"])
    oldest = min((int(trace.get("startTime") or 0) for trace in traces), default=None)
    newest = max((int(trace.get("endTime") or 0) for trace in traces), default=None)
    return {
        "success": True,
        "data": {
            "agent_id": agent_id,
            "tracesTotal": len(traces),
            "success": success,
            "error": error,
            "inProgress": in_progress,
            "eventsTotal": sum(int(trace.get("eventCount") or 0) for trace in traces),
            "oldest": oldest,
            "newest": newest,
        },
    }


async def _logs_stream_handler(websocket: WebSocket, *, path_agent_id: str | None = None) -> None:
    stream = websocket.query_params.get("stream")
    try:
        if path_agent_id:
            agent_id = _resolve_log_agent_id(path_agent_id)
        else:
            agent_id = _resolve_log_agent_id(websocket.query_params.get("agent_id"))
    except HTTPException as exc:
        await websocket.accept()
        await websocket.send_json({"type": "error", "error": str(exc.detail)})
        await websocket.close(code=1008)
        return

    await websocket.accept()
    initial = state.log_buffer.get_logs(
        limit=100,
        offset=0,
        stream=stream,
        agent_id=agent_id,
    )
    await websocket.send_json(
        {
            "type": "initial",
            "data": initial.get("logs", []),
        }
    )

    last_seq = state.log_buffer.latest_seq()
    try:
        while True:
            await asyncio.sleep(LOG_STREAM_POLL_SECONDS)
            entries = state.log_buffer.get_logs_since(
                since_seq=last_seq,
                stream=stream,
                agent_id=agent_id,
            )
            for entry in entries:
                await websocket.send_json({"type": "log", "data": entry})
            if entries:
                last_seq = int(entries[-1].get("seq", last_seq))
    except WebSocketDisconnect:
        return
    except Exception:  # noqa: BLE001
        return


def _memory_snapshot() -> dict[str, Any]:
    try:
        import resource  # type: ignore

        usage = resource.getrusage(resource.RUSAGE_SELF)
        return {"max_rss_kb": int(getattr(usage, "ru_maxrss", 0))}
    except Exception:  # noqa: BLE001
        return {}


def _http_endpoints() -> dict[str, list[str]]:
    builtin = [
        "/health",
        "/prompt",
        "/status",
        "/status/{agent_id}",
        "/metrics",
        "/logs",
        "/logs/stats",
        "/logs/stream",
        "/logs/stream/{agent_id}",
        "/traces",
        "/traces/stats",
        "/traces/{agent_id}",
        "/traces/stats/{agent_id}",
        "/trace/{trace_id}",
    ]
    return {"builtin": builtin, "userDefined": [], "all": builtin}


def _cpu_model() -> str:
    cpuinfo = Path("/proc/cpuinfo")
    if cpuinfo.exists():
        try:
            for line in cpuinfo.read_text(encoding="utf-8", errors="ignore").splitlines():
                if line.lower().startswith("model name"):
                    _, value = line.split(":", 1)
                    parsed = value.strip()
                    if parsed:
                        return parsed
        except Exception:  # noqa: BLE001
            pass
    processor = str(platform.processor() or "").strip()
    if processor:
        return processor
    machine = str(platform.machine() or "").strip()
    if machine:
        return machine
    return "unknown"


def _system_memory_bytes() -> tuple[int, int, int]:
    try:
        page_size = int(os.sysconf("SC_PAGE_SIZE"))
        total_pages = int(os.sysconf("SC_PHYS_PAGES"))
        free_pages = int(os.sysconf("SC_AVPHYS_PAGES"))
        total = page_size * total_pages
        free = page_size * free_pages
        used = max(0, total - free)
        return total, free, used
    except Exception:  # noqa: BLE001
        return 0, 0, 0


def _process_rss_bytes() -> int:
    statm = Path("/proc/self/statm")
    if statm.exists():
        try:
            parts = statm.read_text(encoding="utf-8").split()
            if len(parts) >= 2:
                page_size = int(os.sysconf("SC_PAGE_SIZE"))
                return int(parts[1]) * page_size
        except Exception:  # noqa: BLE001
            pass
    try:
        import resource  # type: ignore

        rss_kb = int(getattr(resource.getrusage(resource.RUSAGE_SELF), "ru_maxrss", 0))
        if rss_kb > 0:
            return rss_kb * 1024
    except Exception:  # noqa: BLE001
        pass
    return 0


def _bytes_mb(value: int) -> str:
    return f"{(int(value) / 1024 / 1024):.2f} MB"


def _bytes_gb(value: int) -> str:
    return f"{(int(value) / 1024 / 1024 / 1024):.2f} GB"


def _runtime_agent_identity() -> tuple[str, str | None]:
    if state.mode == "bundle":
        return os.getenv("AGENT_NAME", "bundle"), None
    if state.default_agent_id and state.default_agent_id in state.agents:
        rec = state.agents[state.default_agent_id]
        return str(rec.get("name") or "unknown"), str(rec.get("id") or state.default_agent_id)
    return os.getenv("AGENT_NAME", "unknown"), os.getenv("AGENT_ID")


def _status_payload(agent_id: str | None = None) -> dict[str, Any]:
    now = time.time()

    def _agent_status(rec: dict[str, Any], *, include_calls: bool) -> dict[str, Any]:
        payload = {
            "name": rec.get("name"),
            "id": rec.get("id"),
            "status": "running",
            "startTime": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(state.start_time)),
            "uptime": int((now - state.start_time) * 1000),
        }
        if include_calls:
            payload.update(
                {
                    "callsTotal": rec.get("calls_total", 0),
                    "errorsTotal": rec.get("errors_total", 0),
                    "lastCallAt": rec.get("last_call_at"),
                    "lastProcessingMs": rec.get("last_processing_ms"),
                    "lastError": rec.get("last_error"),
                }
            )
        return payload

    if agent_id:
        rec = state.agents.get(agent_id)
        if rec is None:
            raise HTTPException(status_code=404, detail=f"Unknown agent id '{agent_id}'")
        agent_section = _agent_status(rec, include_calls=True)
    else:
        if state.mode == "bundle":
            agent_section = {
                "name": os.getenv("AGENT_NAME", "bundle"),
                "id": None,
                "status": "running",
                "startTime": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(state.start_time)),
                "uptime": int((now - state.start_time) * 1000),
            }
        else:
            primary_id = state.default_agent_id or next(iter(state.agents.keys()), "unknown")
            primary = state.agents.get(primary_id, {})
            agent_section = _agent_status(
                {
                    "name": primary.get("name", "unknown"),
                    "id": primary.get("id", primary_id),
                },
                include_calls=False,
            )

    payload = {
        "agent": agent_section,
        "llm": {
            "provider": os.getenv("LLM_PROVIDER"),
            "model": os.getenv("LLM_MODEL") or os.getenv("OPENAI_MODEL"),
            "hasClient": bool(os.getenv("OPENAI_API_KEY") or os.getenv("ANTHROPIC_API_KEY")),
        },
        "http": {
            "enabled": True,
            "port": int(os.getenv("PORT", "3000")),
            "endpoints": _http_endpoints(),
        },
        "handlers": [],
        "environment": {
            "pythonVersion": sys.version.split()[0],
            "platform": sys.platform,
            "memory": _memory_snapshot(),
        },
        "runtime": {
            "mode": state.mode,
            "promptAgentHeader": state.prompt_agent_header,
            "defaultAgentId": state.default_agent_id,
            "availableAgents": [
                {"id": rec.get("id"), "name": rec.get("name")}
                for rec in state.agents.values()
            ],
        },
    }
    if state.mode == "bundle":
        payload["runtime"]["promptRouting"] = state.prompt_routing
    if agent_id is None and state.mode == "bundle":
        payload["agents"] = [
            _agent_status(rec, include_calls=True)
            for rec in state.agents.values()
        ]
    return payload


_stream_capture.start()
initialize_runtime()
app = FastAPI(title="Dank Python Runtime", version="1.0.2")


@app.get("/health")
async def health() -> dict[str, Any]:
    if state.mode == "bundle":
        return {
            "status": "healthy",
            "agent": {
                "name": os.getenv("AGENT_NAME", "bundle"),
                "id": None,
            },
            "runtime": {
                "mode": state.mode,
                "prompt_agent_header": state.prompt_agent_header,
                "prompt_routing": state.prompt_routing,
                "default_agent_id": state.default_agent_id,
                "available_agents": [rec.get("id") for rec in state.agents.values()],
                "generated_index": Path(RUNTIME_INDEX_PATH).exists(),
                "uptime_seconds": int(time.time() - state.start_time),
            },
        }

    return {
        "status": "healthy",
        "agent": {
            "name": (
                state.agents.get(state.default_agent_id, {}).get("name")
                if state.default_agent_id
                else os.getenv("AGENT_NAME", "unknown")
            ),
            "id": (
                state.agents.get(state.default_agent_id, {}).get("id")
                if state.default_agent_id
                else None
            ),
        },
        "runtime": {
            "mode": state.mode,
            "available_agents": [rec.get("id") for rec in state.agents.values()],
            "generated_index": Path(RUNTIME_INDEX_PATH).exists(),
            "uptime_seconds": int(time.time() - state.start_time),
        },
    }


@app.get("/status")
async def status() -> dict[str, Any]:
    return _status_payload(None)


@app.get("/status/{agent_id}")
async def status_for_agent(agent_id: str) -> dict[str, Any]:
    return _status_payload(_normalize_identifier(agent_id))


@app.get("/metrics")
async def metrics() -> dict[str, Any]:
    mem_rss = _process_rss_bytes()

    # Approximate Python heap usage using tracemalloc for parity-like visibility.
    if not tracemalloc.is_tracing():
        tracemalloc.start()
    heap_used, heap_peak = tracemalloc.get_traced_memory()

    measurement_start_cpu = os.times()
    measurement_start_time = time.perf_counter()
    await asyncio.sleep(0.5)
    measurement_end_cpu = os.times()
    measurement_end_time = time.perf_counter()

    measurement_delta_ms = max(0.0, (measurement_end_time - measurement_start_time) * 1000.0)
    delta_user_ms = max(0.0, (measurement_end_cpu.user - measurement_start_cpu.user) * 1000.0)
    delta_system_ms = max(0.0, (measurement_end_cpu.system - measurement_start_cpu.system) * 1000.0)
    total_cpu_ms = delta_user_ms + delta_system_ms
    cpu_percent = (total_cpu_ms / measurement_delta_ms * 100.0) if measurement_delta_ms > 0 else 0.0

    cumulative_user_ms = max(0.0, (measurement_end_cpu.user - state.cpu_usage_start.user) * 1000.0)
    cumulative_system_ms = max(0.0, (measurement_end_cpu.system - state.cpu_usage_start.system) * 1000.0)
    cumulative_total_ms = cumulative_user_ms + cumulative_system_ms
    cumulative_uptime_ms = max(0.0, (measurement_end_time - state.cpu_usage_start_time) * 1000.0)
    cumulative_cpu_percent = (
        (cumulative_total_ms / cumulative_uptime_ms) * 100.0 if cumulative_uptime_ms > 0 else 0.0
    )

    total_mem, free_mem, used_mem = _system_memory_bytes()
    mem_percent = ((used_mem / total_mem) * 100.0) if total_mem > 0 else 0.0
    agent_name, agent_id = _runtime_agent_identity()

    return {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "agent": {
            "name": agent_name,
            "id": agent_id,
            "uptime": int((time.time() - state.start_time) * 1000),
        },
        "cpu": {
            "current": {
                "user": int(delta_user_ms * 1000),  # microseconds
                "system": int(delta_system_ms * 1000),  # microseconds
                "total": round(total_cpu_ms, 3),  # milliseconds
                "percent": float(f"{cpu_percent:.2f}"),
                "measurementPeriod": round(measurement_delta_ms, 3),  # milliseconds
            },
            "cumulative": {
                "user": int(cumulative_user_ms * 1000),  # microseconds
                "system": int(cumulative_system_ms * 1000),  # microseconds
                "total": round(cumulative_total_ms, 3),  # milliseconds
                "percent": float(f"{cumulative_cpu_percent:.2f}"),
                "uptime": round(cumulative_uptime_ms, 3),  # milliseconds
            },
            "cores": os.cpu_count() or 1,
            "model": _cpu_model(),
        },
        "memory": {
            "process": {
                "rss": int(mem_rss),
                "heapTotal": int(heap_peak),
                "heapUsed": int(heap_used),
                "external": 0,
                "arrayBuffers": 0,
            },
            "system": {
                "total": int(total_mem),
                "free": int(free_mem),
                "used": int(used_mem),
                "percent": f"{mem_percent:.2f}",
            },
            "processFormatted": {
                "rss": _bytes_mb(mem_rss),
                "heapTotal": _bytes_mb(heap_peak),
                "heapUsed": _bytes_mb(heap_used),
                "external": _bytes_mb(0),
            },
            "systemFormatted": {
                "total": _bytes_gb(total_mem),
                "free": _bytes_gb(free_mem),
                "used": _bytes_gb(used_mem),
            },
        },
        "loadAverage": list(os.getloadavg()) if hasattr(os, "getloadavg") else [0.0, 0.0, 0.0],
    }


@app.get("/logs")
async def logs(request: Request) -> dict[str, Any]:
    return _log_get_response(request)


@app.get("/logs/stats")
async def logs_stats() -> dict[str, Any]:
    return _log_stats_response()


@app.websocket("/logs/stream")
async def logs_stream(ws: WebSocket) -> None:
    await _logs_stream_handler(ws)


@app.websocket("/logs/stream/{agent_id}")
async def logs_stream_for_agent(agent_id: str, ws: WebSocket) -> None:
    await _logs_stream_handler(ws, path_agent_id=agent_id)


@app.get("/traces")
async def traces(request: Request) -> dict[str, Any]:
    return _traces_response_for_agent(_default_trace_agent_id(), request)


@app.get("/traces/stats")
async def traces_stats(request: Request) -> dict[str, Any]:
    return _traces_stats_response_for_agent(_default_trace_agent_id(), request)


@app.get("/traces/stats/{agent_id}")
async def traces_stats_for_agent(agent_id: str, request: Request) -> dict[str, Any]:
    return _traces_stats_response_for_agent(_resolve_log_agent_id(agent_id) or "", request)


@app.get("/traces/{agent_id}")
async def traces_for_agent(agent_id: str, request: Request) -> dict[str, Any]:
    return _traces_response_for_agent(_resolve_log_agent_id(agent_id) or "", request)


@app.get("/trace/{trace_id}")
async def trace_by_id(trace_id: str) -> dict[str, Any]:
    return _trace_response_for_trace_id(_validate_trace_id_or_404(trace_id))


@app.post("/prompt")
async def prompt(request: Request) -> JSONResponse:
    start = time.time()
    raw_trace_header = request.headers.get(TRACE_ID_HEADER)
    trace_id = _resolve_trace_id(raw_trace_header)
    include_trace = _parse_bool(request.headers.get(INCLUDE_TRACE_HEADER))

    reserved_trace = False
    try:
        try:
            header_trace_valid = bool(raw_trace_header and TRACE_ID_PATTERN.match(str(raw_trace_header).strip()))
            if header_trace_valid:
                if not _reserve_trace_id(trace_id):
                    raise HTTPException(
                        status_code=409,
                        detail=f"Trace id '{trace_id}' already exists. Provide a unique '{TRACE_ID_HEADER}'.",
                        headers={TRACE_ID_HEADER: trace_id},
                    )
                reserved_trace = True
            else:
                attempts = 0
                while attempts < 5:
                    if _reserve_trace_id(trace_id):
                        reserved_trace = True
                        break
                    trace_id = _resolve_trace_id(None)
                    attempts += 1
                if not reserved_trace:
                    raise HTTPException(
                        status_code=500,
                        detail="Failed to allocate a unique trace id.",
                        headers={TRACE_ID_HEADER: trace_id},
                    )
        except HTTPException as exc:
            headers = dict(exc.headers or {})
            headers.setdefault(TRACE_ID_HEADER, trace_id)
            raise HTTPException(status_code=exc.status_code, detail=exc.detail, headers=headers) from exc

        try:
            payload = await request.json()
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                status_code=422,
                detail=f"Invalid JSON body: {exc}",
                headers={TRACE_ID_HEADER: trace_id},
            ) from exc

        try:
            agent_rec = _choose_agent_for_request(request)
        except HTTPException as exc:
            headers = dict(exc.headers or {})
            headers.setdefault(TRACE_ID_HEADER, trace_id)
            raise HTTPException(status_code=exc.status_code, detail=exc.detail, headers=headers) from exc

        spec = agent_rec.get("spec", {})
        io_config = spec.get("io", {}) if isinstance(spec, dict) else {}
        input_ref = io_config.get("input", {}) if isinstance(io_config, dict) else {}
        output_ref = io_config.get("output", {}) if isinstance(io_config, dict) else {}
        strict_output = bool(io_config.get("strict_output", True)) if isinstance(io_config, dict) else True

        agent_ctx_value = str(agent_rec.get("id") or "")
        agent_token = _CURRENT_LOG_AGENT_ID.set(agent_ctx_value or None)
        trace_token = _CURRENT_LOG_TRACE_ID.set(trace_id)
        try:
            validated_input = _validate_payload(payload, input_ref, strict=True, phase="input")
            _emit_runtime_log(
                "stdout",
                (
                    f"[request:start] agent_id={agent_ctx_value or 'unknown'} "
                    f"trace_id={trace_id} agent={agent_rec.get('name')} input={_preview(validated_input)}"
                ),
            )

            agent_rec["calls_total"] = int(agent_rec.get("calls_total", 0)) + 1
            agent_rec["last_call_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            try:
                raw_result = await _invoke_agent(agent_rec, validated_input)
            except HTTPException:
                agent_rec["errors_total"] = int(agent_rec.get("errors_total", 0)) + 1
                agent_rec["last_error"] = "http-error"
                _emit_runtime_log(
                    "stderr",
                    f"[request:error] agent_id={agent_ctx_value or 'unknown'} trace_id={trace_id} error=http-error",
                )
                raise
            except (TypeError, InvocationError) as exc:
                agent_rec["errors_total"] = int(agent_rec.get("errors_total", 0)) + 1
                agent_rec["last_error"] = str(exc)
                _emit_runtime_log(
                    "stderr",
                    (
                        f"[request:error] agent_id={agent_ctx_value or 'unknown'} trace_id={trace_id} "
                        f"invocation_error={_preview(str(exc), 400)}"
                    ),
                )
                raise HTTPException(
                    status_code=400,
                    detail=f"Invocation parameter error: {exc}",
                    headers={TRACE_ID_HEADER: trace_id},
                ) from exc
            except Exception as exc:  # noqa: BLE001
                agent_rec["errors_total"] = int(agent_rec.get("errors_total", 0)) + 1
                agent_rec["last_error"] = str(exc)
                _emit_runtime_log(
                    "stderr",
                    (
                        f"[request:error] agent_id={agent_ctx_value or 'unknown'} trace_id={trace_id} "
                        f"exception={_preview(str(exc), 400)}"
                    ),
                )
                raise HTTPException(
                    status_code=500,
                    detail=f"Invocation failed: {exc}",
                    headers={TRACE_ID_HEADER: trace_id},
                ) from exc

            processing_ms = int((time.time() - start) * 1000)
            agent_rec["last_processing_ms"] = processing_ms
            normalized_result = _normalize(raw_result)
            validated_output = _validate_payload(normalized_result, output_ref, strict=strict_output, phase="output")
            _emit_runtime_log(
                "stdout",
                (
                    f"[request:end] agent_id={agent_ctx_value or 'unknown'} "
                    f"trace_id={trace_id} processing_ms={processing_ms} output={_preview(validated_output)}"
                ),
            )

            body = {
                "result": validated_output,
                "metadata": {
                    "processing_ms": processing_ms,
                    "agent": agent_rec.get("name"),
                    "agent_id": agent_rec.get("id"),
                    "trace_id": trace_id,
                },
            }
            if include_trace:
                body["metadata"]["trace"] = _trace_payload_for_trace_id(trace_id)
            response = JSONResponse(content=body)
            response.headers[TRACE_ID_HEADER] = trace_id
            return response
        except HTTPException as exc:
            headers = dict(exc.headers or {})
            headers.setdefault(TRACE_ID_HEADER, trace_id)
            metadata: dict[str, Any] = {"trace_id": trace_id}
            if "agent_rec" in locals() and isinstance(locals()["agent_rec"], dict):
                metadata["agent"] = locals()["agent_rec"].get("name")
                metadata["agent_id"] = locals()["agent_rec"].get("id")
            if include_trace:
                metadata["trace"] = _trace_payload_for_trace_id(trace_id)
            return JSONResponse(
                status_code=exc.status_code,
                content={"detail": exc.detail, "metadata": metadata},
                headers=headers,
            )
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            metadata: dict[str, Any] = {"trace_id": trace_id}
            if "agent_rec" in locals() and isinstance(locals()["agent_rec"], dict):
                metadata["agent"] = locals()["agent_rec"].get("name")
                metadata["agent_id"] = locals()["agent_rec"].get("id")
            if include_trace:
                metadata["trace"] = _trace_payload_for_trace_id(trace_id)
            return JSONResponse(
                status_code=500,
                content={"detail": f"Invocation failed: {exc}", "metadata": metadata},
                headers={TRACE_ID_HEADER: trace_id},
            )
        finally:
            _CURRENT_LOG_TRACE_ID.reset(trace_token)
            _CURRENT_LOG_AGENT_ID.reset(agent_token)
    finally:
        if reserved_trace:
            _release_trace_id(trace_id)


@app.exception_handler(Exception)
async def unhandled_exception_handler(_request: Request, exc: Exception):
    traceback.print_exc()
    return JSONResponse(status_code=500, content={"error": str(exc)})


if __name__ == "__main__":
    port = int(os.getenv("PORT", "3000"))
    access_log = os.getenv("UVICORN_ACCESS_LOG", "false").lower() in {"1", "true", "yes", "on"}
    log_level = os.getenv("UVICORN_LOG_LEVEL", "warning")
    try:
        uvicorn.run(app, host="0.0.0.0", port=port, access_log=access_log, log_level=log_level)
    except KeyboardInterrupt:
        pass
