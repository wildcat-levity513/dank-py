"""Shared constants for dank-py."""

from __future__ import annotations

import os

DEFAULT_CONFIG_FILE = "dank.config.json"
# Canonical base image for production users. Override for local/dev with:
# DANK_PY_BASE_IMAGE=<your-image:tag>
DEFAULT_BASE_IMAGE_TAG = os.getenv("DANK_PY_BASE_IMAGE_TAG", "v1.0.0")
DEFAULT_BASE_IMAGE = os.getenv("DANK_PY_BASE_IMAGE", f"deltadarkly/dank-py-base:{DEFAULT_BASE_IMAGE_TAG}")
DEFAULT_PORT = 3000
DEFAULT_IMAGE_TAG_SUFFIX = "latest"
DEFAULT_LOCK_PYTHON_VERSION = os.getenv("DANK_PY_LOCK_PYTHON_VERSION", "3.12")

AGENT_CODE_DIR = "/app/agent-code"
RUNTIME_INDEX_PATH = "/app/agent-code/index.py"
DEFAULT_INDEX_PATH = "/app/default_index.py"

DANK_BUILD_DIR = ".dank-py"
BUILD_CONTEXT_PREFIX = "build-context"

DEFAULT_IGNORE_PATTERNS = {
    ".git",
    ".venv",
    "venv",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".idea",
    ".vscode",
    ".DS_Store",
    "node_modules",
    DANK_BUILD_DIR,
}

CALL_TYPE_OPTIONS = {"auto", "callable", "method"}
CALL_STYLE_OPTIONS = {"auto", "single_arg", "kwargs"}

AUTO_METHOD_CANDIDATES = ("invoke", "kickoff", "run", "__call__")
