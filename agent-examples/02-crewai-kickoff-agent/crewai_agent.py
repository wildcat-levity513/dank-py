# Agent: crewai-agent
# What it does:
# - Exposes a CrewAI workflow through `kickoff(...)`.
# - Produces concise support responses for a target audience.
# - Falls back to deterministic mock output when CrewAI/OpenAI is unavailable.
#
# How to call:
# - Entry symbol: `agent`
# - Method: `kickoff(prompt, audience=\"general\", user_id=None, conversation_id=None)`
# - Returns: dict with `response`, `framework`, `mode`, and optional context IDs.

from __future__ import annotations

import os
from typing import Any

from dotenv import load_dotenv

load_dotenv()

try:
    from crewai import Agent, Crew, Process, Task
except Exception:  # pragma: no cover
    Agent = None
    Crew = None
    Process = None
    Task = None


class CrewKickoffAgent:
    def kickoff(
        self,
        prompt: str,
        audience: str = "general",
        user_id: str | None = None,
        conversation_id: str | None = None,
    ) -> dict[str, Any]:
        if Agent is None or Crew is None or Task is None or Process is None:
            return {
                "response": f"[mock-crewai] ({audience}) {prompt}",
                "framework": "crewai",
                "mode": "mock",
                "user_id": user_id,
                "conversation_id": conversation_id,
            }

        if not os.getenv("OPENAI_API_KEY"):
            return {
                "response": f"[mock-crewai-no-key] ({audience}) {prompt}",
                "framework": "crewai",
                "mode": "mock",
                "user_id": user_id,
                "conversation_id": conversation_id,
            }

        writer = Agent(
            role="Customer Support Writer",
            goal="Write concise, helpful support responses.",
            backstory="You are precise and practical.",
            allow_delegation=False,
            verbose=False,
        )

        task = Task(
            description=(
                f"Audience: {audience}. Write a concise response to this request: {prompt}. "
                "Use 2-4 sentences and include one actionable step."
            ),
            expected_output="2-4 sentence concise answer with one action item.",
            agent=writer,
        )

        crew = Crew(
            agents=[writer],
            tasks=[task],
            process=Process.sequential,
            verbose=False,
        )
        result = crew.kickoff()

        return {
            "response": str(result),
            "framework": "crewai",
            "mode": "live",
            "user_id": user_id,
            "conversation_id": conversation_id,
        }


agent = CrewKickoffAgent()
