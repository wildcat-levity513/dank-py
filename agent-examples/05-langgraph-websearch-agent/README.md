# 05 - LangGraph Websearch Agent

## What This Example Shows
This example demonstrates a LangGraph tool-calling agent that can use web search for current information.

It is a reference for function entrypoints (`run(...)`) that orchestrate:
- graph state
- tool invocation
- model response synthesis

## Agent Behavior
`websearch_agent.py`:
- Defines a `web_search` tool backed by SerpAPI.
- Uses a LangGraph state graph with tool routing.
- Exposes `run(prompt, verbose=False)` returning a final string response.

## Run From Scratch
```bash
cd /dank-py/agent-examples/05-langgraph-websearch-agent
cp .env.example .env
# Set OPENAI_API_KEY and SERPAPI_API_KEY for live websearch mode

dank-py auto-init --strict
dank-py run
```

## Prompt Test
```bash
curl -X POST http://localhost:3000/prompt \
  -H "Content-Type: application/json" \
  -d '{"prompt":"What are the latest developments in AI?"}'
```
