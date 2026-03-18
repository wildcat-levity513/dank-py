"""`dank inspect` command."""

from __future__ import annotations

import ast
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


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

METHOD_PRIORITY = ("invoke", "kickoff", "run", "__call__")


@dataclass(slots=True)
class EntryCandidate:
    file: str
    symbol: str
    method: str | None
    call_type: str | None
    call_style: str | None
    score: int
    reason: str
    inferred_input_schema: dict[str, Any] | None = None
    inferred_output_schema: dict[str, Any] | None = None

    def to_dict(self) -> dict:
        return {
            "file": self.file,
            "symbol": self.symbol,
            "method": self.method,
            "call_type": self.call_type,
            "call_style": self.call_style,
            "score": self.score,
            "reason": self.reason,
            "inferred_input_schema": self.inferred_input_schema,
            "inferred_output_schema": self.inferred_output_schema,
        }


@dataclass(slots=True)
class ModelCandidate:
    file: str
    symbol: str
    role: str
    score: int
    reason: str
    kind: str
    schema: dict[str, Any] | None = None

    def to_dict(self) -> dict:
        return {
            "file": self.file,
            "symbol": self.symbol,
            "role": self.role,
            "score": self.score,
            "reason": self.reason,
            "kind": self.kind,
            "schema": self.schema,
        }


def _iter_python_files(root: Path):
    for path in root.rglob("*.py"):
        if any(part in IGNORE_DIRS for part in path.parts):
            continue
        yield path


def _score_function(name: str) -> tuple[int, str]:
    lname = name.lower()
    if lname in {"agent", "main", "invoke", "run"}:
        return 95, "well-known agent entry function"
    if "agent" in lname:
        return 80, "function name contains 'agent'"
    if "invoke" in lname or "kickoff" in lname:
        return 75, "function name suggests invocation"
    return 50, "generic function"


def _score_model(name: str) -> tuple[int, str]:
    lname = name.lower()
    if lname.endswith(("input", "request")):
        return 95, "input model naming convention"
    if lname.endswith(("output", "response")):
        return 90, "output model naming convention"
    if "model" in lname:
        return 70, "generic model naming"
    return 50, "pydantic model"


def _role_from_name(name: str) -> str | None:
    lname = name.lower()
    if any(token in lname for token in ("output", "response", "result", "reply")):
        return "output"
    if any(token in lname for token in ("input", "request", "payload", "prompt")):
        return "input"
    return None


