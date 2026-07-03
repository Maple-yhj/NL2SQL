# Data Agent Memory Graphiti Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a Data Agent memory layer that uses Graphiti-style temporal knowledge graph memory for tenant/user-scoped data corrections, filter rules, table guidance, and analysis learnings while preserving the existing conversation history store.

**Architecture:** Keep `graph.memory_store` as the source of truth for conversation sessions and recent turns. Add a separate `graph.data_memory` module with a protocol, null/in-memory test implementations, and a lazy Graphiti adapter so tests and local development do not require Neo4j. Add LangGraph nodes that recall data memory before planning/SQL generation and propose pending memory updates after persistence without auto-promoting global memory.

**Tech Stack:** Python 3.12, LangGraph, FastAPI, unittest, optional `graphiti-core`, existing project LLM and embedding protocols.

---

## File Structure

- Create `graph/data_memory.py`: Data memory protocol, scope helpers, null/in-memory stores, prompt formatting, conservative update proposal extraction, and lazy Graphiti adapter.
- Modify `graph/state.py`: Add `data_memories` and `pending_memory_updates` to graph state.
- Modify `graph/context.py`: Add `data_memory_store` runtime dependency and optional Graphiti config settings.
- Modify `graph/node.py`: Add `recall_data_memory_node` and `propose_memory_updates_node`; pass recalled data memories into SQL generation.
- Modify `graph/pipeline.py`: Wire new nodes into the graph between question contextualization and intent parsing, and between conversation persistence and finalization.
- Modify `graph/tools/sql_generator.py`: Accept `data_memories` and render them in a dedicated prompt section.
- Modify `pyproject.toml`: Add optional `memory = ["graphiti-core>=0.19"]` dependency group.
- Modify `.env.example` and `README.md`: Document Graphiti/Neo4j settings and safe default behavior.
- Add `tests/test_data_memory.py`: Unit tests for scoping, null/in-memory stores, Graphiti adapter lazy import behavior, memory formatting, and update extraction.
- Modify `tests/test_graph_nodes.py`, `tests/test_graph_pipeline.py`, `tests/test_sql_generator.py`, `tests/test_graph_context.py`, and `tests/test_graph_state.py`: Integration coverage for the new memory channels and graph ordering.

## Task 1: Data Memory Core

**Files:**
- Create: `graph/data_memory.py`
- Test: `tests/test_data_memory.py`

- [ ] **Step 1: Write failing tests for scope isolation and null store**

```python
import unittest

from graph.data_memory import (
    DataMemory,
    DataMemoryScope,
    NullDataMemoryStore,
    data_memory_group_id,
)


class DataMemoryScopeTests(unittest.IsolatedAsyncioTestCase):
    def test_data_memory_group_id_scopes_by_tenant_user_and_conversation(self):
        self.assertEqual(
            data_memory_group_id(
                tenant_id="demo",
                scope=DataMemoryScope.GLOBAL,
            ),
            "tenant:demo:global",
        )
        self.assertEqual(
            data_memory_group_id(
                tenant_id="demo",
                user_id="user-1",
                scope=DataMemoryScope.USER,
            ),
            "tenant:demo:user:user-1",
        )
        self.assertEqual(
            data_memory_group_id(
                tenant_id="demo",
                conversation_id="conv-1",
                scope=DataMemoryScope.CONVERSATION,
            ),
            "tenant:demo:conversation:conv-1",
        )

    def test_data_memory_group_id_rejects_cross_scope_missing_identity(self):
        with self.assertRaises(ValueError):
            data_memory_group_id(tenant_id="demo", scope=DataMemoryScope.USER)
        with self.assertRaises(ValueError):
            data_memory_group_id(tenant_id="demo", scope=DataMemoryScope.CONVERSATION)

    async def test_null_store_returns_empty_context_and_accepts_updates(self):
        store = NullDataMemoryStore()
        self.assertEqual(
            await store.search(
                tenant_id="demo",
                user_id="user-1",
                conversation_id="conv-1",
                query="gmv by seller",
                limit=5,
            ),
            [],
        )
        await store.add_episode(
            tenant_id="demo",
            user_id="user-1",
            conversation_id="conv-1",
            scope=DataMemoryScope.USER,
            name="correction",
            body={"text": "Use net GMV after refunds."},
            source_description="manual correction",
        )
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `conda run -n agents-env python -m unittest tests.test_data_memory -v`

Expected: FAIL with `ModuleNotFoundError: No module named 'graph.data_memory'`.

- [ ] **Step 3: Implement minimal data memory types**

Implement:

```python
class DataMemoryScope(str, Enum):
    GLOBAL = "global"
    USER = "user"
    CONVERSATION = "conversation"


