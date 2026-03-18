from __future__ import annotations

import time

from dank_runtime.logging import LogBufferService


def test_log_buffer_enforces_max_size() -> None:
    buffer = LogBufferService(max_size=3, max_age_ms=60_000)
    base = int(time.time() * 1000)
    buffer.add_log("stdout", "a", timestamp_ms=base + 1)
    buffer.add_log("stdout", "b", timestamp_ms=base + 2)
    buffer.add_log("stdout", "c", timestamp_ms=base + 3)
    buffer.add_log("stdout", "d", timestamp_ms=base + 4)

    payload = buffer.get_logs(limit=10, offset=0)
    messages = [entry["message"] for entry in payload["logs"]]
    assert messages == ["b", "c", "d"]
    assert payload["total"] == 3


def test_log_buffer_time_stream_agent_filters() -> None:
    buffer = LogBufferService(max_size=10, max_age_ms=60_000)
    base = int(time.time() * 1000)
    buffer.add_log("stdout", "container-1", timestamp_ms=base + 1_000)
    buffer.add_log("stderr", "agent-a-1", agent_id="agent-a", timestamp_ms=base + 2_000)
    buffer.add_log("stdout", "agent-b-1", agent_id="agent-b", timestamp_ms=base + 3_000)
    buffer.add_log("stderr", "agent-a-2", agent_id="agent-a", timestamp_ms=base + 4_000)

    filtered = buffer.get_logs(
        start_time=base + 1_500,
        end_time=base + 4_000,
        stream="stderr",
        agent_id="agent-a",
        limit=10,
        offset=0,
    )
    assert filtered["total"] == 2
    assert [entry["message"] for entry in filtered["logs"]] == ["agent-a-1", "agent-a-2"]


def test_log_buffer_pagination_and_since() -> None:
    buffer = LogBufferService(max_size=20, max_age_ms=60_000)
    base = int(time.time() * 1000)
    for idx in range(1, 8):
        buffer.add_log("stdout", f"log-{idx}", timestamp_ms=base + (idx * 100))

    page = buffer.get_logs(limit=3, offset=2)
    assert page["total"] == 7
    assert page["hasMore"] is True
    assert [entry["message"] for entry in page["logs"]] == ["log-3", "log-4", "log-5"]

    latest_seen = int(page["logs"][-1]["seq"])
    delta = buffer.get_logs_since(since_seq=latest_seen)
    assert [entry["message"] for entry in delta] == ["log-6", "log-7"]


def test_log_buffer_stats() -> None:
    buffer = LogBufferService(max_size=10, max_age_ms=60_000)
    base = int(time.time() * 1000)
    buffer.add_log("stdout", "container", timestamp_ms=base + 1_000)
    buffer.add_log("stderr", "agent-a", agent_id="agent-a", timestamp_ms=base + 2_000)
    buffer.add_log("stdout", "agent-a-2", agent_id="agent-a", timestamp_ms=base + 3_000)

    all_stats = buffer.get_stats()
    assert all_stats["total"] == 3
    assert all_stats["stdout"] == 2
    assert all_stats["stderr"] == 1
    assert all_stats["oldest"] == base + 1_000
    assert all_stats["newest"] == base + 3_000
    assert all_stats["maxSize"] == 10
    assert all_stats["bufferBytes"] > 0

    agent_stats = buffer.get_stats(agent_id="agent-a")
    assert agent_stats["total"] == 2
    assert agent_stats["stdout"] == 1
    assert agent_stats["stderr"] == 1
    assert agent_stats["bufferBytes"] > 0


def test_log_buffer_trace_filtering() -> None:
    buffer = LogBufferService(max_size=20, max_age_ms=60_000, max_bytes=32_000)
    base = int(time.time() * 1000)
    buffer.add_log("stdout", "trace-1-start", agent_id="agent-a", trace_id="trc_1", timestamp_ms=base + 1_000)
    buffer.add_log("stdout", "trace-2-start", agent_id="agent-a", trace_id="trc_2", timestamp_ms=base + 2_000)
    buffer.add_log("stderr", "trace-1-end", agent_id="agent-a", trace_id="trc_1", timestamp_ms=base + 3_000)

    filtered = buffer.get_logs(trace_id="trc_1", limit=10, offset=0)
    assert filtered["total"] == 2
    assert [entry["message"] for entry in filtered["logs"]] == ["trace-1-start", "trace-1-end"]

    trace_stats = buffer.get_stats(trace_id="trc_1")
    assert trace_stats["total"] == 2
    assert trace_stats["maxBytes"] == 32_000


def test_log_buffer_enforces_max_bytes() -> None:
    buffer = LogBufferService(max_size=100, max_age_ms=60_000, max_bytes=1024)
    base = int(time.time() * 1000)
    buffer.add_log("stdout", "x" * 700, timestamp_ms=base + 1_000)
    buffer.add_log("stdout", "y" * 700, timestamp_ms=base + 2_000)

    payload = buffer.get_logs(limit=10, offset=0)
    assert payload["total"] == 1
    assert payload["logs"][0]["message"] == "y" * 700
