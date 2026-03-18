"""Default runtime module used when generated /app/agent-code/index.py is missing."""

from __future__ import annotations

import inspect

AGENT_SPEC = {
    "name": "default-agent",
    "id": None,
    "entry": {
        "file": None,
        "symbol": None,
        "method": None,
        "call_type": "auto",
        "call_style": "single_arg",
    },
    "io": {
        "input": {"model": None, "schema": None},
        "output": {"model": None, "schema": None},
        "strict_output": True,
    },
}


def _default_handler(payload):
    prompt = payload.get("prompt") if isinstance(payload, dict) else str(payload)
    return {
        "response": f"Default dank-py runtime response: {prompt or 'no prompt provided'}",
        "runtime": "default",
    }


def resolve_target(symbol):
    if callable(symbol):
        return symbol
    return _default_handler


async def invoke_target(callable_obj, payload):
    fn = callable_obj or _default_handler
    result = fn(payload)
    if inspect.isawaitable(result):
        return await result
    return result