@dataclass(frozen=True, slots=True)
class DataMemory:
    text: str
    scope: str
    source: str = ""
    score: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class DataMemoryStoreProtocol(Protocol):
    async def search(...) -> list[DataMemory]: ...
    async def add_episode(...) -> None: ...


class NullDataMemoryStore:
    async def search(...) -> list[DataMemory]:
        return []
    async def add_episode(...) -> None:
        return None
```

Also implement `data_memory_group_id()` with whitespace stripping and explicit `ValueError` for missing tenant/user/conversation identifiers.

- [ ] **Step 4: Run tests to verify they pass**

Run: `conda run -n agents-env python -m unittest tests.test_data_memory -v`

Expected: PASS.

## Task 2: Formatting and In-Memory Store

**Files:**
- Modify: `graph/data_memory.py`
- Test: `tests/test_data_memory.py`

- [ ] **Step 1: Write failing tests for recall ordering and prompt formatting**

```python
class DataMemoryStoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_in_memory_store_searches_allowed_scopes_only(self):
        store = InMemoryDataMemoryStore()
        await store.add_episode(
            tenant_id="demo",
            user_id="user-1",
            conversation_id="conv-1",
            scope=DataMemoryScope.GLOBAL,
            name="metric-rule",
            body={"text": "GMV uses paid order amount."},
            source_description="global",
        )
        await store.add_episode(
            tenant_id="demo",
            user_id="user-2",
            conversation_id="conv-1",
            scope=DataMemoryScope.USER,
            name="private-rule",
            body={"text": "User 2 prefers refunds excluded."},
            source_description="private",
        )

        results = await store.search(
            tenant_id="demo",
            user_id="user-1",
            conversation_id="conv-1",
            query="gmv refunds",
            limit=10,
        )

        self.assertEqual([item.text for item in results], ["GMV uses paid order amount."])

    def test_format_data_memories_uses_scope_and_source(self):
        rendered = format_data_memories([
            DataMemory(text="Use orders for GMV.", scope="global", source="approved"),
            DataMemory(text="Prefer last 30 days.", scope="user", source="manual"),
        ])

        self.assertIn("- [global] Use orders for GMV. (source: approved)", rendered)
        self.assertIn("- [user] Prefer last 30 days. (source: manual)", rendered)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `conda run -n agents-env python -m unittest tests.test_data_memory -v`

Expected: FAIL with missing `InMemoryDataMemoryStore` and `format_data_memories`.

- [ ] **Step 3: Implement in-memory store and formatter**

Use simple deterministic matching:

```python
query_terms = {term.lower() for term in re.findall(r"\w+", query)}
score = overlap_count / max(len(query_terms), 1)
```

Include only these namespaces:

```python
tenant:{tenant_id}:global
tenant:{tenant_id}:user:{user_id}
tenant:{tenant_id}:conversation:{conversation_id}
```

Return newest matching memories first when scores tie, cap by `limit`, and deep-copy metadata.

- [ ] **Step 4: Run tests**

Run: `conda run -n agents-env python -m unittest tests.test_data_memory -v`

