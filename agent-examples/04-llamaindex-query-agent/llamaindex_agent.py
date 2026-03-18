# Agent: llamaindex-agent
# What it does:
# - Builds a small in-memory LlamaIndex over static docs.
# - Answers user questions using an OpenAI-backed query engine.
# - Falls back to deterministic mock output if initialization/query fails.
#
# How to call:
# - Entry symbol: `agent`
# - Method: `run(prompt, user_id=None, conversation_id=None)`
# - Returns: dict with `response`, `framework`, `mode`, and optional context IDs.

from __future__ import annotations

import os
from typing import Any

from dotenv import load_dotenv

load_dotenv()

try:
    from llama_index.core import Document, Settings, VectorStoreIndex
    from llama_index.embeddings.openai import OpenAIEmbedding
    from llama_index.llms.openai import OpenAI
except Exception:  # pragma: no cover
    Document = None
    Settings = None
    VectorStoreIndex = None
    OpenAIEmbedding = None
    OpenAI = None


DOCS = [
    "Dank Cloud deploys endpoint-callable AI agent containers.",
    "Dank JS and dank-py expose standard /health and /prompt endpoints.",
    "dank-py focuses on framework-agnostic Python agent containerization.",
]


class LlamaIndexQueryAgent:
    def __init__(self) -> None:
        self._query_engine = None

        if None in (Document, Settings, VectorStoreIndex, OpenAIEmbedding, OpenAI):
            return
        if not os.getenv("OPENAI_API_KEY"):
            return

        try:
            model_name = os.getenv("LLAMAINDEX_MODEL", "gpt-4o-mini")
            embedding_model = os.getenv("LLAMAINDEX_EMBEDDING_MODEL", "text-embedding-3-small")

            Settings.embed_model = OpenAIEmbedding(model=embedding_model)
            Settings.llm = OpenAI(model=model_name)
            documents = [Document(text=text) for text in DOCS]
            index = VectorStoreIndex.from_documents(documents)
            self._query_engine = index.as_query_engine(similarity_top_k=2)
        except Exception:
            self._query_engine = None

    def run(
        self,
        prompt: str,
        user_id: str | None = None,
        conversation_id: str | None = None,
    ) -> dict[str, Any]:
        if self._query_engine is None:
            return {
                "response": f"[mock-llamaindex] Unable to initialize query engine. Prompt: {prompt}",
                "framework": "llamaindex",
                "mode": "mock",
                "user_id": user_id,
                "conversation_id": conversation_id,
            }

        try:
            result = self._query_engine.query(prompt)
            return {
                "response": str(result),
                "framework": "llamaindex",
                "mode": "live",
                "user_id": user_id,
                "conversation_id": conversation_id,
            }
        except Exception as exc:
            return {
                "response": f"[fallback-llamaindex] {prompt}",
                "framework": "llamaindex",
                "mode": "mock",
                "error": str(exc),
                "user_id": user_id,
                "conversation_id": conversation_id,
            }


agent = LlamaIndexQueryAgent()
