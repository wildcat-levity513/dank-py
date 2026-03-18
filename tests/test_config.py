from __future__ import annotations

import json

import pytest

from dank_py.lib.config.loader import ConfigLoadError, load_config, select_agent


def test_load_config_success(tmp_path):
    config = {
        "name": "demo",
        "version": "1",
        "agents": [
            {
                "name": "support-agent",
                "entry": {
                    "file": "app/agent.py",
                    "symbol": "agent",
                    "method": None,
                    "call_type": "auto",
                    "call_style": "auto",
                },
                "io": {
                    "input": {"model": None, "schema": None},
                    "output": {"model": None, "schema": None},
                    "strict_output": True,
                },
            }
        ],
    }
    config_path = tmp_path / "dank.config.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")

    loaded = load_config(config_path)
    agent = select_agent(loaded, "support-agent")

    assert loaded.config.name == "demo"
    assert agent.name == "support-agent"


def test_load_config_missing_agents(tmp_path):
    config_path = tmp_path / "dank.config.json"
    config_path.write_text(json.dumps({"name": "bad", "agents": []}), encoding="utf-8")

    with pytest.raises(ConfigLoadError):
        load_config(config_path)


def test_select_agent_not_found(tmp_path):
    config = {
        "agents": [
            {
                "name": "a",
                "entry": {
                    "file": "app/agent.py",
                    "symbol": "agent",
                    "method": None,
                    "call_type": "auto",
                    "call_style": "auto",
                },
            }
        ]
    }
    config_path = tmp_path / "dank.config.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")

    loaded = load_config(config_path)
    with pytest.raises(ConfigLoadError):
        select_agent(loaded, "missing")


def test_load_config_rejects_bundle_required_routing_with_default_agent(tmp_path):
    config = {
        "name": "demo",
        "agents": [
            {
                "name": "a",
                "entry": {
                    "file": "app/agent.py",
                    "symbol": "agent",
                    "method": None,
                    "call_type": "auto",
                    "call_style": "auto",
                },
            }
        ],
        "bundles": [
            {
                "name": "core",
                "agents": ["a"],
                "prompt_routing": "required",
                "default_agent": "a",
            }
        ],
    }
    config_path = tmp_path / "dank.config.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")

    with pytest.raises(ConfigLoadError):
        load_config(config_path)


def test_load_config_rejects_bundle_default_agent_outside_bundle(tmp_path):
    config = {
        "name": "demo",
        "agents": [
            {
                "name": "a",
                "entry": {
                    "file": "app/agent.py",
                    "symbol": "agent",
                    "method": None,
                    "call_type": "auto",
                    "call_style": "auto",
                },
            },
            {
                "name": "b",
                "entry": {
                    "file": "app/agent.py",
                    "symbol": "agent",
                    "method": None,
                    "call_type": "auto",
                    "call_style": "auto",
                },
            }
        ],
        "bundles": [
            {
                "name": "core",
                "agents": ["a"],
                "prompt_routing": "default",
                "default_agent": "b",
            }
        ],
    }
    config_path = tmp_path / "dank.config.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")

    with pytest.raises(ConfigLoadError):
        load_config(config_path)


def test_load_config_rejects_legacy_docker_section(tmp_path):
    config = {
        "name": "demo",
        "agents": [
            {
                "name": "a",
                "entry": {
                    "file": "app/agent.py",
                    "symbol": "agent",
                    "method": None,
                    "call_type": "auto",
                    "call_style": "auto",
                },
            }
        ],
        "docker": {
            "bundles": [],
        },
    }
    config_path = tmp_path / "dank.config.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")

    with pytest.raises(ConfigLoadError):
        load_config(config_path)