Expected: PASS.

## Task 3: Graphiti Adapter and Factory

**Files:**
- Modify: `graph/data_memory.py`
- Modify: `pyproject.toml`
- Test: `tests/test_data_memory.py`

- [ ] **Step 1: Write failing tests for factory and lazy import**

```python
from unittest import mock


class GraphitiDataMemoryFactoryTests(unittest.TestCase):
    def test_factory_returns_null_store_when_graphiti_disabled(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertIsInstance(create_data_memory_store(), NullDataMemoryStore)

    def test_factory_creates_graphiti_store_when_configured(self):
        with mock.patch.dict(
            "os.environ",
            {
                "DATA_MEMORY_PROVIDER": "graphiti",
                "GRAPHITI_NEO4J_URI": "bolt://localhost:7687",
                "GRAPHITI_NEO4J_USER": "neo4j",
                "GRAPHITI_NEO4J_PASSWORD": "secret",
            },
            clear=True,
        ):
            store = create_data_memory_store()

        self.assertIsInstance(store, GraphitiDataMemoryStore)
        self.assertEqual(store.neo4j_uri, "bolt://localhost:7687")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `conda run -n agents-env python -m unittest tests.test_data_memory -v`

Expected: FAIL with missing factory/adapter.

- [ ] **Step 3: Implement factory and lazy adapter**

Add optional dependency:

```toml
[project.optional-dependencies]
dev = ["langgraph-cli[inmem]>=0.4"]
memory = ["graphiti-core>=0.19"]
```

Implement `GraphitiDataMemoryStore` so `graphiti_core` is imported inside methods, not at module import time. Constructor only stores config. `search()` should instantiate `Graphiti`, call `search(query, group_id=...)` for each allowed group, convert results with `.fact`, `.uuid`, `.valid_at`, `.invalid_at`, then close the client in `finally`.

Implement `add_episode()` with `Graphiti.add_episode(...)`, `EpisodeType.json` for dict/list bodies and `EpisodeType.text` for strings. Use `group_id=data_memory_group_id(...)`.

- [ ] **Step 4: Run tests**

Run: `conda run -n agents-env python -m unittest tests.test_data_memory -v`

Expected: PASS.

## Task 4: Runtime Context and State Channels

**Files:**
- Modify: `graph/context.py`
- Modify: `graph/state.py`
- Test: `tests/test_graph_context.py`
- Test: `tests/test_graph_state.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/test_graph_context.py`:

```python
def test_graph_context_excludes_runtime_data_memory_store_from_schema(self):
    schema = GraphContext.model_json_schema()

    self.assertNotIn("data_memory_store", schema["properties"])


def test_graph_context_preserves_explicit_data_memory_store(self):
    store = object()
    context = GraphContext(
        llm=FakeLLM(),
        embeddings=FakeEmbeddings(),
        data_memory_store=store,
    )

    self.assertIs(context.data_memory_store, store)
```

Add to `tests/test_graph_state.py`:

```python
def test_graph_state_contains_data_memory_channels(self):
    annotations = GraphOptionalState.__annotations__

    self.assertIn("data_memories", annotations)
    self.assertIn("pending_memory_updates", annotations)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `conda run -n agents-env python -m unittest tests.test_graph_context tests.test_graph_state -v`

Expected: FAIL with missing fields.

- [ ] **Step 3: Implement context/state fields**

Add `data_memory_store: SkipJsonSchema[DataMemoryStoreProtocol]` to `GraphContext` with `default_factory=create_data_memory_store`. Add serializable settings:

```python
data_memory_provider: str = ""
data_memory_recall_limit: int = 5
```

Add optional state:

```python
data_memories: list[dict[str, Any]]
pending_memory_updates: list[dict[str, Any]]
```

- [ ] **Step 4: Run tests**

Run: `conda run -n agents-env python -m unittest tests.test_graph_context tests.test_graph_state -v`

