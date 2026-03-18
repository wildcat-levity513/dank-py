"""Typed payload validation for input/output models and schemas."""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from jsonschema import ValidationError as JsonSchemaValidationError
from jsonschema import validate as jsonschema_validate
from pydantic import BaseModel as PydanticBaseModel
from pydantic import ValidationError as PydanticValidationError

from dank_py.lib.config.models import IOModelRef
from dank_py.lib.io.model_loader import load_symbol


class PayloadValidationError(RuntimeError):
    """Raised when request/response validation fails."""

    def __init__(self, phase: str, message: str):
        super().__init__(message)
        self.phase = phase


def _normalize_jsonable(value: Any) -> Any:
    if isinstance(value, PydanticBaseModel):
        return value.model_dump(mode="json")
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, dict):
        return {str(k): _normalize_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_normalize_jsonable(v) for v in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def _validate_with_model(model_import_path: str, payload: Any, project_root: Path | None) -> Any:
    symbol = load_symbol(model_import_path, project_root=project_root)

    if not isinstance(symbol, type) or not issubclass(symbol, PydanticBaseModel):
        raise PayloadValidationError(
            phase="model",
            message=f"Symbol '{model_import_path}' is not a Pydantic BaseModel subclass",
        )

    validated = symbol.model_validate(payload)
    return validated.model_dump(mode="json")


def _validate_with_schema(schema: dict[str, Any], payload: Any) -> Any:
    jsonschema_validate(instance=payload, schema=schema)
    return payload


def validate_payload(
    payload: Any,
    reference: IOModelRef,
    *,
    project_root: Path | None,
    phase: str,
    strict: bool = True,
) -> Any:
    if reference.model:
        try:
            return _validate_with_model(reference.model, payload, project_root)
        except (PydanticValidationError, PayloadValidationError) as exc:
            if strict:
                raise PayloadValidationError(phase=phase, message=str(exc)) from exc
            return _normalize_jsonable(payload)
        except Exception as exc:  # noqa: BLE001
            if strict:
                raise PayloadValidationError(phase=phase, message=str(exc)) from exc
            return _normalize_jsonable(payload)

    if reference.schema_:
        try:
            return _validate_with_schema(reference.schema_, payload)
        except JsonSchemaValidationError as exc:
            if strict:
                raise PayloadValidationError(phase=phase, message=str(exc)) from exc
            return _normalize_jsonable(payload)

    return _normalize_jsonable(payload)


def normalize_jsonable(value: Any) -> Any:
    return _normalize_jsonable(value)
