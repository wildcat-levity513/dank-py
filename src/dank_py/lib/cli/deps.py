"""`dank deps` command."""

from __future__ import annotations

import ast
import asyncio
import importlib
from importlib import metadata as importlib_metadata
import importlib.util
import inspect
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dank_py.lib.config.loader import load_config
from dank_py.lib.constants import DEFAULT_LOCK_PYTHON_VERSION
from dank_py.lib.io.model_loader import load_symbol
from dank_runtime.engine import invoke as invoke_target
from dank_runtime.engine import resolve_callable


class DepsError(RuntimeError):
    """Raised when dependency preparation fails."""


class FullValidationFailure(DepsError):
    """Raised when isolated full validation fails with structured details."""

    def __init__(
        self,
        message: str,
        *,
        missing_modules: list[str] | None = None,
        failed_agents: list[str] | None = None,
    ) -> None:
        super().__init__(message)
        self.missing_modules = missing_modules or []
        self.failed_agents = failed_agents or []


@dataclass(slots=True)
class DepsResult:
    lock_path: Path
    validation_mode: str
    validated_agents: int


@dataclass(slots=True)
class ValidationReport:
    mode: str
    validated_agents: int


IMPORT_TO_PACKAGE_MAP = {
    "yaml": "PyYAML",
    "dotenv": "python-dotenv",
    "serpapi": "google-search-results",
    "bs4": "beautifulsoup4",
    "cv2": "opencv-python",
    "PIL": "Pillow",
    "sklearn": "scikit-learn",
    "langchain_openai": "langchain-openai",
    "langchain_community": "langchain-community",
    "langchain_core": "langchain-core",
    "llama_index": "llama-index",
    "pydantic_ai": "pydantic-ai",
    "crewai": "crewai",
    "pkg_resources": "setuptools<81",
}

SYMBOL_TO_EXTRA_PACKAGE_MAP = {
    "SerpAPIWrapper": "google-search-results",
}

IGNORE_DIRS = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".dank-py",
}

def _run(cmd: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, check=False)