Expected: PASS.

## Task 5: Recall Node and SQL Prompt Integration

**Files:**
- Modify: `graph/node.py`
- Modify: `graph/tools/sql_generator.py`
- Test: `tests/test_graph_nodes.py`
- Test: `tests/test_sql_generator.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/test_graph_nodes.py`:

```python
async def test_recall_data_memory_reads_scoped_data_memory(self):
    class Store:
        async def search(self, **kwargs):
            self.kwargs = kwargs
            return [DataMemory(text="Use net GMV after refunds.", scope="global", source="approved")]

        async def add_episode(self, **kwargs):
            pass

    store = Store()
    rt = runtime(data_memory_store=store, data_memory_recall_limit=3)

    result = await node.recall_data_memory_node(
        {
            "question": "gmv trend",
            "contextualized_question": "gmv trend",
            "tenant_id": "demo",
            "execute": False,
            "conversation_id": "conv-1",
            "user_id": "user-1",
            "trace": [],
        },
        rt,
    )

    self.assertEqual(result["data_memories"][0]["text"], "Use net GMV after refunds.")
    self.assertEqual(store.kwargs["tenant_id"], "demo")
    self.assertEqual(store.kwargs["limit"], 3)
```

Add to `tests/test_sql_generator.py`:

```python
def test_prompt_includes_data_agent_memory_context(self):
    prompt = build_sql_prompt(
        question="show gmv",
        intent=QueryIntent(metrics=["gmv"]),
        metrics_result={"metrics": []},
        schema_result={"schema": []},
        retry_feedback=None,
        data_memories=[{"text": "Use net GMV after refunds.", "scope": "global", "source": "approved"}],
    )

    self.assertIn("[DATA AGENT MEMORY]", prompt)
    self.assertIn("Use net GMV after refunds.", prompt)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `conda run -n agents-env python -m unittest tests.test_graph_nodes tests.test_sql_generator -v`

Expected: FAIL with missing node/prompt argument.

- [ ] **Step 3: Implement recall and prompt wiring**

Add `recall_data_memory_node()` after contextualization. On error, return empty `data_memories` and trace failure. Convert dataclasses to dicts. Pass `data_memories` into `generate_sql()`.

In `build_sql_prompt()`, render `data_memories` using `format_data_memories()` under `[DATA AGENT MEMORY]`, before `[CONVERSATION CONTEXT]`.

- [ ] **Step 4: Run tests**

Run: `conda run -n agents-env python -m unittest tests.test_graph_nodes tests.test_sql_generator -v`

Expected: PASS.

## Task 6: Pipeline Ordering

**Files:**
- Modify: `graph/pipeline.py`
- Test: `tests/test_graph_pipeline.py`

- [ ] **Step 1: Write failing pipeline test**

```python
async def test_data_memory_is_recalled_before_intent_and_sql_generation(self):
    class Store:
        async def search(self, **kwargs):
            return [DataMemory(text="Use orders table for GMV.", scope="global", source="approved")]

        async def add_episode(self, **kwargs):
            pass

    with mock.patch.object(
        node, "parse_intent", new=mock.AsyncMock(return_value=QueryIntent(metrics=["gmv"]))
    ) as parser, mock.patch.object(
        node, "search_metrics", new=mock.AsyncMock(return_value=metrics_result())
    ), mock.patch.object(
        node, "search_schema", new=mock.AsyncMock(return_value=schema_result())
    ), mock.patch.object(
        node, "generate_sql", new=mock.AsyncMock(return_value="SELECT amount FROM orders")
    ) as generator, mock.patch.object(
        node, "validate_sql", new=mock.AsyncMock(return_value=valid_sql())
    ):
        result = await pipeline.run_nl2sql(
            "show gmv",
            tenant_id="demo",
            conversation_id="conv-1",
            user_id="user-1",
            llm=FakeLLM(),
            embeddings=FakeEmbeddings(),
            data_memory_store=Store(),
        )

    self.assertTrue(result["ok"])
    generator.assert_awaited_once()
    self.assertEqual(generator.await_args.kwargs["data_memories"][0]["text"], "Use orders table for GMV.")
    self.assertIn("recall_data_memory", [item["node"] for item in result["trace"]])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `conda run -n agents-env python -m unittest tests.test_graph_pipeline -v`

