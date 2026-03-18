# 04 - LlamaIndex Query Agent

## What This Example Shows
This example demonstrates a retrieval-style LlamaIndex agent exposed through `method=run`.

It is a reference for containerizing LlamaIndex projects that build an index and query it per request.

## Agent Behavior
`llamaindex_agent.py`:
- Builds a small in-memory index from sample docs.
- Uses OpenAI-backed embedding + LLM components.
- Returns `mode: "live"` on successful initialization/query.
- Falls back to deterministic mock output if OpenAI config or initialization is unavailable.

## Run From Scratch
```bash
cd /dank-py/agent-examples/04-llamaindex-query-agent
cp .env.example .env
# Set OPENAI_API_KEY for live mode

dank-py auto-init --strict
dank-py run
```

## Prompt Test
```bash
curl -X POST http://localhost:3000/prompt \
  -H "Content-Type: application/json" \
  -d '{"prompt":"How does Dank Cloud package agents?"}'
```
