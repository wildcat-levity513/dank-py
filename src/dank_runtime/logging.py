"""Runtime logging primitives for dank-py containers."""

from __future__ import annotations

import os
import sys
import threading
import time
from collections import deque
from contextvars import ContextVar
from typing import Any, TextIO


def _int_env(name: str, default: int) -> int:
    raw = str(os.getenv(name, "")).strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


DEFAULT_LOG_BUFFER_MAX_SIZE = _int_env("DANK_LOG_BUFFER_MAX_SIZE", 10000)
DEFAULT_LOG_BUFFER_MAX_AGE_MS = _int_env("DANK_LOG_BUFFER_MAX_AGE_MS", 24 * 60 * 60 * 1000)
DEFAULT_LOG_BUFFER_MAX_BYTES = _int_env("DANK_LOG_BUFFER_MAX_BYTES", 16 * 1024 * 1024)


class LogBufferService:
    """In-memory ring buffer for runtime logs."""

    def __init__(
        self,
        *,
        max_size: int = DEFAULT_LOG_BUFFER_MAX_SIZE,
        max_age_ms: int = DEFAULT_LOG_BUFFER_MAX_AGE_MS,
        max_bytes: int = DEFAULT_LOG_BUFFER_MAX_BYTES,
    ):
        self.max_size = max(1, int(max_size))
        self.max_age = max(1000, int(max_age_ms))
        self.max_bytes = max(1024, int(max_bytes))
        self._logs: deque[tuple[dict[str, Any], int]] = deque()
        self._seq = 0
        self._capture_count = 0
        self._total_bytes = 0
        self._lock = threading.Lock()

    def _entry_size(self, entry: dict[str, Any]) -> int:
        stream = str(entry.get("stream") or "")
        message = str(entry.get("message") or "")
        scope = str(entry.get("scope") or "")
        agent_id = str(entry.get("agent_id") or "")
        trace_id = str(entry.get("trace_id") or "")
        # Approximate entry memory footprint (payload + object overhead).
        return len(stream) + len(message) + len(scope) + len(agent_id) + len(trace_id) + 96

    def _evict_oldest_locked(self) -> None:
        if not self._logs:
            return
        _, size = self._logs.popleft()
        self._total_bytes = max(0, self._total_bytes - int(size))

    def add_log(
        self,
        stream: str,
        message: str,
        *,
        agent_id: str | None = None,
        trace_id: str | None = None,
        scope: str | None = None,
        timestamp_ms: int | None = None,
    ) -> dict[str, Any] | None:
        text = str(message).strip()
        if not text:
            return None

        now_ms = int(timestamp_ms if timestamp_ms is not None else time.time() * 1000)
        final_scope = scope or ("agent" if agent_id else "container")
        with self._lock:
            self._seq += 1
            entry = {
                "seq": self._seq,
                "timestamp": now_ms,
                "stream": str(stream or "stdout"),
                "message": text,
                "scope": final_scope,
                "agent_id": str(agent_id) if agent_id else None,
                "trace_id": str(trace_id) if trace_id else None,
            }
            entry_size = self._entry_size(entry)
            self._logs.append((entry, entry_size))
            self._total_bytes += entry_size
            self._capture_count += 1
            while len(self._logs) > self.max_size:
                self._evict_oldest_locked()
            while self._total_bytes > self.max_bytes and self._logs:
                self._evict_oldest_locked()
            if self._capture_count % 100 == 0:
                self._cleanup_locked(now_ms=now_ms)
            return dict(entry)

    def cleanup(self) -> None:
        with self._lock:
            self._cleanup_locked(now_ms=int(time.time() * 1000))

    def _cleanup_locked(self, *, now_ms: int) -> None:
        cutoff = int(now_ms - self.max_age)
        while self._logs and int(self._logs[0][0].get("timestamp", 0)) < cutoff:
            self._evict_oldest_locked()
        while len(self._logs) > self.max_size:
            self._evict_oldest_locked()
        while self._total_bytes > self.max_bytes and self._logs:
            self._evict_oldest_locked()

    def _snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            now_ms = int(time.time() * 1000)
            self._cleanup_locked(now_ms=now_ms)
            return [dict(item) for item, _ in self._logs]

    def get_logs(
        self,
        *,
        start_time: int | None = None,
        end_time: int | None = None,
        limit: int = 100,
        offset: int = 0,
        stream: str | None = None,
        agent_id: str | None = None,
        trace_id: str | None = None,
    ) -> dict[str, Any]:
        logs = self._snapshot()
        filtered = logs

        if start_time is not None:
            filtered = [entry for entry in filtered if int(entry.get("timestamp", 0)) >= int(start_time)]
        if end_time is not None:
            filtered = [entry for entry in filtered if int(entry.get("timestamp", 0)) <= int(end_time)]
        if stream:
            stream_name = str(stream).strip()
            filtered = [entry for entry in filtered if str(entry.get("stream")) == stream_name]
        if agent_id:
            wanted = str(agent_id).strip()
            filtered = [entry for entry in filtered if str(entry.get("agent_id") or "") == wanted]
        if trace_id:
            wanted_trace = str(trace_id).strip()
            filtered = [entry for entry in filtered if str(entry.get("trace_id") or "") == wanted_trace]

        filtered.sort(key=lambda item: int(item.get("timestamp", 0)))
        total = len(filtered)
        page_limit = max(1, int(limit))
        page_offset = max(0, int(offset))
        paginated = filtered[page_offset : page_offset + page_limit]

        return {
            "logs": paginated,
            "total": total,
            "limit": page_limit,
            "offset": page_offset,
            "hasMore": page_offset + page_limit < total,
        }

    def get_logs_since(
        self,
        *,
        since_seq: int,
        stream: str | None = None,
        agent_id: str | None = None,
        trace_id: str | None = None,
    ) -> list[dict[str, Any]]:
        logs = self._snapshot()
        filtered = [entry for entry in logs if int(entry.get("seq", 0)) > int(since_seq)]
        if stream:
            stream_name = str(stream).strip()
            filtered = [entry for entry in filtered if str(entry.get("stream")) == stream_name]
        if agent_id:
            wanted = str(agent_id).strip()
            filtered = [entry for entry in filtered if str(entry.get("agent_id") or "") == wanted]
        if trace_id:
            wanted_trace = str(trace_id).strip()
            filtered = [entry for entry in filtered if str(entry.get("trace_id") or "") == wanted_trace]
        filtered.sort(key=lambda item: int(item.get("seq", 0)))
        return filtered

    def get_stats(self, *, agent_id: str | None = None, trace_id: str | None = None) -> dict[str, Any]:
        if agent_id or trace_id:
            payload = self.get_logs(limit=self.max_size, offset=0, agent_id=agent_id, trace_id=trace_id)
            logs = list(payload.get("logs", []))
            total = int(payload.get("total", len(logs)))
        else:
            logs = self._snapshot()
            total = len(logs)
        logs.sort(key=lambda item: int(item.get("timestamp", 0)))
        buffer_bytes = sum(self._entry_size(item) for item in logs)
        return {
            "total": total,
            "oldest": logs[0].get("timestamp") if logs else None,
            "newest": logs[-1].get("timestamp") if logs else None,
            "stdout": len([item for item in logs if item.get("stream") == "stdout"]),
            "stderr": len([item for item in logs if item.get("stream") == "stderr"]),
            "bufferSize": len(logs),
            "bufferBytes": int(buffer_bytes),
            "maxSize": self.max_size,
            "maxAge": self.max_age,
            "maxBytes": self.max_bytes,
        }

    def latest_seq(self) -> int:
        with self._lock:
            if not self._logs:
                return 0
            return int(self._logs[-1][0].get("seq", 0))


