# Agent: langgraph-state-agent
# What it does:
# - Accepts a prompt and routes it through a LangGraph state machine.
# - Route selection:
#   - "plan" route for prompts containing words like `plan`, `steps`, `roadmap`, `deploy`.
#   - "analyze" route for all other prompts.
# - Each route generates a response (live via OpenAI when available, deterministic fallback otherwise).
#
# How to call:
# - Entry function: `run(prompt, user_id=None, conversation_id=None)`
# - Returns: string response.

from __future__ import annotations

import os
from typing import TypedDict

from dotenv import load_dotenv
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from langgraph.graph import END, START, StateGraph

load_dotenv()


class GraphState(TypedDict, total=False):
    prompt: str
    route: str
    response: str
    user_id: str
    conversation_id: str


def _classify(state: GraphState) -> GraphState:
    prompt = state.get("prompt", "").strip().lower()
    route = "plan" if any(token in prompt for token in ("plan", "steps", "roadmap", "deploy")) else "analyze"
    return {"route": route}


def _llm() -> ChatOpenAI | None:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        return None
    return ChatOpenAI(
        model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
        temperature=0.2,
        api_key=api_key,
    )


def _analyze_node(state: GraphState) -> GraphState:
    prompt = state.get("prompt", "")
    llm = _llm()
    if llm is None:
        words = prompt.split()
        preview = " ".join(words[:10])
        return {"response": f"[langgraph-fallback:analyze] {preview} ({len(words)} words)"}

    result = llm.invoke(
        [
            SystemMessage(
                content=(
                    "You are an AI deployment analyst. "
                    "Provide a concise analysis with key risks and constraints."
                )
            ),
            HumanMessage(content=prompt),
        ]
    )
    return {"response": str(result.content)}


def _plan_node(state: GraphState) -> GraphState:
    prompt = state.get("prompt", "")
    llm = _llm()
    if llm is None:
        return {
            "response": (
                "[langgraph-fallback:plan] 1) Define target environment "
                "2) Validate dependencies 3) Build image 4) Deploy and monitor."
            )
        }

    result = llm.invoke(
        [
            SystemMessage(
                content=(
                    "You are an AI platform engineer. "
                    "Return a pragmatic step-by-step implementation plan."
                )
            ),
            HumanMessage(content=prompt),
        ]
    )
    return {"response": str(result.content)}


def _route_edge(state: GraphState) -> str:
    return state.get("route", "analyze")


_builder = StateGraph(GraphState)
_builder.add_node("classify", _classify)
_builder.add_node("analyze", _analyze_node)
_builder.add_node("plan", _plan_node)
_builder.add_edge(START, "classify")
_builder.add_conditional_edges(
    "classify",
    _route_edge,
    {
        "analyze": "analyze",
        "plan": "plan",
    },
)
_builder.add_edge("analyze", END)
_builder.add_edge("plan", END)
graph = _builder.compile()


def run(prompt: str, user_id: str | None = None, conversation_id: str | None = None) -> str:
    state: GraphState = {"prompt": prompt}
    if user_id:
        state["user_id"] = user_id
    if conversation_id:
        state["conversation_id"] = conversation_id
    result = graph.invoke(state)
    return str(result.get("response", ""))