Expected: FAIL with missing `data_memory_store` parameter and graph node.

- [ ] **Step 3: Wire `run_nl2sql()` and graph edges**

Add optional `data_memory_store` parameter to `run_nl2sql()`. Add graph node `recall_data_memory`; edge:

```text
load_memory -> contextualize_question -> recall_data_memory -> parse_intent
```

Preserve existing error routing by routing recall failure to parse intent with empty memory, not to finalization.

- [ ] **Step 4: Run pipeline tests**

Run: `conda run -n agents-env python -m unittest tests.test_graph_pipeline -v`

Expected: PASS.

## Task 7: Pending Memory Updates

**Files:**
- Modify: `graph/data_memory.py`
- Modify: `graph/node.py`
- Modify: `graph/pipeline.py`
- Test: `tests/test_data_memory.py`
- Test: `tests/test_graph_nodes.py`

- [ ] **Step 1: Write failing tests for conservative extraction**

```python
class DataMemoryProposalTests(unittest.TestCase):
    def test_extract_pending_updates_only_for_explicit_memory_intent(self):
        updates = extract_pending_memory_updates(
            question="记住：GMV 后续默认排除退款订单",
            contextualized_question="记住：GMV 后续默认排除退款订单",
            sql="SELECT 1",
            answer="好的",
            error="",
        )

        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0]["scope"], "user")
        self.assertIn("GMV", updates[0]["text"])

    def test_extract_pending_updates_ignores_normal_questions(self):
        self.assertEqual(
            extract_pending_memory_updates(
                question="show gmv by region",
                contextualized_question="show gmv by region",
                sql="SELECT 1",
                answer="",
                error="",
            ),
            [],
        )
```

Add to `tests/test_graph_nodes.py`:

```python
async def test_propose_memory_updates_exposes_pending_updates_without_writing_global_memory(self):
    rt = runtime()
    result = await node.propose_memory_updates_node(
        {
            "question": "记住：GMV 后续默认排除退款订单",
            "contextualized_question": "记住：GMV 后续默认排除退款订单",
            "tenant_id": "demo",
            "execute": False,
            "conversation_id": "conv-1",
            "user_id": "user-1",
            "validated_sql": "SELECT 1",
            "answer": "",
            "error": "",
            "trace": [],
        },
        rt,
    )

    self.assertEqual(result["pending_memory_updates"][0]["scope"], "user")
    self.assertEqual(result["trace"][-1]["node"], "propose_memory_updates")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `conda run -n agents-env python -m unittest tests.test_data_memory tests.test_graph_nodes -v`

Expected: FAIL with missing extractor/node.

- [ ] **Step 3: Implement pending update extractor and node**

Use deterministic explicit triggers only:

```python
("记住", "请记住", "remember", "save this", "以后默认", "后续默认")
```

Produce pending records:

```python
{
    "scope": "user",
    "text": extracted_text,
    "source": "explicit_user_instruction",
    "metadata": {"requires_confirmation": True},
}
```

Do not call `add_episode()` in this task. This preserves the approved design: no automatic global promotion.

- [ ] **Step 4: Wire node before finalize**

Pipeline tail:

```text
... -> persist_memory -> propose_memory_updates -> finalize
```

- [ ] **Step 5: Run tests**

Run: `conda run -n agents-env python -m unittest tests.test_data_memory tests.test_graph_nodes tests.test_graph_pipeline -v`

Expected: PASS.

## Task 8: API Surface and Documentation

**Files:**
- Modify: `api/schemas.py`
- Modify: `README.md`
- Modify: `.env.example`
- Test: `tests/test_api_conversations.py`

- [ ] **Step 1: Write failing API/schema test**

Add to `tests/test_api_conversations.py`:

```python
def test_post_message_response_includes_pending_memory_updates(self):
    client = TestClient(create_app())
    store = InMemoryConversationStore()

    with mock.patch.dict("os.environ", {"JWT_SECRET_KEY": TEST_JWT_SECRET}), mock.patch(
        "api.routes.create_conversation_store", return_value=store
    ), mock.patch(
        "api.routes.run_nl2sql",
        new=mock.AsyncMock(return_value=graph_output(pending_memory_updates=[
            {"scope": "user", "text": "GMV excludes refunds.", "source": "explicit_user_instruction"}
        ])),
    ):
        created = client.post(
            "/api/conversations",
            headers=auth_headers(),
            json={"tenant_id": "demo", "user_id": "user-1", "title": "GMV"},
        ).json()
        response = client.post(
            f"/api/conversations/{created['conversation_id']}/messages",
            headers=auth_headers(),
            json={"tenant_id": "demo", "user_id": "user-1", "question": "记住：GMV 排除退款"},
        )

    self.assertEqual(response.status_code, 200)
    self.assertEqual(response.json()["pending_memory_updates"][0]["scope"], "user")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `conda run -n agents-env python -m unittest tests.test_api_conversations -v`