class _CapturedStream:
    def __init__(
        self,
        *,
        name: str,
        wrapped: TextIO,
        log_buffer: LogBufferService,
        agent_context: ContextVar[str | None],
        trace_context: ContextVar[str | None],
    ):
        self._name = name
        self._wrapped = wrapped
        self._buffer = log_buffer
        self._agent_context = agent_context
        self._trace_context = trace_context

    def write(self, data: Any) -> int:
        if isinstance(data, bytes):
            text = data.decode("utf-8", errors="replace")
        else:
            text = str(data)
        agent_id = self._agent_context.get(None)
        trace_id = self._trace_context.get(None)
        self._buffer.add_log(
            self._name,
            text,
            agent_id=agent_id,
            trace_id=trace_id,
            scope="agent" if agent_id else "container",
        )
        return self._wrapped.write(data)

    def flush(self) -> None:
        self._wrapped.flush()

    def isatty(self) -> bool:
        try:
            return bool(self._wrapped.isatty())
        except Exception:  # noqa: BLE001
            return False

    def __getattr__(self, item: str) -> Any:
        return getattr(self._wrapped, item)


class StreamCaptureService:
    """Captures stdout/stderr while keeping normal stream output."""

    def __init__(
        self,
        *,
        log_buffer: LogBufferService,
        agent_context: ContextVar[str | None],
        trace_context: ContextVar[str | None],
    ):
        self._buffer = log_buffer
        self._agent_context = agent_context
        self._trace_context = trace_context
        self._started = False
        self._original_stdout: TextIO | None = None
        self._original_stderr: TextIO | None = None

    def start(self) -> None:
        if self._started:
            return
        self._original_stdout = sys.stdout
        self._original_stderr = sys.stderr
        sys.stdout = _CapturedStream(
            name="stdout",
            wrapped=self._original_stdout,
            log_buffer=self._buffer,
            agent_context=self._agent_context,
            trace_context=self._trace_context,
        )
        sys.stderr = _CapturedStream(
            name="stderr",
            wrapped=self._original_stderr,
            log_buffer=self._buffer,
            agent_context=self._agent_context,
            trace_context=self._trace_context,
        )
        self._started = True

    def stop(self) -> None:
        if not self._started:
            return
        if self._original_stdout is not None:
            sys.stdout = self._original_stdout
        if self._original_stderr is not None:
            sys.stderr = self._original_stderr
        self._started = False
