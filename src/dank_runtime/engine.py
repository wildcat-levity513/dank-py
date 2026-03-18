"""Invocation helpers used by generated runtime index modules."""

from __future__ import annotations

import asyncio
import inspect
from typing import Any

from dank_runtime.constants import AUTO_METHOD_CANDIDATES


class InvocationError(RuntimeError):
    """Raised when target invocation cannot be resolved."""


def resolve_callable(target: Any, method: str | None, call_type: str) -> Any:
    if method:
        if not hasattr(target, method):
            raise InvocationError(f"Configured method '{method}' not found on symbol")
        resolved = getattr(target, method)
        if not callable(resolved):
            raise InvocationError(f"Configured method '{method}' is not callable")
        return resolved

    if call_type == "callable":
        if not callable(target):
            raise InvocationError("Configured symbol is not callable")
        return target

    if callable(target):
        return target

    for candidate in AUTO_METHOD_CANDIDATES:
        if hasattr(target, candidate):
            resolved = getattr(target, candidate)
            if callable(resolved):
                return resolved

    raise InvocationError("Could not resolve a callable from configured symbol")


def call_with_style(fn: Any, payload: Any, call_style: str) -> Any:
    def _call_kwargs_safe(values: dict[str, Any]) -> Any:
        try:
            signature = inspect.signature(fn)
        except (TypeError, ValueError):
            return fn(**values)

        params = list(signature.parameters.values())
        if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params):
            return fn(**values)

        allowed: set[str] = {
            p.name
            for p in params
            if p.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
        }
        filtered = {k: v for k, v in values.items() if k in allowed}
        return fn(**filtered)

    if call_style == "single_arg":
        return fn(payload)

    if call_style == "kwargs":
        if not isinstance(payload, dict):
            raise InvocationError("kwargs call_style requires payload to be an object")
        return _call_kwargs_safe(payload)

    # auto
    if isinstance(payload, dict):
        try:
            signature = inspect.signature(fn)
            params = list(signature.parameters.values())
            if not params:
                return fn()
            if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params):
                return fn(**payload)
            required = [
                p
                for p in params
                if p.default is inspect.Parameter.empty
                and p.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
            ]
            if len(required) > 1:
                return _call_kwargs_safe(payload)
            try:
                return _call_kwargs_safe(payload)
            except TypeError:
                return fn(payload)
        except (TypeError, ValueError):
            return fn(payload)

    return fn(payload)


async def invoke(fn: Any, payload: Any, call_style: str) -> Any:
    if inspect.iscoroutinefunction(fn):
        result = call_with_style(fn, payload, call_style)
    else:
        # Run sync callables off the event loop so wrappers like `run_sync`
        # can safely manage their own loop semantics.
        result = await asyncio.to_thread(call_with_style, fn, payload, call_style)
    if inspect.isawaitable(result):
        return await result
    return result
