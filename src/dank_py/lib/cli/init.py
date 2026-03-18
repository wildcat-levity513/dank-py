"""`dank init` command."""

from __future__ import annotations

import json
from importlib import resources
from pathlib import Path

from dank_py.lib.constants import DEFAULT_CONFIG_FILE


class InitError(RuntimeError):
    """Raised when project scaffolding fails."""


def _write_file(path: Path, content: str, force: bool) -> None:
    if path.exists() and not force:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def init_command(name: str | None, force: bool = False) -> Path:
    target_dir = Path(name).expanduser().resolve() if name else Path.cwd().resolve()
    target_dir.mkdir(parents=True, exist_ok=True)

    template = resources.files("dank_py.templates").joinpath("dank.config.json").read_text(encoding="utf-8")
    config_payload = json.loads(template)
    if not config_payload.get("name"):
        config_payload["name"] = target_dir.name

    config_path = target_dir / DEFAULT_CONFIG_FILE
    _write_file(config_path, json.dumps(config_payload, indent=2) + "\n", force=force)

    _write_file(
        target_dir / ".dankignore",
        """# Files excluded from dank build contexts
.venv/
venv/
.env
.env.*
__pycache__/
.pytest_cache/
.mypy_cache/
.git/
.dank-py/
.DS_Store
""",
        force=force,
    )

    return target_dir
