"""Runtime-only constants used inside agent containers."""

from __future__ import annotations

RUNTIME_INDEX_PATH = "/app/agent-code/index.py"
DEFAULT_INDEX_PATH = "/app/default_index.py"

AUTO_METHOD_CANDIDATES = ("invoke", "kickoff", "run", "__call__")

