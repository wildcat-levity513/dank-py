# Agent: pydanticai-agent
# What it does:
# - Validates incoming payloads with `PromptInput`.
# - Produces structured responses that satisfy `PromptOutput`.
# - Uses PydanticAI for live output when configured, with deterministic mock fallback.
#
# How to call:
# - Entry symbol: `agent`
# - Method: `invoke(payload)`
# - Returns: dict matching `PromptOutput`.

from __future__ import annotations

import os
from typing import Literal

from dotenv import load_dotenv
from pydantic import BaseModel, Field

load_dotenv()

try:
    from pydantic_ai import Agent as PydanticAIAgent
except Exception:  # pragma: no cover
    PydanticAIAgent = None


class PromptInput(BaseModel):
    prompt: str
    user_id: str | None = None
    conversation_id: str | None = None
    urgency: Literal["low", "normal", "high"] = "normal"


class PromptOutput(BaseModel):
    response: str
    intent: Literal["question", "task", "other"]
    confidence: float = Field(ge=0.0, le=1.0)
    framework: str = "pydanticai"
    mode: Literal["mock", "live"]


class StructuredSupportAgent:
    def __init__(self) -> None:
        self._agent = None
        if PydanticAIAgent is None or not os.getenv("OPENAI_API_KEY"):
            return

        model_name = os.getenv("PYDANTICAI_MODEL", "openai:gpt-4o-mini")
        try:
            self._agent = PydanticAIAgent(
                model_name,
                output_type=PromptOutput,
                system_prompt=(
                    "Classify user intent and respond clearly. "
                    "Return structured output matching PromptOutput."
                ),
            )
        except Exception:
            self._agent = None

    def invoke(self, payload: dict | PromptInput) -> dict:
        data = PromptInput.model_validate(payload)

        if self._agent is None:
            prompt_lower = data.prompt.lower().strip()
            if "?" in data.prompt:
                intent = "question"
            elif prompt_lower.startswith(("create", "build", "do", "generate")):
                intent = "task"
            else:
                intent = "other"

            output = PromptOutput(
                response=f"[mock-pydanticai:{data.urgency}] {data.prompt}",
                intent=intent,
                confidence=0.64,
                mode="mock",
            )
            return output.model_dump()

        result = self._agent.run_sync(data.prompt)
        raw_output = getattr(result, "output", result)
        parsed = PromptOutput.model_validate(raw_output)
        return parsed.model_copy(update={"mode": "live"}).model_dump()


agent = StructuredSupportAgent()
