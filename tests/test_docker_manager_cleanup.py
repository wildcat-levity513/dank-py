from __future__ import annotations

from pathlib import Path

from dank_py.lib.constants import DANK_BUILD_DIR
from dank_py.lib.docker.manager import DockerManager


def test_cleanup_build_context_removes_empty_build_root(tmp_path: Path) -> None:
    manager = DockerManager()
    build_root = tmp_path / DANK_BUILD_DIR
    context = build_root / "build-context-demo"
    context.mkdir(parents=True, exist_ok=True)
    (context / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")

    manager._cleanup_build_context(context)

    assert not context.exists()
    assert not build_root.exists()


def test_cleanup_build_context_keeps_build_root_if_other_context_exists(tmp_path: Path) -> None:
    manager = DockerManager()
    build_root = tmp_path / DANK_BUILD_DIR
    context_a = build_root / "build-context-a"
    context_b = build_root / "build-context-b"
    context_a.mkdir(parents=True, exist_ok=True)
    context_b.mkdir(parents=True, exist_ok=True)

    manager._cleanup_build_context(context_a)

    assert not context_a.exists()
    assert context_b.exists()
    assert build_root.exists()
