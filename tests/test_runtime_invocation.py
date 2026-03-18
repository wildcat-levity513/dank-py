from __future__ import annotations

import asyncio
import json

import pytest

from dank_py.lib.config.models import AgentConfig, EntryConfig, IOConfig
from dank_runtime.engine import InvocationError, call_with_style, resolve_callable, invoke
from dank_py.lib.runtime.generator import render_index


class AgentObj:
    def invoke(self, payload):
        return {"response": f"ok:{payload['prompt']}"}


async def _async_agent(payload):
    return {"response": payload["prompt"]}


def test_resolve_callable_auto_method():
    target = AgentObj()
    fn = resolve_callable(target, method=None, call_type="auto")
    assert callable(fn)
    assert fn({"prompt": "x"})["response"] == "ok:x"


def test_call_with_style_kwargs():
    def fn(prompt=None):
        return {"response": prompt}

    result = call_with_style(fn, {"prompt": "hello"}, call_style="kwargs")
    assert result["response"] == "hello"


def test_call_with_style_kwargs_ignores_unknown_fields():
    def fn(prompt=None):
        return {"response": prompt}

    result = call_with_style(fn, {"prompt": "hello", "model": "gpt-4o-mini"}, call_style="kwargs")
    assert result["response"] == "hello"


def test_call_with_style_kwargs_requires_dict():
    def fn(**_kwargs):
        return True

    with pytest.raises(InvocationError):
        call_with_style(fn, "not-dict", call_style="kwargs")


def test_async_invoke():
    fn = resolve_callable(_async_agent, method=None, call_type="callable")
    result = asyncio.run(invoke(fn, {"prompt": "hello"}, call_style="single_arg"))
    assert result["response"] == "hello"


def test_render_index_contains_agent_spec():
    agent = AgentConfig(
        name="demo",
        entry=EntryConfig(file="app/agent.py", symbol="agent", method=None, call_type="auto", call_style="auto"),
        io=IOConfig(),
    )
    rendered = render_index(agent)
    assert "AGENT_SPEC" in rendered
    assert json.dumps("demo") in rendered
