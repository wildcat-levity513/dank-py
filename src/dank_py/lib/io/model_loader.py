"""Import helpers for dynamic model and symbol loading."""

from __future__ import annotations

import importlib
import sys
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType
from typing import Iterator, Any


def parse_import_path(path: str) -> tuple[str, str]:
    if ":" not in path:
        raise ValueError(f"Invalid import path '{path}'. Expected format module:Symbol")
    module_name, symbol_name = path.split(":", 1)
    module_name = module_name.strip()
    symbol_name = symbol_name.strip()
    if not module_name or not symbol_name:
        raise ValueError(f"Invalid import path '{path}'. Expected format module:Symbol")
    return module_name, symbol_name


@contextmanager
def _temp_sys_path(path: Path | None) -> Iterator[None]:
    if path is None:
        yield
        return

    str_path = str(path)
    inserted = False
    if str_path not in sys.path:
        sys.path.insert(0, str_path)
        inserted = True

    try:
        yield
    finally:
        if inserted:
            try:
                sys.path.remove(str_path)
            except ValueError:
                pass


def import_module(module_name: str, project_root: Path | None = None) -> ModuleType:
    with _temp_sys_path(project_root):
        return importlib.import_module(module_name)


def load_symbol(import_path: str, project_root: Path | None = None) -> Any:
    module_name, symbol_name = parse_import_path(import_path)
    module = import_module(module_name, project_root=project_root)
    if not hasattr(module, symbol_name):
        raise AttributeError(f"Module '{module_name}' has no symbol '{symbol_name}'")
    symbol = getattr(module, symbol_name)
    rebuild = getattr(symbol, "model_rebuild", None)
    if callable(rebuild):
        try:
            rebuild(force=True)
        except TypeError:
            rebuild()
    return symbol
