# Agent: custom-openai-agent
# What it does:
# - Calls OpenAI Responses API directly (no framework wrapper) with a concise-assistant system prompt.
# - Supports optional `model` override per request.
# - Falls back to a deterministic mock response if API key or SDK is unavailable.
#
# How to call:
# - Entry function: `run(prompt, user_id=None, conversation_id=None, model=None)`
# - Returns: dict with `response`, `framework`, `mode`, `model`, and optional context IDs.

from __future__ import annotations

import os
from typing import Any

from dotenv import load_dotenv

load_dotenv()

try:
    from openai import OpenAI
except Exception:  # pragma: no cover
    OpenAI = None


def _mock_response(prompt: str) -> str:
    return f"[mock-openai] Received: {prompt[:120]}"


def run(
    prompt: str,
    user_id: str | None = None,
    conversation_id: str | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    api_key = os.getenv("OPENAI_API_KEY")
    model_name = model or os.getenv("OPENAI_MODEL", "gpt-4o-mini")

    if not api_key or OpenAI is None:
        return {
            "response": _mock_response(prompt),
            "framework": "custom-openai",
            "mode": "mock",
            "model": model_name,
            "user_id": user_id,
            "conversation_id": conversation_id,
        }

    client = OpenAI(api_key=api_key)
    resp = client.responses.create(
        model=model_name,
        input=[
            {
                "role": "system",
                "content": "You are a concise assistant. Respond in 1-3 sentences.",
            },
            {"role": "user", "content": prompt},
        ],
    )
    text = getattr(resp, "output_text", "") or str(resp)

    return {
        "response": text,
        "framework": "custom-openai",
        "mode": "live",
        "model": model_name,
        "user_id": user_id,
        "conversation_id": conversation_id,
    }
