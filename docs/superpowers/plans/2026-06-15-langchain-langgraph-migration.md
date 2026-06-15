# LangChain and LangGraph Migration Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the legacy engine and hand-written ReAct orchestration with one tested LangChain-backed LangGraph workflow, remove duplicate and obsolete code, and publish the result on the `langgraph` branch.

**Architecture:** LangChain chat and embedding models are created behind small project adapters. A compiled `StateGraph` owns the NL2SQL lifecycle: intent parsing, metric retrieval, schema retrieval, SQL generation, validation/retry, optional execution, explanation, and finalization. Runtime-only dependencies are injected with `context_schema`; request and workflow data remain in typed graph state.

**Tech Stack:** Python 3.12+, LangChain, LangGraph, Google Generative AI integrations, PostgreSQL/asyncpg/pgvector, sqlglot, unittest.

---

## Chunk 1: Runtime and graph contracts

### Task 1: Define model and runtime dependency boundaries

**Files:**
- Modify: `core/llm.py`
- Create: `core/embeddings.py`
- Modify: `graph/context.py`
- Test: `tests/test_llm_protocol.py`
- Test: `tests/test_embedding_client.py`

- [ ] Add failing tests for LangChain chat and embedding adapters.
- [ ] Implement environment-backed LangChain model factories.
- [ ] Keep provider objects in `GraphContext`, never in graph state.
- [ ] Run targeted tests.

### Task 2: Define complete graph state and routing

**Files:**
- Modify: `graph/state.py`
- Replace: `graph/router.py`
- Test: `tests/test_graph_state.py`
- Create: `tests/test_graph_routing.py`

- [ ] Add failing state and routing tests.
- [ ] Add retrieval, authorization, retry, execution, error, and output fields.
- [ ] Implement deterministic routes after retrieval, validation, and execution.
- [ ] Run targeted tests.

## Chunk 2: Complete workflow

### Task 3: Implement all graph nodes

**Files:**
- Replace: `graph/node.py`
- Modify: `graph/tools/sql_store.py`
- Modify: `graph/tools/sql_generator.py`
- Modify: `graph/tools/validate_sql.py`
- Modify: `graph/tools/execute_sql.py`
- Modify: `graph/tools/explain_result.py`
- Create: `tests/test_graph_nodes.py`

- [ ] Write failing unit tests for every node and retry invalidation.
- [ ] Implement async partial-state updates.
- [ ] Enforce non-empty `allowed_tables` before SQL validation or execution.
- [ ] Convert tool exceptions to terminal graph errors.
- [ ] Run node tests.

### Task 4: Compile and expose the graph pipeline

**Files:**
- Replace: `graph/pipeline.py`
- Create: `tests/test_graph_pipeline.py`

- [ ] Add failing end-to-end tests with fake dependencies.
- [ ] Build and compile the graph with input, output, and context schemas.
- [ ] Add a public `run_nl2sql` entry point with recursion limits.
- [ ] Verify validation retry, no-execute, execute, and failure paths.

## Chunk 3: LangChain integrations and public interface

### Task 5: Move embeddings to LangChain

**Files:**
- Replace: `rag/embedding_client.py`
- Modify: `graph/tools/sql_store.py`
- Modify: `scripts/rebuild_embeddings.py`
- Test: `tests/test_embedding_client.py`

- [ ] Add failing tests for async LangChain embedding calls and dimensions.
- [ ] Implement the embedding adapter and factory.
- [ ] Inject embeddings through graph context into retrieval nodes.
- [ ] Run RAG tests.

### Task 6: Replace CLI and documentation

**Files:**
- Replace: `main.py`
- Replace: `README.md`
- Create: `.env.example`
- Create: `pyproject.toml`
- Modify: `.gitignore`
- Modify: `tests/test_main_cli.py`

- [ ] Add failing CLI tests for the single LangGraph path.
- [ ] Implement CLI around `graph.pipeline.run_nl2sql`.
- [ ] Document installation, configuration, architecture, and usage.
- [ ] Add explicit runtime dependencies.

## Chunk 4: Cleanup and delivery

### Task 7: Remove obsolete implementations and generated files

**Files:**
- Delete: `agent/`
- Delete: legacy `engine/pipeline.py`, `engine/sql_generator.py`, `engine/executor.py`, `engine/metrics.py`
- Delete: `graph/tools/registry.py`, `graph/tools/descriptions.py`
- Delete: `core/google_client.py`, `core/stream_chat.py`
- Delete: `note/`, obsolete plan files, `summary.md`, tracked `__pycache__` files
- Preserve: `engine/models.py`, `engine/intent_parser.py`, database/catalog/RAG modules

- [ ] Update imports and tests before deletion.
- [ ] Remove duplicate and unreachable code.
- [ ] Stop tracking `.env` while preserving the local file.
- [ ] Confirm no imports reference deleted modules.

### Task 8: Verify, commit, and push

- [ ] Run the complete test suite.
- [ ] Run compile checks and search for stale imports/secrets/generated files.
- [ ] Review the final diff for unrelated user changes.
- [ ] Commit the migration on `langgraph`.
- [ ] Push `langgraph` to `origin`.
