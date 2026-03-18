# dank-py Agent Examples

These are production-style reference projects for different agent frameworks and runtime patterns supported by `dank-py`.

Each example is a standalone project with:
- agent code
- `dank.config.json`
- `requirements.txt` + `requirements.lock.txt`
- `.dankignore`
- `.env.example`
- usage notes in a local `README.md`

## Quick Start (Any Example)

```bash
cd /dank-py/agent-examples/<example-dir>
cp .env.example .env
# fill required keys

dank-py auto-init --strict
dank-py run
```

## Example Matrix

| Folder | Framework(s) | Container Pattern | Requires API keys | Best for |
| --- | --- | --- | --- | --- |
| `01-multi-agent-mixed-repo` | LangChain + LangGraph + custom OpenAI | Bundle and separate-container scenarios | Yes (`OPENAI_API_KEY`) | Multi-agent config and routing behavior |
| `02-crewai-kickoff-agent` | CrewAI | Single container | Yes (`OPENAI_API_KEY`) | Object-method entry (`kickoff`) patterns |
| `03-pydanticai-structured-agent` | PydanticAI | Single container | Yes (`OPENAI_API_KEY`) | Typed input/output model references in config |
| `04-llamaindex-query-agent` | LlamaIndex (+ OpenAI models) | Single container | Yes (`OPENAI_API_KEY`) | Retrieval/query engine agent wiring |
| `05-langgraph-websearch-agent` | LangGraph + web search tool calling | Single container | Yes (`OPENAI_API_KEY`, `SERPAPI_API_KEY`) | Tool-enabled, external-API agent workflows |

## Picking the Right Example

- Start with `05-langgraph-websearch-agent` if you want a realistic tool-calling agent.
- Start with `01-multi-agent-mixed-repo` if you want to learn bundling and multiplexing.
- Start with `03-pydanticai-structured-agent` if you care about strict typed contracts.
- Start with `02-crewai-kickoff-agent` or `04-llamaindex-query-agent` for framework-specific integration templates.

## What To Compare Across Examples

When adapting one of these templates, compare:
- `entry.file`, `entry.symbol`, `entry.method`
- `call_type` and `call_style`
- `io.input` and `io.output` model/schema strategy
- bundling choices in `bundles[]`

## Notes

- `auto-init --strict` validates that lock + config + runtime invocation work together.
- Examples intentionally keep framework code small so config/runtime behavior is easy to inspect.
- These directories are examples, not unit/integration test fixtures.