Expected: FAIL because response model does not include `pending_memory_updates`.

- [ ] **Step 3: Add response fields and docs**

Add optional `pending_memory_updates: list[dict[str, Any]] = Field(default_factory=list)` to `Nl2SqlResponse` and `ConversationNl2SqlResponse`.

Add `.env.example`:

```env
DATA_MEMORY_PROVIDER=
GRAPHITI_NEO4J_URI=bolt://localhost:7687
GRAPHITI_NEO4J_USER=neo4j
GRAPHITI_NEO4J_PASSWORD=
GRAPHITI_TELEMETRY_ENABLED=false
```

Document that Graphiti is optional; when disabled, the project uses `NullDataMemoryStore` and existing behavior is preserved.

- [ ] **Step 4: Run API tests**

Run: `conda run -n agents-env python -m unittest tests.test_api_conversations tests.test_api_nl2sql tests.test_api_auth -v`

Expected: PASS.

## Task 9: Final Verification

**Files:**
- All modified files

- [ ] **Step 1: Run focused memory and graph tests**

Run:

```powershell
conda run -n agents-env python -m unittest `
  tests.test_data_memory `
  tests.test_graph_context `
  tests.test_graph_state `
  tests.test_graph_nodes `
  tests.test_graph_pipeline `
  tests.test_sql_generator -v
```

Expected: PASS.

- [ ] **Step 2: Run full backend suite**

Run:

```powershell
conda run -n agents-env python -m unittest discover -s tests -v
```

Expected: PASS, matching the baseline of 181 passing tests plus new tests.

- [ ] **Step 3: Inspect git diff**

Run: `git diff -- graph tests api pyproject.toml README.md .env.example docs/superpowers/plans/2026-07-03-data-agent-memory-graphiti.md`

Expected: Diff only includes the planned data memory changes and documentation.

## Self-Review

- Spec coverage: The plan covers Graphiti as primary data memory framework, preserves tenant/user isolation through `group_id`, keeps existing conversation memory, adds recall before SQL planning, adds conservative pending updates, and documents optional runtime configuration.
- Placeholder scan: No placeholder task remains; all implementation tasks include concrete files, test examples, commands, and expected outcomes.
- Type consistency: The plan consistently uses `DataMemory`, `DataMemoryScope`, `DataMemoryStoreProtocol`, `data_memories`, and `pending_memory_updates`.
