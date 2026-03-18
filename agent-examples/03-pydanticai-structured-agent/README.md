# 03 - PydanticAI Structured Agent

## What This Example Shows
This example demonstrates typed I/O contracts using Pydantic models in config:
- input model: `PromptInput`
- output model: `PromptOutput`

It is a reference for model-based validation (`io.input.model` / `io.output.model`) instead of schema-only validation.

## Agent Behavior
`pydanticai_agent.py` defines `StructuredSupportAgent.invoke(...)`.
- Input is validated by `PromptInput`.
- Output is validated/normalized by `PromptOutput`.
- Uses live PydanticAI when `OPENAI_API_KEY` is present, otherwise deterministic mock mode.

## Run From Scratch
```bash
cd /dank-py/agent-examples/03-pydanticai-structured-agent
cp .env.example .env
# Optional: set OPENAI_API_KEY for live mode

dank-py auto-init --strict
dank-py run
```

## Prompt Test
```bash
curl -X POST http://localhost:3000/prompt \
  -H "Content-Type: application/json" \
  -d '{"prompt":"Can you summarize this ticket?","urgency":"high"}'
```
