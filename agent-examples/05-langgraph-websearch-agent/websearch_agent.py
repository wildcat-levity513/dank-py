#!/usr/bin/env python3
"""
Agent: websearch-agent

What it does:
- Uses a LangGraph flow with a SerpAPI-backed search tool.
- Decides when to call the tool and returns a final natural-language answer.

How to call:
- Entry function: `run(prompt, verbose=False)`
- Returns: final response string.
"""

__all__ = ["graph", "run", "State", "web_search"]

import argparse
import os
import sys
from typing import Annotated, TypedDict

from dotenv import load_dotenv
from langchain_core.tracers import ConsoleCallbackHandler
from langchain_community.utilities import SerpAPIWrapper
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI
from langgraph.graph import StateGraph, START, END
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode, tools_condition

load_dotenv()


# -----------------------------------------------------------------------------
# State
# -----------------------------------------------------------------------------

class State(TypedDict):
    messages: Annotated[list, add_messages]


# -----------------------------------------------------------------------------
# Tools
# -----------------------------------------------------------------------------

@tool
def web_search(query: str) -> str:
    """Search the web for up-to-date information. Use for current events, recent news, or facts that may have changed."""
    return SerpAPIWrapper().run(query)


tools = [web_search]

# -----------------------------------------------------------------------------
# Graph
# -----------------------------------------------------------------------------

llm = ChatOpenAI(model="gpt-4o-mini", temperature=0)
llm_with_tools = llm.bind_tools(tools)


def chatbot(state: State):
    return {"messages": [llm_with_tools.invoke(state["messages"])]}


builder = StateGraph(State)
builder.add_node("chatbot", chatbot)
builder.add_node("tools", ToolNode(tools))
builder.add_edge(START, "chatbot")
builder.add_conditional_edges("chatbot", tools_condition)
builder.add_edge("tools", "chatbot")
graph = builder.compile()


def run(prompt: str, *, verbose: bool = False) -> str:
    """
    Run the agent with a prompt and return the final response text.

    Args:
        prompt: The user's question or request.
        verbose: If True, print graph steps and LLM traces.

    Returns:
        The agent's response as a string.
    """
    kwargs: dict = {}
    if verbose:
        kwargs["print_mode"] = ["updates", "values"]
        kwargs["config"] = {"callbacks": [ConsoleCallbackHandler()]}
    state = graph.invoke({"messages": [{"role": "user", "content": prompt}]}, **kwargs)
    return state["messages"][-1].content


# -----------------------------------------------------------------------------
# CLI
# -----------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("prompt", nargs="*", default=["What are the latest developments in AI?"])
    parser.add_argument("-v", "--verbose", action="store_true", help="Show graph steps and LLM tool calls")
    args = parser.parse_args()

    if not os.environ.get("OPENAI_API_KEY"):
        print("Error: OPENAI_API_KEY not set. Add it to .env", file=sys.stderr)
        sys.exit(1)
    if not os.environ.get("SERPAPI_API_KEY"):
        print("Error: SERPAPI_API_KEY not set. Add it to .env", file=sys.stderr)
        sys.exit(1)

    prompt = " ".join(args.prompt)
    print(run(prompt, verbose=args.verbose))
