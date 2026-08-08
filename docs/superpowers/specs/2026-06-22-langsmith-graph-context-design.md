# LangSmith Studio GraphContext Compatibility Design

> **Historical / superseded:** Retained for the runtime-only-context schema decision. The current Studio adapter exports the native Analysis Agent graph with an isolated offline development context.

## Problem

The compiled graph declares `GraphContext` as its `context_schema`. The current dataclass exposes `LLMProtocol` and `EmbeddingClientProtocol` as schema fields. Pydantic cannot generate JSON Schema for those runtime-only objects, so the LangGraph API cannot filter Studio-provided context. LangSmith Studio then passes `thread_id` into `GraphContext(**context)`, causing the reported `TypeError`.

Adding a `thread_id` dataclass field would only hide the immediate symptom. Schema generation would remain broken and other API-owned fields could fail later.

## Design

Replace the dataclass with a frozen Pydantic model while preserving its public attribute interface.

- Allow arbitrary types so injected LLM and embedding test doubles remain valid.
- Ignore unknown fields so API-owned context such as `thread_id` cannot break construction.
- Create LLM and embedding dependencies with existing default factories for Studio runs.
- Exclude those runtime dependencies from serialization and JSON Schema.
- Keep `dsn`, `timeout_ms`, `max_limit`, and `max_validation_attempts` as normal schema fields.

Graph nodes retain the existing `runtime.context.<field>` access pattern.

## Data Flow

For Studio runs, LangGraph API reads the generated context JSON Schema and filters API-owned fields. Pydantic then constructs the context and invokes the runtime-client default factories.

For CLI, FastAPI, and tests, `run_nl2sql()` continues to pass explicit LLM and embedding instances when provided, preserving dependency injection.

## Error Handling

Missing provider credentials continue to fail through `create_llm()` and `create_embedding_client()`. The fix only prevents unrelated Studio metadata from being interpreted as application context.

## Tests

Regression tests will verify:

1. `graph.get_context_jsonschema()` succeeds.
2. The schema excludes `llm` and `embeddings` but retains scalar settings.
3. `GraphContext(thread_id="...")` accepts Studio metadata when factories are mocked.
4. Explicit fake LLM and embedding dependencies remain accepted.
5. The full existing unit test suite remains green.

The regression test must fail against the current dataclass before production code changes.

## Scope

This change only addresses LangSmith Studio runtime-context compatibility. It does not change graph state, checkpoints, tenant handling, database schema, or provider selection.
