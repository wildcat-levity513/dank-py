from __future__ import annotations

import json

from dank_py.lib.cli.init import init_command
from dank_py.lib.cli.inspect import (
    apply_candidates_to_config,
    apply_entry_to_config,
    apply_top_candidate_to_config,
    inspect_command,
)


def test_init_command_creates_scaffold(tmp_path):
    project_dir = tmp_path / "sample"
    result = init_command(str(project_dir), force=False)

    assert result == project_dir.resolve()
    assert (project_dir / "dank.config.json").exists()
    assert (project_dir / ".dankignore").exists()
    dankignore = (project_dir / ".dankignore").read_text(encoding="utf-8")
    assert ".env" in dankignore
    assert ".env.*" in dankignore
    assert not (project_dir / ".gitignore").exists()
    assert not (project_dir / "requirements.txt").exists()


def test_inspect_command_returns_json(tmp_path):
    project_dir = tmp_path / "sample"
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / "agent.py").write_text(
        """
class DemoAgent:
    def invoke(self, payload):
        return payload
""",
        encoding="utf-8",
    )

    output = inspect_command(project_dir=str(project_dir), as_json=True)
    payload = json.loads(output)

    assert "entry_candidates" in payload
    assert payload["entry_candidates"]


def test_inspect_apply_top_candidate_updates_config(tmp_path):
    project_dir = tmp_path / "sample"
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / "websearch_agent.py").write_text(
        """
def run(prompt: str) -> str:
    return "ok"
""",
        encoding="utf-8",
    )
    init_command(str(project_dir), force=False)

    applied = apply_top_candidate_to_config(project_dir=str(project_dir), config_path="dank.config.json", candidate_index=0)
    assert applied is True

    config = json.loads((project_dir / "dank.config.json").read_text(encoding="utf-8"))
    entry = config["agents"][0]["entry"]
    assert entry["file"] == "websearch_agent.py"
    assert entry["symbol"] == "run"
    io = config["agents"][0]["io"]
    assert io["input"]["schema"]["type"] == "object"
    assert "prompt" in io["input"]["schema"]["required"]
    assert io["output"]["schema"]["type"] == "string"


def test_inspect_apply_entry_to_config_overrides_call_fields(tmp_path):
    project_dir = tmp_path / "sample"
    project_dir.mkdir(parents=True, exist_ok=True)
    init_command(str(project_dir), force=False)

    applied = apply_entry_to_config(
        project_dir=str(project_dir),
        config_path="dank.config.json",
        entry_values={
            "file": "agent.py",
            "symbol": "agent",
            "method": "invoke",
            "call_type": "method",
            "call_style": "single_arg",
        },
    )
    assert applied is True

    config = json.loads((project_dir / "dank.config.json").read_text(encoding="utf-8"))
    entry = config["agents"][0]["entry"]
    assert entry["file"] == "agent.py"
    assert entry["symbol"] == "agent"
    assert entry["method"] == "invoke"
    assert entry["call_type"] == "method"
    assert entry["call_style"] == "single_arg"


def test_inspect_apply_candidates_to_config_creates_multiple_agents(tmp_path):
    project_dir = tmp_path / "sample"
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / "a.py").write_text(
        """
def run(payload):
    return payload
""",
        encoding="utf-8",
    )
    (project_dir / "b.py").write_text(
        """
def invoke(payload):
    return payload
""",
        encoding="utf-8",
    )
    init_command(str(project_dir), force=False)

    count = apply_candidates_to_config(project_dir=str(project_dir), config_path="dank.config.json", min_score=75, max_agents=10)
    assert count >= 2

    config = json.loads((project_dir / "dank.config.json").read_text(encoding="utf-8"))
    assert len(config["agents"]) >= 2


def test_inspect_detects_model_candidates_and_prefills_io_model_refs(tmp_path):
    project_dir = tmp_path / "sample"
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / "websearch_agent.py").write_text(
        """
from pydantic import BaseModel

class PromptInput(BaseModel):
    prompt: str

class PromptOutput(BaseModel):
    response: str

def run(payload):
    return {"response": "ok"}
""",
        encoding="utf-8",
    )
    init_command(str(project_dir), force=False)

    output = inspect_command(project_dir=str(project_dir), as_json=True)
    payload = json.loads(output)
    model_candidates = payload.get("model_candidates", [])
    assert any(item.get("symbol") == "PromptInput" for item in model_candidates)
    assert any(item.get("symbol") == "PromptOutput" for item in model_candidates)

    count = apply_candidates_to_config(project_dir=str(project_dir), config_path="dank.config.json", min_score=75, max_agents=5)
    assert count >= 1
    config = json.loads((project_dir / "dank.config.json").read_text(encoding="utf-8"))
    io = config["agents"][0]["io"]
    assert io["input"]["model"] == "websearch_agent:PromptInput"
    assert io["output"]["model"] == "websearch_agent:PromptOutput"
    assert io["input"]["schema"] is None
    assert io["output"]["schema"] is None


def test_inspect_prefers_exported_instance_over_class_symbol(tmp_path):
    project_dir = tmp_path / "sample"
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / "agent_module.py").write_text(
        """
class DemoAgent:
    def invoke(self, prompt: str, user_id: str | None = None) -> dict:
        return {"response": prompt, "user_id": user_id}

agent = DemoAgent()
""",
        encoding="utf-8",
    )
    init_command(str(project_dir), force=False)

    payload = json.loads(inspect_command(project_dir=str(project_dir), as_json=True))
    top = payload["entry_candidates"][0]
    assert top["symbol"] == "agent"
    assert top["method"] == "invoke"
    assert top["call_type"] == "method"
    assert top["call_style"] == "kwargs"

    count = apply_candidates_to_config(project_dir=str(project_dir), config_path="dank.config.json", min_score=75, max_agents=5)
    assert count >= 1
    config = json.loads((project_dir / "dank.config.json").read_text(encoding="utf-8"))
    agent_cfg = config["agents"][0]
    assert agent_cfg["name"] == "agent-module"
    assert agent_cfg["entry"]["symbol"] == "agent"
    assert agent_cfg["entry"]["method"] == "invoke"
    assert agent_cfg["entry"]["call_type"] == "method"
    assert agent_cfg["entry"]["call_style"] == "kwargs"
