# Agent: langchain-tool-agent
# What it does:
# - Accepts a prompt plus optional keyword/context IDs.
# - Uses local tools to compute prompt metrics (word count + exact keyword matches).
# - Uses those metrics to produce an operations-focused response via OpenAI chat model.
#
# How to call:
# - Entry symbol: `agent`
# - Method: `invoke(prompt, keyword="ai", user_id=None, conversation_id=None)`
# - Returns: dict with `response`, metrics, framework metadata, and mode/model fields.

from __future__ import annotations

import os
from typing import Any

from dotenv import load_dotenv
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI

load_dotenv()


@tool
def word_count(text: str) -> int:
    """Count words in a text."""
    return len([w for w in text.split() if w.strip()])


@tool
def keyword_count(text: str, keyword: str) -> int:
    """Count case-insensitive keyword matches."""
    needle = keyword.strip().lower()
    if not needle:
        return 0
    tokens = [t.strip(".,!?;:\"'()[]{}") for t in text.lower().split()]
    return sum(1 for token in tokens if token == needle)


class LangChainToolAgent:
    def __init__(self) -> None:
        self.model_name = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

    def invoke(
        self,
        prompt: str,
        keyword: str = "ai",
        user_id: str | None = None,
        conversation_id: str | None = None,
    ) -> dict[str, Any]:
        total_words = int(word_count.invoke({"text": prompt}))
        matches = int(keyword_count.invoke({"text": prompt, "keyword": keyword}))
        api_key = os.getenv("OPENAI_API_KEY")

        if api_key:
            llm = ChatOpenAI(
                model=self.model_name,
                temperature=0.2,
                api_key=api_key,
            )
            system = (
                "You are a concise operations assistant for deployed AI agents. "
                "Use the provided prompt metrics to give a practical response in 2-4 sentences."
            )
            human = (
                f"User prompt: {prompt}\n"
                f"Prompt word count: {total_words}\n"
                f"Exact '{keyword}' matches: {matches}\n"
                "Answer the user request directly and mention one concrete next step."
            )
            model_result = llm.invoke([SystemMessage(content=system), HumanMessage(content=human)])
            response = str(model_result.content)
            mode = "live"
        else:
            response = (
                f"[langchain-fallback] Prompt has {total_words} words and {matches} exact matches for '{keyword}'. "
                "Set OPENAI_API_KEY to enable live LLM responses."
            )
            mode = "fallback"

        return {
            "response": response,
            "framework": "langchain",
            "tool_used": "word_count+keyword_count",
            "word_count": total_words,
            "keyword_matches": matches,
            "mode": mode,
            "model": self.model_name,
            "user_id": user_id,
            "conversation_id": conversation_id,
        }


agent = LangChainToolAgent()