def _is_json_schema_dict(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    return "type" in value and ("properties" in value or "$schema" in value or "items" in value)


def _module_path_from_file(file_path: str) -> str:
    p = Path(file_path)
    if p.suffix == ".py":
        p = p.with_suffix("")
    parts = [part for part in p.parts if part not in {"."}]
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _schema_from_python_value(value: Any) -> dict[str, Any]:
    if isinstance(value, bool):
        return {"type": "boolean"}
    if isinstance(value, int) and not isinstance(value, bool):
        return {"type": "integer"}
    if isinstance(value, float):
        return {"type": "number"}
    if isinstance(value, str):
        return {"type": "string"}
    if value is None:
        return {"type": "null"}
    return {}


def _normalize_type_field(schema: dict[str, Any]) -> list[str]:
    type_value = schema.get("type")
    if isinstance(type_value, str):
        return [type_value]
    if isinstance(type_value, list):
        return [item for item in type_value if isinstance(item, str)]
    return []


def _merge_type_lists(left: list[str], right: list[str]) -> list[str]:
    merged: list[str] = []
    for item in [*left, *right]:
        if item not in merged:
            merged.append(item)
    return merged


def _merge_property_schema(existing: dict[str, Any] | None, incoming: dict[str, Any] | None) -> dict[str, Any]:
    if not existing:
        return incoming or {}
    if not incoming:
        return existing
    if existing == incoming:
        return existing

    left_types = _normalize_type_field(existing)
    right_types = _normalize_type_field(incoming)
    if left_types and right_types:
        merged_types = _merge_type_lists(left_types, right_types)
        if len(merged_types) == 1:
            return {"type": merged_types[0]}
        return {"type": merged_types}

    return existing or incoming


def _annotation_to_schema(annotation: ast.AST | None) -> dict[str, Any] | None:
    if annotation is None:
        return None

    if isinstance(annotation, ast.Constant) and annotation.value is None:
        return {"type": "null"}

    if isinstance(annotation, ast.Name):
        mapping: dict[str, dict[str, Any]] = {
            "str": "string",
            "int": "integer",
            "float": "number",
            "bool": "boolean",
            "dict": "object",
            "list": "array",
            "None": "null",
        }
        mapped = mapping.get(annotation.id)
        return {"type": mapped} if mapped else None

    if isinstance(annotation, ast.Attribute):
        if annotation.attr == "Any":
            return {}
        return None

    if isinstance(annotation, ast.BinOp) and isinstance(annotation.op, ast.BitOr):
        left = _annotation_to_schema(annotation.left)
        right = _annotation_to_schema(annotation.right)
        left_types = _normalize_type_field(left or {})
        right_types = _normalize_type_field(right or {})
        if left_types and right_types:
            merged = _merge_type_lists(left_types, right_types)
            return {"type": merged if len(merged) > 1 else merged[0]}
        return {}

    if isinstance(annotation, ast.Tuple):
        schemas = [_annotation_to_schema(item) for item in annotation.elts]
        all_types: list[str] = []
        for schema in schemas:
            all_types = _merge_type_lists(all_types, _normalize_type_field(schema or {}))
        if all_types:
            return {"type": all_types if len(all_types) > 1 else all_types[0]}
        return {}

    if isinstance(annotation, ast.Subscript):
        base = annotation.value
        base_name: str | None = None
        if isinstance(base, ast.Name):
            base_name = base.id
        elif isinstance(base, ast.Attribute):
            base_name = base.attr

        if base_name in {"dict", "Dict", "Mapping"}:
            return {"type": "object"}
        if base_name in {"list", "List", "Sequence", "Tuple", "set", "Set"}:
            return {"type": "array"}

        subscript_args: list[ast.AST]
        if isinstance(annotation.slice, ast.Tuple):
            subscript_args = list(annotation.slice.elts)
        else:
            subscript_args = [annotation.slice]

        if base_name == "Optional" and subscript_args:
            inner = _annotation_to_schema(subscript_args[0]) or {}
            inner_types = _normalize_type_field(inner)
            merged = _merge_type_lists(inner_types, ["null"]) if inner_types else ["null"]
            return {"type": merged if len(merged) > 1 else merged[0]}

        if base_name == "Union":
            union_types: list[str] = []
            for arg in subscript_args:
                union_types = _merge_type_lists(union_types, _normalize_type_field(_annotation_to_schema(arg) or {}))
            if union_types:
                return {"type": union_types if len(union_types) > 1 else union_types[0]}
            return {}

        if base_name == "Literal":
            values: list[Any] = []
            for arg in subscript_args:
                if isinstance(arg, ast.Constant):
                    values.append(arg.value)
            if not values:
                return {}
            enum_types = _merge_type_lists([], [t for value in values for t in _normalize_type_field(_schema_from_python_value(value))])
            schema: dict[str, Any] = {"enum": values}
            if enum_types:
                schema["type"] = enum_types[0] if len(enum_types) == 1 else enum_types
            return schema

    if isinstance(annotation, ast.Constant) and isinstance(annotation.value, str):
        return _annotation_to_schema(ast.Name(id=annotation.value))

    return None


def _schema_is_informative(schema: dict[str, Any] | None) -> bool:
    if not isinstance(schema, dict) or not schema:
        return False
    if "enum" in schema:
        return True
    if "properties" in schema or "required" in schema:
        return True
    schema_types = _normalize_type_field(schema)
    if schema_types and not (len(schema_types) == 1 and schema_types[0] == "object"):
        return True
    return False


def _value_node_to_schema(value: ast.AST, variable_schemas: dict[str, dict[str, Any]]) -> dict[str, Any]:
    if isinstance(value, ast.Constant):
        return _schema_from_python_value(value.value)
    if isinstance(value, ast.JoinedStr):
        return {"type": "string"}
    if isinstance(value, ast.Name):
        return variable_schemas.get(value.id, {})
    if isinstance(value, ast.Call):
        if isinstance(value.func, ast.Name):
            if value.func.id in {"str", "repr"}:
                return {"type": "string"}
            if value.func.id == "int":
                return {"type": "integer"}
            if value.func.id == "float":
                return {"type": "number"}
            if value.func.id == "bool":
                return {"type": "boolean"}
    return {}


def _collect_local_assignment_schemas(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    base_schemas: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    locals_map: dict[str, dict[str, Any]] = {}
    combined = dict(base_schemas)

    for child in ast.walk(node):
        if isinstance(child, ast.Assign):
            if len(child.targets) != 1 or not isinstance(child.targets[0], ast.Name):
                continue
            name = child.targets[0].id
            inferred = _value_node_to_schema(child.value, combined)
            locals_map[name] = _merge_property_schema(locals_map.get(name), inferred)
            combined[name] = locals_map[name]

        if isinstance(child, ast.AnnAssign) and isinstance(child.target, ast.Name):
            name = child.target.id
            annotated = _annotation_to_schema(child.annotation) or {}
            inferred = _value_node_to_schema(child.value, combined) if child.value is not None else {}
            locals_map[name] = _merge_property_schema(locals_map.get(name), _merge_property_schema(annotated, inferred))
            combined[name] = locals_map[name]

    return locals_map


def _infer_output_from_return_dict(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    param_schemas: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    properties: dict[str, dict[str, Any]] = {}
    returned_key_sets: list[set[str]] = []
    local_schemas = _collect_local_assignment_schemas(node, param_schemas)
    variable_schemas = {**param_schemas, **local_schemas}

    for child in ast.walk(node):
        if not isinstance(child, ast.Return) or not isinstance(child.value, ast.Dict):
            continue

        keys_in_return: set[str] = set()
        for key_node, value_node in zip(child.value.keys, child.value.values):
            if not isinstance(key_node, ast.Constant) or not isinstance(key_node.value, str):
                continue
            key = key_node.value
            keys_in_return.add(key)
            properties[key] = _merge_property_schema(properties.get(key), _value_node_to_schema(value_node, variable_schemas))

        if keys_in_return:
            returned_key_sets.append(keys_in_return)

    if not properties:
        return None

    # Keep output contracts conservative by default:
    # if "response" exists, require only that key; otherwise require none.
    required: list[str] = ["response"] if "response" in properties else []

    output_schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": True,
    }
    if required:
        output_schema["required"] = required
    return output_schema


def _infer_call_style_from_callable(node: ast.FunctionDef | ast.AsyncFunctionDef, *, is_method: bool) -> str:
    args = [arg for arg in node.args.args if not (is_method and arg.arg == "self")]
    kwonly_count = len(node.args.kwonlyargs)
    has_var_kw = node.args.kwarg is not None
    has_var_positional = node.args.vararg is not None
    total_named = len(args) + kwonly_count

    if has_var_kw:
        return "kwargs"
    if total_named == 0:
        return "auto"
    if total_named == 1 and not has_var_positional and kwonly_count == 0:
        return "single_arg"
    return "kwargs"


def _infer_io_from_function(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    *,
    is_method: bool = False,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    args = [arg for arg in node.args.args if arg.arg != "self"]
    kwonly_args = list(node.args.kwonlyargs)
    input_schema: dict[str, Any] | None = None
    output_schema: dict[str, Any] | None = None
    param_schemas: dict[str, dict[str, Any]] = {}

    if args or kwonly_args:
        properties: dict[str, Any] = {}
        required: list[str] = []
        defaults_count = len(node.args.defaults)
        required_cutoff = len(args) - defaults_count

        for idx, arg in enumerate(args):
            inferred_schema = _annotation_to_schema(arg.annotation) or {}
            properties[arg.arg] = inferred_schema
            param_schemas[arg.arg] = inferred_schema
            if idx < required_cutoff and not (is_method and idx == 0 and arg.arg == "self"):
                required.append(arg.arg)

        for idx, arg in enumerate(kwonly_args):
            inferred_schema = _annotation_to_schema(arg.annotation) or {}
            properties[arg.arg] = inferred_schema
            param_schemas[arg.arg] = inferred_schema
            # kw_defaults uses None to indicate a required kw-only arg.
            if idx >= len(node.args.kw_defaults) or node.args.kw_defaults[idx] is None:
                required.append(arg.arg)

        input_schema = {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": True,
        }

    output_schema = _annotation_to_schema(node.returns)
    if not _schema_is_informative(output_schema):
        dict_inferred = _infer_output_from_return_dict(node, param_schemas)
        if dict_inferred:
            output_schema = dict_inferred
        elif not _schema_is_informative(output_schema):
            output_schema = None

    return input_schema, output_schema


def _default_input_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "required": ["prompt"],
        "properties": {
            "prompt": {"type": "string"},
            "user_id": {"type": "string"},
            "conversation_id": {"type": "string"},
        },
        "additionalProperties": True,
    }


def _default_output_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "required": ["response"],
        "properties": {"response": {"type": "string"}},
        "additionalProperties": True,
    }


def _best_io_ref_for_role(
    entry_file: str,
    role: str,
    model_candidates: list[dict[str, Any]],
) -> dict[str, Any]:
    same_file = [m for m in model_candidates if m.get("file") == entry_file and m.get("role") == role]
    cross_file = [m for m in model_candidates if m.get("role") == role]
    pool = same_file or cross_file

    if pool:
        best = sorted(pool, key=lambda item: int(item.get("score", 0)), reverse=True)[0]
        kind = best.get("kind")
        if kind == "pydantic_model":
            module_path = _module_path_from_file(str(best.get("file") or ""))
            symbol = str(best.get("symbol") or "")
            if module_path and symbol:
                return {"model": f"{module_path}:{symbol}", "schema": None}
        if kind == "json_schema":
            schema = best.get("schema")
            if isinstance(schema, dict):
                return {"model": None, "schema": schema}

    if role == "input":
        return {"model": None, "schema": _default_input_schema()}
    return {"model": None, "schema": _default_output_schema()}


def inspect_command(project_dir: str | None = None, as_json: bool = False) -> str:
    root = Path(project_dir or Path.cwd()).resolve()

    entry_candidates: list[EntryCandidate] = []
    model_candidates: list[ModelCandidate] = []

    for file_path in _iter_python_files(root):
        rel_file = file_path.relative_to(root).as_posix()
        try:
            tree = ast.parse(file_path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue

        class_methods: dict[str, dict[str, ast.FunctionDef | ast.AsyncFunctionDef]] = {}
        exported_instances: list[tuple[str, str]] = []

        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                methods: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
                for child in node.body:
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        methods[child.name] = child
                class_methods[node.name] = methods

                base_names = []
                for base in node.bases:
                    if isinstance(base, ast.Name):
                        base_names.append(base.id)
                    elif isinstance(base, ast.Attribute):
                        base_names.append(base.attr)

                if "BaseModel" in base_names:
                    score, role_reason = _score_model(node.name)
                    role = _role_from_name(node.name) or "output"
                    model_candidates.append(
                        ModelCandidate(
                            file=rel_file,
                            symbol=node.name,
                            role=role,
                            score=score,
                            reason=role_reason,
                            kind="pydantic_model",
                        )
                    )

            if isinstance(node, ast.Assign):
                value = node.value
                target_names = [target.id for target in node.targets if isinstance(target, ast.Name)]

                for target_name in target_names:
                    role = _role_from_name(target_name)
                    if role is not None:
                        try:
                            literal = ast.literal_eval(value)
                        except Exception:
                            literal = None
                        if _is_json_schema_dict(literal):
                            model_candidates.append(
                                ModelCandidate(
                                    file=rel_file,
                                    symbol=target_name,
                                    role=role,
                                    score=85,
                                    reason="inline JSON schema variable",
                                    kind="json_schema",
                                    schema=literal,
                                )
                            )

                    if isinstance(value, ast.Call):
                        called_symbol: str | None = None
                        if isinstance(value.func, ast.Name):
                            called_symbol = value.func.id
                        elif isinstance(value.func, ast.Attribute):
                            called_symbol = value.func.attr
                        if called_symbol and called_symbol in class_methods:
                            exported_instances.append((target_name, called_symbol))

            if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value is not None:
                role = _role_from_name(node.target.id)
                if role is not None:
                    try:
                        literal = ast.literal_eval(node.value)
                    except Exception:
                        literal = None
                    if _is_json_schema_dict(literal):
                        model_candidates.append(
                            ModelCandidate(
                                file=rel_file,
                                symbol=node.target.id,
                                role=role,
                                score=85,
                                reason="inline JSON schema variable",
                                kind="json_schema",
                                schema=literal,
                            )
                        )

                if isinstance(node.value, ast.Call):
                    called_symbol: str | None = None
                    if isinstance(node.value.func, ast.Name):
                        called_symbol = node.value.func.id
                    elif isinstance(node.value.func, ast.Attribute):
                        called_symbol = node.value.func.attr
                    if called_symbol and called_symbol in class_methods:
                        exported_instances.append((node.target.id, called_symbol))

        exported_class_names = {class_name for _, class_name in exported_instances}

        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                score, reason = _score_function(node.name)
                inferred_input_schema, inferred_output_schema = _infer_io_from_function(node)
                entry_candidates.append(
                    EntryCandidate(
                        file=rel_file,
                        symbol=node.name,
                        method=None,
                        call_type="auto",
                        call_style=_infer_call_style_from_callable(node, is_method=False),
                        score=score,
                        reason=reason,
                        inferred_input_schema=inferred_input_schema,
                        inferred_output_schema=inferred_output_schema,
                    )
                )

        # Prefer exported instance symbols (e.g., `agent = MyAgent()`) over class names.
        for instance_name, class_name in exported_instances:
            methods = class_methods.get(class_name, {})
            chosen_method: str | None = None
            for candidate_method in METHOD_PRIORITY:
                if candidate_method in methods:
                    chosen_method = candidate_method
                    break
            if not chosen_method:
                continue

            method_node = methods[chosen_method]
            inferred_input_schema, inferred_output_schema = _infer_io_from_function(method_node, is_method=True)
            entry_candidates.append(
                EntryCandidate(
                    file=rel_file,
                    symbol=instance_name,
                    method=chosen_method,
                    call_type="method",
                    call_style=_infer_call_style_from_callable(method_node, is_method=True),
                    score=98,
                    reason=f"exported instance of '{class_name}' with '{chosen_method}'",
                    inferred_input_schema=inferred_input_schema,
                    inferred_output_schema=inferred_output_schema,
                )
            )

        for class_name, method_map in class_methods.items():
            if class_name in exported_class_names:
                continue

            chosen_method: str | None = None
            for candidate_method in METHOD_PRIORITY:
                if candidate_method in method_map:
                    chosen_method = candidate_method
                    break
            if not chosen_method:
                continue

            method_node = method_map[chosen_method]
            inferred_input_schema, inferred_output_schema = _infer_io_from_function(method_node, is_method=True)
            priority_index = METHOD_PRIORITY.index(chosen_method)
            score = 95 - (priority_index * 5)
            entry_candidates.append(
                EntryCandidate(
                    file=rel_file,
                    symbol=class_name,
                    method=chosen_method,
                    call_type="method",
                    call_style=_infer_call_style_from_callable(method_node, is_method=True),
                    score=score,
                    reason=f"class exposes '{chosen_method}'",
                    inferred_input_schema=inferred_input_schema,
                    inferred_output_schema=inferred_output_schema,
                )
            )

    entry_candidates.sort(key=lambda candidate: candidate.score, reverse=True)
    model_candidates.sort(key=lambda candidate: candidate.score, reverse=True)

    payload = {
        "entry_candidates": [candidate.to_dict() for candidate in entry_candidates[:20]],
        "model_candidates": [candidate.to_dict() for candidate in model_candidates[:20]],
    }

    if as_json:
        return json.dumps(payload, indent=2)

    lines = ["Entry Candidates:"]
    if payload["entry_candidates"]:
        for item in payload["entry_candidates"]:
            method_suffix = f".{item['method']}" if item["method"] else ""
            lines.append(
                f"- {item['file']}: {item['symbol']}{method_suffix} (score={item['score']}, reason={item['reason']})"
            )
    else:
        lines.append("- none found")

    lines.append("\nModel Candidates:")
    if payload["model_candidates"]:
        for item in payload["model_candidates"]:
            lines.append(
                f"- {item['file']}: {item['symbol']} [{item['role']}] "
                f"(kind={item['kind']}, score={item['score']}, reason={item['reason']})"
            )
    else:
        lines.append("- none found")

    return "\n".join(lines)


def inspect_payload(project_dir: str | None = None) -> dict[str, Any]:
    return json.loads(inspect_command(project_dir=project_dir, as_json=True))


def _slugify(value: str) -> str:
    cleaned = []
    for ch in value.lower():
        if ch.isalnum():
            cleaned.append(ch)
        elif ch in {"_", "-", " "}:
            cleaned.append("-")
    slug = "".join(cleaned).strip("-")
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug or "agent"


def _agent_name_from_candidate(candidate: dict[str, Any]) -> str:
    symbol = str(candidate.get("symbol") or "agent")
    file_name = Path(str(candidate.get("file") or "agent.py")).stem
    file_slug = _slugify(file_name)
    symbol_slug = _slugify(symbol)
    generic_symbols = {"run", "invoke", "kickoff", "agent", "main", "__call__"}

    if symbol_slug in generic_symbols or symbol_slug == file_slug:
        return file_slug
    return f"{file_slug}-{symbol_slug}"


def _candidate_to_agent(candidate: dict[str, Any], model_candidates: list[dict[str, Any]]) -> dict[str, Any]:
    agent_name = _agent_name_from_candidate(candidate)
    input_ref = _best_io_ref_for_role(str(candidate.get("file") or ""), "input", model_candidates)
    output_ref = _best_io_ref_for_role(str(candidate.get("file") or ""), "output", model_candidates)

    inferred_input_schema = candidate.get("inferred_input_schema")
    inferred_output_schema = candidate.get("inferred_output_schema")
    if isinstance(inferred_input_schema, dict) and not input_ref.get("model"):
        input_ref = {"model": None, "schema": inferred_input_schema}
    if isinstance(inferred_output_schema, dict) and _schema_is_informative(inferred_output_schema) and not output_ref.get("model"):
        output_ref = {"model": None, "schema": inferred_output_schema}

    method = candidate.get("method")
    call_type = candidate.get("call_type") or ("method" if method else "auto")
    call_style = candidate.get("call_style") or "auto"

    return {
        "name": agent_name,
        "id": _slugify(agent_name),
        "entry": {
            "file": candidate.get("file"),
            "symbol": candidate.get("symbol"),
            "method": method,
            "call_type": call_type,
            "call_style": call_style,
        },
        "io": {
            "input": input_ref,
            "output": output_ref,
            "strict_output": True,
        },
    }


def _ensure_unique_agent_names(agents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    used: set[str] = set()
    for agent in agents:
        original_name = str(agent.get("name") or "agent")
        base_name = _slugify(original_name)
        candidate = base_name
        suffix = 2
        while candidate in used:
            candidate = f"{base_name}-{suffix}"
            suffix += 1
        used.add(candidate)
        agent["name"] = candidate
        current_id = str(agent.get("id") or "").strip()
        if not current_id or _slugify(current_id) == _slugify(original_name):
            agent["id"] = candidate
    return agents


def _order_dict(value: dict[str, Any], preferred: tuple[str, ...]) -> dict[str, Any]:
    ordered: dict[str, Any] = {}
    for key in preferred:
        if key in value:
            ordered[key] = value[key]
    for key, item in value.items():
        if key not in ordered:
            ordered[key] = item
    return ordered


def _normalize_schema(value: Any) -> Any:
    if isinstance(value, dict):
        normalized: dict[str, Any] = {}
        for key, item in value.items():
            if key == "properties" and isinstance(item, dict):
                normalized[key] = {prop: _normalize_schema(prop_schema) for prop, prop_schema in item.items()}
            elif key in {"items", "oneOf", "anyOf", "allOf"}:
                normalized[key] = _normalize_schema(item)
            else:
                normalized[key] = _normalize_schema(item)
        return _order_dict(
            normalized,
            (
                "type",
                "required",
                "properties",
                "items",
                "enum",
                "additionalProperties",
                "oneOf",
                "anyOf",
                "allOf",
                "$ref",
            ),
        )

    if isinstance(value, list):
        return [_normalize_schema(item) for item in value]
    return value


def _normalize_config_for_write(config: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(config)

    agents = normalized.get("agents")
    if isinstance(agents, list):
        normalized_agents: list[dict[str, Any]] = []
        for agent in agents:
            if not isinstance(agent, dict):
                continue
            entry = agent.get("entry")
            io_cfg = agent.get("io")

            normalized_entry = {}
            if isinstance(entry, dict):
                normalized_entry = _order_dict(
                    dict(entry),
                    ("file", "symbol", "method", "call_type", "call_style"),
                )

            normalized_io = {}
            if isinstance(io_cfg, dict):
                input_ref = io_cfg.get("input")
                output_ref = io_cfg.get("output")

                normalized_input: dict[str, Any] = {}
                if isinstance(input_ref, dict):
                    normalized_input = _order_dict(
                        {
                            "model": input_ref.get("model"),
                            "schema": _normalize_schema(input_ref.get("schema")),
                        },
                        ("model", "schema"),
                    )

                normalized_output: dict[str, Any] = {}
                if isinstance(output_ref, dict):
                    normalized_output = _order_dict(
                        {
                            "model": output_ref.get("model"),
                            "schema": _normalize_schema(output_ref.get("schema")),
                        },
                        ("model", "schema"),
                    )

                normalized_io = _order_dict(
                    {
                        "input": normalized_input,
                        "output": normalized_output,
                        "strict_output": io_cfg.get("strict_output", True),
                    },
                    ("input", "output", "strict_output"),
                )

            normalized_agent = _order_dict(
                {
                    "name": agent.get("name"),
                    "id": agent.get("id"),
                    "entry": normalized_entry,
                    "io": normalized_io,
                },
                ("name", "id", "entry", "io"),
            )
            normalized_agents.append(normalized_agent)

        normalized["agents"] = normalized_agents

    return _order_dict(normalized, ("name", "version", "agents"))


def _is_scalar(value: Any) -> bool:
    return value is None or isinstance(value, (str, int, float, bool))


def _is_scalar_list(value: Any) -> bool:
    return isinstance(value, list) and all(_is_scalar(item) for item in value)


def _inline_json(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False)
    if isinstance(value, dict) and raw != "{}":
        return "{ " + raw[1:-1] + " }"
    return raw


def _can_inline_dict(value: dict[str, Any], *, max_len: int = 88) -> bool:
    if not value:
        return True
    for item in value.values():
        if _is_scalar(item) or _is_scalar_list(item):
            continue
        return False
    return len(_inline_json(value)) <= max_len


def _can_inline_list(value: list[Any], *, max_len: int = 88) -> bool:
    return _is_scalar_list(value) and len(_inline_json(value)) <= max_len


def _indent_lines(lines: list[str], spaces: int) -> list[str]:
    prefix = " " * spaces
    return [prefix + line for line in lines]


def _format_json_lines(value: Any, *, indent: int = 2) -> list[str]:
    if _is_scalar(value):
        return [json.dumps(value, ensure_ascii=False)]

    if isinstance(value, list):
        if _can_inline_list(value):
            return [_inline_json(value)]

        if not value:
            return ["[]"]

        lines = ["["]
        for idx, item in enumerate(value):
            comma = "," if idx < len(value) - 1 else ""
            rendered = _format_json_lines(item, indent=indent)
            if len(rendered) == 1:
                lines.append(" " * indent + rendered[0] + comma)
            else:
                block = _indent_lines(rendered, indent)
                block[-1] = block[-1] + comma
                lines.extend(block)
        lines.append("]")
        return lines

    if isinstance(value, dict):
        if _can_inline_dict(value):
            return [_inline_json(value)]

        if not value:
            return ["{}"]

        items = list(value.items())
        lines = ["{"]
        for idx, (key, item) in enumerate(items):
            comma = "," if idx < len(items) - 1 else ""
            rendered = _format_json_lines(item, indent=indent)
            key_json = json.dumps(key, ensure_ascii=False)
            if len(rendered) == 1:
                lines.append(" " * indent + f"{key_json}: {rendered[0]}{comma}")
            else:
                lines.append(" " * indent + f"{key_json}: {rendered[0]}")
                tail = _indent_lines(rendered[1:], indent)
                tail[-1] = tail[-1] + comma
                lines.extend(tail)
        lines.append("}")
        return lines

    return [json.dumps(value, ensure_ascii=False)]


def _format_config_json(config: dict[str, Any]) -> str:
    normalized = _normalize_config_for_write(config)
    return "\n".join(_format_json_lines(normalized, indent=2)) + "\n"


def apply_entry_to_config(
    project_dir: str | None = None,
    *,
    config_path: str = "dank.config.json",
    entry_values: dict[str, Any],
) -> bool:
    root = Path(project_dir or Path.cwd()).resolve()
    cfg = Path(config_path)
    if not cfg.is_absolute():
        cfg = (root / cfg).resolve()
    if not cfg.exists():
        return False

    try:
        config = json.loads(cfg.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return False

    agents = config.get("agents")
    if not isinstance(agents, list) or not agents:
        return False

    entry = agents[0].get("entry")
    if not isinstance(entry, dict):
        return False

    entry["file"] = entry_values.get("file", entry.get("file"))
    entry["symbol"] = entry_values.get("symbol", entry.get("symbol"))
    entry["method"] = entry_values.get("method")
    entry["call_type"] = entry_values.get("call_type", entry.get("call_type", "auto"))
    entry["call_style"] = entry_values.get("call_style", entry.get("call_style", "auto"))

    cfg.write_text(_format_config_json(config), encoding="utf-8")
    return True


def apply_candidates_to_config(
    project_dir: str | None = None,
    *,
    config_path: str = "dank.config.json",
    candidate_indexes: list[int] | None = None,
    min_score: int = 75,
    max_agents: int = 5,
) -> int:
    root = Path(project_dir or Path.cwd()).resolve()
    cfg = Path(config_path)
    if not cfg.is_absolute():
        cfg = (root / cfg).resolve()
    if not cfg.exists():
        return 0

    payload = inspect_payload(project_dir=str(root))
    entries = payload.get("entry_candidates", [])
    model_candidates = payload.get("model_candidates", [])
    if not isinstance(entries, list) or not entries:
        return 0
    if not isinstance(model_candidates, list):
        model_candidates = []

    if candidate_indexes is None:
        selected_candidates = [entry for entry in entries if isinstance(entry, dict) and int(entry.get("score", 0)) >= min_score]
    else:
        selected_candidates = []
        for idx in candidate_indexes:
            if idx < 0 or idx >= len(entries):
                continue
            chosen = entries[idx]
            if isinstance(chosen, dict):
                selected_candidates.append(chosen)

    # Dedupe by runtime entry tuple and cap total agents.
    deduped: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str | None]] = set()
    for candidate in selected_candidates:
        key = (
            str(candidate.get("file") or ""),
            str(candidate.get("symbol") or ""),
            candidate.get("method"),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(candidate)
        if len(deduped) >= max_agents:
            break

    if not deduped:
        return 0

    try:
        config = json.loads(cfg.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return 0

    if not isinstance(config.get("name"), str) or config.get("name") in {"", "my-python-project"}:
        config["name"] = root.name

    config["agents"] = _ensure_unique_agent_names(
        [_candidate_to_agent(candidate, model_candidates) for candidate in deduped]
    )
    cfg.write_text(_format_config_json(config), encoding="utf-8")
    return len(deduped)


def apply_top_candidate_to_config(
    project_dir: str | None = None,
    *,
    config_path: str = "dank.config.json",
    candidate_index: int = 0,
) -> bool:
    root = Path(project_dir or Path.cwd()).resolve()
    payload = inspect_payload(project_dir=str(root))
    entries = payload.get("entry_candidates", [])
    if not isinstance(entries, list) or not entries:
        return False
    if candidate_index < 0 or candidate_index >= len(entries):
        return False
    chosen = entries[candidate_index]
    if not isinstance(chosen, dict):
        return False

    return (
        apply_candidates_to_config(
            project_dir=str(root),
            config_path=config_path,
            candidate_indexes=[candidate_index],
            min_score=0,
            max_agents=1,
        )
        == 1
    )