def _python_minor_version(executable: str, *, cwd: Path) -> str | None:
    try:
        result = subprocess.run(
            [str(executable), "-c", "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            check=False,
        )
    except Exception:  # noqa: BLE001
        return None
    if result.returncode != 0:
        return None
    return (result.stdout or "").strip() or None


def _resolve_python_interpreter(target_version: str, *, project_root: Path) -> tuple[str, str]:
    host_exe = sys.executable
    host_ver = _python_minor_version(host_exe, cwd=project_root) or f"{sys.version_info.major}.{sys.version_info.minor}"
    if host_ver == target_version:
        return host_exe, host_ver

    candidates = [f"python{target_version}"]
    candidates.extend(
        [
            f"/opt/homebrew/opt/python@{target_version}/bin/python{target_version}",
            f"/usr/local/opt/python@{target_version}/bin/python{target_version}",
            f"/Library/Frameworks/Python.framework/Versions/{target_version}/bin/python{target_version}",
        ]
    )
    candidates.append(host_exe)

    seen: set[str] = set()
    for candidate in candidates:
        if not candidate:
            continue
        resolved = candidate if os.path.isabs(candidate) else (shutil.which(candidate) or candidate)
        if resolved in seen:
            continue
        seen.add(resolved)
        version = _python_minor_version(resolved, cwd=project_root)
        if version == target_version:
            return resolved, version
    return host_exe, host_ver


def _is_pinned_requirements(content: str) -> bool:
    lines = [line.strip() for line in content.splitlines() if line.strip() and not line.strip().startswith("#")]
    if not lines:
        return False

    pinned = 0
    tracked = 0
    for line in lines:
        if line.startswith(("-", "--")):
            continue
        tracked += 1
        if "==" in line or " @ " in line or line.startswith(("git+", "http://", "https://")):
            pinned += 1

    return tracked > 0 and pinned == tracked


def _has_requirement_entries(content: str) -> bool:
    for raw in content.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith(("-", "--")):
            continue
        return True
    return False


def _clean_lock_lines(text: str, *, include_comments: bool = False) -> str:
    cleaned: list[str] = []
    seen: set[str] = set()
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("-e "):
            continue
        if line.startswith((".", "/")):
            continue
        if line.startswith("#"):
            if include_comments:
                cleaned.append(line)
            continue
        if line in seen:
            continue
        seen.add(line)
        cleaned.append(line)

    if include_comments:
        return "\n".join(cleaned) + ("\n" if cleaned else "")

    pinned_only = sorted([line for line in cleaned if not line.startswith("#")])
    return "\n".join(pinned_only) + ("\n" if pinned_only else "")


def _generate_lock_from_freeze(
    project_root: Path,
    lock_path: Path,
    *,
    include_comments: bool,
    python_executable: str,
) -> Path:
    result = _run([python_executable, "-m", "pip", "freeze"], cwd=project_root)
    if result.returncode != 0:
        raise DepsError(f"pip freeze failed: {result.stderr.strip() or result.stdout.strip()}")

    lock_path.write_text(_clean_lock_lines(result.stdout, include_comments=include_comments), encoding="utf-8")
    return lock_path


def _piptools_available(project_root: Path, *, python_executable: str) -> bool:
    importlib.invalidate_caches()
    result = _run(
        [
            python_executable,
            "-c",
            "import importlib.util,sys; sys.exit(0 if importlib.util.find_spec('piptools') else 1)",
        ],
        cwd=project_root,
    )
    return result.returncode == 0


def _install_piptools(project_root: Path, *, python_executable: str) -> None:
    print(f"Deps: installing pip-tools for lock interpreter ({python_executable}) ...")
    result = _run([python_executable, "-m", "pip", "install", "pip-tools"], cwd=project_root)
    if result.returncode != 0:
        raise DepsError(f"Failed to install pip-tools: {result.stderr.strip() or result.stdout.strip()}")
    print("Deps: pip-tools installation completed.")


def _ensure_piptools(
    project_root: Path,
    *,
    python_executable: str,
    install_tools: bool,
    prompt_install_tools: bool,
) -> bool:
    if _piptools_available(project_root, python_executable=python_executable):
        return True

    should_install = install_tools
    if not should_install and prompt_install_tools and sys.stdin.isatty() and sys.stdout.isatty():
        answer = (
            input(f"pip-tools is not installed for {python_executable}. Install it now? [y/N]: ")
            .strip()
            .lower()
        )
        should_install = answer in {"y", "yes"}

    if should_install:
        try:
            _install_piptools(project_root, python_executable=python_executable)
            return _piptools_available(project_root, python_executable=python_executable)
        except DepsError as exc:
            detail = str(exc).lower()
            if "externally-managed-environment" in detail or "externally managed" in detail:
                raise DepsError(
                    "Target interpreter is externally managed (PEP 668). "
                    "Use a project virtualenv with the target Python version and install pip-tools there. "
                    f"Example:\n"
                    f"  {python_executable} -m venv .venv\n"
                    "  source .venv/bin/activate\n"
                    "  python -m pip install pip-tools"
                )
            raise

    return False


def _compile_lock_with_piptools(
    project_root: Path,
    requirements_path: Path,
    lock_path: Path,
    *,
    python_executable: str,
    install_tools: bool,
    prompt_install_tools: bool,
    lock_python_version: str,
    include_comments: bool,
) -> Path:
    if not _ensure_piptools(
        project_root,
        python_executable=python_executable,
        install_tools=install_tools,
        prompt_install_tools=prompt_install_tools,
    ):
        raise DepsError(
            "pip-tools is required for resolver-based locking. Install it with "
            f"`{python_executable} -m pip install pip-tools`, rerun with --install-tools, "
            "or use --fallback-freeze."
        )

    del lock_python_version  # pip-tools version pin flag is not used for compatibility.
    cmd = [
        python_executable,
        "-m",
        "piptools",
        "compile",
        "--allow-unsafe",
        str(requirements_path),
        "-o",
        str(lock_path),
    ]

    print("Deps: compiling requirements.lock.txt with pip-tools ...")
    result = _run(cmd, cwd=project_root)

    if result.returncode != 0:
        raise DepsError(f"pip-tools compile failed: {result.stderr.strip() or result.stdout.strip()}")

    lock_path.write_text(
        _clean_lock_lines(lock_path.read_text(encoding="utf-8"), include_comments=include_comments),
        encoding="utf-8",
    )
    return lock_path


def _extract_deps_from_pyproject(project_root: Path) -> list[str]:
    pyproject_path = project_root / "pyproject.toml"
    if not pyproject_path.exists():
        return []

    data = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    project_deps = data.get("project", {}).get("dependencies", [])

    poetry_deps_raw = data.get("tool", {}).get("poetry", {}).get("dependencies", {})
    poetry_deps: list[str] = []
    for name, version in poetry_deps_raw.items():
        if str(name).lower() == "python":
            continue
        if isinstance(version, str):
            poetry_deps.append(f"{name}{version if version.startswith(('=', '>', '<', '~', '^')) else ''}")
        elif isinstance(version, dict) and "version" in version and isinstance(version["version"], str):
            poetry_deps.append(f"{name}{version['version']}")
        else:
            poetry_deps.append(str(name))

    merged: list[str] = []
    for dep in [*project_deps, *poetry_deps]:
        entry = str(dep).strip()
        if entry and entry not in merged:
            merged.append(entry)

    return merged


def _export_lock_from_other_lockfiles(project_root: Path, lock_path: Path, *, include_comments: bool) -> Path | None:
    poetry_lock = project_root / "poetry.lock"
    if poetry_lock.exists():
        result = _run(
            ["poetry", "export", "-f", "requirements.txt", "--without-hashes", "-o", str(lock_path)],
            cwd=project_root,
        )
        if result.returncode == 0:
            lock_path.write_text(
                _clean_lock_lines(lock_path.read_text(encoding="utf-8"), include_comments=include_comments),
                encoding="utf-8",
            )
            return lock_path
        raise DepsError(
            "Found poetry.lock but failed to export requirements. "
            "Install Poetry or use --fallback-freeze."
        )

    uv_lock = project_root / "uv.lock"
    if uv_lock.exists():
        result = _run(
            ["uv", "export", "--format", "requirements-txt", "--no-dev", "--output-file", str(lock_path)],
            cwd=project_root,
        )
        if result.returncode == 0:
            lock_path.write_text(
                _clean_lock_lines(lock_path.read_text(encoding="utf-8"), include_comments=include_comments),
                encoding="utf-8",
            )
            return lock_path
        raise DepsError("Found uv.lock but failed to export requirements. Install uv or use --fallback-freeze.")

    pipfile_lock = project_root / "Pipfile.lock"
    if pipfile_lock.exists():
        result = _run(["pipenv", "lock", "-r"], cwd=project_root)
        if result.returncode == 0:
            lock_path.write_text(
                _clean_lock_lines(result.stdout, include_comments=include_comments),
                encoding="utf-8",
            )
            return lock_path
        raise DepsError(
            "Found Pipfile.lock but failed to export requirements. Install pipenv or use --fallback-freeze."
        )

    return None


def _iter_python_files(root: Path):
    for path in root.rglob("*.py"):
        if any(part in IGNORE_DIRS for part in path.parts):
            continue
        yield path


def _collect_local_modules(root: Path) -> set[str]:
    local: set[str] = set()
    for py in root.glob("*.py"):
        if py.name == "__init__.py":
            continue
        local.add(py.stem)
    for init_file in root.rglob("__init__.py"):
        if any(part in IGNORE_DIRS for part in init_file.parts):
            continue
        local.add(init_file.parent.name)
    return local


def _normalize_import_to_package(import_name: str) -> str:
    if import_name in IMPORT_TO_PACKAGE_MAP:
        return IMPORT_TO_PACKAGE_MAP[import_name]
    if "_" in import_name:
        return import_name.replace("_", "-")
    return import_name


def _map_missing_module_to_package(module_name: str) -> str | None:
    """Map module import names to installable package names with high confidence only."""
    top = (module_name or "").strip().split(".", 1)[0]
    if not top:
        return None
    if top in IMPORT_TO_PACKAGE_MAP:
        return IMPORT_TO_PACKAGE_MAP[top]
    # Prefer installed distribution metadata when available.
    try:
        by_package = importlib_metadata.packages_distributions()
        candidates = by_package.get(top) or []
        if candidates:
            return sorted(candidates)[0]
    except Exception:  # noqa: BLE001
        pass
    # High-confidence fallback only when module/package names match directly.
    if re.fullmatch(r"[A-Za-z0-9]+", top):
        return top
    return None


def _append_requirements(project_root: Path, package_specs: list[str]) -> list[str]:
    req_path = project_root / "requirements.txt"
    existing_lines = []
    if req_path.exists():
        existing_lines = [line.strip() for line in req_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    existing_set = set(existing_lines)

    to_add: list[str] = []
    for spec in package_specs:
        candidate = spec.strip()
        if not candidate or candidate in existing_set:
            continue
        to_add.append(candidate)
        existing_set.add(candidate)

    if not to_add:
        return []

    merged = [*existing_lines, *to_add]
    req_path.write_text("\n".join(merged) + "\n", encoding="utf-8")
    return to_add


def _relock_from_requirements(
    project_root: Path,
    *,
    lock_python_executable: str,
    fallback_freeze: bool,
    install_tools: bool,
    prompt_install_tools: bool,
    lock_python_version: str,
    include_comments: bool,
) -> Path:
    req_path = project_root / "requirements.txt"
    if not req_path.exists():
        raise DepsError("Cannot relock dependencies: requirements.txt not found.")

    lock_path = project_root / "requirements.lock.txt"
    if lock_path.exists():
        lock_path.unlink()

    try:
        return _compile_lock_with_piptools(
            project_root,
            req_path,
            lock_path,
            python_executable=lock_python_executable,
            install_tools=install_tools,
            prompt_install_tools=prompt_install_tools,
            lock_python_version=lock_python_version,
            include_comments=include_comments,
        )
    except DepsError:
        if fallback_freeze:
            return _generate_lock_from_freeze(
                project_root,
                lock_path,
                include_comments=include_comments,
                python_executable=lock_python_executable,
            )
        raise


def _discover_requirements_from_imports(project_root: Path, requirements_path: Path) -> bool:
    stdlib = set(getattr(sys, "stdlib_module_names", set()))
    local_modules = _collect_local_modules(project_root)
    imported: set[str] = set()
    inferred_extras: set[str] = set()

    for file_path in _iter_python_files(project_root):
        try:
            tree = ast.parse(file_path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    top = alias.name.split(".", 1)[0]
                    if top:
                        imported.add(top)
            elif isinstance(node, ast.ImportFrom):
                if node.level and node.level > 0:
                    continue
                if node.module:
                    top = node.module.split(".", 1)[0]
                    if top:
                        imported.add(top)
                    if node.module.startswith("langchain_community"):
                        for alias in node.names:
                            extra = SYMBOL_TO_EXTRA_PACKAGE_MAP.get(alias.name)
                            if extra:
                                inferred_extras.add(extra)

    packages: list[str] = []
    for name in sorted(imported):
        if name in stdlib:
            continue
        if name in local_modules:
            continue
        if name.startswith("_"):
            continue
        if name in {"dank_py", "dank_runtime"}:
            continue
        package_name = _normalize_import_to_package(name)
        if package_name not in packages:
            packages.append(package_name)

    for package_name in sorted(inferred_extras):
        if package_name not in packages:
            packages.append(package_name)

    if not packages:
        return False

    if requirements_path.exists():
        existing_lines = requirements_path.read_text(encoding="utf-8").splitlines()
        existing_clean = {line.strip() for line in existing_lines if line.strip() and not line.strip().startswith("#")}
        to_add = [pkg for pkg in packages if pkg not in existing_clean]
        if not to_add:
            return False
        merged = [*existing_lines]
        if merged and merged[-1].strip():
            merged.append("")
        merged.extend(to_add)
        requirements_path.write_text("\n".join(merged).rstrip() + "\n", encoding="utf-8")
        return True

    requirements_path.write_text("\n".join(packages) + "\n", encoding="utf-8")
    return True


def _ensure_lock_file(
    project_root: Path,
    *,
    lock_python_executable: str,
    refresh_lock: bool,
    fallback_freeze: bool,
    discover_imports: bool,
    install_tools: bool,
    prompt_install_tools: bool,
    lock_python_version: str,
    include_comments: bool,
) -> Path:
    lock_path = project_root / "requirements.lock.txt"
    req_path = project_root / "requirements.txt"
    had_existing_lock = lock_path.exists()

    if had_existing_lock and not refresh_lock:
        print("Deps: reusing existing requirements.lock.txt (--no-refresh-lock).")
        lock_path.write_text(
            _clean_lock_lines(lock_path.read_text(encoding="utf-8"), include_comments=include_comments),
            encoding="utf-8",
        )
        return lock_path

    exported = _export_lock_from_other_lockfiles(project_root, lock_path, include_comments=include_comments)
    if exported is not None:
        print("Deps: exported dependency lock from existing lockfile metadata.")
        return exported

    if req_path.exists():
        if discover_imports and refresh_lock:
            print("Deps: scanning project imports to enrich requirements.txt ...")
            _discover_requirements_from_imports(project_root, req_path)
        req_content = req_path.read_text(encoding="utf-8")
        if not _has_requirement_entries(req_content):
            lock_path.write_text("", encoding="utf-8")
            print("Deps: requirements.txt has no third-party entries; using empty requirements.lock.txt.")
            return lock_path
        if _is_pinned_requirements(req_content):
            print("Deps: requirements.txt is already pinned; using it as lock baseline.")
            lock_path.write_text(
                _clean_lock_lines(req_content, include_comments=include_comments),
                encoding="utf-8",
            )
            return lock_path

        try:
            return _compile_lock_with_piptools(
                project_root,
                req_path,
                lock_path,
                python_executable=lock_python_executable,
                install_tools=install_tools,
                prompt_install_tools=prompt_install_tools,
                lock_python_version=lock_python_version,
                include_comments=include_comments,
            )
        except DepsError:
            if fallback_freeze:
                print("Deps: pip-tools compile failed; falling back to pip freeze.")
                return _generate_lock_from_freeze(
                    project_root,
                    lock_path,
                    include_comments=include_comments,
                    python_executable=lock_python_executable,
                )
            raise

    pyproject_deps = _extract_deps_from_pyproject(project_root)
    if pyproject_deps:
        print("Deps: extracted dependencies from pyproject.toml.")
        if not req_path.exists():
            req_path.write_text("\n".join(pyproject_deps) + "\n", encoding="utf-8")
        try:
            return _compile_lock_with_piptools(
                project_root,
                req_path,
                lock_path,
                python_executable=lock_python_executable,
                install_tools=install_tools,
                prompt_install_tools=prompt_install_tools,
                lock_python_version=lock_python_version,
                include_comments=include_comments,
            )
        except DepsError:
            if fallback_freeze:
                print("Deps: pip-tools compile failed; falling back to pip freeze.")
                return _generate_lock_from_freeze(
                    project_root,
                    lock_path,
                    include_comments=include_comments,
                    python_executable=lock_python_executable,
                )
            raise

    if discover_imports:
        print("Deps: requirements not found; inferring dependencies from imports.")
        discovered = _discover_requirements_from_imports(project_root, req_path)
        if discovered:
            try:
                return _compile_lock_with_piptools(
                    project_root,
                    req_path,
                    lock_path,
                    python_executable=lock_python_executable,
                    install_tools=install_tools,
                    prompt_install_tools=prompt_install_tools,
                    lock_python_version=lock_python_version,
                    include_comments=include_comments,
                )
            except DepsError:
                if fallback_freeze:
                    print("Deps: pip-tools compile failed; falling back to pip freeze.")
                    return _generate_lock_from_freeze(
                        project_root,
                        lock_path,
                        include_comments=include_comments,
                        python_executable=lock_python_executable,
                    )
                raise

    if fallback_freeze:
        print("Deps: generating lock via pip freeze fallback.")
        return _generate_lock_from_freeze(
            project_root,
            lock_path,
            include_comments=include_comments,
            python_executable=lock_python_executable,
        )

    if not req_path.exists():
        # Support stdlib-only projects: create explicit empty dependency files.
        req_path.write_text("", encoding="utf-8")
        lock_path.write_text("", encoding="utf-8")
        print("Deps: no third-party dependencies detected; created empty requirements.txt and requirements.lock.txt.")
        return lock_path

    if had_existing_lock:
        # No source metadata to regenerate from; keep existing lock as last-resort baseline.
        lock_path.write_text(
            _clean_lock_lines(lock_path.read_text(encoding="utf-8"), include_comments=include_comments),
            encoding="utf-8",
        )
        return lock_path

    raise DepsError(
        "Could not determine dependencies from lockfiles, requirements.txt, pyproject.toml, or import discovery. "
        "Install pip-tools and provide dependency metadata, or rerun with --fallback-freeze."
    )


def _load_module_from_path(path: Path, module_name: str):
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise DepsError(f"Cannot load module from {path}")
    module = importlib.util.module_from_spec(spec)
    original = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = original
    return module


def _sample_payload_from_schema(schema: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(schema, dict):
        return {"prompt": "dank dependency smoke test"}

    props = schema.get("properties", {}) if isinstance(schema.get("properties"), dict) else {}
    required = schema.get("required", []) if isinstance(schema.get("required"), list) else []

    sample: dict[str, Any] = {}

    def sample_for(field_schema: dict[str, Any]) -> Any:
        field_type = field_schema.get("type")
        if field_type == "string":
            return "sample"
        if field_type == "integer":
            return 1
        if field_type == "number":
            return 1.0
        if field_type == "boolean":
            return True
        if field_type == "array":
            return []
        if field_type == "object":
            return {}
        return "sample"

    for key in required:
        field_schema = props.get(key, {}) if isinstance(props.get(key), dict) else {}
        sample[key] = sample_for(field_schema)

    if "prompt" in props and "prompt" not in sample:
        sample["prompt"] = "dank dependency smoke test"

    if not sample:
        sample["prompt"] = "dank dependency smoke test"

    return sample


def _resolve_agent_callable(project_root: Path, entry: dict[str, Any]):
    file_ref = entry.get("file")
    symbol_name = entry.get("symbol")
    if not file_ref or not symbol_name:
        raise DepsError("Agent entry.file and entry.symbol are required for validation")

    file_path = Path(file_ref)
    if not file_path.is_absolute():
        file_path = project_root / file_ref

    if not file_path.exists():
        raise DepsError(f"Configured entry file does not exist: {file_path}")

    module = _load_module_from_path(file_path, f"dank_validation_{abs(hash(file_path))}")
    if not hasattr(module, symbol_name):
        raise DepsError(f"Configured symbol '{symbol_name}' not found in {file_path}")

    symbol = getattr(module, symbol_name)
    if inspect.isclass(symbol):
        try:
            symbol = symbol()
        except TypeError as exc:
            raise DepsError(
                f"Configured class symbol '{symbol_name}' requires constructor arguments"
            ) from exc

    callable_obj = resolve_callable(symbol, method=entry.get("method"), call_type=entry.get("call_type", "auto"))
    return callable_obj


def _validate_model_ref_syntax(model_ref: str) -> None:
    if ":" not in model_ref:
        raise DepsError(f"Invalid model path '{model_ref}', expected module:Symbol")
    module_name, symbol_name = model_ref.split(":", 1)
    if not module_name.strip() or not symbol_name.strip():
        raise DepsError(f"Invalid model path '{model_ref}', expected module:Symbol")


def _parse_ast_file(file_path: Path) -> ast.AST:
    try:
        return ast.parse(file_path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, SyntaxError) as exc:
        raise DepsError(f"Failed to parse Python file '{file_path}': {exc}") from exc


def _find_top_level_symbol(tree: ast.AST, symbol_name: str) -> tuple[str, ast.AST]:
    for node in getattr(tree, "body", []):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == symbol_name:
            return "function", node
        if isinstance(node, ast.ClassDef) and node.name == symbol_name:
            return "class", node
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == symbol_name:
                    return "assign", node
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == symbol_name:
            return "assign", node
    raise DepsError(f"Configured symbol '{symbol_name}' not found")


def _class_def_by_name(tree: ast.AST, class_name: str) -> ast.ClassDef | None:
    for node in getattr(tree, "body", []):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            return node
    return None


def _class_has_method(class_node: ast.ClassDef, method_name: str) -> bool:
    for node in class_node.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == method_name:
            return True
    return False


def _validate_agent_entry_static(project_root: Path, entry: dict[str, Any]) -> None:
    file_ref = entry.get("file")
    symbol_name = entry.get("symbol")
    method_name = entry.get("method")

    if not file_ref or not symbol_name:
        raise DepsError("Agent entry.file and entry.symbol are required for validation")

    file_path = Path(file_ref)
    if not file_path.is_absolute():
        file_path = project_root / file_ref
    if not file_path.exists():
        raise DepsError(f"Configured entry file does not exist: {file_path}")

    tree = _parse_ast_file(file_path)
    kind, symbol_node = _find_top_level_symbol(tree, str(symbol_name))

    if not method_name:
        return

    if kind == "function":
        raise DepsError(f"Configured method '{method_name}' cannot be used with function symbol '{symbol_name}'")

    # class symbol: validate method directly
    if kind == "class" and isinstance(symbol_node, ast.ClassDef):
        if not _class_has_method(symbol_node, str(method_name)):
            raise DepsError(f"Configured method '{method_name}' not found on class symbol '{symbol_name}'")
        return

    # assignment symbol: try to resolve instance creation (e.g. `agent = MyAgent()`)
    assigned_value: ast.AST | None = None
    if isinstance(symbol_node, ast.Assign):
        assigned_value = symbol_node.value
    elif isinstance(symbol_node, ast.AnnAssign):
        assigned_value = symbol_node.value

    if isinstance(assigned_value, ast.Call):
        called_name: str | None = None
        if isinstance(assigned_value.func, ast.Name):
            called_name = assigned_value.func.id
        elif isinstance(assigned_value.func, ast.Attribute):
            called_name = assigned_value.func.attr

        if called_name:
            class_node = _class_def_by_name(tree, called_name)
            if class_node and not _class_has_method(class_node, str(method_name)):
                raise DepsError(
                    f"Configured method '{method_name}' not found on inferred class '{called_name}' for symbol '{symbol_name}'"
                )


def _extract_required_env_vars_from_entry(project_root: Path, entry: dict[str, Any]) -> set[str]:
    file_ref = entry.get("file")
    if not file_ref:
        return set()
    file_path = Path(str(file_ref))
    if not file_path.is_absolute():
        file_path = project_root / file_path
    if not file_path.exists():
        return set()

    try:
        tree = ast.parse(file_path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return set()

    required: set[str] = set()

    def _call_first_string_arg(node: ast.Call) -> str | None:
        if not node.args:
            return None
        arg = node.args[0]
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            return arg.value
        return None

    def _has_default_arg(node: ast.Call) -> bool:
        if len(node.args) > 1:
            return True
        for kw in node.keywords:
            if kw.arg in {"default", "fallback"}:
                return True
        return False

    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            # os.getenv("KEY")
            if (
                node.func.attr == "getenv"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "os"
            ):
                key = _call_first_string_arg(node)
                if key and not _has_default_arg(node):
                    required.add(key)

            # os.environ.get("KEY")
            if (
                node.func.attr == "get"
                and isinstance(node.func.value, ast.Attribute)
                and node.func.value.attr == "environ"
                and isinstance(node.func.value.value, ast.Name)
                and node.func.value.value.id == "os"
            ):
                key = _call_first_string_arg(node)
                if key and not _has_default_arg(node):
                    required.add(key)

        # os.environ["KEY"]
        if isinstance(node, ast.Subscript):
            container = node.value
            if (
                isinstance(container, ast.Attribute)
                and container.attr == "environ"
                and isinstance(container.value, ast.Name)
                and container.value.id == "os"
            ):
                key_node = node.slice
                if isinstance(key_node, ast.Constant) and isinstance(key_node.value, str):
                    required.add(key_node.value)

    return required


def _load_dotenv_map(project_root: Path) -> dict[str, str]:
    dotenv_path = project_root / ".env"
    if not dotenv_path.exists():
        return {}

    loaded: dict[str, str] = {}
    for raw in dotenv_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'").strip('"')
        if key:
            loaded[key] = value
    return loaded


def _validate_agents(project_root: Path, config_path: Path, mode: str) -> ValidationReport:
    loaded = load_config(config_path)
    validated = 0

    for agent in loaded.config.agents:
        entry_payload = agent.entry.model_dump()

        if mode == "dry":
            _validate_agent_entry_static(project_root=project_root, entry=entry_payload)
            if agent.io.input.model:
                _validate_model_ref_syntax(agent.io.input.model)
            if agent.io.output.model:
                _validate_model_ref_syntax(agent.io.output.model)
            validated += 1
            continue

        callable_obj = _resolve_agent_callable(project_root=project_root, entry=entry_payload)

        # Full mode validates model import paths are resolvable.
        if agent.io.input.model:
            load_symbol(agent.io.input.model, project_root=project_root)
        if agent.io.output.model:
            load_symbol(agent.io.output.model, project_root=project_root)

        if mode == "full":
            payload = _sample_payload_from_schema(agent.io.input.schema_)
            result = asyncio.run(invoke_target(callable_obj, payload, call_style=agent.entry.call_style))

            if agent.io.strict_output and agent.io.output.schema_ and isinstance(agent.io.output.schema_, dict):
                output_type = agent.io.output.schema_.get("type")
                if output_type == "object" and not isinstance(result, dict):
                    raise DepsError(
                        f"Full validation failed for agent '{agent.name}': output is not object as required by schema"
                    )

        validated += 1

    return ValidationReport(mode=mode, validated_agents=validated)


def _validate_agents_full_isolated(
    project_root: Path,
    config_path: Path,
    lock_path: Path,
    *,
    mode: str = "full",
    validation_python: str = sys.executable,
    lock_python_version: str = DEFAULT_LOCK_PYTHON_VERSION,
) -> ValidationReport:
    if mode not in {"dry", "full"}:
        raise DepsError(f"Unsupported validation mode: {mode}")
    if not lock_path.exists():
        raise DepsError(f"Lock file not found for {mode} validation: {lock_path}")

    try:
        config_payload = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise DepsError(f"Invalid config JSON for {mode} validation: {config_path}") from exc

    agent_names: list[str] = []
    agent_entries: list[dict[str, Any]] = []
    for item in config_payload.get("agents", []) if isinstance(config_payload, dict) else []:
        if isinstance(item, dict):
            agent_names.append(str(item.get("name") or "unknown-agent"))
            entry = item.get("entry")
            if isinstance(entry, dict):
                agent_entries.append(entry)

    print(f"Validation ({mode}): creating isolated environment for {len(agent_names)} agent(s)...")

    if mode == "full":
        required_env: set[str] = set()
        for entry in agent_entries:
            required_env.update(_extract_required_env_vars_from_entry(project_root, entry))
        effective_env = dict(os.environ)
        effective_env.update(_load_dotenv_map(project_root))
        missing_env = sorted([name for name in required_env if not effective_env.get(name)])
        if missing_env:
            raise FullValidationFailure(
                "Full validation requires live env vars; missing: " + ", ".join(missing_env)
            )

    runner = r"""
import ast
import asyncio
import importlib
from importlib import metadata as importlib_metadata
import importlib.util
import inspect
import json
import re
import sys
from pathlib import Path

IMPORT_TO_PACKAGE_MAP = {
    "yaml": "PyYAML",
    "dotenv": "python-dotenv",
    "serpapi": "google-search-results",
    "bs4": "beautifulsoup4",
    "cv2": "opencv-python",
    "PIL": "Pillow",
    "sklearn": "scikit-learn",
    "langchain_openai": "langchain-openai",
    "langchain_community": "langchain-community",
    "langchain_core": "langchain-core",
    "llama_index": "llama-index",
    "pydantic_ai": "pydantic-ai",
    "crewai": "crewai",
    "pkg_resources": "setuptools<81",
}


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
    return module


def _rebuild_pydantic_models(module):
    for value in vars(module).values():
        rebuild = getattr(value, "model_rebuild", None)
        if not callable(rebuild):
            continue
        try:
            rebuild(force=True)
        except TypeError:
            try:
                rebuild()
            except Exception:
                continue
        except Exception:
            continue


def _resolve_callable(target, method: str | None, call_type: str):
    if method:
        if not hasattr(target, method):
            raise RuntimeError(f"Configured method '{method}' not found on symbol")
        resolved = getattr(target, method)
        if not callable(resolved):
            raise RuntimeError(f"Configured method '{method}' is not callable")
        return resolved

    if call_type == "callable":
        if not callable(target):
            raise RuntimeError("Configured symbol is not callable")
        return target

    if callable(target):
        return target

    for candidate in ("invoke", "kickoff", "run", "__call__"):
        if hasattr(target, candidate):
            resolved = getattr(target, candidate)
            if callable(resolved):
                return resolved
    raise RuntimeError("Could not resolve a callable from configured symbol")


def _invoke_with_style(fn, payload, call_style: str):
    if call_style == "single_arg":
        return fn(payload)
    if call_style == "kwargs":
        if not isinstance(payload, dict):
            raise RuntimeError("kwargs call_style requires payload to be an object")
        return fn(**payload)

    if isinstance(payload, dict):
        try:
            signature = inspect.signature(fn)
            params = list(signature.parameters.values())
            if not params:
                return fn()
            if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params):
                return fn(**payload)
            required = [
                p for p in params
                if p.default is inspect.Parameter.empty
                and p.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
            ]
            if len(required) > 1:
                return fn(**payload)
            try:
                return fn(**payload)
            except TypeError:
                return fn(payload)
        except (TypeError, ValueError):
            return fn(payload)
    return fn(payload)


def _invoke(fn, payload, call_style: str):
    result = _invoke_with_style(fn, payload, call_style)
    if inspect.isawaitable(result):
        return asyncio.run(result)
    return result


def _sample_payload_from_schema(schema):
    if not isinstance(schema, dict):
        return {"prompt": "dank dependency smoke test"}
    props = schema.get("properties", {}) if isinstance(schema.get("properties"), dict) else {}
    required = schema.get("required", []) if isinstance(schema.get("required"), list) else []
    sample = {}

    def sample_for(field_schema):
        if not isinstance(field_schema, dict):
            return "sample"
        field_type = field_schema.get("type")
        if isinstance(field_type, list):
            field_type = next((t for t in field_type if t != "null"), field_type[0] if field_type else None)
        if field_type == "string":
            return "sample"
        if field_type == "integer":
            return 1
        if field_type == "number":
            return 1.0
        if field_type == "boolean":
            return True
        if field_type == "array":
            return []
        if field_type == "object":
            return {}
        return "sample"

    for key in required:
        sample[key] = sample_for(props.get(key, {}))
    if "prompt" in props and "prompt" not in sample:
        sample["prompt"] = "dank dependency smoke test"
    if not sample:
        sample["prompt"] = "dank dependency smoke test"
    return sample


def _looks_like_mock_result(result):
    if isinstance(result, dict):
        mode = str(result.get("mode", "")).lower()
        if mode in {"mock", "fallback", "stub"}:
            return True
        response = result.get("response")
        if isinstance(response, str):
            lower = response.lower()
            if "[mock" in lower or "fallback" in lower:
                return True
    if isinstance(result, str):
        lower = result.lower()
        if "[mock" in lower or "fallback" in lower:
            return True
    return False


def _resolve_from_entry(project_root: Path, entry: dict):
    file_ref = entry.get("file")
    symbol_name = entry.get("symbol")
    if not file_ref or not symbol_name:
        raise RuntimeError("Agent entry.file and entry.symbol are required for validation")

    file_path = Path(file_ref)
    if not file_path.is_absolute():
        file_path = project_root / file_ref
    if not file_path.exists():
        raise RuntimeError(f"Configured entry file does not exist: {file_path}")

    module = _load_module_from_path(file_path, f"dank_validation_{abs(hash(file_path))}")
    _rebuild_pydantic_models(module)
    if not hasattr(module, symbol_name):
        raise RuntimeError(f"Configured symbol '{symbol_name}' not found in {file_path}")
    symbol = getattr(module, symbol_name)
    if inspect.isclass(symbol):
        symbol = symbol()
    return _resolve_callable(symbol, method=entry.get("method"), call_type=entry.get("call_type", "auto"))


def _validate_model_ref(model_ref: str):
    if ":" not in model_ref:
        raise RuntimeError(f"Invalid model path '{model_ref}', expected module:Symbol")
    module_name, symbol_name = model_ref.split(":", 1)
    if not module_name.strip() or not symbol_name.strip():
        raise RuntimeError(f"Invalid model path '{model_ref}', expected module:Symbol")
    module = importlib.import_module(module_name)
    if not hasattr(module, symbol_name):
        raise RuntimeError(f"Model symbol '{symbol_name}' not found in module '{module_name}'")


def _module_to_package(module_name: str):
    top = str(module_name).split(".", 1)[0]
    if top in IMPORT_TO_PACKAGE_MAP:
        return IMPORT_TO_PACKAGE_MAP[top]
    try:
        by_package = importlib_metadata.packages_distributions()
        candidates = by_package.get(top) or []
        if candidates:
            return sorted(candidates)[0]
    except Exception:
        pass
    if re.fullmatch(r"[A-Za-z0-9]+", top):
        return top
    return top


def _collect_local_modules(project_root: Path):
    excluded_parts = {".venv", "venv", ".git", "__pycache__", ".dank-py"}
    local = set()
    for py in project_root.glob("*.py"):
        if py.name != "__init__.py":
            local.add(py.stem)
    for init_file in project_root.rglob("__init__.py"):
        if any(part in excluded_parts for part in init_file.parts):
            continue
        local.add(init_file.parent.name)
    return local


def _collect_import_modules_from_entry(project_root: Path, entry: dict):
    file_ref = entry.get("file")
    if not file_ref:
        return set()
    file_path = Path(file_ref)
    if not file_path.is_absolute():
        file_path = project_root / file_path
    if not file_path.exists():
        return set()
    try:
        tree = ast.parse(file_path.read_text(encoding="utf-8"))
    except Exception:
        return set()
    modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".", 1)[0]
                if top:
                    modules.add(top)
        elif isinstance(node, ast.ImportFrom):
            if node.level and node.level > 0:
                continue
            if node.module:
                top = node.module.split(".", 1)[0]
                if top:
                    modules.add(top)
    return modules


def _probe_import_modules(modules, local_modules):
    stdlib = set(getattr(sys, "stdlib_module_names", set()))
    missing = []
    for module_name in sorted(set(modules)):
        top = str(module_name).split(".", 1)[0]
        if not top or top in stdlib or top in local_modules:
            continue
        try:
            importlib.import_module(top)
        except ModuleNotFoundError as exc:
            missing_name = (getattr(exc, "name", None) or "").strip()
            missing_top = missing_name.split(".", 1)[0] if missing_name else top
            if not missing_top:
                missing_top = top
            if missing_top in stdlib or missing_top in local_modules:
                continue
            missing.append(
                {
                    "module": missing_top,
                    "package": _module_to_package(missing_top),
                    "imported_by": top,
                    "reason": str(exc).strip(),
                }
            )
        except ImportError as exc:
            text = str(exc).strip()
            match = re.search(r"No module named ['\"]([^'\"]+)['\"]", text)
            if not match:
                continue
            missing_name = (match.group(1) or "").strip()
            missing_top = missing_name.split(".", 1)[0] if missing_name else top
            if not missing_top or missing_top in stdlib or missing_top in local_modules:
                continue
            missing.append(
                {
                    "module": missing_top,
                    "package": _module_to_package(missing_top),
                    "imported_by": top,
                    "reason": text,
                }
            )
    return missing


def main():
    if len(sys.argv) != 4:
        raise SystemExit("usage: <script> <project_root> <config_path> <mode>")

    project_root = Path(sys.argv[1]).resolve()
    config_path = Path(sys.argv[2]).resolve()
    mode = sys.argv[3].strip().lower()
    if mode not in {"dry", "full"}:
        raise SystemExit("mode must be 'dry' or 'full'")

    raw = json.loads(config_path.read_text(encoding="utf-8"))
    agents = raw.get("agents", [])
    local_modules = _collect_local_modules(project_root)
    validated = 0
    validated_names = []
    failures = []
    missing_modules = set()

    for agent in agents:
        agent_name = str(agent.get("name", "unknown-agent"))
        print(f"[full-validate] smoke testing agent: {agent_name}", file=sys.stderr)
        entry = agent.get("entry") or {}
        io_cfg = agent.get("io") or {}
        input_cfg = io_cfg.get("input") or {}
        output_cfg = io_cfg.get("output") or {}
        strict_output = bool(io_cfg.get("strict_output", True))

        if input_cfg.get("model"):
            _validate_model_ref(input_cfg["model"])
        if output_cfg.get("model"):
            _validate_model_ref(output_cfg["model"])

        if mode == "full":
            import_modules = _collect_import_modules_from_entry(project_root, entry)
            missing_imports = _probe_import_modules(import_modules, local_modules)
            if missing_imports:
                missing_modules.update([item["module"] for item in missing_imports])
                detail = ", ".join(
                    [
                        (
                            f"{item['module']} (package: {item['package']}, imported_by: {item.get('imported_by')})"
                            if item.get("imported_by") and item.get("imported_by") != item.get("module")
                            else f"{item['module']} (package: {item['package']})"
                        )
                        for item in missing_imports
                    ]
                )
                failures.append(
                    {
                        "agent": agent_name,
                        "type": "ImportProbeError",
                        "error": (
                            f"Full validation import probe failed for agent '{agent_name}'. "
                            f"Missing dependencies: {detail}"
                        ),
                    }
                )
                continue

        try:
            fn = _resolve_from_entry(project_root, entry)
            payload = _sample_payload_from_schema(input_cfg.get("schema"))
            result = _invoke(fn, payload, call_style=entry.get("call_style", "auto"))
            if mode == "full" and _looks_like_mock_result(result):
                raise RuntimeError(
                    f"Full validation failed for agent '{agent_name}': "
                    "agent returned mock/fallback output instead of live output"
                )

            if strict_output and isinstance(output_cfg.get("schema"), dict):
                schema = output_cfg["schema"]
                expected_type = schema.get("type")
                if expected_type == "object" and not isinstance(result, dict):
                    raise RuntimeError(
                        f"Full validation failed for agent '{agent_name}': "
                        "output is not object as required by schema"
                    )
                if expected_type == "string" and not isinstance(result, str):
                    raise RuntimeError(
                        f"Full validation failed for agent '{agent_name}': "
                        "output is not string as required by schema"
                    )

            validated += 1
            validated_names.append(agent_name)
        except Exception as exc:  # noqa: BLE001
            error_text = str(exc).strip() or exc.__class__.__name__
            failures.append({"agent": agent_name, "type": exc.__class__.__name__, "error": error_text})
            missing_name = None
            if isinstance(exc, ModuleNotFoundError):
                missing_name = getattr(exc, "name", None)
            if not missing_name:
                m = re.search(r"No module named ['\"]([^'\"]+)['\"]", error_text)
                if m:
                    missing_name = m.group(1)
            if missing_name:
                missing_modules.add(str(missing_name).split(".", 1)[0])

    print(
        json.dumps(
            {
                "validated_agents": validated,
                "validated_names": validated_names,
                "failures": failures,
                "missing_modules": sorted(missing_modules),
            }
        )
    )
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
"""

    with tempfile.TemporaryDirectory(prefix="dank-py-validate-") as tmp_dir:
        tmp_root = Path(tmp_dir)
        env_dir = tmp_root / ".venv"
        print(f"Validation ({mode}): temp venv path: {env_dir}")
        print(f"Validation ({mode}): using interpreter: {validation_python}")

        create_venv = _run([validation_python, "-m", "venv", str(env_dir)], cwd=project_root)
        if create_venv.returncode != 0:
            raise DepsError(f"Failed to create isolated validation venv: {create_venv.stderr.strip() or create_venv.stdout.strip()}")

        env_python = env_dir / ("Scripts/python.exe" if sys.platform.startswith("win") else "bin/python")
        if not env_python.exists():
            raise DepsError(f"Isolated validation python not found: {env_python}")

        print(f"Validation ({mode}): upgrading installer tooling (pip/setuptools/wheel) ...")
        bootstrap = _run(
            [str(env_python), "-m", "pip", "install", "--upgrade", "pip", "setuptools", "wheel"],
            cwd=project_root,
        )
        if bootstrap.returncode != 0:
            print(
                f"Validation ({mode}): warning: failed to upgrade installer tooling; continuing.",
                file=sys.stderr,
            )

        print(f"Validation ({mode}): installing lock dependencies from {lock_path.name} ...")
        install = _run([str(env_python), "-m", "pip", "install", "-r", str(lock_path)], cwd=project_root)
        if install.returncode != 0:
            install_detail = install.stderr.strip() or install.stdout.strip()
            hint = ""
            if "can't find rust compiler" in install_detail.lower():
                hint = (
                    " Hint: a Rust-backed dependency attempted source build. "
                    f"Validation target lock/runtime is Python {lock_python_version}; "
                    f"prefer running validation with a matching interpreter (e.g. `python{lock_python_version}`), "
                    "or install Rust if building from source is expected."
                )
            raise DepsError(
                f"Failed to install lock dependencies for {mode} validation: "
                f"{install_detail}{hint}"
            )
        print(f"Validation ({mode}): dependency install completed.")

        runner_path = tmp_root / "validate_runner.py"
        runner_path.write_text(runner, encoding="utf-8")

        env = dict(**os.environ)
        env.update(_load_dotenv_map(project_root))
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        existing_pythonpath = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = str(project_root) if not existing_pythonpath else f"{project_root}:{existing_pythonpath}"

        run_validation = subprocess.run(
            [str(env_python), str(runner_path), str(project_root), str(config_path), mode],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        stderr_text = (run_validation.stderr or "").strip()
        if stderr_text:
            for line in stderr_text.splitlines():
                if line.strip():
                    print(f"Validation ({mode}): {line.strip()}")

        try:
            payload = json.loads((run_validation.stdout or "").strip())
            validated = int(payload.get("validated_agents", 0))
            validated_names = payload.get("validated_names", [])
            failures = payload.get("failures", [])
            missing_modules = payload.get("missing_modules", [])
        except Exception as exc:  # noqa: BLE001
            raise DepsError(
                f"Full validation produced unexpected output: {run_validation.stdout.strip() or run_validation.stderr.strip()}"
            ) from exc

        if run_validation.returncode != 0:
            failed_agents: list[str] = []
            if isinstance(failures, list):
                for item in failures:
                    if isinstance(item, dict):
                        failed_agents.append(str(item.get("agent") or "unknown-agent"))
            detail = ""
            if isinstance(failures, list) and failures:
                first = failures[0]
                if isinstance(first, dict):
                    detail = str(first.get("error") or "")
            missing_list = [str(x) for x in missing_modules] if isinstance(missing_modules, list) else []
            message = (
                f"Full validation failed in isolated environment: {detail or 'agent smoke test failed'} "
                f"(failed_agents={','.join(failed_agents) or 'unknown'})"
            )
            raise FullValidationFailure(
                message,
                missing_modules=missing_list,
                failed_agents=failed_agents,
            )
        if isinstance(validated_names, list):
            for name in validated_names:
                print(f"Validation ({mode}): passed smoke test for agent '{name}'")

    print(f"Validation ({mode}): cleaning up isolated environment.")
    print(f"Validation ({mode}): summary {validated}/{len(agent_names) or validated} agents passed.")

    return ValidationReport(mode=mode, validated_agents=validated)


def deps_command(
    project_dir: str | None = None,
    *,
    validate: str = "none",
    config_path: str | None = None,
    refresh_lock: bool = True,
    fallback_freeze: bool = False,
    discover_imports: bool = True,
    install_tools: bool = False,
    prompt_install_tools: bool = True,
    lock_python_version: str = DEFAULT_LOCK_PYTHON_VERSION,
    include_lock_comments: bool = False,
) -> DepsResult:
    project_root = Path(project_dir or Path.cwd()).resolve()
    print("Deps: resolving project dependencies and lock file ...")
    # Keep lock generation bound to the active environment interpreter for
    # predictable UX (same behavior as earlier CLI versions).
    lock_python_executable = sys.executable
    lock_python_executable_version = (
        _python_minor_version(lock_python_executable, cwd=project_root)
        or f"{sys.version_info.major}.{sys.version_info.minor}"
    )
    host_version = f"{sys.version_info.major}.{sys.version_info.minor}"
    lock_version_mismatch = lock_python_executable_version != lock_python_version
    if lock_version_mismatch:
        print(
            "Warning: dependency lock is being generated with active Python "
            f"{lock_python_executable_version} while target lock/runtime is {lock_python_version}.",
            file=sys.stderr,
        )

    lock_path = _ensure_lock_file(
        project_root,
        lock_python_executable=lock_python_executable,
        refresh_lock=refresh_lock,
        fallback_freeze=fallback_freeze,
        discover_imports=discover_imports,
        install_tools=install_tools,
        prompt_install_tools=prompt_install_tools,
        lock_python_version=lock_python_version,
        include_comments=include_lock_comments,
    )
    print(f"Deps: initial lock ready at {lock_path}")

    report = ValidationReport(mode="none", validated_agents=0)
    if validate in {"dry", "full"}:
        validation_python, validation_python_version = _resolve_python_interpreter(
            lock_python_version,
            project_root=project_root,
        )
        emitted_version_mismatch_warning = False
        if validation_python_version != lock_python_version:
            emitted_version_mismatch_warning = True
            print(
                "Warning: validation is running on host Python "
                f"{host_version} while lock/runtime target is {lock_python_version}. "
                f"Could not find a local Python {lock_python_version} interpreter. "
                "Continuing with host validation; prefer matching versions for strict parity.",
                file=sys.stderr,
            )
        elif validation_python != sys.executable:
            print(
                "Validation: using matching Python interpreter "
                f"{validation_python_version} at {validation_python}",
                file=sys.stderr,
            )

        if config_path:
            cfg = Path(config_path)
            if not cfg.is_absolute():
                cfg = (project_root / cfg).resolve()
        else:
            cfg = (project_root / "dank.config.json").resolve()
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            if validate == "full":
                auto_heal_round = 0
                total_auto_added = 0
                max_auto_heal_rounds = 2
                max_auto_added_packages = 5
                seen_missing_modules: set[str] = set()

                while True:
                    try:
                        report = _validate_agents_full_isolated(
                            project_root=project_root,
                            config_path=cfg,
                            lock_path=lock_path,
                            mode="full",
                            validation_python=validation_python,
                            lock_python_version=lock_python_version,
                        )
                        break
                    except FullValidationFailure as exc:
                        candidate_modules = [m for m in exc.missing_modules if m and m not in seen_missing_modules]
                        for item in candidate_modules:
                            seen_missing_modules.add(item)

                        remaining_rounds = max_auto_heal_rounds - auto_heal_round
                        remaining_slots = max_auto_added_packages - total_auto_added
                        if remaining_rounds <= 0 or remaining_slots <= 0 or not candidate_modules:
                            raise

                        mapped: list[str] = []
                        unresolved: list[str] = []
                        for module_name in candidate_modules:
                            pkg = _map_missing_module_to_package(module_name)
                            if pkg:
                                mapped.append(pkg)
                            else:
                                unresolved.append(module_name)

                        if unresolved:
                            print(
                                "Validation (full): auto-heal skipped unresolved missing module(s): "
                                + ", ".join(sorted(set(unresolved))),
                                file=sys.stderr,
                            )

                        mapped_unique = []
                        seen_pkgs: set[str] = set()
                        for pkg in mapped:
                            if pkg in seen_pkgs:
                                continue
                            seen_pkgs.add(pkg)
                            mapped_unique.append(pkg)

                        if not mapped_unique:
                            raise

                        to_apply = mapped_unique[:remaining_slots]
                        added = _append_requirements(project_root, to_apply)
                        if not added:
                            raise

                        auto_heal_round += 1
                        total_auto_added += len(added)
                        print(
                            "Validation (full): auto-heal detected missing modules and added package(s): "
                            + ", ".join(added),
                            file=sys.stderr,
                        )
                        print(
                            f"Validation (full): regenerating lock file (retry {auto_heal_round}/{max_auto_heal_rounds})...",
                            file=sys.stderr,
                        )
                        lock_path = _relock_from_requirements(
                            project_root,
                            lock_python_executable=lock_python_executable,
                            fallback_freeze=fallback_freeze,
                            install_tools=install_tools,
                            prompt_install_tools=prompt_install_tools,
                            lock_python_version=lock_python_version,
                            include_comments=include_lock_comments,
                        )
                        print(
                            f"Validation (full): lock file refreshed after auto-heal: {lock_path}",
                            file=sys.stderr,
                        )
                        continue
            else:
                report = _validate_agents_full_isolated(
                    project_root=project_root,
                    config_path=cfg,
                    lock_path=lock_path,
                    mode="dry",
                    validation_python=validation_python,
                    lock_python_version=lock_python_version,
                )

        unique_warnings: list[str] = []
        seen: set[str] = set()
        for warning in caught:
            text = str(warning.message).strip()
            if not text or text in seen:
                continue
            # Avoid noisy duplicate compatibility messaging when version mismatch was already reported.
            if emitted_version_mismatch_warning and (
                "isn't compatible with Python " in text
                or "is not compatible with Python " in text
            ):
                continue
            seen.add(text)
            unique_warnings.append(text)

        if unique_warnings:
            print(
                f"Warning: {len(unique_warnings)} non-fatal compatibility warning(s) during validation:",
                file=sys.stderr,
            )
            for message in unique_warnings[:3]:
                print(f"- {message}", file=sys.stderr)
            if len(unique_warnings) > 3:
                print(f"- ... and {len(unique_warnings) - 3} more", file=sys.stderr)

    return DepsResult(lock_path=lock_path, validation_mode=report.mode, validated_agents=report.validated_agents)
