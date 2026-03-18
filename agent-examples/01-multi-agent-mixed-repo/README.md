# 01 - Multi-Agent Mixed Repo

This example shows how one `dank.config.json` can manage multiple Python agents with different frameworks and invocation styles.

## What Is In This Project

| Agent ID | File | Framework/Style | What it does |
| --- | --- | --- | --- |
| `langchain-tool-agent` | `langchain_tool_agent.py` | LangChain object (`agent.invoke`) | Counts words/keyword hits and returns a structured response |
| `langgraph-state-agent` | `langgraph_state_agent.py` | LangGraph function (`run`) | Routes prompt through a small state graph and returns route-aware output |
| `custom-openai-agent` | `custom_openai_agent.py` | Direct OpenAI function (`run`) | Calls OpenAI directly and returns a normalized JSON response |

## Why This Example Matters

It demonstrates all of the following in one repo:
- multi-agent inspect/deps/validation
- bundle-based multiplexing
- per-agent routing in a bundled container
- same runtime contract across mixed frameworks

## Run From Scratch

```bash
cd /dank-py/agent-examples/01-multi-agent-mixed-repo
cp .env.example .env
# Set OPENAI_API_KEY in .env

dank-py auto-init --strict
dank-py run
```

## Bundle vs Separate Behavior

This project currently includes a configured bundle in `dank.config.json`:
- bundle name: `all-agents`
- members: all 3 agents
- routing mode: `default`

### Scenario A: Full bundle (current config)

`dank-py run` starts one container (single port), and `/prompt` is multiplexed by header:

```bash
curl -X POST http://localhost:3000/prompt \
  -H "Content-Type: application/json" \
  -H "x-dank-agent-id: langchain-tool-agent" \
  -d '{"prompt":"Plan a rollout for our support assistant.","keyword":"assistant"}'

curl -X POST http://localhost:3000/prompt \
  -H "Content-Type: application/json" \
  -H "x-dank-agent-id: langgraph-state-agent" \
  -d '{"prompt":"Give me a 5-step deployment checklist."}'

curl -X POST http://localhost:3000/prompt \
  -H "Content-Type: application/json" \
  -H "x-dank-agent-id: custom-openai-agent" \
  -d '{"prompt":"In two sentences, explain your primary responsibility."}'
```

Notes:
- With `prompt_routing: "default"`, header is optional.
- If no `default_agent` is set, runtime falls back to the first bundle member.

### Scenario B: All agents separate containers

Two easy ways to run separate containers:

1) Remove/empty `bundles` in `dank.config.json`, then run:
```bash
dank-py run
```

2) Or run specific agents one-by-one:
```bash
dank-py run --agent langchain-tool-agent --detached
dank-py run --agent langgraph-state-agent --detached
dank-py run --agent custom-openai-agent --detached
```

In separate mode each container gets its own host port (`3000`, `3001`, `3002`, ...).

### Scenario C: Partial bundle (for example 2 of 3 agents)

If only two agents are bundled, default `dank-py run` starts:
- one bundled container for those two agents (single shared port)
- one separate container for the unbundled agent (next free port)

Example config fragment:

```json
"bundles": [
  {
    "name": "lang-agents",
    "agents": ["langchain-tool-agent", "langgraph-state-agent"],
    "prompt_routing": "default",
    "default_agent": "langchain-tool-agent"
  }
]
```

Then:
- call bundled members at bundle port with `x-dank-agent-id`
- call unbundled agent at its own port normally

## Useful Commands While Testing

```bash
dank-py status
dank-py logs
# or
# dank-py logs --follow all-agents

dank-py stop
dank-py clean
```
