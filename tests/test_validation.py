from __future__ import annotations

import textwrap

import pytest

from dank_py.lib.config.models import IOModelRef
from dank_py.lib.io.validator import PayloadValidationError, validate_payload


def test_validate_payload_with_pydantic_model(tmp_path):
    module_path = tmp_path / "schemas.py"
    module_path.write_text(
        textwrap.dedent(
            '''
            from pydantic import BaseModel

            class PromptInput(BaseModel):
                prompt: str
                user_id: str | None = None
            '''
        ),
        encoding="utf-8",
    )

    ref = IOModelRef(model="schemas:PromptInput")
    payload = {"prompt": "hello", "user_id": "u1"}
    result = validate_payload(payload, ref, project_root=tmp_path, phase="input")

    assert result["prompt"] == "hello"
    assert result["user_id"] == "u1"


def test_validate_payload_with_schema():
    ref = IOModelRef(
        schema={
            "type": "object",
            "required": ["prompt"],
            "properties": {"prompt": {"type": "string"}},
        }
    )
    payload = {"prompt": "hello"}
    result = validate_payload(payload, ref, project_root=None, phase="input")
    assert result == payload


def test_validate_payload_raises_for_invalid_model(tmp_path):
    module_path = tmp_path / "schemas.py"
    module_path.write_text(
        textwrap.dedent(
            '''
            from pydantic import BaseModel

            class PromptInput(BaseModel):
                prompt: str
            '''
        ),
        encoding="utf-8",
    )

    ref = IOModelRef(model="schemas:PromptInput")
    with pytest.raises(PayloadValidationError):
        validate_payload({"no_prompt": True}, ref, project_root=tmp_path, phase="input")
