# 02 - CrewAI Kickoff Agent

## What This Example Shows
This example demonstrates a CrewAI agent exposed through a method entrypoint:
- symbol: `agent`
- method: `kickoff`
- call style: kwargs payload mapping

It is a reference for framework integrations where the callable is a class method (not a top-level function).

## Agent Behavior
`crewai_agent.py` defines `CrewKickoffAgent.kickoff(...)`.
- With `OPENAI_API_KEY`: runs a live CrewAI sequential task.
- Without key: returns deterministic mock output.

## Run From Scratch
```bash
cd /dank-py/agent-examples/02-crewai-kickoff-agent
cp .env.example .env
# Optional: set OPENAI_API_KEY for live mode

dank-py auto-init --strict
dank-py run
```

## Prompt Test
```bash
curl -X POST http://localhost:3000/prompt \
  -H "Content-Type: application/json" \
  -d '{"prompt":"How do I reset my password?","audience":"enterprise admin"}'
```
